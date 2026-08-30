"""API-key generation and hashing. Plaintext keys are never persisted --
only `hash_key()`'s sha256 digest is stored (see `store.py`), so a leaked
database dump can't be replayed as a working key.
"""

from __future__ import annotations

import hashlib
import secrets

_PREFIX = "bk_live_"
_TOKEN_LENGTH = 32
_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _random_token(length: int = _TOKEN_LENGTH) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Returns `(plaintext, prefix, sha256_hex)`. `plaintext` must be shown to
    the caller exactly once, at creation, and never stored server-side;
    `prefix` is safe to keep and display in a list view so an operator can
    recognize a key without its secret remaining visible anywhere.
    """

    plaintext = f"{_PREFIX}{_random_token()}"
    prefix = plaintext[: len(_PREFIX) + 8]
    return plaintext, prefix, hash_key(plaintext)
