"""Credential storage.

Order of preference: the OS keychain, then the environment. A password sitting
in a plaintext config file next to the mail database is exactly the failure this
project exists to avoid, so it is never read from there.
"""

from __future__ import annotations

import os

SERVICE = "bmail"


def _env_key(account: str) -> str:
    safe = "".join(char.upper() if char.isalnum() else "_" for char in account)
    return f"BMAIL_PASSWORD_{safe}"


def get_secret(account: str, username: str = "") -> str:
    """Return the password for an account, or "" if none is stored."""
    try:  # pragma: no cover - optional dependency
        import keyring

        stored = keyring.get_password(SERVICE, account or username)
        if stored:
            return stored
    except Exception:
        pass
    return os.environ.get(_env_key(account), "") or os.environ.get("BMAIL_PASSWORD", "")


def set_secret(account: str, password: str) -> str:
    """Store a password in the OS keychain. Returns where it went."""
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError(
            "keychain support needs the optional extra: pip install 'bmail[keyring]'.\n"
            f"Until then, export {_env_key(account)} in your shell instead."
        ) from exc
    keyring.set_password(SERVICE, account, password)
    backend = type(keyring.get_keyring()).__name__
    return f"stored in the OS keychain ({backend})"


def delete_secret(account: str) -> bool:
    try:  # pragma: no cover - optional dependency
        import keyring

        keyring.delete_password(SERVICE, account)
        return True
    except Exception:
        return False


def env_hint(account: str) -> str:
    return _env_key(account)
