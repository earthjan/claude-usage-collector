"""One model-id normalisation shared by rows and cross-check totals."""
import re

from . import guard
from .hashing import hash_value

UNKNOWN_MODEL = "unknown"
CLAUDE_MODEL = re.compile(r"claude-[A-Za-z0-9._:-]*")


def model_slug(model, salt: bytes) -> str:
    """Keeps Claude model ids (minus provider prefixes and @versions); hashes the rest."""
    if not isinstance(model, str) or not model:
        return UNKNOWN_MODEL
    found = CLAUDE_MODEL.search(model)
    if found and guard.is_slug(found.group()[:64]):
        return found.group()[:64]
    return hash_value(salt, model)
