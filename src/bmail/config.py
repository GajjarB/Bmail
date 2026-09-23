"""Configuration and on-disk layout.

Config lives in a TOML file; secrets never do. Passwords and OAuth tokens belong
in the OS keychain (macOS Keychain, Windows DPAPI, libsecret) or in the
environment, and `bmail doctor` will tell you if you have put one in the file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bmail.errors import ConfigError

try:  # `tomllib` is standard library from Python 3.11; `tomli` is the backport.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only on Python 3.10
    import tomli as tomllib

APP_NAME = "bmail"
ENV_HOME = "BMAIL_HOME"


def default_home() -> Path:
    """XDG on Linux, Application Support on macOS, AppData on Windows."""
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":  # pragma: no cover - platform specific
        base = os.environ.get("APPDATA") or "~/AppData/Roaming"
        return Path(base).expanduser() / "Bmail"
    if os.uname().sysname == "Darwin":  # pragma: no cover - platform specific
        return Path("~/Library/Application Support/Bmail").expanduser()
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / APP_NAME


@dataclass(slots=True)
class LLMConfig:
    enabled: bool = True
    host: str = "http://127.0.0.1:11434"
    model: str = ""             # empty = auto-detect from hardware
    num_ctx: int = 4096
    timeout: float = 180.0
    allow_remote: bool = False  # sending mail off-machine must be deliberate
    max_calls_per_run: int = 500
    only_when_plugged_in: bool = False


@dataclass(slots=True)
class ClassifierConfig:
    vectoriser: str = "hashing"          # hashing | sentence-transformers | ollama
    embedding_model: str = ""
    accept_threshold: float = 0.72
    distil_after_backfill: bool = True


@dataclass(slots=True)
class Config:
    home: Path = field(default_factory=default_home)
    db_path: Path | None = None
    pack: str = "job-seeker"
    pack_dirs: list[Path] = field(default_factory=list)
    recipe_dir: str | None = None
    accounts: list[dict[str, Any]] = field(default_factory=list)
    llm: LLMConfig = field(default_factory=LLMConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    owner_addresses: list[str] = field(default_factory=list)
    timezone: str = "UTC"

    # -- paths ---------------------------------------------------------------- #

    @property
    def database(self) -> Path:
        return self.db_path or (self.home / "bmail.sqlite3")

    @property
    def model_path(self) -> Path:
        return self.home / "classifier.json"

    @property
    def config_path(self) -> Path:
        return self.home / "config.toml"

    def ensure_home(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            self.home.chmod(0o700)  # the store holds mail; keep it to the owner
        except OSError:  # pragma: no cover - some filesystems refuse
            pass
        return self.home

    # -- loading ---------------------------------------------------------------- #

    @classmethod
    def load(cls, path: str | Path | None = None, *, home: str | Path | None = None) -> Config:
        """Read the config. `home` is an explicit data directory (`--home`): its
        config.toml is the one read, and it outranks a `home` set inside it."""
        explicit = Path(home).expanduser() if home else None
        base = explicit or default_home()
        config_path = Path(path).expanduser() if path else base / "config.toml"
        config = cls(home=base)
        if not config_path.exists():
            config.accounts = read_app_accounts(config.home)
            return config
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read {config_path}: {exc}") from exc

        if (home_value := data.get("home")) and explicit is None:
            config.home = Path(str(home_value)).expanduser()
        if db_value := data.get("database"):
            config.db_path = Path(str(db_value)).expanduser()
        config.pack = str(data.get("pack", config.pack))
        config.timezone = str(data.get("timezone", config.timezone))
        config.owner_addresses = [str(a).lower() for a in data.get("owner_addresses", [])]
        config.pack_dirs = [Path(str(p)).expanduser() for p in data.get("pack_dirs", [])]
        config.recipe_dir = data.get("recipe_dir")
        config.accounts = list(data.get("accounts", []))

        llm = data.get("llm", {})
        config.llm = LLMConfig(
            enabled=bool(llm.get("enabled", True)),
            host=str(llm.get("host", config.llm.host)),
            model=str(llm.get("model", "")),
            num_ctx=int(llm.get("num_ctx", 4096)),
            timeout=float(llm.get("timeout", 180.0)),
            allow_remote=bool(llm.get("allow_remote", False)),
            max_calls_per_run=int(llm.get("max_calls_per_run", 500)),
            only_when_plugged_in=bool(llm.get("only_when_plugged_in", False)),
        )
        classifier = data.get("classifier", {})
        config.classifier = ClassifierConfig(
            vectoriser=str(classifier.get("vectoriser", "hashing")),
            embedding_model=str(classifier.get("embedding_model", "")),
            accept_threshold=float(classifier.get("accept_threshold", 0.72)),
            distil_after_backfill=bool(classifier.get("distil_after_backfill", True)),
        )
        # Accounts added in the app live beside the config, never in it: the
        # file you wrote by hand is not rewritten behind your back. On a name
        # clash the hand-written one wins.
        written = {str(a.get("name")) for a in config.accounts}
        config.accounts += [a for a in read_app_accounts(config.home)
                            if str(a.get("name")) not in written]
        return config

    def account(self, name: str) -> dict[str, Any]:
        for entry in self.accounts:
            if entry.get("name") == name:
                return dict(entry)
        raise ConfigError(f"no account named '{name}' in {self.config_path}")

    def resolved_account(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Fill in a password from the environment or the keychain, never the file."""
        entry = dict(entry)
        if entry.get("provider") == "imap" and not entry.get("password"):
            from bmail.secrets import get_secret

            entry["password"] = get_secret(str(entry.get("name", "")), str(entry.get("username", "")))
        return entry


TEMPLATE = """\
# Bmail configuration. Secrets do not belong in this file — see docs/PROVIDERS.md.

pack = "job-seeker"
timezone = "Europe/Berlin"
owner_addresses = ["you@example.com"]

[llm]
enabled = true
host = "http://127.0.0.1:11434"
# model = "qwen3:8b"     # leave unset to pick from available RAM
num_ctx = 4096
allow_remote = false

[classifier]
vectoriser = "hashing"    # hashing | sentence-transformers | ollama
accept_threshold = 0.72

# A local mbox or Maildir — the easiest way to start, and it needs no credentials.
[[accounts]]
name = "archive"
provider = "file"
path = "~/mail/export.mbox"

# [[accounts]]
# name = "gmail"
# provider = "gmail"
# client_secret_file = "~/.local/share/bmail/client_secret.json"
# token_file = "~/.local/share/bmail/gmail_token.json"
# allow_modify = false          # true only if you want Bmail to write labels

# [[accounts]]
# name = "outlook"
# provider = "graph"                 # Outlook.com, Hotmail, Live, Microsoft 365
# client_id = "00000000-0000-0000-0000-000000000000"
# tenant = "common"                  # consumers | organizations | common | <tenant id>
# token_cache_file = "~/.local/share/bmail/outlook_graph.json"
# folder = "inbox"                   # "" for the whole mailbox
# auth_mode = "interactive"          # "device" on a machine with no browser
# allow_modify = false               # true only to let Bmail write categories

# [[accounts]]
# name = "uni"
# provider = "imap"
# host = "imap.example.edu"
# username = "you@example.edu"
# folder = "INBOX"
# password comes from the keychain: bmail accounts set-password uni
"""


def write_template(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(TEMPLATE, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover
            pass
    return path


APP_ACCOUNTS_FILE = "accounts.json"


def read_app_accounts(home: Path) -> list[dict[str, Any]]:
    """Accounts added from the app. Never holds a password: those are in the
    OS keychain. A damaged file costs the list, not the start-up."""
    path = Path(home) / APP_ACCOUNTS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("accounts", []) if isinstance(data, dict) else []
    return [dict(entry, added_by="app") for entry in entries if isinstance(entry, dict)]
