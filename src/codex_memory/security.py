"""Conservative secret recognition and restrictive local file permissions."""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from pathlib import Path


_SECRET_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"-----BEGIN (?:(?:RSA |DSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----",
    r"\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    r"\b(?:xox[baprs]-[A-Za-z0-9-]{16,}|AIza[A-Za-z0-9_-]{30,})\b",
    r"\b(?:hf_[A-Za-z0-9]{20,}|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,})\b",
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
    r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}={0,2}\b",
    r"\b(?:password|passwd|pwd|api[_ -]?key|client[_ -]?secret|access[_ -]?token|refresh[_ -]?token|auth[_ -]?token|oauth[_ -]?token|secret[_ -]?key|aws_secret_access_key|recovery[_ -]?codes?)\b[\"']?\s*(?:=|:|\bis\b)\s*[\"']?[^\s\"',;}{]+",
    r"\b(?:Cookie|Set-Cookie)\s*:\s*[^\r\n]+",
    r"\b(?:sessionid|session_id|auth_cookie|authentication_cookie)\s*=\s*[^\s;]{4,}",
    r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@",
    r"\b(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp)://[^\s/@]+@",
))


def reject_secrets(value: str) -> None:
    """Reject recognizable credentials without including them in the error."""
    if any(pattern.search(unicodedata.normalize("NFKC", value)) for pattern in _SECRET_PATTERNS):
        raise ValueError("Recognizable sensitive credentials cannot be stored.")


def validate_text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string.")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds the supported length.")
    if "\x00" in value:
        raise ValueError(f"{field} contains an unsupported character.")
    reject_secrets(value)
    return value.strip()


def secure_directory(directory: Path) -> None:
    """Prepare a dedicated private data directory, without following symlinks."""
    directory = directory.absolute()
    if any(component.is_symlink() for component in (directory, *directory.parents)):
        raise ValueError("The memory data directory must not be a symbolic link.")
    if directory in (Path("/"), Path("/tmp"), Path("/var/tmp"), Path.home()):
        raise ValueError("The database needs a dedicated private data directory.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("The memory data directory must be owned by the current user.")
    directory.chmod(0o700)


def secure_file(path: Path) -> None:
    """Restrict an existing regular file owned by this user to mode 0600."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    # A concurrently unlinked SQLite sidecar can yield metadata for an inode
    # whose link count has already reached zero. It has no path to secure.
    if info.st_nlink == 0:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise ValueError("A memory data file must be a regular file owned by the current user.")
    try:
        path.chmod(0o600, follow_symlinks=False)
    except FileNotFoundError:
        # Another SQLite connection may close and unlink its WAL/SHM between
        # lstat and chmod. A missing file needs no permission repair.
        return


def secure_sqlite_files(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal", ".maintenance.lock"):
        secure_file(Path(str(path) + suffix))
