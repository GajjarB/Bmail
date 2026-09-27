# Security

Bmail reads people's mail, so a security problem in it matters more than most.
Thank you for taking the time to report one.

## Reporting a problem

Please report it **privately**, not in a public issue:

1. Open the repository's **Security** tab on GitHub.
2. Choose **Report a vulnerability**.

That opens a private advisory that only the maintainer can see. Include what you
found, how to reproduce it, and what someone could do with it. You will get an
answer within a week, and credit in the release notes if you would like it.

If the Security tab does not show that button, open an ordinary issue that says
only "security report, please contact me", with no details, and the maintainer
will reach you privately.

## Which versions are fixed

Only the newest release. The desktop app updates itself, so a fix reaches
installed copies as soon as it is released.

## What Bmail is built to protect

- **Mail never leaves your computer.** There is no Bmail server. The only
  network contact besides your mail server is the desktop app's update check
  with GitHub; see [docs/PRIVACY.md](docs/PRIVACY.md).
- **The local server is for this computer only.** It listens on `127.0.0.1`,
  needs a session key that is new at every start, rejects a `Host` header that
  is not local (DNS rebinding) and any request from another site (`Origin`).
- **Mail is never code.** Messages are shown as text, never as HTML; the page
  runs under a strict Content-Security-Policy that loads nothing from anywhere
  else.
- **Passwords stay in the system keychain**, never in a file. The mail store
  and settings are readable by your user account only.
- **Encryption to the mail server is required**, except to a bridge running on
  this computer.
- **Updates are signed**, and checked against a key built into the app before
  they are installed.
- **The local AI model has no tools**: it only returns text that is checked
  before use, and it must run on this computer unless you explicitly allow
  otherwise.

## Out of scope

- Someone who already controls your user account (they can read your mail
  client too).
- Warnings from Windows or macOS that the installer is not code-signed: signing
  certificates cost money, so the installers are not signed. The updates are.
