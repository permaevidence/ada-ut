# Ada for Ubuntu Touch

Installer & control panel for [Ada CLI](https://github.com/permaevidence/ada-cli)
on Ubuntu Touch 24.04 — install, set up, and manage an always-on Ada without
ever opening the Terminal.

The app drives the CLI's machine-readable setup surface (`ada setup-api`,
JSON over stdin/stdout, schema 1) — it never re-implements validation or
persistence.

## Status (M6)

- M2: Welcome/detect screen, checksum-verified CDN install with progress.
- M3: guided setup wizard (provider & model with the catalog served by
  setup-api, OpenAI/Serper/Jina keys, name, email/calendar provider,
  Telegram) — every step probes live before saving; and Settings, where
  every value is editable at any time (masked current values, remove paths,
  restart-to-apply hints when the daemon is running).
- M4: Always-on screen (systemd user service + linger + the UT kernel
  wakelock, root steps run under in-app `sudo -S` with the passcode
  collected in a dialog and passed on stdin only) and the Dashboard
  (health rows, start/stop/restart, journal tail, CLI update).
- Requires Ada CLI ≥ v0.1.43 on the device (the first release with
  `setup-api`); the installer refuses older releases by design. Keep-awake
  additionally requires v0.1.44's trap-protected scripts — the app checks
  the served script's content and refuses to run one that could leave the
  system partition writable on failure.
- Terminal-free scope: complete for the None and AgentMail email paths.
  Google Workspace still needs one `gws auth login` in the Terminal
  (browser OAuth round-trip) after saving credentials in the app.
- M5: QR key transfer (`docs/QR_KEYS_SPEC.md`). The website page
  `ada-app-psi.vercel.app/qr` turns API keys into QR codes entirely in the
  browser; the app scans them with the camera — per-field Scan buttons and
  a multi-frame "key bundle" that pre-fills every setup field in one scan.
  Decoding is pure-stdlib Python (`py/qr_scan.py`, QR v1–10), so the click
  stays free of compiled code; scanned values still probe live before
  saving. Selftest: `python3 scripts/qr_selftest.py`.
- M6: Chat (`qml/pages/ChatPage.qml`).
  A live chat window onto the local Ada daemon over its companion-app
  Unix socket (`~/.local/share/ada/app-chat.sock`, JSON lines,
  same-user-only): bubble history from the server's snapshot + live
  message/status events, text turns, attachments through a built-in file
  picker (paths only — files never cross the socket), voice notes
  recorded with GStreamer/arecord and transcribed by Ada's configured
  provider, a Stop control, and `/command` pass-through to the shared
  command set. Requires Ada CLI ≥ v0.1.45 (first release serving the
  socket); older daemons simply show "connecting…" with a hint to update.
  Selftest: `python3 scripts/chat_selftest.py` (fake server + fake
  recorders + QML structural sweep).

## Stack

- QML with Lomiri Components 1.3: `qml/Main.qml` is the shell (state,
  Python bridge, wizard sequencing, launch routing, boot/Install pages);
  each screen lives in `qml/pages/` and receives `{app: root}`. Since
  v0.7.0 the home screen is a single shell page with Chat / Dashboard /
  Settings header sections over three always-alive views (visibility
  switching, so the chat socket and composer survive tab hops); launch
  routes straight into the guided setup until it completes, then into
  Chat. Sub-screens (wizard steps, scanner, viewers, install log) push on
  the PageStack above the shell.
- Python backend via PyOtherSide (`py/ada_bridge.py`): process spawning,
  downloads, setup-api round trips. **On-device verification of PyOtherSide
  availability is an explicit M2 checkpoint** — the Welcome page shows the
  import traceback if the module is missing.
- Unconfined AppArmor template (required: the app writes `~/.local/bin`,
  runs `systemctl --user`, and spawns `ada`; confinement is inherited by
  children, so there is no confined alternative).

## Build

No Clickable or Docker needed — the app is architecture-independent:

    python3 scripts/build_click.py

writes `build/ada.permaevidence_<version>_all.click`, byte-identical across
runs (SOURCE_DATE_EPOCH honored). (`clickable.yaml` is included for people
who prefer the Clickable workflow; both produce the same app.)

Offline test of the install pipeline (staged validation, transactional
swap + rollback, checksum/extraction guards):

    python3 scripts/bridge_selftest.py

Packaging regression test (builds the click twice and inspects the ar
archive: LICENSE + manifest ship in the data area, determinism, no build
artifacts):

    python3 scripts/click_selftest.py

## Install on the phone

Any one of:

1. Copy the .click over and install from the terminal:

       scp build/ada.permaevidence_*.click phablet@<phone>:
       ssh phablet@<phone> pkcon install-local --allow-untrusted ada.permaevidence_*.click

2. Download the .click on the phone (Morph browser / Telegram) and open it —
   the OpenStore app performs local installation after an "untrusted
   package" confirmation.

3. `clickable install` from a Clickable checkout.

4. Public download page: https://ada-app-psi.vercel.app/app — links the
   latest signed GitHub Release (the page verifies the release envelope
   with the pinned app key before it shows a download link).

Once installed, the app updates itself: Settings has a manual "check for
updates" action and an optional auto-update toggle (off by default).

## Signed releases

Everything this app downloads is authenticated before a byte of it is
trusted (`py/release_verify.py`):

- **Ada CLI installs/updates** read the CLI's signed release envelope from
  `github.com/permaevidence/ada-cli/releases/latest/download/manifest.sig.json`
  and verify it with the pinned CLI release key — so a phone first-install
  is authenticated, not merely TLS-protected.
- **App self-updates** read this repository's own signed envelope
  (`releases/latest/download/manifest.sig.json`) and verify it with the
  pinned app key (`.release-keys/ada-ut-release.pub.pem`).

Both channels enforce: exact Ed25519 signature over a domain-separated
input, schema/channel/SemVer/expiry/not-before checks, assets restricted
to the pinned per-version GitHub location, authenticated size + SHA-256
while streaming (no Content-Length trust), a hard byte bound, and
anti-rollback: the sequence must be ≥ the minimum baked into this build
and ≥ the highest sequence this phone ever accepted for the same channel
and location (`~/.config/ada-ut/release_trust.json`, locked, monotonic).

Ed25519 on the phone: a system OpenSSL proven against the RFC 8032
known-answer vectors is used when present; otherwise a dependency-free
verify-only implementation (also proven against the vectors, and
cross-checked against OpenSSL in `scripts/release_selftest.py`). Settings
shows which one this phone uses. There are deliberately no environment
overrides for keys, URLs or the verifier.

Publishing (maintainers): `scripts/publish_click.sh` builds the click
deterministically, checks supersession against the authenticated live
release, signs with the local app key (`~/.ada-release-keys/`, mode 0600,
never on argv), verifies the envelope with the committed public key AND
with the app's own verifier, publishes an immutable GitHub Release (assets,
envelope last, atomic go-live), re-downloads and byte-compares the public
state, and only then records the publication. `--dry-run` stops before
publishing; `--bootstrap` is accepted once, for the first signed release;
`--legacy-blob` additionally refreshes the pre-signature Blob layout during
the transition window so app versions ≤ 0.7.3 can make their last
unsigned hop. Every release bumps `APP_RELEASE_SEQUENCE` in
`py/release_verify.py` together with `manifest.json`'s version.

Tests: `scripts/release_selftest.py` (verifier), `scripts/bridge_selftest.py`
(signed install/update paths, rollback, tampering, fault injection) and
`scripts/publish_selftest.py` (the publisher against a fake GitHub
Releases API with injected faults).

## Device assumptions

- Ubuntu Touch 24.04 (framework `ubuntu-touch-24.04-1.x`, AppArmor policy
  2404.1). Verify on a device with `ls /usr/share/click/frameworks/`.
- aarch64 or x86_64 (the bridge maps `platform.machine()` to the ada-cli
  CDN's `linux-arm64` / `linux-x64` builds).

## License

This app is **source-available** (not open source) under the Business
Source License 1.1 — see `LICENSE`. Production use is free for individual
people, including commercial use; companies and other entities need a
commercial license (contact address in `LICENSE`). Non-production use is
free for everyone, and each released version converts to Apache-2.0 four
years after release. The app bundles no third-party code (QML + pure-stdlib
Python, including the in-house QR decoder); PyOtherSide and the Lomiri
components are system packages on the device. The app icon is original
artwork owned by the project.

External contributions are not being accepted yet while the contribution
policy (CLA) is finalized — bug reports and security reports are very
welcome.
