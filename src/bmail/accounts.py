"""Mail accounts added from the app.

Two rules shape this module. The password goes to the OS keychain and nowhere
else; the account's other settings go to `accounts.json` beside the config,
never into the config file a person wrote by hand. And nothing is saved until a
sign-in has actually worked, so a typo is caught while the form is still open
rather than as a sync that fails at three in the morning.

The presets exist so that most people type an address and a password and are
done. They cover providers that still accept an app password over IMAP. Outlook
and Hotmail do not — Microsoft turned password sign-in off — so those addresses
are recognised and pointed at the Microsoft sign-in instead of failing a login.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bmail import secrets
from bmail.config import APP_ACCOUNTS_FILE, Config, read_app_accounts
from bmail.errors import BmailError

SINCE_CHOICES = (90, 180, 365, 0)   # days; 0 reads everything
DEFAULT_SINCE = 180
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    host: str = ""
    port: int = 993
    domains: tuple[str, ...] = field(default_factory=tuple)
    kind: str = "imap"               # imap | microsoft
    password_label: str = "Password"
    help: str = ""
    help_url: str = ""


PRESETS: tuple[Preset, ...] = (
    Preset("gmail", "Gmail", "imap.gmail.com", domains=("gmail.com", "googlemail.com"),
           password_label="App password",
           help="Gmail needs an app password, not your normal one. Turn on 2-Step "
                "Verification, then create an app password and paste it here.",
           help_url="https://myaccount.google.com/apppasswords"),
    Preset("gmx", "GMX", "imap.gmx.net",
           domains=("gmx.de", "gmx.net", "gmx.at", "gmx.ch", "gmx.com"),
           help="Switch on IMAP first: in GMX, Settings → POP3 & IMAP."),
    Preset("webde", "WEB.DE", "imap.web.de", domains=("web.de",),
           help="Switch on IMAP first: in WEB.DE, Settings → POP3 & IMAP."),
    Preset("yahoo", "Yahoo Mail", "imap.mail.yahoo.com",
           domains=("yahoo.com", "yahoo.de", "yahoo.co.uk", "ymail.com"),
           password_label="App password",
           help="Yahoo needs an app password: Account security → Generate app password.",
           help_url="https://login.yahoo.com/account/security"),
    Preset("icloud", "iCloud Mail", "imap.mail.me.com", domains=("icloud.com", "me.com", "mac.com"),
           password_label="App-specific password",
           help="iCloud needs an app-specific password from your Apple Account page.",
           help_url="https://account.apple.com"),
    Preset("tonline", "T-Online", "secureimap.t-online.de", domains=("t-online.de",),
           help="Use the e-mail password (Passwort für E-Mail-Programme), not your login."),
    Preset("posteo", "Posteo", "posteo.de", domains=("posteo.de", "posteo.net")),
    Preset("mailbox", "mailbox.org", "imap.mailbox.org", domains=("mailbox.org",)),
    Preset("microsoft", "Outlook", kind="microsoft",
           domains=("outlook.com", "outlook.de", "hotmail.com", "hotmail.de", "live.com",
                    "live.de", "msn.com"),
           help="Microsoft no longer lets apps sign in with a password. Outlook "
                "and Hotmail connect through Microsoft sign-in, which currently "
                "needs a one-time app registration.",
           help_url="https://github.com/GajjarB/Bmail/blob/main/docs/PROVIDERS.md"
                    "#outlook-hotmail-and-microsoft-365"),
)


def detect(address: str) -> Preset:
    """The preset for an address, or a best guess at its IMAP server."""
    domain = address.rsplit("@", 1)[-1].strip().lower()
    for preset in PRESETS:
        if domain in preset.domains:
            return preset
    return Preset("custom", domain or "Other", host=f"imap.{domain}" if domain else "")


def presets() -> list[dict[str, Any]]:
    return [asdict(preset) for preset in PRESETS]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def build_spec(fields: dict[str, Any]) -> dict[str, Any]:
    """An account entry from what the form sent, or a BmailError saying what is wrong."""
    address = str(fields.get("address") or "").strip().lower()
    if not _ADDRESS.match(address):
        raise BmailError("that does not look like an e-mail address")
    preset = detect(address)
    if preset.kind == "microsoft":
        raise BmailError(preset.help)
    host = str(fields.get("host") or preset.host).strip().lower()
    if not host or any(char.isspace() for char in host):
        raise BmailError("which server? Give the IMAP host, such as imap.example.com")
    try:
        port = int(fields.get("port") or preset.port)
    except (TypeError, ValueError) as exc:
        raise BmailError("the port must be a number, usually 993") from exc
    if not 0 < port < 65536:
        raise BmailError("the port must be between 1 and 65535, usually 993")
    try:
        since_days = int(fields.get("since_days", DEFAULT_SINCE))
    except (TypeError, ValueError) as exc:
        raise BmailError("how far back must be a number of days") from exc
    if since_days not in SINCE_CHOICES:
        raise BmailError("how far back must be 3 months, 6 months, a year, or everything")
    spec: dict[str, Any] = {
        "name": address,
        "provider": "imap",
        "address": address,
        "host": host,
        "port": port,
        "username": str(fields.get("username") or address).strip(),
        "folder": str(fields.get("folder") or "INBOX").strip() or "INBOX",
        "since_days": since_days,
    }
    if fields.get("ssl") is False:
        spec["ssl"] = False        # the connector allows this for loopback only
    return spec


def _password(fields: dict[str, Any]) -> str:
    password = str(fields.get("password") or "")
    if not password:
        raise BmailError("the password is empty")
    return password


def probe(spec: dict[str, Any], password: str) -> int:
    """Sign in and count the folder's messages. Nothing is saved or read."""
    from bmail.ingest.imap import ImapSource

    source = ImapSource(
        spec["host"], spec["username"], password, name=spec["name"], port=spec["port"],
        folder=spec["folder"], use_ssl=spec.get("ssl", True) is not False, timeout=20.0,
    )
    try:
        return source.probe()
    except BmailError as exc:
        raise BmailError(explain(str(exc), spec)) from exc


def explain(error: str, spec: dict[str, Any]) -> str:
    """Turn what imaplib and the socket layer say into what a person can act on."""
    text = error.lower()
    preset = detect(spec.get("address", ""))
    if "authenticationfailed" in text or "invalid credentials" in text or "login failed" in text \
            or "authentication failed" in text:
        advice = f" {preset.help}" if preset.password_label != "Password" and preset.help else ""
        return f"{preset.label if preset.key != 'custom' else 'The server'} refused that " \
               f"password.{advice}"
    if "name or service not known" in text or "nodename nor servname" in text \
            or "getaddrinfo" in text or "no address associated" in text:
        return f"No server called {spec['host']} could be found. Check the server name."
    if "timed out" in text:
        return f"{spec['host']} did not answer. Check the server name and your connection."
    if "connection refused" in text:
        return f"{spec['host']} refused a connection on port {spec['port']}. IMAP is usually 993."
    if "certificate" in text or "ssl" in text:
        return f"The secure connection to {spec['host']} failed: {error}"
    if "no folder" in text:
        return error[0].upper() + error[1:]
    return error


def check(fields: dict[str, Any]) -> dict[str, Any]:
    spec = build_spec(fields)
    count = probe(spec, _password(fields))
    return {"ok": True, "messages": count, "folder": spec["folder"], "host": spec["host"]}


# --------------------------------------------------------------------------- #
# Saving
# --------------------------------------------------------------------------- #


def add(config: Config, fields: dict[str, Any]) -> dict[str, Any]:
    """Test, then keep: the password in the keychain, the rest in accounts.json."""
    spec = build_spec(fields)
    if any(str(entry.get("name")) == spec["name"] for entry in config.accounts):
        raise BmailError(f"{spec['address']} is already connected")
    password = _password(fields)
    keychain = secrets.keychain_status()
    if not keychain["available"]:
        raise BmailError(f"there is nowhere safe to keep the password: {keychain['hint']}")
    count = probe(spec, password)
    try:
        secrets.set_secret(spec["name"], password)
    except Exception as exc:
        raise BmailError(f"the password could not be saved to the keychain: {exc}") from exc
    stored = [{k: v for k, v in entry.items() if k != "added_by"}
              for entry in read_app_accounts(config.home)]
    _write(config.home, [*stored, spec])
    reload(config)
    return {"account": public(spec | {"added_by": "app"}), "messages": count}


def remove(config: Config, name: str) -> None:
    """Forget an account added in the app. Its mail stays in the store."""
    stored = read_app_accounts(config.home)
    if not any(str(entry.get("name")) == name for entry in stored):
        if any(str(entry.get("name")) == name for entry in config.accounts):
            raise BmailError(f"{name} is set up in config.toml; remove it there")
        raise BmailError(f"no account called {name}")
    _write(config.home, [{k: v for k, v in entry.items() if k != "added_by"}
                         for entry in stored if str(entry.get("name")) != name])
    secrets.delete_secret(name)
    reload(config)


def reload(config: Config) -> None:
    """Re-read the app's accounts into a running config, keeping the file's."""
    written = [entry for entry in config.accounts if entry.get("added_by") != "app"]
    names = {str(entry.get("name")) for entry in written}
    config.accounts = written + [entry for entry in read_app_accounts(config.home)
                                 if str(entry.get("name")) not in names]


def public(entry: dict[str, Any]) -> dict[str, Any]:
    """An account as the page may see it: never a password, even a stray one."""
    shown = {k: v for k, v in entry.items() if k != "password"}
    shown["removable"] = entry.get("added_by") == "app"
    shown["label"] = detect(str(entry.get("address") or entry.get("username") or "")).label \
        if entry.get("provider") == "imap" else str(entry.get("provider", ""))
    return shown


def _write(home: Path, entries: list[dict[str, Any]]) -> None:
    """Replace accounts.json in one step, readable by its owner only."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"accounts": entries}, indent=2, ensure_ascii=False) + "\n"
    handle, temporary = tempfile.mkstemp(dir=home, prefix=".accounts-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(payload)
        try:
            os.chmod(temporary, 0o600)
        except OSError:  # pragma: no cover - some filesystems refuse
            pass
        os.replace(temporary, home / APP_ACCOUNTS_FILE)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
