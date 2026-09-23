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

With an update key, the app checks GitHub for a newer published release when
it starts, downloads it in the background, and installs it when the window is
closed. Every download is verified against the public key built into the app;
without a key the updater is not built in at all.

1. Generate a key pair once, and keep the private half safe. Lose it, and
   installed copies can never be updated again.

   ```bash
   cd desktop && npm ci
   npx tauri signer generate -w ~/.bmail-update.key
   ```

2. In the repository's **Settings → Secrets and variables → Actions**:
   - secret `TAURI_SIGNING_PRIVATE_KEY`: the contents of `~/.bmail-update.key`
   - secret `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`: its password
   - variable `TAURI_UPDATER_PUBKEY`: the contents of `~/.bmail-update.key.pub`

Updates are only offered from **published** releases (drafts are invisible to
the updater), through `latest.json`, which the workflow attaches.

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
