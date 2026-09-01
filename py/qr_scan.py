"""Pure-stdlib QR decoding for the Briglia UT app (no zbar/zxing/PIL — the click
ships no compiled code, so the whole pipeline is Python + zlib).

Pipeline: PNG frame (saved by QML `grabToImage`, which always writes PNG —
the one raster format the stdlib can decode via zlib) → grayscale →
block-adaptive threshold → finder-pattern detection → perspective sampling
(bottom-right alignment pattern when the version has one) → format decode →
unmask → codeword deinterleave → Reed-Solomon correction → byte-stream parse.

Scope: QR versions 1–10 (21–57 modules), byte/numeric/alphanumeric modes,
all four EC levels. The Briglia key-bundle generator (website /qr page) caps
itself far below that, so anything it emits decodes here; per-field scans
of third-party QR codes work within the same limits.

Key-bundle framing (docs/QR_KEYS_SPEC.md):
    ADAK1:<i>/<n>:<crc32hex8>:<chunk>
where the concatenated chunks 1..n form '{"v":1,"keys":{...}}' and the crc
is zlib.crc32 of that full payload. One session assembles frames as the
camera sees them, in any order; mixing two different bundles is refused by
the crc key.
"""

import json
import os
import shutil
import struct
import time
import zlib

MAX_VERSION = 10
MAX_FRAMES = 16
MAX_PAYLOAD = 16 * 1024
MAX_VALUE_LEN = 4096
CHUNK_LEN = 100          # generator-side chunk size (kept in the JS port)
MAX_PNG_PIXELS = 4_000_000

BUNDLE_PREFIX = "ADAK1:"
KNOWN_KEYS = ("opencode", "openrouter", "custom", "openai", "serper", "jina",
              "telegram_token", "telegram_chat_id", "agentmail")

# ---------------------------------------------------------------- GF(256)

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256


def _gf_init():
    x = 1
    for i in range(255):
        _GF_EXP[i] = x
        _GF_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]


_gf_init()


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _gf_div(a, b):
    if a == 0:
        return 0
    return _GF_EXP[(_GF_LOG[a] - _GF_LOG[b]) % 255]


def _gf_pow(x, power):
    return _GF_EXP[(_GF_LOG[x] * power) % 255]


def _gf_inverse(x):
    return _GF_EXP[255 - _GF_LOG[x]]


def _gf_poly_scale(p, x):
    return [_gf_mul(c, x) for c in p]


def _gf_poly_add(p, q):
    r = [0] * max(len(p), len(q))
    r[len(r) - len(p):] = p
    for i, c in enumerate(q):
        r[i + len(r) - len(q)] ^= c
    return r


def _gf_poly_mul(p, q):
    r = [0] * (len(p) + len(q) - 1)
    for j, qc in enumerate(q):
        if qc == 0:
            continue
        for i, pc in enumerate(p):
            if pc:
                r[i + j] ^= _gf_mul(pc, qc)
    return r


def _gf_poly_eval(p, x):
    y = p[0]
    for c in p[1:]:
        y = _gf_mul(y, x) ^ c
    return y


def _gf_poly_div(dividend, divisor):
    out = list(dividend)
    for i in range(len(dividend) - len(divisor) + 1):
        coef = out[i]
        if coef == 0:
            continue
        for j in range(1, len(divisor)):
            if divisor[j]:
                out[i + j] ^= _gf_mul(divisor[j], coef)
    sep = -(len(divisor) - 1)
    return out[:sep], out[sep:]


# ------------------------------------------------------------- RS decode

def rs_generator_poly(nsym):
    g = [1]
    for i in range(nsym):
        g = _gf_poly_mul(g, [1, _gf_pow(2, i)])
    return g


def _rs_syndromes(msg, nsym):
    return [_gf_poly_eval(msg, _gf_pow(2, i)) for i in range(nsym)]


def _rs_error_locator(synd, nsym):
    err_loc = [1]
    old_loc = [1]
    for i in range(nsym):
        old_loc.append(0)
        delta = synd[i]
        for j in range(1, len(err_loc)):
            delta ^= _gf_mul(err_loc[-(j + 1)], synd[i - j])
        if delta != 0:
            if len(old_loc) > len(err_loc):
                new_loc = _gf_poly_scale(old_loc, delta)
                old_loc = _gf_poly_scale(err_loc, _gf_inverse(delta))
                err_loc = new_loc
            err_loc = _gf_poly_add(err_loc, _gf_poly_scale(old_loc, delta))
    while len(err_loc) and err_loc[0] == 0:
        del err_loc[0]
    errs = len(err_loc) - 1
    if errs * 2 > nsym:
        return None
    return err_loc


def _rs_find_errors(err_loc, nmess):
    positions = []
    for i in range(nmess):
        if _gf_poly_eval(err_loc, _gf_pow(2, i)) == 0:
            positions.append(nmess - 1 - i)
    if len(positions) != len(err_loc) - 1:
        return None
    return positions


def _rs_correct(msg_in, nsym):
    """Correct up to nsym//2 errors in-place-ish; returns the corrected
    codeword list or None when uncorrectable."""
    if len(msg_in) > 255:
        return None
    synd = _rs_syndromes(msg_in, nsym)
    if max(synd) == 0:
        return list(msg_in)
    err_loc = _rs_error_locator(synd, nsym)
    if err_loc is None:
        return None
    err_pos = _rs_find_errors(err_loc, len(msg_in))
    if err_pos is None:
        return None
    # Forney
    coef_pos = [len(msg_in) - 1 - p for p in err_pos]
    loc = [1]
    for i in coef_pos:
        loc = _gf_poly_mul(loc, _gf_poly_add([1], [_gf_pow(2, i), 0]))
    _, err_eval = _gf_poly_div(_gf_poly_mul(synd[::-1], loc),
                               [1] + [0] * (len(loc)))
    X = [_gf_pow(2, -(255 - c)) for c in coef_pos]
    msg = list(msg_in)
    for i, Xi in enumerate(X):
        Xi_inv = _gf_inverse(Xi)
        prime = 1
        for j, Xj in enumerate(X):
            if j != i:
                prime = _gf_mul(prime, 1 ^ _gf_mul(Xi_inv, Xj))
        if prime == 0:
            return None
        y = _gf_poly_eval(err_eval[::-1], Xi_inv)
        y = _gf_mul(Xi, y)
        msg[err_pos[i]] ^= _gf_div(y, prime)
    if max(_rs_syndromes(msg, nsym)) != 0:
        return None
    return msg


# --------------------------------------------------------------- tables

# EC_BLOCKS[version][level] = list of (data_codewords, ec_codewords) blocks
# in transmission order (group 1 then group 2). Totals cross-checked against
# the per-version codeword counts (26, 44, 70, 100, 134, 172, 196, 242, 292,
# 346) during the selftest.
EC_BLOCKS = {
    1: {"L": [(19, 7)], "M": [(16, 10)], "Q": [(13, 13)], "H": [(9, 17)]},
    2: {"L": [(34, 10)], "M": [(28, 16)], "Q": [(22, 22)], "H": [(16, 28)]},
    3: {"L": [(55, 15)], "M": [(44, 26)], "Q": [(17, 18)] * 2, "H": [(13, 22)] * 2},
    4: {"L": [(80, 20)], "M": [(32, 18)] * 2, "Q": [(24, 26)] * 2, "H": [(9, 16)] * 4},
    5: {"L": [(108, 26)], "M": [(43, 24)] * 2,
        "Q": [(15, 18)] * 2 + [(16, 18)] * 2, "H": [(11, 22)] * 2 + [(12, 22)] * 2},
    6: {"L": [(68, 18)] * 2, "M": [(27, 16)] * 4, "Q": [(19, 24)] * 4, "H": [(15, 28)] * 4},
    7: {"L": [(78, 20)] * 2, "M": [(31, 18)] * 4,
        "Q": [(14, 18)] * 2 + [(15, 18)] * 4, "H": [(13, 26)] * 4 + [(14, 26)]},
    8: {"L": [(97, 24)] * 2, "M": [(38, 22)] * 2 + [(39, 22)] * 2,
        "Q": [(18, 22)] * 4 + [(19, 22)] * 2, "H": [(14, 26)] * 4 + [(15, 26)] * 2},
    9: {"L": [(116, 30)] * 2, "M": [(36, 22)] * 3 + [(37, 22)] * 2,
        "Q": [(16, 20)] * 4 + [(17, 20)] * 4, "H": [(12, 24)] * 4 + [(13, 24)] * 4},
    10: {"L": [(68, 18)] * 2 + [(69, 18)] * 2, "M": [(43, 26)] * 4 + [(44, 26)],
         "Q": [(19, 24)] * 6 + [(20, 24)] * 2, "H": [(15, 28)] * 6 + [(16, 28)] * 2},
}

ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
         7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}

_EC_BITS = {"M": 0, "L": 1, "H": 2, "Q": 3}  # spec bit values
_EC_FROM_BITS = {v: k for k, v in _EC_BITS.items()}


def _format_code(level, mask):
    data = (_EC_BITS[level] << 3) | mask
    rem = data << 10
    for i in range(14, 9, -1):
        if rem & (1 << i):
            rem ^= 0x537 << (i - 10)
    return ((data << 10) | rem) ^ 0x5412


_FORMAT_TABLE = {}
for _level in "LMQH":
    for _mask in range(8):
        _FORMAT_TABLE[_format_code(_level, _mask)] = (_level, _mask)


def _mask_bit(mask, r, c):
    if mask == 0:
        return (r + c) % 2 == 0
    if mask == 1:
        return r % 2 == 0
    if mask == 2:
        return c % 3 == 0
    if mask == 3:
        return (r + c) % 3 == 0
    if mask == 4:
        return (r // 2 + c // 3) % 2 == 0
    if mask == 5:
        return (r * c) % 2 + (r * c) % 3 == 0
    if mask == 6:
        return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    return ((r + c) % 2 + (r * c) % 3) % 2 == 0


def function_map(version):
    """dim×dim booleans: True = function module (not data)."""
    dim = 17 + 4 * version
    fm = [[False] * dim for _ in range(dim)]
    for r in range(9):
        for c in range(9):
            fm[r][c] = True
        for c in range(dim - 8, dim):
            fm[r][c] = True
    for r in range(dim - 8, dim):
        for c in range(9):
            fm[r][c] = True
    for i in range(dim):
        fm[6][i] = True
        fm[i][6] = True
    for cy in ALIGN[version]:
        for cx in ALIGN[version]:
            if (cy <= 8 and cx <= 8) or (cy <= 8 and cx >= dim - 9) \
                    or (cy >= dim - 9 and cx <= 8):
                continue
            for r in range(cy - 2, cy + 3):
                for c in range(cx - 2, cx + 3):
                    fm[r][c] = True
    if version >= 7:
        for r in range(6):
            for c in range(dim - 11, dim - 8):
                fm[r][c] = True
                fm[c][r] = True
    return fm


def _format_positions(dim):
    copy1 = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
             (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    copy2 = [(dim - 1 - i, 8) for i in range(7)] \
        + [(8, dim - 8 + i) for i in range(8)]
    return copy1, copy2


# ----------------------------------------------------------- PNG loading

def load_png_gray(path):
    """Minimal PNG reader (8-bit, non-interlaced, gray/RGB/RGBA/palette) →
    (grayscale bytearray, width, height)."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    pos = 8
    width = height = None
    bitdepth = colortype = interlace = None
    idat = []
    palette = b""
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bitdepth, colortype, _, _, interlace = \
                struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"PLTE":
            palette = chunk
        elif ctype == b"IDAT":
            idat.append(chunk)
        elif ctype == b"IEND":
            break
    if width is None or not idat:
        raise ValueError("truncated PNG")
    if width * height > MAX_PNG_PIXELS:
        raise ValueError("PNG too large (%dx%d)" % (width, height))
    if interlace != 0 or bitdepth not in (1, 8) \
            or (bitdepth == 1 and colortype not in (0, 3)):
        raise ValueError("unsupported PNG (bit depth %s, color type %s, "
                         "interlace %s)" % (bitdepth, colortype, interlace))
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colortype)
    if channels is None:
        raise ValueError("unsupported PNG color type %s" % colortype)
    raw = zlib.decompress(b"".join(idat))
    if bitdepth == 1:
        # 1-bit rows (packed MSB-first; filters act on whole bytes).
        stride = (width + 7) // 8
        gray = bytearray(width * height)
        prev = bytearray(stride)
        pos = 0
        for y in range(height):
            ftype = raw[pos]
            pos += 1
            line = bytearray(raw[pos:pos + stride])
            pos += stride
            if ftype == 1:
                for i in range(1, stride):
                    line[i] = (line[i] + line[i - 1]) & 0xFF
            elif ftype == 2:
                for i in range(stride):
                    line[i] = (line[i] + prev[i]) & 0xFF
            elif ftype == 3:
                for i in range(stride):
                    a = line[i - 1] if i else 0
                    line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
            elif ftype == 4:
                for i in range(stride):
                    a = line[i - 1] if i else 0
                    b = prev[i]
                    cc = prev[i - 1] if i else 0
                    p = a + b - cc
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - cc)
                    line[i] = (line[i] + (a if pa <= pb and pa <= pc
                                          else b if pb <= pc else cc)) & 0xFF
            elif ftype != 0:
                raise ValueError("bad PNG filter %d" % ftype)
            prev = line
            base = y * width
            for x in range(width):
                bit = (line[x >> 3] >> (7 - (x & 7))) & 1
                if colortype == 3:
                    o = bit * 3
                    value = ((palette[o] * 299 + palette[o + 1] * 587
                              + palette[o + 2] * 114) // 1000
                             if o + 2 < len(palette) else bit * 255)
                else:
                    value = bit * 255
                gray[base + x] = value
        return gray, width, height
    stride = width * channels
    if len(raw) < height * (stride + 1):
        raise ValueError("PNG data shorter than expected")
    gray = bytearray(width * height)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                cc = prev[i - channels] if i >= channels else 0
                p = a + b - cc
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - cc)
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = cc
                line[i] = (line[i] + pr) & 0xFF
        elif ftype != 0:
            raise ValueError("bad PNG filter %d" % ftype)
        prev = line
        base = y * width
        if colortype == 0:
            gray[base:base + width] = line
        elif colortype in (2, 6):
            for x in range(width):
                o = x * channels
                gray[base + x] = (line[o] * 299 + line[o + 1] * 587
                                  + line[o + 2] * 114) // 1000
        elif colortype == 4:
            for x in range(width):
                gray[base + x] = line[x * 2]
        else:
            for x in range(width):
                o = line[x] * 3
                if o + 2 < len(palette):
                    gray[base + x] = (palette[o] * 299 + palette[o + 1] * 587
                                      + palette[o + 2] * 114) // 1000
    return gray, width, height


def load_bmp_gray(path):
    """Qt-written BMP (24/32bpp, BI_RGB or byte-aligned BI_BITFIELDS) →
    (grayscale bytearray, w, h). BMP is the fast path on-device: no
    zlib, no per-byte defiltering — the green channel is extracted with
    C-speed slicing (green ≈ luminance is plenty for black/white QR).
    Pure-Python PNG defiltering cost ~5s/frame on a Pixel 3a."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] != b"BM" or len(data) < 54:
        raise ValueError("not a BMP file")
    pixel_off = struct.unpack("<I", data[10:14])[0]
    dib = struct.unpack("<I", data[14:18])[0]
    if dib < 40:
        raise ValueError("unsupported BMP header size %d" % dib)
    width, height = struct.unpack("<ii", data[18:26])
    bpp = struct.unpack("<H", data[28:30])[0]
    compression = struct.unpack("<I", data[30:34])[0]
    if width <= 0 or width * abs(height) > MAX_PNG_PIXELS:
        raise ValueError("BMP too large (%dx%d)" % (width, height))
    if bpp not in (24, 32):
        raise ValueError("unsupported BMP bit depth %d" % bpp)
    px = bpp // 8
    if compression == 0:
        g_idx = 1  # BGR(A)
    elif compression == 3 and len(data) >= 66:
        # masks sit right after a 40-byte header (classic BI_BITFIELDS)
        # and at the same absolute offset inside V4/V5 headers: R@54,
        # G@58, B@62
        gmask = struct.unpack("<I", data[58:62])[0]
        if gmask not in (0xFF, 0xFF00, 0xFF0000, 0xFF000000):
            raise ValueError("unsupported BMP green mask 0x%X" % gmask)
        g_idx = (gmask.bit_length() - 1) // 8
    else:
        raise ValueError("unsupported BMP compression %d" % compression)
    abs_h = abs(height)
    stride = ((bpp * width + 31) // 32) * 4
    if pixel_off + stride * abs_h > len(data):
        raise ValueError("truncated BMP")
    rows = []
    for y in range(abs_h):
        off = pixel_off + y * stride
        rows.append(data[off + g_idx:off + px * width:px])
    if height > 0:      # bottom-up: un-flip, or the code reads mirrored
        rows.reverse()
    return bytearray(b"".join(rows)), width, abs_h


def load_frame_gray(path):
    """Camera frame of either format (BMP fast path, PNG compatibility)."""
    with open(path, "rb") as f:
        magic = f.read(2)
    if magic == b"BM":
        return load_bmp_gray(path)
    return load_png_gray(path)


def _downscale(gray, w, h, target=760):
    factor = max(1, (max(w, h) + target - 1) // target)
    if factor == 1:
        return gray, w, h
    nw, nh = w // factor, h // factor
    out = bytearray(nw * nh)
    for y in range(nh):
        src = y * factor * w
        dst = y * nw
        for x in range(nw):
            out[dst + x] = gray[src + x * factor]
    return out, nw, nh


def _binarize(gray, w, h):
    """Block-adaptive threshold (zxing HybridBinarizer scheme). The crucial
    part is the FLAT-block rule: a block with no local contrast (inside a
    large dark area — finder centers, long dark runs) must inherit its
    black point from already-computed left/top neighbors instead of using
    its own mean, or solid dark regions binarize as white.
    Returns bytearray of 0/1 (1 = black)."""
    bs = 8
    bw, bh = (w + bs - 1) // bs, (h + bs - 1) // bs
    bp = [[0] * bw for _ in range(bh)]
    for by in range(bh):
        y0, y1 = by * bs, min((by + 1) * bs, h)
        for bx in range(bw):
            x0, x1 = bx * bs, min((bx + 1) * bs, w)
            total = 0
            lo, hi = 255, 0
            for y in range(y0, y1):
                segment = gray[y * w + x0:y * w + x1]
                total += sum(segment)
                slo, shi = min(segment), max(segment)
                if slo < lo:
                    lo = slo
                if shi > hi:
                    hi = shi
            count = (y1 - y0) * (x1 - x0)
            mean = total // max(1, count)
            if hi - lo > 24:
                point = mean
            else:
                point = lo // 2
                if by > 0 and bx > 0:
                    neighbor = (bp[by][bx - 1] + 2 * bp[by - 1][bx]
                                + bp[by - 1][bx - 1]) // 4
                    if lo < neighbor:
                        point = neighbor
            bp[by][bx] = point
    bits = bytearray(w * h)
    for by in range(bh):
        ny0, ny1 = max(0, by - 2), min(bh - 1, by + 2)
        y0, y1 = by * bs, min((by + 1) * bs, h)
        for bx in range(bw):
            nx0, nx1 = max(0, bx - 2), min(bw - 1, bx + 2)
            total = count = 0
            for ny in range(ny0, ny1 + 1):
                for nx in range(nx0, nx1 + 1):
                    total += bp[ny][nx]
                    count += 1
            threshold = total // count
            x0, x1 = bx * bs, min((bx + 1) * bs, w)
            # translate() thresholds the whole block segment at C speed —
            # the per-pixel Python loop here was one of the two hot spots
            # that made a Pixel 3a spend ~8s on a single frame.
            tab = _thresh_table(threshold)
            for y in range(y0, y1):
                row = y * w
                bits[row + x0:row + x1] = gray[row + x0:row + x1].translate(tab)
    return bits


_THRESH_TABLES = {}


def _thresh_table(threshold):
    tab = _THRESH_TABLES.get(threshold)
    if tab is None:
        tab = bytes(1 if i <= threshold else 0 for i in range(256))
        _THRESH_TABLES[threshold] = tab
    return tab


# ---------------------------------------------------- finder detection

def _ratio_ok(runs, expected):
    """runs vs expected module multiples (e.g. (1,1,3,1,1)); zxing-style
    50%-of-module tolerance."""
    total = sum(runs)
    units = sum(expected)
    if total < units:
        return 0
    unit = total / units
    maxvar = unit / 1.85
    for run, exp in zip(runs, expected):
        if abs(exp * unit - run) > exp * maxvar:
            return 0
    return unit


def _cross_check(bits, w, h, cx, cy, expected, axis):
    """Walk outward from (cx,cy) along a direction — 'v' column, 'h' row,
    'd' main diagonal, 'a' anti-diagonal — collecting the runs centered on
    the middle run. Returns (unit, refined_center_along_axis) or None."""
    steps = len(expected)
    mid = steps // 2
    runs = [0] * steps
    dx, dy = {"v": (0, 1), "h": (1, 0), "d": (1, 1), "a": (1, -1)}[axis]

    def bit_at(step):
        x, y = cx + dx * step, cy + dy * step
        if x < 0 or y < 0 or x >= w or y >= h:
            return None
        return bits[y * w + x]

    if bit_at(0) != 1:
        return None
    lo = 0
    while bit_at(lo - 1) == 1:
        lo -= 1
    hi = 0
    while bit_at(hi + 1) == 1:
        hi += 1
    runs[mid] = hi - lo + 1
    p = lo - 1
    for k in range(mid - 1, -1, -1):
        want = 1 - (mid - k) % 2
        count = 0
        while bit_at(p) == want:
            count += 1
            p -= 1
        if count == 0:
            return None
        runs[k] = count
    p = hi + 1
    for k in range(mid + 1, steps):
        want = 1 - (k - mid) % 2
        count = 0
        while bit_at(p) == want:
            count += 1
            p += 1
        if count == 0:
            return None
        runs[k] = count
    unit = _ratio_ok(runs, expected)
    if not unit:
        return None
    center = (lo + hi) / 2.0
    if axis == "v":
        return unit, cy + center
    if axis == "h":
        return unit, cx + center
    return unit, center  # diagonals: validation only


def _find_finders(bits, w, h):
    """Scan rows for 1:1:3:1:1, cross-check vertically then horizontally;
    cluster nearby hits. Returns [(cx, cy, module_size), ...]."""
    pattern = (1, 1, 3, 1, 1)
    found = []  # [cx, cy, unit, weight]
    y = 0
    while y < h:
        row = y * w
        # Run-length extraction via big-int shift/xor: bits are 0/1 bytes,
        # so x ^ (x >> 8) marks exactly the positions where a run starts —
        # all at C speed. The per-pixel while-loops this replaces were the
        # second hot spot on slow phones.
        rowbytes = bytes(bits[row:row + w])
        as_int = int.from_bytes(rowbytes, "big")
        diff = (as_int ^ (as_int >> 8)).to_bytes(w, "big")
        starts = [0]
        pos = diff.find(1, 1)
        while pos != -1:
            starts.append(pos)
            pos = diff.find(1, pos + 1)
        runs = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
        runs.append(w - starts[-1])
        first_black = 0 if bits[row + starts[0]] else 1
        for i in range(first_black, len(runs) - 4, 2):
            window = runs[i:i + 5]
            unit = _ratio_ok(window, pattern)
            if not unit:
                continue
            cx = starts[i + 2] + runs[i + 2] // 2
            vertical = _cross_check(bits, w, h, cx, y, pattern, "v")
            if not vertical:
                continue
            vunit, cy = vertical
            horizontal = _cross_check(bits, w, h, cx, int(cy), pattern, "h")
            if not horizontal:
                continue
            hunit, cx2 = horizontal
            # Diagonal cross-check (zxing does the same): kills data-region
            # patterns that happen to read 1:1:3:1:1 on both axes.
            if not _cross_check(bits, w, h, int(cx2), int(cy), pattern, "d"):
                continue
            module = (vunit + hunit) / 2.0
            merged = False
            for entry in found:
                if abs(entry[0] - cx2) < module * 3 and abs(entry[1] - cy) < module * 3:
                    weight = entry[3]
                    entry[0] = (entry[0] * weight + cx2) / (weight + 1)
                    entry[1] = (entry[1] * weight + cy) / (weight + 1)
                    entry[2] = (entry[2] * weight + module) / (weight + 1)
                    entry[3] = weight + 1
                    merged = True
                    break
            if not merged:
                found.append([cx2, cy, module, 1])
        y += 1
    return [(e[0], e[1], e[2]) for e in found if e[3] >= 2] \
        or [(e[0], e[1], e[2]) for e in found]


def _rank_triples(candidates):
    """Rank 3-candidate combinations that agree in module size and form a
    right angle; returns [(TL, TR, BL), ...] best-first. More than one is
    kept because a plausible-but-wrong triple can outscore the real one —
    the caller tries each until a decode succeeds."""
    from itertools import combinations
    scored = []
    for combo in combinations(candidates, 3):
        sizes = [c[2] for c in combo]
        spread = (max(sizes) - min(sizes)) / max(min(sizes), 0.001)
        if spread > 0.6:
            continue
        ordered = _order_corners(combo)
        if ordered is None:
            continue
        tl, tr, bl = ordered
        d_top = _dist(tl, tr)
        d_left = _dist(tl, bl)
        if d_top < 10 or d_left < 10:
            continue
        asym = abs(d_top - d_left) / max(d_top, d_left)
        if asym > 0.4:
            continue
        # angle at TL should be ~90°
        dot = ((tr[0] - tl[0]) * (bl[0] - tl[0])
               + (tr[1] - tl[1]) * (bl[1] - tl[1]))
        cos = abs(dot) / (d_top * d_left)
        if cos > 0.35:
            continue
        score = spread + asym + cos - min(sizes) / 50.0
        scored.append((score, ordered))
    scored.sort(key=lambda entry: entry[0])
    return [ordered for _, ordered in scored]


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _order_corners(combo):
    pairs = [(0, 1, 2), (0, 2, 1), (1, 2, 0)]
    hyp = max(pairs, key=lambda p: _dist(combo[p[0]], combo[p[1]]))
    tl = combo[hyp[2]]
    a, b = combo[hyp[0]], combo[hyp[1]]
    # cross product decides which end of the hypotenuse is TR (y grows down)
    cross = ((a[0] - tl[0]) * (b[1] - tl[1])
             - (a[1] - tl[1]) * (b[0] - tl[0]))
    if cross > 0:
        tr, bl = a, b
    else:
        tr, bl = b, a
    return tl, tr, bl


# ------------------------------------------------------- perspective

def _solve_homography(src_pts, dst_pts):
    """8×8 solve mapping (x,y) → (X,Y) for 4 correspondences; returns the
    8 coefficients or None if degenerate."""
    A = []
    B = []
    for (x, y), (X, Y) in zip(src_pts, dst_pts):
        A.append([x, y, 1, 0, 0, 0, -x * X, -y * X])
        B.append(X)
        A.append([0, 0, 0, x, y, 1, -x * Y, -y * Y])
        B.append(Y)
    n = 8
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(A[r][col]))
        if abs(A[pivot][col]) < 1e-9:
            return None
        A[col], A[pivot] = A[pivot], A[col]
        B[col], B[pivot] = B[pivot], B[col]
        inv = 1.0 / A[col][col]
        for r in range(col + 1, n):
            f = A[r][col] * inv
            if f == 0:
                continue
            for c in range(col, n):
                A[r][c] -= f * A[col][c]
            B[r] -= f * B[col]
    coef = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = B[r] - sum(A[r][c] * coef[c] for c in range(r + 1, n))
        coef[r] = s / A[r][r]
    return coef


def _project(coef, x, y):
    denom = coef[6] * x + coef[7] * y + 1.0
    if abs(denom) < 1e-9:
        return None
    return ((coef[0] * x + coef[1] * y + coef[2]) / denom,
            (coef[3] * x + coef[4] * y + coef[5]) / denom)


def _find_alignment(bits, w, h, px, py, module):
    """Search around the affine-predicted (px,py) for the bottom-right
    alignment pattern (slices through its center read 1:1:1 with a black
    middle on both axes). The affine prediction ignores perspective, whose
    displacement grows with distance from the finder triangle — so search
    expanding windows (4, 8, 16 modules, zxing's scheme), nearest hit of
    the first window that has one wins. Returns (x, y) or None."""
    pattern = (1, 1, 1)
    for allowance in (4, 8, 16):
        radius = int(module * allowance) + 2
        hits = []
        y0 = max(1, int(py) - radius)
        y1 = min(h - 2, int(py) + radius)
        x_lo = max(1, int(px) - radius)
        x_hi = min(w - 2, int(px) + radius)
        for y in range(y0, y1 + 1):
            x = x_lo
            row = y * w
            while x <= x_hi:
                if not bits[row + x]:
                    x += 1
                    continue
                run_start = x
                while x <= x_hi and bits[row + x]:
                    x += 1
                run = x - run_start
                if abs(run - module) > module * 0.7 or run < 1:
                    continue
                cx = run_start + run // 2
                vertical = _cross_check(bits, w, h, cx, y, pattern, "v")
                if not vertical:
                    continue
                _, cy = vertical
                horizontal = _cross_check(bits, w, h, cx, int(cy), pattern, "h")
                if not horizontal:
                    continue
                _, cx2 = horizontal
                # data-region white-black-white columns pass both axis
                # checks distressingly often; the diagonal filters most
                if not _cross_check(bits, w, h, int(cx2), int(cy), pattern, "d"):
                    continue
                hits.append((_dist((cx2, cy), (px, py)), cx2, cy))
        if hits:
            hits.sort()
            deduped = []
            for d, cx2, cy in hits:
                if all(_dist((cx2, cy), (ox, oy)) > module
                       for _, ox, oy in deduped):
                    deduped.append((d, cx2, cy))
            return [(cx2, cy) for _, cx2, cy in deduped[:3]]
    return []


def _timing_score(bits, w, h, coef, dim):
    """Fraction of correctly-alternating timing modules under a candidate
    transform — cheap validity gate for a (finders + 4th point) grid."""
    good = total = 0
    for i in range(8, dim - 8):
        expected = 1 if i % 2 == 0 else 0
        for x, y in ((i + 0.5, 6.5), (6.5, i + 0.5)):
            p = _project(coef, x, y)
            if p is None:
                return 0.0
            px, py = int(p[0] + 0.5), int(p[1] + 0.5)
            if px < 0 or py < 0 or px >= w or py >= h:
                return 0.0
            total += 1
            if bits[py * w + px] == expected:
                good += 1
    return good / max(1, total)


def _sample_grid(bits, w, h, tl, tr, bl, version):
    dim = 17 + 4 * version
    module = (tl[2] + tr[2] + bl[2]) / 3.0
    base_src = [(3.5, 3.5), (dim - 3.5, 3.5), (3.5, dim - 3.5)]
    base_dst = [(tl[0], tl[1]), (tr[0], tr[1]), (bl[0], bl[1])]
    # Candidate 4th correspondences: each detected bottom-right alignment
    # pattern (versions that have one), then the parallelogram-estimated
    # corner (exact only for a flat, parallel scan). Each candidate grid
    # must pass the timing-pattern gate — a nearer-but-false alignment hit
    # otherwise silently shears the whole sampling grid.
    options = []
    if version >= 2:
        src4 = (dim - 6.5, dim - 6.5)
        fx = (src4[0] - 3.5) / (dim - 7)
        fy = (src4[1] - 3.5) / (dim - 7)
        ax = tl[0] + (tr[0] - tl[0]) * fx + (bl[0] - tl[0]) * fy
        ay = tl[1] + (tr[1] - tl[1]) * fx + (bl[1] - tl[1]) * fy
        for hit in _find_alignment(bits, w, h, ax, ay, module):
            options.append((src4, hit))
    options.append(((dim - 3.5, dim - 3.5),
                    (tr[0] + bl[0] - tl[0], tr[1] + bl[1] - tl[1])))
    best = None
    for src4, dst4 in options:
        coef = _solve_homography(base_src + [src4], base_dst + [dst4])
        if coef is None:
            continue
        score = _timing_score(bits, w, h, coef, dim)
        if best is None or score > best[0]:
            best = (score, coef)
        if score >= 0.9:
            break
    if best is None or best[0] < 0.75:
        return None
    coef = best[1]
    matrix = []
    for r in range(dim):
        row = []
        for c in range(dim):
            p = _project(coef, c + 0.5, r + 0.5)
            if p is None:
                return None
            x, y = int(p[0] + 0.5), int(p[1] + 0.5)
            if x < 0 or y < 0 or x >= w or y >= h:
                return None
            row.append(bits[y * w + x])
        matrix.append(row)
    return matrix


# --------------------------------------------------------- matrix decode

def _read_format(matrix, dim):
    copy1, copy2 = _format_positions(dim)
    for positions in (copy1, copy2):
        raw = 0
        for r, c in positions:
            raw = (raw << 1) | matrix[r][c]
        best = None
        for code, meaning in _FORMAT_TABLE.items():
            distance = bin(raw ^ code).count("1")
            if best is None or distance < best[0]:
                best = (distance, meaning)
        if best and best[0] <= 3:
            return best[1]
    return None


def decode_matrix(matrix):
    """Bit matrix (lists of 0/1) → decoded text, or None."""
    dim = len(matrix)
    version = (dim - 17) // 4
    if version < 1 or version > MAX_VERSION or 17 + 4 * version != dim:
        return None
    fmt = _read_format(matrix, dim)
    if fmt is None:
        return None
    level, mask = fmt
    fm = function_map(version)
    bits = []
    col = dim - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(dim - 1, -1, -1) if upward else range(dim)
        for r in rows:
            for c in (col, col - 1):
                if not fm[r][c]:
                    bit = matrix[r][c]
                    if _mask_bit(mask, r, c):
                        bit ^= 1
                    bits.append(bit)
        upward = not upward
        col -= 2
    blocks = EC_BLOCKS[version][level]
    total_cw = sum(d + e for d, e in blocks)
    if len(bits) < total_cw * 8:
        return None
    codewords = []
    for i in range(total_cw):
        byte = 0
        for j in range(8):
            byte = (byte << 1) | bits[i * 8 + j]
        codewords.append(byte)
    # deinterleave: data round-robin (shorter blocks skip), then EC
    data_lens = [d for d, _ in blocks]
    ec_len = blocks[0][1]
    block_data = [[] for _ in blocks]
    block_ec = [[] for _ in blocks]
    idx = 0
    for i in range(max(data_lens)):
        for b in range(len(blocks)):
            if i < data_lens[b]:
                block_data[b].append(codewords[idx])
                idx += 1
    for i in range(ec_len):
        for b in range(len(blocks)):
            block_ec[b].append(codewords[idx])
            idx += 1
    payload = bytearray()
    for b in range(len(blocks)):
        corrected = _rs_correct(block_data[b] + block_ec[b], ec_len)
        if corrected is None:
            return None
        payload.extend(corrected[:data_lens[b]])
    return _parse_stream(payload, version)


_ALNUM = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"


def _parse_stream(payload, version):
    bits = []
    for byte in payload:
        for j in range(7, -1, -1):
            bits.append((byte >> j) & 1)

    pos = [0]

    def take(n):
        if pos[0] + n > len(bits):
            raise ValueError("stream underrun")
        value = 0
        for _ in range(n):
            value = (value << 1) | bits[pos[0]]
            pos[0] += 1
        return value

    out = bytearray()
    try:
        while pos[0] + 4 <= len(bits):
            mode = take(4)
            if mode == 0:
                break
            if mode == 4:  # byte
                count = take(8 if version < 10 else 16)
                for _ in range(count):
                    out.append(take(8))
            elif mode == 1:  # numeric
                count = take(10 if version < 10 else 12)
                while count >= 3:
                    out.extend(("%03d" % take(10)).encode())
                    count -= 3
                if count == 2:
                    out.extend(("%02d" % take(7)).encode())
                elif count == 1:
                    out.extend(("%d" % take(4)).encode())
            elif mode == 2:  # alphanumeric
                count = take(9 if version < 10 else 11)
                while count >= 2:
                    value = take(11)
                    out.append(ord(_ALNUM[value // 45]))
                    out.append(ord(_ALNUM[value % 45]))
                    count -= 2
                if count == 1:
                    out.append(ord(_ALNUM[take(6)]))
            elif mode == 7:  # ECI — skip the designator, keep decoding
                first = take(8)
                if first >> 6 == 0b10:
                    take(8)
                elif first >> 5 == 0b110:
                    take(16)
            else:
                return None  # kanji/structured append unsupported
    except (ValueError, IndexError):
        return None
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return out.decode("latin-1")


# ------------------------------------------------------------ top level

def decode_image(gray, w, h, stats=None):
    """Grayscale frame → decoded text or None. Tries the estimated version
    ±1 (the module-size estimate can be off by one step). `stats` (dict)
    is filled with per-stage counters for the on-screen diagnostics."""
    gray, w, h = _downscale(gray, w, h)
    bits = _binarize(gray, w, h)
    candidates = _find_finders(bits, w, h)
    if stats is not None:
        stats["cands"] = len(candidates)
        stats["dim"] = "%dx%d" % (w, h)
    if len(candidates) < 3:
        return None
    triples = _rank_triples(candidates)
    if stats is not None:
        stats["triples"] = len(triples)
    for tl, tr, bl in triples[:4]:
        module = (tl[2] + tr[2] + bl[2]) / 3.0
        top_modules = _dist(tl, tr) / module
        estimate = round((top_modules + 7 - 17) / 4)
        # The scalar module-size estimate is unreliable under anisotropic
        # perspective (horizontal and vertical module sizes differ), so try
        # every supported version ordered by closeness to the estimate — a
        # wrong grid fails the format read almost immediately, so the
        # extra attempts are nearly free.
        for version in sorted(range(1, MAX_VERSION + 1),
                              key=lambda v: abs(v - estimate)):
            matrix = _sample_grid(bits, w, h, tl, tr, bl, version)
            if matrix is None:
                continue
            text = decode_matrix(matrix)
            if text is not None:
                return text
    return None


def decode_png(path):
    gray, w, h = load_png_gray(path)
    return decode_image(gray, w, h)


# ---------------------------------------------------------- key bundles

def make_frames(payload_text, chunk_len=CHUNK_LEN):
    """Split a bundle payload into ADAK1 frame texts (reference for the
    website generator's JS port; also used by the selftest)."""
    if len(payload_text.encode("utf-8")) > MAX_PAYLOAD:
        raise ValueError("payload too large")
    crc = "%08x" % (zlib.crc32(payload_text.encode("utf-8")) & 0xFFFFFFFF)
    chunks = [payload_text[i:i + chunk_len]
              for i in range(0, len(payload_text), chunk_len)] or [""]
    if len(chunks) > MAX_FRAMES:
        raise ValueError("payload needs %d frames (max %d)"
                         % (len(chunks), MAX_FRAMES))
    return ["%s%d/%d:%s:%s" % (BUNDLE_PREFIX, i + 1, len(chunks), crc, chunk)
            for i, chunk in enumerate(chunks)]


def parse_frame(text):
    """ADAK1 frame → (index, total, crc, chunk) or None."""
    if not text.startswith(BUNDLE_PREFIX):
        return None
    rest = text[len(BUNDLE_PREFIX):]
    parts = rest.split(":", 2)
    if len(parts) != 3:
        return None
    counter, crc, chunk = parts
    if "/" not in counter:
        return None
    idx_s, total_s = counter.split("/", 1)
    if not (idx_s.isdigit() and total_s.isdigit()):
        return None
    idx, total = int(idx_s), int(total_s)
    if not (1 <= total <= MAX_FRAMES and 1 <= idx <= total):
        return None
    if len(crc) != 8 or any(ch not in "0123456789abcdef" for ch in crc):
        return None
    if len(chunk) > MAX_PAYLOAD:
        return None
    return idx, total, crc, chunk


def parse_bundle(payload_text):
    """Assembled payload JSON → (keys dict, ignored names) or raises
    ValueError with a user-showable message."""
    try:
        data = json.loads(payload_text)
    except ValueError:
        raise ValueError("the bundle is not valid JSON")
    if not isinstance(data, dict) or data.get("v") != 1 \
            or not isinstance(data.get("keys"), dict):
        raise ValueError('the bundle is not {"v":1,"keys":{...}}')
    keys = {}
    ignored = []
    for name, value in data["keys"].items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("bundle entries must be strings")
        if len(value) > MAX_VALUE_LEN:
            raise ValueError("a bundle value is unreasonably long")
        if name in KNOWN_KEYS:
            if value.strip():
                keys[name] = value.strip()
        else:
            ignored.append(name)
    if not keys:
        raise ValueError("the bundle contains no usable keys")
    return keys, ignored


# One in-progress bundle at a time (the scan page resets on open).
_session = {"crc": None, "total": 0, "chunks": {}}


def reset_session():
    _session["crc"] = None
    _session["total"] = 0
    _session["chunks"] = {}
    return True


def _feed_bundle(text):
    frame = parse_frame(text)
    if frame is None:
        return {"found": True, "kind": "not_bundle",
                "message": "That QR code is not a Briglia key bundle."}
    idx, total, crc, chunk = frame
    if _session["crc"] is not None and _session["crc"] != crc:
        return {"found": True, "kind": "bundle_mismatch",
                "message": "This frame belongs to a DIFFERENT bundle — "
                           "regenerate the codes and scan one set only."}
    if _session["crc"] is None:
        _session["crc"] = crc
        _session["total"] = total
    elif _session["total"] != total:
        return {"found": True, "kind": "bundle_mismatch",
                "message": "Inconsistent frame count — rescan from the start."}
    _session["chunks"][idx] = chunk
    have = len(_session["chunks"])
    if have < total:
        return {"found": True, "kind": "bundle", "done": False,
                "have": have, "total": total,
                "missing": [i for i in range(1, total + 1)
                            if i not in _session["chunks"]]}
    payload = "".join(_session["chunks"][i] for i in range(1, total + 1))
    actual = "%08x" % (zlib.crc32(payload.encode("utf-8")) & 0xFFFFFFFF)
    if actual != crc:
        reset_session()
        return {"found": True, "kind": "bundle_corrupt",
                "message": "The assembled bundle failed its checksum — "
                           "scanning restarts; try again."}
    try:
        keys, ignored = parse_bundle(payload)
    except ValueError as exc:
        reset_session()
        return {"found": True, "kind": "bundle_corrupt", "message": str(exc)}
    reset_session()
    return {"found": True, "kind": "bundle", "done": True,
            "have": total, "total": total,
            "keys": keys, "ignored": ignored,
            "key_names": sorted(keys.keys())}


def frame_path(token=None):
    """Where QML saves camera grabs. App cache, deleted after every read.

    `token` (the scan page's capture generation) makes each capture's path
    unique, so an abandoned capture's late saveToFile can never overwrite
    the frame a newer capture is about to decode. Frames whose read was
    lost are purged here by age."""
    base = os.environ.get("BRIGLIA_QR_FRAME_DIR") \
        or os.path.expanduser("~/.cache/briglia.permaevidence")
    os.makedirs(base, exist_ok=True)
    try:
        import time
        cutoff = time.time() - 120
        for name in os.listdir(base):
            if name.startswith("qr-") and name.endswith((".png", ".jpg",
                                                         ".bmp")):
                stale = os.path.join(base, name)
                try:
                    if os.path.getmtime(stale) < cutoff:
                        os.unlink(stale)
                except OSError:
                    pass
    except OSError:
        pass
    name = "qr-frame-%s.bmp" % token if token is not None else "qr-frame.bmp"
    return os.path.join(base, name)


def photo_path(token=None):
    """Where QML saves still-capture photos (the photo-mode fallback when
    viewfinder grabs come back blank). Same cache dir and stale-purge
    rules as frame_path; JPEG because that is what the camera writes."""
    base = os.path.dirname(frame_path(token))  # reuses dir + stale purge
    name = "qr-photo-%s.jpg" % token if token is not None else "qr-photo.jpg"
    return os.path.join(base, name)


def remove_file(path):
    """Delete one scanner temp file. Restricted to qr-* names inside the
    scanner's own cache dir — QML hands paths back after the photo
    pipeline finishes with them, and this must never be a generic rm."""
    base = os.path.dirname(frame_path())
    full = os.path.abspath(path)
    name = os.path.basename(full)
    if os.path.dirname(full) != os.path.abspath(base) \
            or not name.startswith("qr-"):
        return False
    try:
        os.unlink(full)
        return True
    except OSError:
        return False


# ------------------------------------------------------------ diagnostics
#
# When the scan page's debug switch is on, every analyzed frame is copied
# to a user-visible folder (with one rotation step) and a one-line verdict
# is appended to scan-log.txt — so a phone where scanning fails can simply
# send the exact camera bytes the decoder saw. Never on by default: bundle
# frames contain API keys, so persisted copies are strictly opt-in.

def debug_dir():
    base = os.environ.get("BRIGLIA_QR_DEBUG_DIR") \
        or os.path.expanduser("~/Documents/briglia-qr-debug")
    os.makedirs(base, exist_ok=True)
    return base


def _debug_rotate_copy(src, stem, ext):
    """Copy src into the debug dir as last-<stem>.<ext>, keeping the
    previous one as prev-<stem>.<ext>."""
    try:
        base = debug_dir()
        last = os.path.join(base, "last-%s.%s" % (stem, ext))
        prev = os.path.join(base, "prev-%s.%s" % (stem, ext))
        if os.path.exists(last):
            os.replace(last, prev)
        shutil.copyfile(src, last)
        return last
    except OSError:
        return None


def env_info():
    """One-line runtime description for the diagnostics log header."""
    import sys
    try:
        import numpy  # noqa: F401  (only probing availability)
        numpy_state = "numpy"
    except ImportError:
        numpy_state = "no-numpy"
    return "py%d.%d · %s" % (sys.version_info[0], sys.version_info[1],
                             numpy_state)


def log_event(msg, debug=True):
    """Append one line to the debug scan log (QML camera events land here
    too). No-op when the debug switch is off."""
    if not debug:
        return False
    try:
        path = os.path.join(debug_dir(), "scan-log.txt")
        try:
            if os.path.getsize(path) > 200_000:
                os.unlink(path)
        except OSError:
            pass
        with open(path, "a") as f:
            f.write(time.strftime("%H:%M:%S") + " " + str(msg) + "\n")
        return True
    except OSError:
        return False


def finish_photo(path, debug=False):
    """Dispose of a photo-mode JPEG once the pipeline is done with it:
    debug on → rotate a copy into the debug dir first. Same qr-* cache-dir
    restriction as remove_file."""
    base = os.path.dirname(frame_path())
    full = os.path.abspath(path)
    if os.path.dirname(full) != os.path.abspath(base) \
            or not os.path.basename(full).startswith("qr-"):
        return False
    if debug and os.path.exists(full):
        _debug_rotate_copy(full, "photo", "jpg")
    return remove_file(full)


# Frames with no contrast at all are a device symptom, not a scanning miss:
# on some Ubuntu Touch devices the camera preview is an external GPU texture
# that grabToImage renders as a uniform block. The scan page counts these
# and switches to still-photo capture.
BLANK_SPREAD = 12


def _is_blank(gray, w, h):
    step = max(1, (w * h) // 4096)
    sample = gray[::step]
    return (max(sample) - min(sample)) < BLANK_SPREAD if sample else True


def scan_png(path, mode, debug=False):
    """One camera frame. mode 'single' returns any decoded text; mode
    'bundle' feeds the ADAK assembly session. The frame file is deleted
    after reading — camera grabs shouldn't linger in the cache. With
    `debug` the frame is first copied to the debug dir and the verdict
    logged, and the result carries ms/cands diagnostics."""
    stats = {}
    started = time.time()
    try:
        try:
            if debug:
                _debug_rotate_copy(path, "frame",
                                   "bmp" if path.endswith(".bmp") else "png")
            gray, w, h = load_frame_gray(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        text = decode_image(gray, w, h, stats)
    except (OSError, ValueError) as exc:
        log_event("frame error: %s" % exc, debug)
        return {"found": False, "error": str(exc)}
    ms = int((time.time() - started) * 1000)
    diag = {"ms": ms, "cands": stats.get("cands", 0),
            "dim": stats.get("dim", "?")}
    if text is None:
        if _is_blank(gray, w, h):
            log_event("frame %s: BLANK (uniform) %dms" % (diag["dim"], ms),
                      debug)
            return dict(diag, found=False, blank=True)
        log_event("frame %s: no decode — %d finder candidates, %d triples, "
                  "%dms" % (diag["dim"], diag["cands"],
                            stats.get("triples", 0), ms), debug)
        return dict(diag, found=False)
    log_event("frame %s: DECODED %d chars, %dms" % (diag["dim"], len(text), ms),
              debug)
    if mode == "bundle":
        return dict(_feed_bundle(text), **diag)
    if text.startswith(BUNDLE_PREFIX):
        return dict(diag, found=True, kind="bundle_in_single",
                    message="This is a key BUNDLE code — use the bundle "
                            "scanner ('Scan keys from computer') instead.")
    return dict(diag, found=True, kind="text", text=text)
