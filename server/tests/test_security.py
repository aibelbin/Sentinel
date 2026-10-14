"""Credential handling. Pure functions, no database."""

from app.security import (
    DEVICE_KEY_PREFIX,
    hash_password,
    new_device_key,
    new_session_token,
    sha256_hex,
    verify_password,
)


def test_password_round_trip():
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)


def test_wrong_password_is_rejected():
    assert not verify_password("wrong", hash_password("right"))


def test_password_hash_is_salted():
    """Two hashes of the same password must differ, or the hash leaks equality."""
    assert hash_password("same") != hash_password("same")


def test_malformed_hash_is_rejected_rather_than_raising():
    assert not verify_password("anything", "not-a-valid-argon2-hash")


def test_device_key_is_prefixed_and_high_entropy():
    plaintext, _ = new_device_key()
    assert plaintext.startswith(DEVICE_KEY_PREFIX)
    # 32 random bytes in base64url, padding stripped.
    assert len(plaintext) - len(DEVICE_KEY_PREFIX) == 43


def test_device_keys_are_unique():
    keys = {new_device_key()[0] for _ in range(50)}
    assert len(keys) == 50


def test_stored_device_key_is_the_hash_not_the_key():
    plaintext, stored = new_device_key()
    assert stored == sha256_hex(plaintext)
    assert plaintext not in stored
    assert len(stored) == 64


def test_session_token_hash_matches():
    token, stored = new_session_token()
    assert stored == sha256_hex(token)
