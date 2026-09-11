"""SimHash over shingled tokens, for near-duplicate collapse.

Forty "we have received your application" mails differ only in a name and a
reference number. SimHash + a small Hamming radius folds them into one row
without an embedding model.
"""

from __future__ import annotations

import hashlib
import re

_TOKEN = re.compile(r"[a-zA-ZäöüßÄÖÜ]{2,}|\d+")
_DIGITS = re.compile(r"\d+")


def _shingles(text: str, width: int = 3) -> list[str]:
    # Numbers are the variable part of a template; blank them so the boilerplate
    # dominates the fingerprint.
    tokens = _TOKEN.findall(_DIGITS.sub("0", text.lower()))
    if len(tokens) < width:
        return tokens
    return [" ".join(tokens[i : i + width]) for i in range(len(tokens) - width + 1)]


# 63 rather than 64 bits: the value is stored in a SQLite INTEGER column, which
# is signed 64-bit, and an unsigned 64-bit fingerprint overflows it.
DEFAULT_BITS = 63


def simhash(text: str, bits: int = DEFAULT_BITS, width: int = 3) -> int:
    """Return a `bits`-wide SimHash of `text` (0 for empty input)."""
    shingles = _shingles(text, width)
    if not shingles:
        return 0
    vector = [0] * bits
    for shingle in shingles:
        digest = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(), "big")
        for bit in range(bits):
            vector[bit] += 1 if digest >> bit & 1 else -1
    value = 0
    for bit in range(bits):
        if vector[bit] > 0:
            value |= 1 << bit
    return value


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def template_fingerprint(text: str) -> str:
    """Stable hex fingerprint of a message's non-variable text."""
    return f"{simhash(text):016x}"
