import base64
import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()

DEVICE_KEY_PREFIX = "sk_dev_"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _random_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def new_session_token() -> tuple[str, str]:
    """Return (plaintext, sha256 hex). Only the hash is stored."""
    token = _random_token()
    return token, sha256_hex(token)


def new_device_key() -> tuple[str, str]:
    """Return (plaintext, sha256 hex).

    SHA-256 rather than a password hash is correct here: the key is 32 bytes of
    full-entropy randomness, not a human-chosen secret, so it is not subject to
    dictionary attack and does not need a slow KDF.
    """
    key = DEVICE_KEY_PREFIX + _random_token()
    return key, sha256_hex(key)
