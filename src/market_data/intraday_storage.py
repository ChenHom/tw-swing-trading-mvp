"""Append-only, crash-aware raw market-data journal and offline archive operations.

Linux file locks coordinate independent writer and maintenance processes. All
writes and maintenance are local; no Shioaji login, trading DB or broker calls.
"""
from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


class ArchiveBusyError(RuntimeError):
    """The raw file is currently owned by a writer or maintenance process."""


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


@contextmanager
def raw_file_lock(path: Path) -> Iterator[None]:
    """Nonblocking cross-process lock, shared by append, gzip and retention."""
    guard = _lock_path(path)
    guard.parent.mkdir(parents=True, exist_ok=True)
    with guard.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ArchiveBusyError(f"raw file is in use: {path.name}") from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def append_raw(path: Path, record: Mapping[str, Any], *, stop_at_disk_pct: float) -> Path:
    """Durable single-event append. On partial-write crash replay rejects truncation."""
    if not 0 < stop_at_disk_pct < 100:
        raise ValueError("invalid disk watermark")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = (json.dumps(record, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    with raw_file_lock(path):
        if Path(str(path) + ".gz").exists():
            raise FileExistsError("archived raw file cannot be reopened for append")
        usage = shutil.disk_usage(path.parent)
        if (usage.used + len(blob)) * 100 >= usage.total * stop_at_disk_pct:
            raise OSError("collector disk watermark exceeded")
        # O_APPEND plus flock isolates writers even in separate processes.
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            offset = 0
            while offset < len(blob):
                written = os.write(fd, blob[offset:])
                if written <= 0:
                    raise OSError("raw append made no progress")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
    return path


def compress_raw(path: Path) -> dict[str, Any]:
    """Only compress a closed, parseable JSONL file with a matching gzip digest.

    Per-file cross-process lock ensures no active writer can append during
    compression; original remains untouched on validation/compression failure.
    """
    path = Path(path)
    if path.suffix != ".jsonl":
        raise ValueError("expected .jsonl file")
    target = Path(str(path) + ".gz")
    manifest = Path(str(target) + ".manifest.json")
    temp = Path(str(target) + ".tmp")
    with raw_file_lock(path):
        if target.exists() or manifest.exists():
            raise FileExistsError("archive or manifest already exists; manual recovery required")
        # A previous crash may leave a temp file; refuse to stomp over it.
        if temp.exists():
            raise FileExistsError("archive temporary file exists; inspect and recover")
        digest = hashlib.sha256()
        lines = 0
        try:
            with path.open("rb") as source, temp.open("xb") as dst:
                with gzip.GzipFile(fileobj=dst, mode="wb", filename="", mtime=0) as gz:
                    for line in source:
                        if not line.endswith(b"\n") or not line.strip():
                            raise ValueError("truncated or blank raw JSONL line")
                        item = json.loads(line)
                        if not isinstance(item, dict):
                            raise ValueError("non-object raw JSONL line")
                        digest.update(line)
                        lines += 1
                        gz.write(line)
                dst.flush()
                os.fsync(dst.fileno())
            verified = hashlib.sha256()
            with gzip.open(temp, "rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    verified.update(chunk)
            if verified.hexdigest() != digest.hexdigest():
                raise OSError("archive checksum mismatch; raw retained")
            # Publish gzip + manifest before ever unlinking original.
            os.replace(temp, target)
            info = {"schema_version": 1, "archive": str(target),
                    "raw_sha256": digest.hexdigest(), "lines": lines}
            staging = Path(str(manifest) + ".tmp")
            try:
                with staging.open("x", encoding="utf-8") as output:
                    json.dump(info, output, ensure_ascii=False, sort_keys=True)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(staging, manifest)
            finally:
                staging.unlink(missing_ok=True)
            # Manifest and archive exist; crash leaves both or raw for recovery.
            path.unlink()
            _fsync_dir(path.parent)
            return info
        finally:
            temp.unlink(missing_ok=True)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def archive_closed_sessions(
    root: Path, *, before_date: date,
    is_trading_day: Callable[[date], bool],
) -> dict[str, Any]:
    """Manually invoked, offline post-close archival; does NOT delete old gzip.

    Only files strictly before before_date are eligible. The caller must
    verify today/session shutdown separately; no automatic schedule installed.
    """
    root = Path(root)
    processed: list[dict[str, Any]] = []
    blocked: list[dict[str, str]] = []
    for stream in ("ticks", "bidask"):
        for path in sorted((root / "shioaji" / stream).glob("*/*/*.jsonl")):
            try:
                day = date.fromisoformat(path.parent.parent.name)
                if day >= before_date or not is_trading_day(day):
                    continue
                processed.append(compress_raw(path))
            except (ArchiveBusyError, FileExistsError, ValueError, OSError) as exc:
                blocked.append({"file": str(path), "error": type(exc).__name__})
    return {"archived": processed, "blocked": blocked,
            "archives_retained": True, "pruned": 0}


def retention_audit(
    root: Path, *, as_of: date, is_trading_day: Callable[[date], bool],
    minimum_sessions: int = 180,
) -> dict[str, Any]:
    """Report candidates ONLY, never delete researchers' retained market facts."""
    if minimum_sessions < 180:
        raise ValueError("market raw retention may not be shorter than 180 sessions")
    count = 0
    cursor = as_of
    while count < minimum_sessions:
        if is_trading_day(cursor):
            count += 1
        cursor = date.fromordinal(cursor.toordinal() - 1)
    earliest_kept = date.fromordinal(cursor.toordinal() + 1)
    eligible = []
    for stream in ("ticks", "bidask"):
        for path in sorted((Path(root) / "shioaji" / stream).glob("*/*/*.jsonl.gz")):
            try:
                if date.fromisoformat(path.parent.parent.name) < earliest_kept:
                    eligible.append(str(path))
            except ValueError:
                pass
    return {"minimum_trading_sessions": minimum_sessions,
            "earliest_retained_session": earliest_kept.isoformat(),
            "old_archives_review_only": eligible,
            "automatic_deletion": False, "deleted": 0}


def disk_status(root: Path, *, warn_pct: float = 80.0,
                stop_pct: float = 90.0) -> dict[str, Any]:
    if not 0 < warn_pct < stop_pct < 100:
        raise ValueError("invalid disk thresholds")
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(path)
    pct = disk.used * 100 / disk.total
    return {"used_pct": round(pct, 2), "warn": pct >= warn_pct,
            "stop": pct >= stop_pct, "free_bytes": disk.free}
