# Connecting mail accounts

Start with a file. It needs no credentials, no OAuth screen and no rate limits,
and it is the fastest way to learn what your own mail actually looks like.

## Local files — start here

Gmail Takeout, a Thunderbird export or any `.mbox` works, as do Maildir folders
and directories of `.eml` files.

```bash
bmail ingest --path ~/Downloads/takeout/Mail/All\ mail.mbox --account archive
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

Bmail asks for `gmail.readonly`. It never requests `https://mail.google.com/`.
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
pip install 'bmail[gmail]'
```

```toml
[[accounts]]
name = "gmail"
provider = "gmail"
client_secret_file = "~/.local/share/bmail/client_secret.json"
token_file = "~/.local/share/bmail/gmail_token.json"
allow_modify = false                  # true only to let Bmail write labels
# query = "newer_than:2y"             # optional Gmail search filter
```

The first `bmail ingest --account gmail` opens a browser once. After that,
syncing is incremental via `historyId`. If the history cursor expires — Gmail
drops it after roughly a week of inactivity — re-run with `--reset`.

## Outlook, Hotmail and Microsoft 365

Basic authentication is retired for these mailboxes, and IMAP works only with
XOAUTH2, so Microsoft Graph is simply the easier path. Publisher verification is
free, and because no mail content leaves your machine this stays a client-side
integration — you register your own app, and no shared server holds anyone's
tokens.

```bash
pip install 'bmail[graph]'
```

### Register a desktop app

1. Go to <https://entra.microsoft.com/> → **App registrations** → **New
   registration**.
2. Supported account types: **Accounts in any organizational directory and
   personal Microsoft accounts** for a mix of work and Outlook.com mailboxes;
   personal-only is fine if that is all you have.
3. Under **Authentication**, add a platform → **Mobile and desktop
   applications**, and tick the `http://localhost` redirect URI. If you plan to
   sign in on a machine with no browser, also set **Allow public client flows**
   to *Yes*.
4. Under **API permissions**, add the delegated Microsoft Graph permission
   `Mail.Read` — and `Mail.ReadWrite` only if you want Bmail to write categories
   back.
5. Copy the **Application (client) ID**.

No client secret. A desktop app cannot keep one, so Bmail uses the public-client
flow with PKCE.

```toml
[[accounts]]
name = "outlook"
provider = "graph"
client_id = "00000000-0000-0000-0000-000000000000"
tenant = "common"                  # consumers | organizations | common | <tenant id>
token_cache_file = "~/.local/share/bmail/outlook_graph.json"
folder = "inbox"                   # "" reads the whole mailbox
auth_mode = "interactive"          # "device" on a machine with no browser
allow_modify = false               # true only to let Bmail write categories
```

Use `tenant = "consumers"` for a personal Outlook.com or Hotmail account,
`"organizations"` for work or school only, and `"common"` for both.

```bash
bmail ingest --account outlook
```

The first run opens a browser once. With `auth_mode = "device"` it prints a code
and a URL to open on another device instead — the right choice over SSH. Tokens
land in `token_cache_file`, written `0600`.

### How syncing works

Bmail reads `/me/mailFolders/{folder}/messages/delta` for change tracking, then
fetches each message's original MIME via `$value` and hands it to the same
normaliser that parses an mbox. Outlook mail therefore goes through exactly the
same parsing as every other source, so provider quirks cannot leak downstream.
Graph's `conversationId` is used as the thread id, which is more reliable than
reconstructing the chain from headers.

A few behaviours worth knowing:

- **Deletions are ignored.** Delta reports removed items; Bmail skips them. It
  never deletes anything from its own store either.
- **`--limit` never advances the cursor.** A truncated run keeps the previous
  delta link, because storing the new one would silently skip everything that
  was not read.
- **Expired cursors self-heal.** Graph drops sync state after a long gap; Bmail
  falls back to a full read of the folder rather than failing the run.
- **Throttling is obeyed.** Graph answers 429 with a `Retry-After` header, and
  Bmail waits exactly that long. Ignoring it is how a backfill gets an account
  temporarily blocked.
- **A JSON fallback covers the rest.** Graph will not serve MIME for every item
  it lists; those are rebuilt from the JSON projection instead of being dropped.

### Writing categories back

Graph has no labels. Outlook **categories** are the closest equivalent and are
purely additive, so that is what Bmail writes — and only with
`allow_modify = true`, which is what escalates the scope to `Mail.ReadWrite`.

```bash
bmail labels                                   # plan only
bmail labels --apply outlook --confirm         # add categories
```

Commas are stripped from category names, because Outlook stores categories as a
comma-delimited list and a comma inside a name would split it in two. A new
category appears without a colour until you assign one in Outlook; Bmail does not
request the extra scope needed to manage the master category list.

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
bmail accounts set-password uni          # OS keychain, needs bmail[keyring]
export BMAIL_PASSWORD_UNI='…'            # or the environment
```

Incremental sync tracks `UIDVALIDITY` plus the highest UID seen. If the server
changes `UIDVALIDITY`, every stored UID is meaningless and the folder is
re-scanned — silently ignoring that is the most common way naive IMAP sync loses
mail.

A bridge on this machine — Proton Mail Bridge, DavMail — can be reached without
TLS by adding `ssl = false`. That is refused for any host other than `127.0.0.1`
or `localhost`, because plain IMAP sends the password in the clear.

## Staying up to date

While Bmail is open it checks every live account on its own — every 5 minutes by
default; change it or turn it off under **Setup → Check automatically**. The
setting is kept in the database and survives a restart. Files are never polled.

IMAP accounts whose server supports IDLE (almost all do) are **live**: Bmail
keeps one connection per account parked in IDLE, the server says the moment mail
lands, and it is fetched and sorted within seconds. The connection is renewed
every 20 minutes, well inside the 30 the RFC allows, and mail that arrives while
it reconnects is still noticed. Gmail and Outlook are checked on the interval.

Nothing is checked while Bmail is closed, and a background check never opens a
sign-in window. If a Gmail or Outlook token can no longer be refreshed silently,
the account is marked **needs you to sign in** in the sidebar; press **Check for
new mail** to do it.

## Writing labels back

The only thing Bmail ever writes to a provider is a label. Never a delete, never
a move, never a send. It takes three deliberate steps:

```bash
bmail labels                                   # plan only; writes nothing outward
bmail labels --apply gmail                     # dry run, shows what would happen
bmail labels --apply gmail --confirm           # actually applies
```

The account must also have `allow_modify = true`, which is what escalates the
Gmail scope to `gmail.modify`.

## Adding a provider

Implement `SourceConnector` and register it in `bmail/ingest/__init__.py`:

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
