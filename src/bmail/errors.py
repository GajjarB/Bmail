"""Exception hierarchy. Every failure the CLI reports should be one of these."""


class BmailError(Exception):
    """Base class for all expected failures."""


class ConfigError(BmailError):
    """Configuration is missing or invalid."""


class StoreError(BmailError):
    """The SQLite store could not be opened or migrated."""


class IngestError(BmailError):
    """A source could not be read."""


class PackError(BmailError):
    """A domain pack is malformed."""


class RecipeError(BmailError):
    """A template recipe is malformed."""


class LLMError(BmailError):
    """The local model backend is unreachable or returned something unusable."""


class ExportError(BmailError):
    """An export target could not be written."""


class SignInRequired(IngestError):
    """An account needs its owner to sign in again.

    Raised instead of opening a sign-in window whenever a sync is running
    unattended. A browser tab appearing out of nowhere every few minutes is not
    a prompt anyone can act on; the UI shows this state and waits to be asked.
    """
