"""Password hashing and at-rest encryption for the multi-user platform.

Two separate primitives for two different threats: bcrypt for passwords
(one-way — we never need the plaintext back, only "does this match"),
Fernet for OANDA API tokens (reversible — the system needs the real token
back to actually call the broker on the user's behalf, so it's encrypted,
not hashed). The Fernet key lives only in `.env` (AUTH_ENCRYPTION_KEY),
never in the database — the whole point of the split is that a stolen
`data/forex.db` file alone isn't enough to recover anyone's broker token.
"""
from __future__ import annotations

import bcrypt
from cryptography.fernet import Fernet

from src.config import auth_encryption_key


def hash_password(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()


def verify_password(plaintext: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plaintext.encode(), password_hash.encode())
    except ValueError:
        # A malformed/legacy hash should fail closed, not raise into a caller
        # that might treat an exception as "try another auth path."
        return False


def _fernet() -> Fernet:
    return Fernet(auth_encryption_key())


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()
