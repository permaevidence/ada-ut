/*
 * QR encoder (byte mode, versions 1-10) — direct port of ada-ut's
 * scripts/qr_ref.py, which is the source of truth. The ada-ut selftest
 * cross-checks this file matrix-for-matrix via node, so the tables here
 * cannot drift from the phone-side decoder.
 *
 * Everything runs in the browser: keys never leave the page.
 */

export const MAX_VERSION = 10;
export const CHUNK_LEN = 100;
export const MAX_FRAMES = 16;
export const BUNDLE_PREFIX = "ADAK1:";

// ---------------------------------------------------------------- GF(256)

const GF_EXP = new Array(512).fill(0);
const GF_LOG = new Array(256).fill(0);
(() => {
  let x = 1;
  for (let i = 0; i < 255; i++) {
    GF_EXP[i] = x;
    GF_LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d;
  }
  for (let i = 255; i < 512; i++) GF_EXP[i] = GF_EXP[i - 255];
})();

function gfMul(a, b) {
  if (a === 0 || b === 0) return 0;
  return GF_EXP[GF_LOG[a] + GF_LOG[b]];
}

function gfPow(x, power) {
  return GF_EXP[((GF_LOG[x] * power) % 255 + 255) % 255];
}

// --------------------------------------------------------------- tables

export const EC_BLOCKS = {
  1: { L: [[19, 7]], M: [[16, 10]], Q: [[13, 13]], H: [[9, 17]] },
  2: { L: [[34, 10]], M: [[28, 16]], Q: [[22, 22]], H: [[16, 28]] },
  3: { L: [[55, 15]], M: [[44, 26]], Q: [[17, 18], [17, 18]], H: [[13, 22], [13, 22]] },
  4: { L: [[80, 20]], M: [[32, 18], [32, 18]], Q: [[24, 26], [24, 26]],
       H: [[9, 16], [9, 16], [9, 16], [9, 16]] },
  5: { L: [[108, 26]], M: [[43, 24], [43, 24]],
       Q: [[15, 18], [15, 18], [16, 18], [16, 18]],
       H: [[11, 22], [11, 22], [12, 22], [12, 22]] },
  6: { L: [[68, 18], [68, 18]], M: [[27, 16], [27, 16], [27, 16], [27, 16]],
       Q: [[19, 24], [19, 24], [19, 24], [19, 24]],
       H: [[15, 28], [15, 28], [15, 28], [15, 28]] },
  7: { L: [[78, 20], [78, 20]], M: [[31, 18], [31, 18], [31, 18], [31, 18]],
       Q: [[14, 18], [14, 18], [15, 18], [15, 18], [15, 18], [15, 18]],
       H: [[13, 26], [13, 26], [13, 26], [13, 26], [14, 26]] },
  8: { L: [[97, 24], [97, 24]], M: [[38, 22], [38, 22], [39, 22], [39, 22]],
       Q: [[18, 22], [18, 22], [18, 22], [18, 22], [19, 22], [19, 22]],
       H: [[14, 26], [14, 26], [14, 26], [14, 26], [15, 26], [15, 26]] },
  9: { L: [[116, 30], [116, 30]],
       M: [[36, 22], [36, 22], [36, 22], [37, 22], [37, 22]],
       Q: [[16, 20], [16, 20], [16, 20], [16, 20], [17, 20], [17, 20], [17, 20], [17, 20]],
       H: [[12, 24], [12, 24], [12, 24], [12, 24], [13, 24], [13, 24], [13, 24], [13, 24]] },
  10: { L: [[68, 18], [68, 18], [69, 18], [69, 18]],
        M: [[43, 26], [43, 26], [43, 26], [43, 26], [44, 26]],
        Q: [[19, 24], [19, 24], [19, 24], [19, 24], [19, 24], [19, 24], [20, 24], [20, 24]],
        H: [[15, 28], [15, 28], [15, 28], [15, 28], [15, 28], [15, 28], [16, 28], [16, 28]] },
};

export const ALIGN = {
  1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
  7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
};

const EC_BITS = { M: 0, L: 1, H: 2, Q: 3 };

function formatCode(level, mask) {
  const data = (EC_BITS[level] << 3) | mask;
  let rem = data << 10;
  for (let i = 14; i > 9; i--) {
    if (rem & (1 << i)) rem ^= 0x537 << (i - 10);
  }
  return ((data << 10) | rem) ^ 0x5412;
}

function maskBit(mask, r, c) {
  switch (mask) {
    case 0: return (r + c) % 2 === 0;
    case 1: return r % 2 === 0;
    case 2: return c % 3 === 0;
    case 3: return (r + c) % 3 === 0;
    case 4: return (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0;
    case 5: return ((r * c) % 2) + ((r * c) % 3) === 0;
    case 6: return (((r * c) % 2) + ((r * c) % 3)) % 2 === 0;
    default: return (((r + c) % 2) + ((r * c) % 3)) % 2 === 0;
  }
}

function functionMap(version) {
  const dim = 17 + 4 * version;
  const fm = Array.from({ length: dim }, () => new Array(dim).fill(false));
  for (let r = 0; r < 9; r++) {
    for (let c = 0; c < 9; c++) fm[r][c] = true;
    for (let c = dim - 8; c < dim; c++) fm[r][c] = true;
  }
  for (let r = dim - 8; r < dim; r++) {
    for (let c = 0; c < 9; c++) fm[r][c] = true;
  }
  for (let i = 0; i < dim; i++) {
    fm[6][i] = true;
    fm[i][6] = true;
  }
  for (const cy of ALIGN[version]) {
    for (const cx of ALIGN[version]) {
      if ((cy <= 8 && cx <= 8) || (cy <= 8 && cx >= dim - 9)
          || (cy >= dim - 9 && cx <= 8)) continue;
      for (let r = cy - 2; r <= cy + 2; r++) {
        for (let c = cx - 2; c <= cx + 2; c++) fm[r][c] = true;
      }
    }
  }
  if (version >= 7) {
    for (let r = 0; r < 6; r++) {
      for (let c = dim - 11; c < dim - 8; c++) {
        fm[r][c] = true;
        fm[c][r] = true;
      }
    }
  }
  return fm;
}

function formatPositions(dim) {
  const copy1 = [[8, 0], [8, 1], [8, 2], [8, 3], [8, 4], [8, 5], [8, 7], [8, 8],
                 [7, 8], [5, 8], [4, 8], [3, 8], [2, 8], [1, 8], [0, 8]];
  const copy2 = [];
  for (let i = 0; i < 7; i++) copy2.push([dim - 1 - i, 8]);
  for (let i = 0; i < 8; i++) copy2.push([8, dim - 8 + i]);
  return [copy1, copy2];
}

// --------------------------------------------------------------- encoder

export function capacity(version, level) {
  let dataBytes = 0;
  for (const [d] of EC_BLOCKS[version][level]) dataBytes += d;
  const countBits = version < 10 ? 8 : 16;
  return Math.floor((dataBytes * 8 - 4 - countBits) / 8);
}

export function pickVersion(payloadLen, level) {
  for (let v = 1; v <= MAX_VERSION; v++) {
    if (capacity(v, level) >= payloadLen) return v;
  }
  throw new Error(`payload of ${payloadLen} bytes exceeds version ${MAX_VERSION} at level ${level}`);
}

function rsGeneratorPoly(nsym) {
  let g = [1];
  for (let i = 0; i < nsym; i++) {
    const q = [1, gfPow(2, i)];
    const r = new Array(g.length + 1).fill(0);
    for (let j = 0; j < q.length; j++) {
      if (q[j] === 0) continue;
      for (let k = 0; k < g.length; k++) {
        if (g[k]) r[k + j] ^= gfMul(g[k], q[j]);
      }
    }
    g = r;
  }
  return g;
}

function rsEncodeBlock(data, ecLen) {
  const gen = rsGeneratorPoly(ecLen);
  const msg = data.concat(new Array(ecLen).fill(0));
  for (let i = 0; i < data.length; i++) {
    const coef = msg[i];
    if (coef === 0) continue;
    for (let j = 1; j < gen.length; j++) {
      msg[i + j] ^= gfMul(gen[j], coef);
    }
  }
  return msg.slice(data.length);
}

function buildCodewords(payload, version, level) {
  const blocks = EC_BLOCKS[version][level];
  let dataTotal = 0;
  for (const [d] of blocks) dataTotal += d;
  const countBits = version < 10 ? 8 : 16;
  const bits = [];
  const put = (value, n) => {
    for (let j = n - 1; j >= 0; j--) bits.push((value >> j) & 1);
  };
  put(4, 4);
  put(payload.length, countBits);
  for (const byte of payload) put(byte, 8);
  const terminator = Math.min(4, dataTotal * 8 - bits.length);
  put(0, terminator);
  while (bits.length % 8) bits.push(0);
  const data = [];
  for (let i = 0; i < bits.length; i += 8) {
    let byte = 0;
    for (let j = 0; j < 8; j++) byte = (byte << 1) | bits[i + j];
    data.push(byte);
  }
  const pad = [0xec, 0x11];
  let p = 0;
  while (data.length < dataTotal) data.push(pad[p++ % 2]);
  const blockData = [];
  let pos = 0;
  for (const [d] of blocks) {
    blockData.push(data.slice(pos, pos + d));
    pos += d;
  }
  const blockEc = blockData.map((bd, i) => rsEncodeBlock(bd, blocks[i][1]));
  const out = [];
  const maxLen = Math.max(...blocks.map(([d]) => d));
  for (let i = 0; i < maxLen; i++) {
    for (const bd of blockData) if (i < bd.length) out.push(bd[i]);
  }
  for (let i = 0; i < blocks[0][1]; i++) {
    for (const be of blockEc) out.push(be[i]);
  }
  return out;
}

function placeFunctionPatterns(matrix, version) {
  const dim = matrix.length;
  const finder = (r0, c0) => {
    for (let r = -1; r < 8; r++) {
      for (let c = -1; c < 8; c++) {
        const rr = r0 + r, cc = c0 + c;
        if (rr < 0 || cc < 0 || rr >= dim || cc >= dim) continue;
        const inside = r >= 0 && r <= 6 && c >= 0 && c <= 6;
        const dark = inside && (r === 0 || r === 6 || c === 0 || c === 6
                                || (r >= 2 && r <= 4 && c >= 2 && c <= 4));
        matrix[rr][cc] = dark ? 1 : 0;
      }
    }
  };
  finder(0, 0);
  finder(0, dim - 7);
  finder(dim - 7, 0);
  for (let i = 8; i < dim - 8; i++) {
    const bit = i % 2 === 0 ? 1 : 0;
    matrix[6][i] = bit;
    matrix[i][6] = bit;
  }
  for (const cy of ALIGN[version]) {
    for (const cx of ALIGN[version]) {
      if ((cy <= 8 && cx <= 8) || (cy <= 8 && cx >= dim - 9)
          || (cy >= dim - 9 && cx <= 8)) continue;
      for (let r = -2; r <= 2; r++) {
        for (let c = -2; c <= 2; c++) {
          matrix[cy + r][cx + c] = Math.max(Math.abs(r), Math.abs(c)) !== 1 ? 1 : 0;
        }
      }
    }
  }
  matrix[dim - 8][8] = 1;
  if (version >= 7) {
    let vinfo = version << 12;
    let rem = vinfo;
    for (let i = 17; i > 11; i--) {
      if (rem & (1 << i)) rem ^= 0x1f25 << (i - 12);
    }
    vinfo |= rem;
    for (let i = 0; i < 18; i++) {
      const bit = (vinfo >> i) & 1;
      const r = Math.floor(i / 3), c = dim - 11 + (i % 3);
      matrix[r][c] = bit;
      matrix[c][r] = bit;
    }
  }
}

function placeFormat(matrix, level, mask) {
  const dim = matrix.length;
  const code = formatCode(level, mask);
  const [copy1, copy2] = formatPositions(dim);
  for (let k = 0; k < 15; k++) {
    const bit = (code >> (14 - k)) & 1;
    matrix[copy1[k][0]][copy1[k][1]] = bit;
    matrix[copy2[k][0]][copy2[k][1]] = bit;
  }
}

function placeData(matrix, fm, codewords) {
  const dim = matrix.length;
  const bits = [];
  for (const byte of codewords) {
    for (let j = 7; j >= 0; j--) bits.push((byte >> j) & 1);
  }
  let idx = 0;
  let col = dim - 1;
  let upward = true;
  while (col > 0) {
    if (col === 6) col -= 1;
    const rows = [];
    if (upward) { for (let r = dim - 1; r >= 0; r--) rows.push(r); }
    else { for (let r = 0; r < dim; r++) rows.push(r); }
    for (const r of rows) {
      for (const c of [col, col - 1]) {
        if (!fm[r][c]) {
          matrix[r][c] = idx < bits.length ? bits[idx] : 0;
          idx += 1;
        }
      }
    }
    upward = !upward;
    col -= 2;
  }
}

function penalty(matrix) {
  const dim = matrix.length;
  let score = 0;
  const columns = Array.from({ length: dim },
    (_, c) => matrix.map((row) => row[c]));
  for (const lines of [matrix, columns]) {
    for (const line of lines) {
      let run = 1;
      for (let i = 1; i < dim; i++) {
        if (line[i] === line[i - 1]) run += 1;
        else {
          if (run >= 5) score += 3 + run - 5;
          run = 1;
        }
      }
      if (run >= 5) score += 3 + run - 5;
    }
  }
  for (let r = 0; r < dim - 1; r++) {
    for (let c = 0; c < dim - 1; c++) {
      const v = matrix[r][c];
      if (matrix[r][c + 1] === v && matrix[r + 1][c] === v
          && matrix[r + 1][c + 1] === v) score += 3;
    }
  }
  const patA = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0];
  const patB = [...patA].reverse();
  const matches = (line, i, pat) => {
    for (let k = 0; k < 11; k++) if (line[i + k] !== pat[k]) return false;
    return true;
  };
  for (const lines of [matrix, columns]) {
    for (const line of lines) {
      for (let i = 0; i <= dim - 11; i++) {
        if (matches(line, i, patA) || matches(line, i, patB)) score += 40;
      }
    }
  }
  let dark = 0;
  for (const row of matrix) for (const v of row) dark += v;
  const ratio = Math.floor((dark * 100) / (dim * dim));
  score += Math.floor(Math.abs(ratio - 50) / 5) * 10;
  return score;
}

export function encodeQR(text, level = "M", version = null) {
  const payload = Array.from(new TextEncoder().encode(text));
  if (version === null) version = pickVersion(payload.length, level);
  else if (capacity(version, level) < payload.length) {
    throw new Error(`payload does not fit forced version ${version}`);
  }
  const dim = 17 + 4 * version;
  const codewords = buildCodewords(payload, version, level);
  const fm = functionMap(version);
  const base = Array.from({ length: dim }, () => new Array(dim).fill(0));
  placeFunctionPatterns(base, version);
  placeData(base, fm, codewords);
  let best = null;
  for (let mask = 0; mask < 8; mask++) {
    const matrix = base.map((row) => [...row]);
    for (let r = 0; r < dim; r++) {
      for (let c = 0; c < dim; c++) {
        if (!fm[r][c] && maskBit(mask, r, c)) matrix[r][c] ^= 1;
      }
    }
    placeFormat(matrix, level, mask);
    const score = penalty(matrix);
    if (best === null || score < best.score) best = { score, matrix };
  }
  return { matrix: best.matrix, version };
}

// ------------------------------------------------------------- bundles

const CRC_TABLE = (() => {
  const table = new Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

export function crc32(text) {
  const bytes = new TextEncoder().encode(text);
  let crc = 0xffffffff;
  for (const byte of bytes) crc = CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

export function makeFrames(payloadText, chunkLen = CHUNK_LEN) {
  // Chunk by code points, matching Python slicing semantics (bundle
  // payloads are ASCII JSON in practice).
  const crc = crc32(payloadText).toString(16).padStart(8, "0");
  const chars = Array.from(payloadText);
  const chunks = [];
  for (let i = 0; i < chars.length; i += chunkLen) {
    chunks.push(chars.slice(i, i + chunkLen).join(""));
  }
  if (chunks.length === 0) chunks.push("");
  if (chunks.length > MAX_FRAMES) {
    throw new Error(`payload needs ${chunks.length} frames (max ${MAX_FRAMES})`);
  }
  return chunks.map((chunk, i) =>
    `${BUNDLE_PREFIX}${i + 1}/${chunks.length}:${crc}:${chunk}`);
}

export function buildBundlePayload(keys) {
  // Stable field order, ASCII-only output (escape anything exotic).
  const clean = {};
  for (const [name, value] of Object.entries(keys)) {
    const v = String(value).trim();
    if (v !== "") clean[name] = v;
  }
  const json = JSON.stringify({ v: 1, keys: clean });
  return json.replace(/[\u0080-\uffff]/g,
    (ch) => "\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0"));
}

// ------------------------------------------------- node cross-check CLI

if (typeof process !== "undefined" && process.argv
    && import.meta.url === `file://${process.argv[1]}`) {
  const [, , command, level, ...rest] = process.argv;
  if (command === "matrix") {
    const { matrix, version } = encodeQR(rest.join(" "), level);
    console.log(version);
    for (const row of matrix) console.log(row.join(""));
  } else if (command === "frames") {
    for (const frame of makeFrames(rest.join(" "))) console.log(frame);
  } else {
    console.error("usage: node qrlib.mjs matrix <level> <text> | frames _ <text>");
    process.exit(2);
  }
}
