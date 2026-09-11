# Privacy and the legal position

Local processing here is not a privacy nicety bolted on afterwards. It is the
architectural choice the rest of the project follows from, and it removes three
costs that otherwise sink a project in this category.

None of this is legal advice. Verify current requirements before you distribute
anything — this area changes yearly.

## What leaves your machine

| Destination | What goes there | When |
|---|---|---|
| Your mail provider | API/IMAP requests to read your own mail | Only while ingesting |
| Your mail provider | A label, if you explicitly enable and confirm write-back | Never by default |
| `127.0.0.1:11434` | Message text, to a model running on your own machine | Only for messages that reach Stage 3 |
| Notion | Case fields, if you configure a token and pass `--confirm` | Never by default |

That is the complete list. There is no Bmail server, no telemetry, no analytics,
no crash reporting and no update check. `bmail doctor` prints the current state of
these settings, and warns if a password has ended up in the config file.

These guards enforce it rather than merely documenting it:

- `OllamaClient` refuses any non-loopback host unless `llm.allow_remote = true`
  is set deliberately.
- The local daemon binds `127.0.0.1` only, on a random port, behind a token
  generated at startup, with the `Host` header pinned against DNS rebinding and
  `Origin` checked on every request.
- The daemon stops when the window that started it goes away — including if that
  window is killed outright — so a mail-reading server never lingers.
- The UI fetches nothing from the internet: no fonts, no CDN, no analytics. A
  test fails the build if any request leaves the machine.

## Google's restricted scopes

`gmail.readonly`, `gmail.modify` and `gmail.metadata` are *restricted* scopes.
Google's rule is that an app which stores or transmits restricted-scope data on
servers — or can reach it from or through a third-party server — must pass an
annual security assessment (CASA) by an approved lab. Recent self-serve pricing
starts in the high hundreds of dollars for simple apps and rises with tier, with
several weeks of review, **every year**. Google's own guidance has said for
years: architect so that Google user data is only ever stored client-side.

A desktop app that never ships mail content anywhere sidesteps the assessment.
It still needs OAuth brand and scope verification, but the recurring audit — the
thing that kills indie projects in this space — falls away. That is a moat you
get for free by choosing the local architecture.

Bmail's scope policy follows from this:

- `gmail.readonly` by default — all that triage needs.
- `gmail.modify` only when you turn label write-back on.
- `https://mail.google.com/` (full access, including permanent delete) is
  **never** requested. It buys nothing here and pushes the app into a stricter
  tier.

## GDPR

Your inbox contains personal data about *other people* — recruiters, professors,
candidates. Send it to a third-party API and you are a controller using a
processor: you need a data processing agreement, a transfer mechanism, and a
privacy notice you can defend.

Process it on your own machine and, for personal use, you are largely in
household-exemption territory and there is no processor at all. In the EU that
is the difference between an interesting side project and something shippable.

If you deploy Bmail inside an organisation — the `hr-recruiter` pack is exactly
that case — the household exemption does not apply and you are a controller
with the usual obligations. The local architecture still helps: with no
processor and no transfer, the remaining work is a lawful basis, a retention
policy, and a record of processing.

## Distribution

Unverified OAuth apps cap at 100 test users behind an alarming consent screen.
The options, in increasing order of effort:

1. **Personal use.** Exempt. This is where to start.
2. **Bring your own client id.** Each user creates their own Google Cloud project
   and pastes their own OAuth client. This is what several privacy-focused mail
   tools do, and it is what `docs/PROVIDERS.md` documents.
3. **Brand and scope verification.** Worth doing once there are real users —
   and the desktop-only architecture is what keeps you out of the annual
   assessment even then.

## Data handling inside Bmail

- The store lives in your data directory with `0700` permissions.
- Attachment *payloads* are never stored, only filenames, types and sizes.
- Passwords go to the OS keychain (macOS Keychain, Windows DPAPI, libsecret) or
  the environment. Never the config file — `bmail doctor` warns if one is there.
- OAuth token files are written `0600`.
- URLs are reduced to their host before any text reaches a prompt, so no
  tracking pixel is loaded and no link is followed.
- Nothing is ever deleted from your mailbox. The only outward write is a label,
  and only after you confirm it.

## Retention

Everything is in one SQLite file. To delete all data:

```bash
rm -rf ~/.local/share/bmail        # Linux
rm -rf ~/Library/Application\ Support/Bmail   # macOS
```

To revoke access, remove the app from your provider's connected-apps page and
delete the token file.
