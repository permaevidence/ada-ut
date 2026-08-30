"""Reference QR ENCODER (byte mode, versions 1-10) — test-side only, never
shipped in the click. Purpose:

1. The selftest round-trips it against py/qr_scan.py's decoder, including
   perspective-warped and degraded renders.
2. It is the source of truth for the website generator's JS port
   (ada-website app/qr/qrlib.ts); the selftest cross-checks the two
   matrix-for-matrix via node when available, so the tables cannot drift.

Shares GF arithmetic and the EC-block/alignment tables with the decoder by
importing them — one copy on the Python side by construction.
"""

import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "py"))

import qr_scan
from qr_scan import (ALIGN, EC_BLOCKS, _format_code, _format_positions,
                     _gf_mul, _mask_bit, function_map, rs_generator_poly)


def _capacity(version, level):
    data_bytes = sum(d for d, _ in EC_BLOCKS[version][level])
    count_bits = 8 if version < 10 else 16
    return (data_bytes * 8 - 4 - count_bits) // 8


def pick_version(payload_len, level):
    for version in range(1, qr_scan.MAX_VERSION + 1):
        if _capacity(version, level) >= payload_len:
            return version
    raise ValueError("payload of %d bytes exceeds version %d at level %s"
                     % (payload_len, qr_scan.MAX_VERSION, level))


def _rs_encode_block(data, ec_len):
    gen = rs_generator_poly(ec_len)
    msg = list(data) + [0] * ec_len
    for i in range(len(data)):
        coef = msg[i]
        if coef == 0:
            continue
        for j in range(1, len(gen)):
            msg[i + j] ^= _gf_mul(gen[j], coef)
    return msg[len(data):]


def _build_codewords(payload, version, level):
    blocks = EC_BLOCKS[version][level]
    data_total = sum(d for d, _ in blocks)
    count_bits = 8 if version < 10 else 16
    bits = []

    def put(value, n):
        for j in range(n - 1, -1, -1):
            bits.append((value >> j) & 1)

    put(4, 4)  # byte mode
    put(len(payload), count_bits)
    for byte in payload:
        put(byte, 8)
    terminator = min(4, data_total * 8 - len(bits))
    put(0, terminator)
    while len(bits) % 8:
        bits.append(0)
    data = []
    for i in range(0, len(bits), 8):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | b
        data.append(byte)
    pad = (0xEC, 0x11)
    i = 0
    while len(data) < data_total:
        data.append(pad[i % 2])
        i += 1
    # split into blocks, RS per block, interleave
    block_data = []
    pos = 0
    for d, _ in blocks:
        block_data.append(data[pos:pos + d])
        pos += d
    block_ec = [_rs_encode_block(bd, blocks[i][1])
                for i, bd in enumerate(block_data)]
    out = []
    for i in range(max(d for d, _ in blocks)):
        for bd in block_data:
            if i < len(bd):
                out.append(bd[i])
    for i in range(blocks[0][1]):
        for be in block_ec:
            out.append(be[i])
    return out


def _place_function_patterns(matrix, version):
    dim = len(matrix)

    def finder(r0, c0):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = r0 + r, c0 + c
                if rr < 0 or cc < 0 or rr >= dim or cc >= dim:
                    continue
                inside = 0 <= r <= 6 and 0 <= c <= 6
                dark = inside and (r in (0, 6) or c in (0, 6)
                                   or (2 <= r <= 4 and 2 <= c <= 4))
                matrix[rr][cc] = 1 if dark else 0

    finder(0, 0)
    finder(0, dim - 7)
    finder(dim - 7, 0)
    for i in range(8, dim - 8):
        bit = 1 if i % 2 == 0 else 0
        matrix[6][i] = bit
        matrix[i][6] = bit
    for cy in ALIGN[version]:
        for cx in ALIGN[version]:
            if (cy <= 8 and cx <= 8) or (cy <= 8 and cx >= dim - 9) \
                    or (cy >= dim - 9 and cx <= 8):
                continue
            for r in range(-2, 3):
                for c in range(-2, 3):
                    dark = max(abs(r), abs(c)) != 1
                    matrix[cy + r][cx + c] = 1 if dark else 0
    matrix[dim - 8][8] = 1  # dark module
    if version >= 7:
        vinfo = version << 12
        rem = vinfo
        for i in range(17, 11, -1):
            if rem & (1 << i):
                rem ^= 0x1F25 << (i - 12)
        vinfo |= rem
        for i in range(18):
            bit = (vinfo >> i) & 1
            r, c = i // 3, len(matrix) - 11 + (i % 3)
            matrix[r][c] = bit
            matrix[c][r] = bit


def _place_format(matrix, level, mask):
    dim = len(matrix)
    code = _format_code(level, mask)
    copy1, copy2 = _format_positions(dim)
    for k in range(15):
        bit = (code >> (14 - k)) & 1
        r, c = copy1[k]
        matrix[r][c] = bit
        r, c = copy2[k]
        matrix[r][c] = bit


def _place_data(matrix, fm, codewords):
    dim = len(matrix)
    bits = []
    for byte in codewords:
        for j in range(7, -1, -1):
            bits.append((byte >> j) & 1)
    idx = 0
    col = dim - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(dim - 1, -1, -1) if upward else range(dim)
        for r in rows:
            for c in (col, col - 1):
                if not fm[r][c]:
                    matrix[r][c] = bits[idx] if idx < len(bits) else 0
                    idx += 1
        upward = not upward
        col -= 2


def _penalty(matrix):
    dim = len(matrix)
    score = 0
    # rule 1: runs >= 5 in rows and columns
    for lines in (matrix, list(zip(*matrix))):
        for line in lines:
            run = 1
            for i in range(1, dim):
                if line[i] == line[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run = 1
            if run >= 5:
                score += 3 + run - 5
    # rule 2: 2x2 blocks
    for r in range(dim - 1):
        for c in range(dim - 1):
            v = matrix[r][c]
            if matrix[r][c + 1] == v and matrix[r + 1][c] == v \
                    and matrix[r + 1][c + 1] == v:
                score += 3
    # rule 3: finder-like pattern with 4-module light run on either side
    pat_a = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat_b = pat_a[::-1]
    for lines in (matrix, list(zip(*matrix))):
        for line in lines:
            line = list(line)
            for i in range(dim - 10):
                window = line[i:i + 11]
                if window == pat_a or window == pat_b:
                    score += 40
    # rule 4: dark balance
    dark = sum(sum(row) for row in matrix)
    ratio = dark * 100 // (dim * dim)
    score += abs(ratio - 50) // 5 * 10
    return score


def encode(text, level="M", version=None):
    """Text → (matrix, version). Byte mode, best-penalty mask."""
    payload = text.encode("utf-8")
    if version is None:
        version = pick_version(len(payload), level)
    elif _capacity(version, level) < len(payload):
        raise ValueError("payload does not fit forced version %d" % version)
    dim = 17 + 4 * version
    codewords = _build_codewords(payload, version, level)
    fm = function_map(version)
    base = [[0] * dim for _ in range(dim)]
    _place_function_patterns(base, version)
    _place_data(base, fm, codewords)
    best = None
    for mask in range(8):
        matrix = [row[:] for row in base]
        for r in range(dim):
            for c in range(dim):
                if not fm[r][c] and _mask_bit(mask, r, c):
                    matrix[r][c] ^= 1
        _place_format(matrix, level, mask)
        score = _penalty(matrix)
        if best is None or score < best[0]:
            best = (score, matrix)
    return best[1], version


# ------------------------------------------------------------ rendering

def write_png_gray(path, pixels, w, h):
    """Minimal grayscale PNG writer (filter 0)."""
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw.extend(pixels[y * w:(y + 1) * w])

    def chunk(ctype, data):
        body = ctype + data
        return struct.pack(">I", len(data)) + body \
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw))))
        f.write(chunk(b"IEND", b""))


def render_gray(matrix, scale=8, quiet=4, dark=25, light=235):
    dim = len(matrix)
    size = (dim + 2 * quiet) * scale
    pixels = bytearray([light]) * (size * size)
    for r in range(dim):
        for c in range(dim):
            if matrix[r][c]:
                for y in range((quiet + r) * scale, (quiet + r + 1) * scale):
                    base = y * size + (quiet + c) * scale
                    for x in range(scale):
                        pixels[base + x] = dark
    return pixels, size


def warp_gray(pixels, size, out_w, out_h, corners, background=220, noise=0):
    """Projectively warp a rendered code into `corners` (TL,TR,BR,BL) of an
    out_w×out_h canvas — simulates an off-axis camera shot of a screen."""
    src = [(0.0, 0.0), (float(size), 0.0), (float(size), float(size)),
           (0.0, float(size))]
    coef = qr_scan._solve_homography(corners, src)  # dest→src mapping
    out = bytearray([background]) * (out_w * out_h)
    seed = 12345
    for y in range(out_h):
        for x in range(out_w):
            p = qr_scan._project(coef, x + 0.5, y + 0.5)
            if p is None:
                continue
            sx, sy = int(p[0]), int(p[1])
            if 0 <= sx < size and 0 <= sy < size:
                value = pixels[sy * size + sx]
                if noise:
                    seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
                    value = max(0, min(255, value + (seed % (2 * noise + 1))
                                       - noise))
                out[y * out_w + x] = value
    return out


def encode_to_png(text, path, level="M", scale=8, quiet=4):
    matrix, version = encode(text, level)
    pixels, size = render_gray(matrix, scale, quiet)
    write_png_gray(path, pixels, size, size)
    return version
