# Releasing

A release is a tag. Push `v0.2.0` and GitHub Actions builds installers for
macOS, Windows and Linux and attaches them to a **draft** release. Read the
draft, try an installer, then publish it; nothing reaches anyone before that.

## Cutting a release

```bash
python packaging/set_version.py 0.2.0     # writes the version into all seven files
git commit -am "Release 0.2.0"
git tag v0.2.0
git push origin main v0.2.0
```

The workflow refuses a tag whose version does not match the files, and the test
suite fails if the files disagree with each other, so the number the app reports
is always the number it was released as.

To try the build without releasing anything, run **release** from the Actions
tab, or push to the `release` branch (`git push origin main:release`), useful
where tags cannot be pushed. The same installers land on a draft release named
after the current version; publishing that draft creates its tag.

## What gets built

| System | Built on | Files |
|---|---|---|
| macOS, Apple silicon | macos-14 | `.dmg`, `.app.tar.gz` |
| Windows, x64 | windows-latest | `.msi`, `-setup.exe` (installs without administrator rights) |
| Linux, x64 | ubuntu-22.04 | `.AppImage`, `.deb`, `.rpm` |

Intel Macs are not built. Linux is built on 22.04 on purpose: an AppImage runs
on systems at least as new as the one it was built on.

Each installer carries two programs:

- **the engine** — the Python core frozen by PyInstaller into one file
  (`packaging/build_sidecar.py`). The build script starts what it built and
  fetches the page, a script, a font, the packs and the API before the job is
  allowed to continue, because a frozen app missing one data file still builds
  cleanly and only fails on someone's machine.
- **the shell** — the Tauri window (`desktop/`), which starts the engine beside
  it, points a webview at it, and stops it when the window closes. If the window
  is killed outright, the engine notices within two seconds and stops itself.

Nobody needs Python installed. The engine is about 24 MB and the `.deb` about
the same, compressed; the AppImage is about 95 MB because it carries its own
WebKit.

The trainable classifier (numpy, scikit-learn) and the Gmail-API and Microsoft
libraries are left out of the installers to keep them small. The app connects
mail over IMAP and sorts with the rules and the built-in classifier; people who
want the extras install with `pip`.

## Signing and updates

Everything below is optional. Without it the workflow still builds working,
**unsigned** installers, and says so in the log.

### Automatic updates

With an update key, the app checks for a newer published release when it
starts, downloads it in the background, and installs it when the window is
closed. Every download is verified against the public key built into the app;
without a key the updater is not built in at all. Copies installed before the
key existed (0.1.0) have no updater: their owners install the next version once
by hand, and it updates itself from then on.

**1. Make a key pair, once, on your own computer.** Needs Node.js; no checkout.

```bash
npx @tauri-apps/cli@2 signer generate -w ~/.bmail-update.key
```

Choose a password when asked. This writes `~/.bmail-update.key` (private) and
`~/.bmail-update.key.pub` (public). Back the private key and its password up,
in a password manager for instance: lose either, and installed copies can
never be updated again, since nothing else can sign an update they will accept.

**2. Give the build the key.** In the repository's **Settings → Secrets and
variables → Actions**:

| Kind | Name | Value |
|---|---|---|
| Secret | `TAURI_SIGNING_PRIVATE_KEY` | the whole contents of `~/.bmail-update.key` |
| Secret | `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | its password |
| Variable | `TAURI_UPDATER_PUBKEY` | the whole contents of `~/.bmail-update.key.pub` |

**3. Put the releases where an app can reach them.** An installed app asks for
updates without signing in, and a private repository answers "not found". One
of:

- **Make this repository public** (Settings → General → Danger Zone). Nothing
  else to set.
- **Keep the code private and publish to a public releases repository.**
  1. Create a public repository, say `GajjarB/Bmail-releases`, with a README
     so it has a `main` branch.
  2. Create a fine-grained personal access token (GitHub → Settings →
     Developer settings → Fine-grained tokens) for that repository only, with
     **Contents: Read and write**.
  3. Here, add the variable `RELEASES_REPO` = `GajjarB/Bmail-releases` and the
     secret `RELEASES_TOKEN` = the token.
  4. Point the README's Download link at that repository's releases.

The build log says which is in effect ("Auto-update: on, from …"), and warns if
updates would come from a private repository.

**4. Release as usual.** Updates are offered only from **published** releases;
drafts are invisible to the updater. The workflow attaches `latest.json` and a
signature for each installer. Windows updates through the per-user
`-setup.exe`, so no update asks for administrator rights.

### macOS

Unsigned apps open only after **System Settings → Privacy & Security → Open
Anyway**. To sign and notarise, an Apple Developer account ($99 a year) is
needed; add these secrets:

| Secret | What it is |
|---|---|
| `APPLE_CERTIFICATE` | Your "Developer ID Application" certificate as a base64 `.p12` |
| `APPLE_CERTIFICATE_PASSWORD` | Its export password |
| `APPLE_SIGNING_IDENTITY` | e.g. `Developer ID Application: Your Name (TEAMID)` |
| `APPLE_ID`, `APPLE_PASSWORD`, `APPLE_TEAM_ID` | For notarisation; the password is an app-specific one |

`desktop/src-tauri/Entitlements.plist` lets the notarised app load the engine,
which unpacks its Python libraries at run time.

### Windows

Unsigned installers show SmartScreen's **More info → Run anyway**. Code signing
needs a certificate (Azure Trusted Signing is the cheapest, about $10 a month)
and a `signCommand` in `desktop/src-tauri/tauri.conf.json`; it is not wired up
yet.

## Building locally

```bash
python packaging/build_sidecar.py         # the engine, for this machine, then its smoke test
cd desktop && npm ci
npx tauri build                           # installers in src-tauri/target/release/bundle/
```

On Linux this needs the WebKitGTK development packages (see `desktop/README.md`).
