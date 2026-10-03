"""Content-addressed blob storage for the ROM pool.

A blob is addressed by its SHA-1.  It lives either as a loose file
(``pool/<sha1[:2]>/<sha1>``, the original layout) or as a member named
``<sha1>`` inside a pack ZIP (``pool/packs/<name>.zip``).  The ``blob_packs``
table maps a SHA-1 to its pack path (relative to ``pool``); a blob without a
row there is loose.  All reads go through :class:`BlobStore` so callers never
need to know the layout.  Every pack also carries a ``manifest.json`` listing
its blobs and their origins, so the database can be rebuilt from the packs.
"""
from __future__ import annotations
from collections import OrderedDict
import json
import os
from pathlib import Path
import sqlite3
import uuid
import zipfile
from arcade_common import CHUNK, check_cancel

MANIFEST_NAME = "manifest.json"
PACK_DIR = "packs"
PACK_DATE = (1980, 1, 1, 0, 0, 0)


class PackWriteError(OSError):
    """The pack being written could not be extended; the whole archive is abandoned."""


class BlobStore:
    """Read access to pool content, whether loose or packed."""

    def __init__(self, db, work: Path, max_open_packs: int = 16):
        self.db = db
        self.work = Path(work)
        self.pool = self.work / "pool"
        self.max_open_packs = max_open_packs
        self._packs: OrderedDict[str, zipfile.ZipFile] = OrderedDict()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        while self._packs:
            _, archive = self._packs.popitem()
            try:
                archive.close()
            except OSError:
                pass

    def loose_path(self, sha1: str) -> Path:
        return self.pool / sha1[:2] / sha1

    def pack_of(self, sha1: str):
        """Return the pack path (relative to pool) holding the blob, or None if loose."""
        if self.db is None:
            return None
        try:
            row = self.db.execute("SELECT pack FROM blob_packs WHERE sha1=?", (sha1,)).fetchone()
        except sqlite3.OperationalError:  # database from before packs existed
            return None
        return row[0] if row else None

    def _pack_zip(self, pack: str) -> zipfile.ZipFile:
        archive = self._packs.get(pack)
        if archive is not None:
            self._packs.move_to_end(pack)
            return archive
        archive = zipfile.ZipFile(self.pool / pack)
        self._packs[pack] = archive
        while len(self._packs) > self.max_open_packs:
            _, old = self._packs.popitem(last=False)
            old.close()
        return archive

    def open(self, sha1: str):
        """Open a blob for binary reading (seekable; usable as a context manager)."""
        pack = self.pack_of(sha1)
        if pack:
            return self._pack_zip(pack).open(sha1)
        return self.loose_path(sha1).open("rb")

    def has(self, sha1: str) -> bool:
        """Cheap presence check: the pack file exists or a loose file does (no pack read)."""
        pack = self.pack_of(sha1)
        if pack:
            return (self.pool / pack).is_file()
        return self.loose_path(sha1).is_file()

    def exists(self, sha1: str) -> bool:
        pack = self.pack_of(sha1)
        if pack:
            try:
                self._pack_zip(pack).getinfo(sha1)
                return True
            except (KeyError, OSError, zipfile.BadZipFile):
                return False
        return self.loose_path(sha1).is_file()


class PackWriter:
    """Write one pack ZIP atomically: members go to a ``.part`` file, ``finish`` renames it."""

    def __init__(self, pool: Path):
        self.pool = Path(pool)
        (self.pool / PACK_DIR).mkdir(parents=True, exist_ok=True)
        self.name = f"{PACK_DIR}/{uuid.uuid4().hex}.zip"
        self.final = self.pool / self.name
        self.temp = self.final.with_name(self.final.name + ".part")
        self.blobs: list[dict] = []
        self.finished = False
        self._zip = zipfile.ZipFile(self.temp, "w", compression=zipfile.ZIP_DEFLATED)

    def add(self, sha1: str, source: Path, size: int, crc32: str, cancel_requested=None):
        info = zipfile.ZipInfo(sha1, date_time=PACK_DATE)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16
        try:
            with Path(source).open("rb") as src, self._zip.open(info, "w") as dst:
                while block := src.read(CHUNK):
                    check_cancel(cancel_requested)
                    dst.write(block)
        except OSError as exc:
            raise PackWriteError(f"cannot write pack member {sha1}: {exc}") from exc
        self.blobs.append({"sha1": sha1, "size": size, "crc32": crc32})

    def finish(self, origins: list[dict]) -> str:
        """Write the manifest, flush to disk and publish the pack under its final name."""
        manifest = {"version": 1, "blobs": self.blobs, "origins": origins}
        info = zipfile.ZipInfo(MANIFEST_NAME, date_time=PACK_DATE)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16
        try:
            self._zip.writestr(info, json.dumps(manifest, ensure_ascii=False, indent=1))
            self._zip.close()
            with open(self.temp, "rb+") as handle:
                os.fsync(handle.fileno())
            os.replace(self.temp, self.final)
        except OSError as exc:
            raise PackWriteError(f"cannot finish pack {self.name}: {exc}") from exc
        self.finished = True
        return self.name

    def abort(self):
        """Remove the unfinished (or, after rollback, the just-finished) pack file."""
        try:
            self._zip.close()
        except (OSError, ValueError):
            pass
        for path in (self.temp, self.final):
            try:
                path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Maintenance: migrate loose blobs into packs, verify the pool, rebuild the index
# ---------------------------------------------------------------------------
import contextlib
import hashlib
import re
from arcade_common import OperationCancelled, digest_file

_SHA1 = re.compile(r"[0-9a-f]{40}")


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _origins_for(db, sha1s):
    rows = []
    for part in _chunks(list(sha1s), 500):
        marks = ",".join("?" * len(part))
        rows.extend(db.execute(f"SELECT archive,member,sha1,size,crc32 FROM origins WHERE sha1 IN ({marks})", part))
    return [{"archive": r[0], "member": r[1], "sha1": r[2], "size": r[3], "crc32": r[4]} for r in rows]


def _verify_pack_members(pack_path: Path, expected: dict, cancel_requested=None):
    """Re-read every expected member of a pack; return a list of problem strings."""
    problems = []
    try:
        with zipfile.ZipFile(pack_path) as archive:
            for sha1, size in expected.items():
                check_cancel(cancel_requested)
                try:
                    info = archive.getinfo(sha1)
                except KeyError:
                    problems.append(f"{sha1}: member missing from pack")
                    continue
                if size is not None and info.file_size != size:
                    problems.append(f"{sha1}: size {info.file_size} != {size}")
                    continue
                digest = hashlib.sha1()
                with archive.open(info) as handle:  # CRC is checked by zipfile at end of stream
                    while block := handle.read(CHUNK):
                        check_cancel(cancel_requested)
                        digest.update(block)
                if digest.hexdigest() != sha1:
                    problems.append(f"{sha1}: content hash mismatch")
    except (OSError, zipfile.BadZipFile) as exc:
        problems.append(f"{pack_path.name}: {exc}")
    return problems


def pack_pool(db, work: Path, progress=None, cancel_requested=None, dry_run=False,
              delete_loose=True, max_pack_bytes=256 * 1024 * 1024, max_pack_blobs=5000):
    """Move loose pool files into verified pack ZIPs.

    Order per pack: verify the loose files -> write the pack -> re-read and verify it ->
    commit the database mapping -> only then delete the loose files.
    """
    progress = progress or (lambda message: None)
    work = Path(work)
    pool = work / "pool"
    result = {"dry_run": dry_run, "loose_blobs": 0, "packs_written": 0, "packed_blobs": 0,
              "bytes_in": 0, "pack_bytes": 0, "loose_deleted": 0, "problems": []}
    rows = db.execute("""SELECT b.sha1, b.size, b.crc32 FROM blobs b
                         LEFT JOIN blob_packs p ON p.sha1 = b.sha1
                         LEFT JOIN origins o ON o.sha1 = b.sha1
                         WHERE p.sha1 IS NULL GROUP BY b.sha1
                         ORDER BY MIN(o.archive), MIN(o.member), b.sha1""").fetchall()
    result["loose_blobs"] = len(rows)
    batches, current, current_bytes = [], [], 0
    for row in rows:
        if current and (len(current) >= max_pack_blobs or current_bytes + (row[1] or 0) > max_pack_bytes):
            batches.append(current); current, current_bytes = [], 0
        current.append(row); current_bytes += row[1] or 0
    if current:
        batches.append(current)
    if dry_run:
        result["packs_planned"] = len(batches)
        result["bytes_in"] = sum(r[1] or 0 for r in rows)
        return result
    for number, batch in enumerate(batches, 1):
        check_cancel(cancel_requested)
        progress(f"プール移行中 ({number}/{len(batches)}): {len(batch)}件")
        usable = []
        for sha1, size, crc32 in batch:
            check_cancel(cancel_requested)
            loose = pool / sha1[:2] / sha1
            if not loose.is_file():
                result["problems"].append({"kind": "missing-loose-file", "sha1": sha1})
            elif digest_file(loose, cancel_requested=cancel_requested) != sha1:
                result["problems"].append({"kind": "corrupt-loose-file", "sha1": sha1})
            else:
                usable.append((sha1, loose, loose.stat().st_size, crc32))
        if not usable:
            continue
        writer = PackWriter(pool)
        try:
            for sha1, loose, size, crc32 in usable:
                writer.add(sha1, loose, size, crc32, cancel_requested)
            writer.finish(_origins_for(db, [u[0] for u in usable]))
            problems = _verify_pack_members(writer.final, {u[0]: u[2] for u in usable}, cancel_requested)
            if problems:
                writer.abort()
                result["problems"].extend({"kind": "pack-verification-failed", "detail": p} for p in problems)
                continue
            with db:
                db.executemany("INSERT OR REPLACE INTO blob_packs(sha1,pack) VALUES(?,?)",
                               [(u[0], writer.name) for u in usable])
        except BaseException:
            writer.abort()
            raise
        result["packs_written"] += 1
        result["packed_blobs"] += len(usable)
        result["bytes_in"] += sum(u[2] for u in usable)
        result["pack_bytes"] += writer.final.stat().st_size
        if delete_loose:
            for sha1, loose, _, _ in usable:
                with contextlib.suppress(OSError):
                    loose.unlink()
                    result["loose_deleted"] += 1
    if delete_loose:
        result["loose_deleted"] += _remove_mapped_loose_files(db, pool)
    return result


def _remove_mapped_loose_files(db, pool: Path) -> int:
    """Delete loose files whose blob is already safely mapped to a pack; drop empty shard folders."""
    removed = 0
    mapped = {}
    if not pool.is_dir():
        return 0
    for shard in pool.iterdir():
        if not (shard.is_dir() and re.fullmatch(r"[0-9a-f]{2}", shard.name)):
            continue
        for entry in os.scandir(shard):
            if not (entry.is_file() and _SHA1.fullmatch(entry.name)):
                continue
            row = db.execute("SELECT pack FROM blob_packs WHERE sha1=?", (entry.name,)).fetchone()
            if not row:
                continue
            pack_path = pool / row[0]
            archive = mapped.get(row[0])
            if archive is None:
                try:
                    archive = mapped[row[0]] = zipfile.ZipFile(pack_path)
                except (OSError, zipfile.BadZipFile):
                    continue
            try:
                archive.getinfo(entry.name)
            except KeyError:
                continue
            with contextlib.suppress(OSError):
                os.unlink(entry.path)
                removed += 1
        with contextlib.suppress(OSError):
            shard.rmdir()
    for archive in mapped.values():
        archive.close()
    return removed


def verify_pool(db, work: Path, deep=True, progress=None, cancel_requested=None):
    """Check that every blob in the database can be read back (and, if deep, matches its SHA-1)."""
    progress = progress or (lambda message: None)
    pool = Path(work) / "pool"
    result = {"blobs": 0, "loose": 0, "packed": 0, "packs": 0, "problems": [], "deep": deep}
    by_pack: dict[str, dict] = {}
    loose = []
    for sha1, size, pack in db.execute("""SELECT b.sha1, b.size, p.pack FROM blobs b
                                          LEFT JOIN blob_packs p ON p.sha1 = b.sha1"""):
        result["blobs"] += 1
        if pack:
            by_pack.setdefault(pack, {})[sha1] = size
        else:
            loose.append((sha1, size))
    result["loose"], result["packed"], result["packs"] = len(loose), result["blobs"] - len(loose), len(by_pack)
    for index, (sha1, size) in enumerate(loose, 1):
        check_cancel(cancel_requested)
        path = pool / sha1[:2] / sha1
        if not path.is_file():
            result["problems"].append({"kind": "missing-loose-file", "sha1": sha1})
        elif size is not None and path.stat().st_size != size:
            result["problems"].append({"kind": "loose-size-mismatch", "sha1": sha1})
        elif deep and digest_file(path, cancel_requested=cancel_requested) != sha1:
            result["problems"].append({"kind": "corrupt-loose-file", "sha1": sha1})
    for number, (pack, expected) in enumerate(sorted(by_pack.items()), 1):
        check_cancel(cancel_requested)
        progress(f"プール検証中 ({number}/{len(by_pack)}): {pack}")
        pack_path = pool / pack
        if not pack_path.is_file():
            result["problems"].append({"kind": "missing-pack", "pack": pack, "blobs": len(expected)})
            continue
        if deep:
            problems = _verify_pack_members(pack_path, expected, cancel_requested)
        else:
            problems = []
            try:
                with zipfile.ZipFile(pack_path) as archive:
                    for sha1, size in expected.items():
                        try:
                            info = archive.getinfo(sha1)
                        except KeyError:
                            problems.append(f"{sha1}: member missing from pack")
                            continue
                        if size is not None and info.file_size != size:
                            problems.append(f"{sha1}: size {info.file_size} != {size}")
            except (OSError, zipfile.BadZipFile) as exc:
                problems.append(f"{pack}: {exc}")
        result["problems"].extend({"kind": "pack-problem", "pack": pack, "detail": p} for p in problems)
    result["ok"] = not result["problems"]
    return result


def rebuild_index(db, work: Path, progress=None, cancel_requested=None):
    """Restore blobs, blob_packs and origins in the database from the packs' manifests."""
    progress = progress or (lambda message: None)
    pack_dir = Path(work) / "pool" / PACK_DIR
    result = {"packs": 0, "blobs": 0, "origins": 0, "unreadable": []}
    for number, pack_path in enumerate(sorted(pack_dir.glob("*.zip")) if pack_dir.is_dir() else [], 1):
        check_cancel(cancel_requested)
        try:
            with zipfile.ZipFile(pack_path) as archive:
                manifest = json.loads(archive.read(MANIFEST_NAME))
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile, KeyError, ValueError) as exc:
            result["unreadable"].append(f"{pack_path.name}: {exc}")
            continue
        relative = f"{PACK_DIR}/{pack_path.name}"
        with db:
            for blob in manifest.get("blobs", []):
                if blob["sha1"] in names:
                    db.execute("INSERT OR IGNORE INTO blobs VALUES(?,?,?)", (blob["sha1"], blob["size"], blob["crc32"]))
                    db.execute("INSERT OR REPLACE INTO blob_packs(sha1,pack) VALUES(?,?)", (blob["sha1"], relative))
                    result["blobs"] += 1
            for origin in manifest.get("origins", []):
                db.execute("INSERT OR IGNORE INTO blobs VALUES(?,?,?)", (origin["sha1"], origin["size"], origin["crc32"]))
                cursor = db.execute("INSERT OR IGNORE INTO origins(archive,member,sha1,size,crc32) VALUES(?,?,?,?,?)",
                                    (origin["archive"], origin["member"], origin["sha1"], origin["size"], origin["crc32"]))
                result["origins"] += cursor.rowcount
        result["packs"] += 1
        if number % 100 == 0:
            progress(f"インデックス再構築中: {number}個のパック")
    return result
