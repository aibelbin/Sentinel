"""Keeps device keys out of anything stored or sent to the model."""

import re

_KEY = re.compile(r"\bsk_[A-Za-z]+_[A-Za-z0-9_\-]{8,}")


def mask_key(key: str) -> str:
    return f"{key[:7]}…{key[-4:]}" if len(key) > 11 else "…"


def redact(text: str) -> str:
    return _KEY.sub(lambda match: mask_key(match.group(0)), text)
