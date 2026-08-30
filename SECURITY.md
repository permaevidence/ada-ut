# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's security advisory
form: on this repository, go to **Security → Report a vulnerability**. Do
not open a public issue for anything you believe is a security problem.

You can expect an acknowledgement within a few days. Please include the
app version, device/channel, and reproduction steps.

## Scope

The Ada Companion app for Ubuntu Touch is an unconfined installer and
control panel for Ada CLI; it runs with the permissions of the user who
installs it, by design. Reports we consider in scope include:

- The install/update chain: manifest or checksum bypasses, downgrade
  acceptance, transactional-install recovery flaws that could activate
  unverified binaries.
- QR onboarding: crafted QR bundles that inject unintended configuration
  or exfiltrate scanned secrets; secrets persisting in frames, logs, or
  diagnostics.
- The app-chat socket client: peer confusion, path traversal in
  attachments, acknowledgement/durability flaws that lose or duplicate
  operator messages.
- Secret handling: keys leaking into logs, command-line arguments, or
  world-readable files; the sudo passcode dialog leaking or storing the
  passcode.

Out of scope: the fact that the app can install software and manage a
user-level service when its operator asks it to — that is the product.

## Supported versions

Only the latest released version is supported. Fixes ship as a new
release; there are no security backports.
