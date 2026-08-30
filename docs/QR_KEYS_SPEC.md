# QR key transfer — format spec

Typing API keys on a phone keyboard is the worst step of Ada's phone setup.
This spec defines how keys move computer → phone by QR code. Three parties
implement it and are cross-checked against each other by
`scripts/qr_selftest.py`:

- **Generator** — the website page `ada-app-psi.vercel.app/qr`
  (`ada-website/app/qr/qrlib.mjs` + `QrGenerator.tsx`). Fully client-side:
  keys never leave the browser; the page makes no network requests after
  load and works offline.
- **Reference encoder** — `scripts/qr_ref.py` (test-side only, never
  shipped). Source of truth the JS is a port of; the selftest compares the
  two matrix-for-matrix via node.
- **Scanner** — `py/qr_scan.py` in the click, driven by
  `qml/pages/ScanPage.qml`. Pure stdlib (no zbar/zxing/PIL — the click
  stays compiled-code-free and architecture "all").

## QR profile

- Byte mode, EC level **M**, versions **1–10** only. The generator picks
  the smallest fitting version; the scanner refuses anything beyond v10.
- The scanner also reads third-party codes (numeric/alphanumeric modes,
  levels L/Q/H, ECI headers) within v1–10 — per-field Scan buttons accept
  any QR that fits those limits.
- Known limitation: v1 codes (no alignment pattern) survive only mild
  perspective. The generator never emits v1 for realistic keys, so this
  affects only tiny third-party codes scanned at a steep angle.

## Single-key codes ("Un codice per chiave")

Payload = the raw key text, nothing else. Scanned by the per-field Scan
button, which pastes the text into the field; the normal probe-then-apply
still validates before anything is saved.

## Key bundles ("Pacchetto unico")

Bundle payload (ASCII JSON; the generator `\uXXXX`-escapes anything else):

```json
{"v":1,"keys":{"opencode":"...","openai":"...", ...}}
```

Known key names (anything else is reported as ignored):
`opencode`, `openrouter`, `custom`, `openai`, `serper`, `jina`,
`telegram_token`, `telegram_chat_id`, `agentmail`.

The payload is split into frames of at most **100 characters** (code
points), each framed as

```
ADAK1:<i>/<n>:<crc32hex8>:<chunk>
```

- `i` ∈ 1..`n`, `n` ≤ **16** frames, payload ≤ 16 KB, value ≤ 4 KB.
- `crc32hex8` = zero-padded lowercase hex of `zlib.crc32` over the FULL
  payload UTF-8 — identical in every frame. The scanner keys its assembly
  session on it, so frames from two different bundles cannot mix; a wrong
  assembled checksum resets the session.
- The generator page cycles the frames automatically (~1.3 s each); the
  scanner assembles them in whatever order the camera catches them,
  duplicates are idempotent.

On completion the app stores the keys **in memory only** (`Main.qml`
`scannedKeys`); wizard/Settings pages pre-fill their fields from it and
every value still goes through the same live probe + `setup-api apply` as
a typed one. Nothing is persisted by the scan itself.

## Camera pipeline

`ScanPage.qml` grabs the viewfinder via `grabToImage` (~1 frame/s, ≤720 px
wide) to a PNG in `~/.cache/ada.permaevidence/` — PNG because it is the
one raster format pure-stdlib Python can decode (zlib). `qr_scan.scan_png`
deletes the frame file after each read. Decode pipeline: grayscale →
zxing-style block-adaptive binarization (flat-block black-point
propagation) → finder detection (row-scan 1:1:3:1:1 + vertical/horizontal/
diagonal cross-checks, ranked triples) → bottom-right alignment search
(expanding 4/8/16-module windows, diagonal-checked candidates) →
projective sampling gated on timing-pattern alternation (≥ 75%) → format
BCH decode → unmask → block deinterleave → Reed-Solomon per block →
byte-stream parse. ~0.13 s/frame on the Mac; expect ~1–2 s on a Pixel 3a.

## Validation

`scripts/qr_selftest.py` (77 checks): EC-table totals, 40-combo clean
round-trip, strong/mild perspective batteries, seeded harsh sweep, 1-bit
PNG, framing/session/bundle guards, scan modes, JS↔Python byte-identical
matrices and frames, and a JS→warped-PNG→Python end-to-end bundle.
External validation performed 2026-08-28: our decoder read 28/28
python-qrcode codes; zbar read 40/40 of our encoder's codes (v1–10 ×
L/M/Q/H).
