# What Bmail costs to run: nothing

Bmail is built and distributed with free services only. This page lists every
service involved, why it costs nothing, and the paid options that were
deliberately left out.

## Free, and how to keep it free

| Service | Used for | Why it is free |
|---|---|---|
| GitHub (Free plan) | Code, issues, releases, downloads | Free for public and private repositories |
| GitHub Actions | Tests on every push; installers for each release | Unlimited for **public** repositories. A private one gets 2,000 minutes a month, and macOS minutes count ten times, Windows twice; a release uses roughly 50 of them |
| GitHub Releases | Hosting installers and updates | Free, no bandwidth charge |
| Dependabot | Warnings about vulnerable dependencies | Free |
| Tauri's updater | Signed automatic updates | Open source; the signing key is made on your own computer |
| Ollama and open models | The optional local AI | Free; runs on the user's computer |
| Python, Rust, Node.js, every library | Building the app | Open source (see THIRD_PARTY_NOTICES.md) |

**You cannot be charged by accident.** GitHub bills only an account with a
payment method *and* a spending limit above $0, and both are off by default. If
a private repository used up its free minutes, builds would stop until the next
month; nothing would be charged. To be certain: GitHub, Settings, Billing and
licensing, and check that no payment method is saved and every budget is $0.

## Left out on purpose

| Paid option | What it would do | Cost | What happens without it |
|---|---|---|---|
| Windows code-signing certificate | Remove the SmartScreen warning on first install | About €200 to €400 a year (Microsoft's cheaper Artifact Signing is for individuals in the US and Canada only) | Windows warns once: More info, then Run anyway |
| Apple Developer Program | Notarise the macOS app | $99 a year | macOS warns once: System Settings, Privacy & Security, Open Anyway |
| A Bmail server | Sync, web access | Hosting, and a GDPR processor role | Everything stays on the user's computer, which is the point |
| Google's yearly security assessment (CASA) | Needed only by apps that keep Gmail data on a server | Hundreds to thousands of dollars a year | Not needed: Bmail keeps mail on the user's computer |

Updates are signed with the project's own free key either way, so a missing
code-signing certificate never weakens what an installed copy accepts.

## If this changes

Adding any paid service needs a line in this file first, saying what it costs
and why the free route was not enough.
