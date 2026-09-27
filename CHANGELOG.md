# Changelog

What changed in each version of Bmail, newest first. The section for a version
is also its release notes: the release build copies it onto the release page,
and the desktop app shows it under "What's new" when that version is ready to
install.

## 0.2.0

The first version that keeps itself up to date: once it is installed, new
versions download in the background and install when you close Bmail, or
straight away from Setup, with **Restart now**.

### New

- **A new look.** A calm, panelled layout in light and dark: Today with a
  greeting, four numbers and a calendar; your applications as a board, one
  column per stage; Mail as three panes, like a mail client.
- **Light or dark, your choice.** Setup, Appearance, Colours: Match system,
  Light or Dark, applied the moment you pick it.
- **A calendar you can use.** Every dated thing on Today's calendar. Press a
  day to see only what falls on it.
- **About Bmail** in Setup: the version you have and where its next update
  stands.

### Better

- **Sorts a real inbox**, not only job mail: banks, shops, security alerts and
  newsletters are filed quietly, and mail about a credit card or an internet
  contract is no longer mistaken for a job application.
- **Reads recruiters' own wording** for interview invitations and document
  requests, in English and German.
- **Marketing mail reads like mail.** No more stylesheet code or walls of
  tracking links at the top of a message; long links show where they go.
- **HTML and very long mail** read the way a mail client shows them.
- **Opens at once**, and never redraws the message you are reading when new
  mail arrives.
- **Accessible.** Every screen passes an automated accessibility check in light
  and dark, and the whole app works from the keyboard.

### Open and safe

- **Every licence ships with the app.** The installers carry
  THIRD_PARTY_NOTICES.md: each component inside, its licence, and where its
  source is. Everything in them is free and open source, and the release build
  stops if a dependency's licence is not on the allowed list.
- **One less licence to comply with.** In the app, HTML is read with Python's
  own parser instead of a library that bundles an old LGPL engine; the text is
  identical.
- **SECURITY.md** says how to report a security problem privately.
- **The privacy notice lists the one outside contact** the desktop app makes:
  the update check with GitHub, which carries no mail or account data.

### Fixed

- **Setup is never asked twice.** Closing and opening Bmail no longer starts the
  welcome again, and your settings are kept between starts.
- Mail already in Bmail is cleaned again with the better rules the first time
  this version opens it.

## 0.1.0

The first release: Bmail reads your mailbox on your computer, tells you what
needs you, and tracks every application from the mail about it.
