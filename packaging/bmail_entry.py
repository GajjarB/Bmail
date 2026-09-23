"""Entry point for the frozen engine. PyInstaller needs a script, not a module."""

import sys

from bmail.cli import main

if __name__ == "__main__":
    sys.exit(main())
