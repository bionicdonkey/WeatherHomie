"""AES-256-GCM encryption for secrets at rest.

Key = HKDF-SHA256(MASTER_KEY). Each ciphertext uses a random 96-bit nonce and binds the
row name as AAD, so a ciphertext can't be copied to another secret slot.
"""
from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class WrongMasterKey(Exception):
    pass


class SecretBox:
    def __init__(self, master_key: str):
        self._key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                         info=b"stormtracker/secrets/v1").derive(master_key.encode())
        self._session = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                             info=b"stormtracker/session/v1").derive(master_key.encode())

    @property
    def session_secret(self) -> str:
        return self._session.hex()

    def encrypt(self, name: str, plaintext: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + AESGCM(self._key).encrypt(nonce, plaintext.encode(), name.encode())

    def decrypt(self, name: str, blob: bytes) -> str:
        try:
            return AESGCM(self._key).decrypt(blob[:12], blob[12:], name.encode()).decode()
        except InvalidTag as e:
            raise WrongMasterKey(f"cannot decrypt secret {name!r}: wrong MASTER_KEY or corrupt data") from e
