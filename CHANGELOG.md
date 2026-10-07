# Changelog

What changed in each version of Bmail, newest first. The section for a version
is also its release notes: the release build copies it onto the release page,
and the desktop app shows it under "What's new" when that version is ready to
install.

## 0.2.2

Mail that reads the way it was sent, and sorting you can trust after an update.

### New

- **Messages look like they do in your mail program.** The reader shows a
  message as its sender laid it out: logos, pictures, buttons, columns. *Text*
  beside it shows what Bmail read. A link opens in your own browser.
- **Mail you already have** was read in before layouts were kept. Open one and
  choose *Read my mail again* to fetch them.

### Changed

- **Pictures in a message are shown when you open it.** A picture fetched from
  a sender tells them the message was opened. To be asked first, open Setup
  and set **Pictures in mail** to *Ask first*. Sorting still fetches nothing.

### Fixed

- **A broadband order is not a job offer.** An unsure guess about mail a
  machine sent was final, so orders, bank notices and promotions were shown as
  offers and interviews to answer today. They are now filed as what they are.
- **Fixes reach the mail you already have.** After an update that changes the
  rules, everything is sorted again once, and the application cards are rebuilt.
  Your own corrections are kept.
- **Job digests stay job alerts**, whatever the adverts in them say.
- **Recommendation and digest mailers are not "personal mail".**
- **Mail that came out empty or as code is readable.** Messages whose HTML
  never closed its header were blank; others showed tags, `&amp;` and
  `%placeholders%`, or one tracking link over and over.
- **Opening Bmail after an update is quick.** Re-reading stored text took nine
  minutes on a large mailbox, and started over if Bmail was closed first. It
  now takes seconds.
- **The installer and uninstaller carry Bmail's mark** on Windows, as does the
  engine in Task Manager.

## 0.2.1

0.2.0 with a new mark, and quick on Windows. Coming from 0.1.0? Everything
listed under 0.2.0 in the changelog is new for you too.

### New

- **A new mark.** A cream postage stamp carrying a B, in place of the three
  lines that read as a menu button: on the installers, the app's icon, the
  window and the browser tab.

### Fixed

- **No two-second wait on every screen on Windows.** Bmail asked whether
  Ollama was running each time a screen loaded, and without it Windows takes
  two seconds to answer. It now asks quickly, and only every few seconds.
- **The screen you opened is the one you see.** Opening a second screen while
  the first was still loading could leave the first one's content on the page
  under the second one's name.
- **A message that takes a moment to load still opens.** On a busy computer the
  reading pane could be left showing grey bars until you changed screen.
- **No error in the terminal when a page is closed mid-load.** On Windows,
  closing or reloading the page while Bmail was answering printed a stack
  trace where `bmail` was started.

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
