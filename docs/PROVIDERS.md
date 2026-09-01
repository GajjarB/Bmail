# Connecting mail accounts

Start with a file. It needs no credentials, no OAuth screen and no rate limits,
and it is the fastest way to learn what your own mail actually looks like.

## Local files — start here

Gmail Takeout, a Thunderbird export or any `.mbox` works, as do Maildir folders
and directories of `.eml` files.

```bash
sift ingest --path ~/Downloads/takeout/Mail/All\ mail.mbox --account archive
```

Re-ingesting is idempotent: messages are keyed on `Message-ID`, and a message
without one gets a deterministic synthetic id derived from its content.

```toml
[[accounts]]
name = "archive"
provider = "file"          # mbox, Maildir or a directory of .eml files
path = "~/mail/export.mbox"
```

## Gmail

Sift asks for `gmail.readonly`. It never requests `https://mail.google.com/`.
Because no mail content leaves your machine, this stays a client-side
integration — see `docs/PRIVACY.md` for why that matters.

Each user supplies their own OAuth client, so there is no shared server holding
anyone's tokens:

1. Create a project at <https://console.cloud.google.com/>.
2. Enable the **Gmail API**.
3. Configure the OAuth consent screen (External, testing mode is fine) and add
   your own address as a test user.
4. Create credentials → **OAuth client ID** → **Desktop app**, and download the
   JSON.

```bash
pip install 'sift-mail[gmail]'
```

```toml
[[accounts]]
name = "gmail"
provider = "gmail"
client_secret_file = "~/.local/share/sift/client_secret.json"
token_file = "~/.local/share/sift/gmail_token.json"
allow_modify = false                  # true only to let Sift write labels
# query = "newer_than:2y"             # optional Gmail search filter
```

The first `sift ingest --account gmail` opens a browser once. After that,
syncing is incremental via `historyId`. If the history cursor expires — Gmail
drops it after roughly a week of inactivity — re-run with `--reset`.

## Outlook, Hotmail and Microsoft 365

Basic authentication is retired for consumer accounts; IMAP works only with
XOAUTH2, which makes Microsoft Graph the simpler path. Publisher verification is
free.

The Graph connector is the one planned connector not yet implemented. Until it
lands, export to `.mbox` and use the file source, or connect via IMAP with
XOAUTH2 credentials you already hold. The interface it must implement is
`SourceConnector` in `sift/ingest/base.py`: `fetch(cursor)` and `cursor()`,
resuming from a `deltaLink`.

## Generic IMAP — Yahoo, GMX, Web.de, iCloud, university mail

Most providers now require an app password with two-factor authentication
enabled. Yahoo: Account Security → External connections → Create app password.

```toml
[[accounts]]
name = "uni"
provider = "imap"
host = "imap.example.edu"
port = 993
username = "you@example.edu"
folder = "INBOX"
```

Store the password outside the config:

```bash
sift accounts set-password uni          # OS keychain, needs sift-mail[keyring]
export SIFT_PASSWORD_UNI='…'            # or the environment
```

Incremental sync tracks `UIDVALIDITY` plus the highest UID seen. If the server
changes `UIDVALIDITY`, every stored UID is meaningless and the folder is
re-scanned — silently ignoring that is the most common way naive IMAP sync loses
mail.

## Writing labels back

The only thing Sift ever writes to a provider is a label. Never a delete, never
a move, never a send. It takes three deliberate steps:

```bash
sift labels                                   # plan only; writes nothing outward
sift labels --apply gmail                     # dry run, shows what would happen
sift labels --apply gmail --confirm           # actually applies
```

The account must also have `allow_modify = true`, which is what escalates the
Gmail scope to `gmail.modify`.

## Adding a provider

Implement `SourceConnector` and register it in `sift/ingest/__init__.py`:

```python
class MySource:
    name: str
    provider: str

    def fetch(self, cursor: dict | None = None, *, limit: int | None = None) -> Iterator[Message]:
        ...

    def cursor(self) -> dict:
        ...
```

Return canonical `Message` objects via `parse_message()` and keep every
provider quirk inside your connector.
