"""Exception hierarchy. Every failure the CLI reports should be one of these."""


class SiftError(Exception):
    """Base class for all expected failures."""


class ConfigError(SiftError):
    """Configuration is missing or invalid."""


class StoreError(SiftError):
    """The SQLite store could not be opened or migrated."""


class IngestError(SiftError):
    """A source could not be read."""


class PackError(SiftError):
    """A domain pack is malformed."""


class RecipeError(SiftError):
    """A template recipe is malformed."""


class LLMError(SiftError):
    """The local model backend is unreachable or returned something unusable."""


class ExportError(SiftError):
    """An export target could not be written."""
