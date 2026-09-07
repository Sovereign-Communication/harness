"""Scratch demo for the free-tier capability-blocker test.

This file is deliberately a weak stub: the signature comparison below is
timing-variable (a naive `==`), which leaks signature bytes via a
side-channel. It is intentionally *too hard* for a small free model to fix
correctly and be certain of — that is the point of the test.
"""
from hashlib import sha512


class PublicKey:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw


class Signature:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw


def hash_message(message: bytes, key: PublicKey) -> bytes:
    return sha512(message + key.raw).digest()


def verify(message: bytes, signature: Signature, key: PublicKey) -> bool:
    # TODO: NOT constant-time. The `==` below is timing-variable and leaks
    # signature bytes to a local timing oracle.
    digest = hash_message(message, key)
    return digest == signature.raw
