import hashlib
import hmac
import os

HASH_LENGTH = 12
SALT_BYTES = 32


class SaltError(Exception):
    pass


def hash_value(salt: bytes, value: str) -> str:
    digest = hmac.new(salt, value.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:HASH_LENGTH]


def create_salt(path: str) -> bytes:
    if os.path.exists(path):
        return load_salt(path)
    salt = os.urandom(SALT_BYTES)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(salt.hex())
    return salt


def load_salt(path: str) -> bytes:
    try:
        with open(path) as handle:
            salt = bytes.fromhex(handle.read().strip())
    except (OSError, ValueError) as error:
        raise SaltError("salt file missing or unreadable, run init") from error
    if len(salt) != SALT_BYTES:
        raise SaltError("salt file has the wrong length, run init")
    return salt
