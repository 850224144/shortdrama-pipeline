"""P0 source identities and atomic JSON snapshots (no legacy cache reuse)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile


def json_hash(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def file_hash(path: str | Path) -> str:
    """Hash the entire file; reject observable mutation while reading."""
    path = Path(path)
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if identity(before) != identity(after):
        raise ValueError("SOURCE_CHANGED: 文件在读取期间发生变化")
    return digest.hexdigest()


def contained_path(root: str | Path, path: str | Path) -> Path:
    """Resolve a user selection and forbid paths escaping its selected root."""
    root = Path(root).resolve(strict=True)
    value = Path(path)
    target = (value if value.is_absolute() else root / value).resolve(strict=True)
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError("PATH_OUTSIDE_ROOT: 文件不在已选择的素材目录内")
    return target


def atomic_json(path: str | Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
