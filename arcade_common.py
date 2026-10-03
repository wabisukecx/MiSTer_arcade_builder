"""Shared paths, names, and hashes for arcade ROM processing."""
from __future__ import annotations
import hashlib
from pathlib import Path, PurePosixPath
import re

CHUNK = 1024 * 1024
MAX_MEMBER = 1024 * 1024 * 1024
# Limits for one source archive, so a hostile ZIP cannot fill the disk or exhaust memory.
MAX_ARCHIVE_TOTAL = 8 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 20000

class OperationCancelled(Exception):
    """Cooperative stop requested by the user."""


def check_cancel(cancel_requested=None):
    if cancel_requested is not None and cancel_requested():
        raise OperationCancelled("operation cancelled")


def digest_file(path: Path, algorithm="sha1", cancel_requested=None) -> str:
    check_cancel(cancel_requested)
    h = hashlib.new(algorithm)
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            check_cancel(cancel_requested)
            h.update(block)
    return h.hexdigest()

def safe_name(name: str) -> bool:
    name = name.replace("\\", "/")
    p = PurePosixPath(name)
    return bool(name and not name.startswith("/") and not re.match(r"^[A-Za-z]:", name)
                and all(x not in ("", ".", "..") for x in name.split("/")))

def canonical_zip(name: str) -> str:
    return name.replace("\\", "/").lstrip("/").lower()

def archive_matches(actual: str, desired: str) -> bool:
    a, d = canonical_zip(actual), canonical_zip(desired)
    return a == d or ("/" not in d and a.endswith("/" + d))

def normalize_crc(value: str | None) -> str | None:
    if not value or value.lower() == "none":
        return None
    if not re.fullmatch(r"[0-9a-fA-F]{1,8}", value):
        raise ValueError(f"invalid CRC: {value}")
    return value.lower().zfill(8)
