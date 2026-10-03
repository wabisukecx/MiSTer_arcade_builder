"""Import source ZIPs into the content pool and match MRA ROM parts."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
import zipfile
import zlib
from arcade_common import CHUNK, MAX_ARCHIVE_MEMBERS, MAX_ARCHIVE_TOTAL, MAX_MEMBER, OperationCancelled, canonical_zip, check_cancel, safe_name
from arcade_store import BlobStore, PackWriteError, PackWriter

def ingest(db, work: Path, roots: list[Path], skip_unchanged=False, progress=None,
           cancel_requested=None, storage="pack"):
    """Import ROM ZIPs into the pool.

    ``storage="pack"`` (default) stores each archive's new content in one pack ZIP
    plus a manifest; ``"loose"`` keeps the original one-file-per-blob layout.
    """
    if storage not in ("pack", "loose"):
        raise ValueError(f"unknown pool storage: {storage}")
    progress = progress or (lambda message: None)
    archives = sorted({p.resolve() for root in roots for p in (root.rglob("*") if root.is_dir() else [root]) if p.is_file()})
    result = {"archives": 0, "skipped_unchanged": 0, "members": 0, "new_blobs": 0, "duplicate_content": 0, "rejected": [], "unsupported_7z": []}
    pool = work / "pool"
    pool.mkdir(parents=True, exist_ok=True)
    with BlobStore(db, work) as store:
        for index, archive in enumerate(archives, 1):
            check_cancel(cancel_requested)
            if index == 1 or index % 50 == 0 or index == len(archives):
                progress(f"ROM ZIP取込中 ({index}/{len(archives)}): {archive.name}")
            if archive.suffix.lower() == ".7z":
                result["unsupported_7z"].append(str(archive)); continue
            if archive.suffix.lower() != ".zip": continue
            stat = archive.stat()
            previous = db.execute("SELECT size,mtime_ns FROM ingested_archives WHERE path=?",(str(archive),)).fetchone()
            if skip_unchanged and previous and (previous["size"],previous["mtime_ns"]) == (stat.st_size,stat.st_mtime_ns):
                result["skipped_unchanged"] += 1
                continue
            result["archives"] += 1
            rejected_before = len(result["rejected"])
            counts_before = {key: result[key] for key in ("members", "new_blobs", "duplicate_content")}
            new_paths = []
            writer = None
            def discard_new_content():
                for path in new_paths:
                    with contextlib.suppress(OSError): path.unlink()
                if writer is not None:
                    writer.abort()
            try:
                with db:
                    db.execute("DELETE FROM origins WHERE archive=?",(str(archive),))
                    db.execute("DELETE FROM ingested_archives WHERE path=?",(str(archive),))
                    origins = []
                    pending = set()
                    with zipfile.ZipFile(archive) as z:
                        seen = set()
                        infos = z.infolist()
                        declared = sum(info.file_size for info in infos if not info.is_dir())
                        if len(infos) > MAX_ARCHIVE_MEMBERS or declared > MAX_ARCHIVE_TOTAL:
                            raise OSError(f"archive too large: {len(infos)} members, {declared} bytes declared")
                        extracted = 0
                        for info in infos:
                            check_cancel(cancel_requested)
                            if extracted > MAX_ARCHIVE_TOTAL:
                                raise OSError("archive expands beyond the size limit")
                            if info.is_dir(): continue
                            name = info.filename
                            if not safe_name(name) or name in seen or info.file_size > MAX_MEMBER:
                                result["rejected"].append(f"{archive}:{name}: unsafe/duplicate/oversized"); continue
                            seen.add(name)
                            h = hashlib.sha1(); crc = 0; size = 0
                            fd, tmp_name = tempfile.mkstemp(prefix="ingest-", dir=pool)
                            try:
                                with os.fdopen(fd,"wb") as out, z.open(info) as src:
                                    while block := src.read(CHUNK):
                                        check_cancel(cancel_requested)
                                        size += len(block)
                                        if size > MAX_MEMBER: raise ValueError("expanded member too large")
                                        h.update(block); crc = zlib.crc32(block,crc); out.write(block)
                                extracted += size
                                sha = h.hexdigest(); crc_text = f"{crc & 0xffffffff:08x}"
                                if sha in pending or store.has(sha):
                                    result["duplicate_content"] += 1
                                elif storage == "loose":
                                    dest = pool / sha[:2] / sha
                                    dest.parent.mkdir(exist_ok=True)
                                    os.replace(tmp_name,dest)
                                    new_paths.append(dest)
                                    result["new_blobs"] += 1
                                else:
                                    if writer is None: writer = PackWriter(pool)
                                    writer.add(sha, Path(tmp_name), size, crc_text, cancel_requested)
                                    pending.add(sha)
                                    db.execute("INSERT OR REPLACE INTO blob_packs(sha1,pack) VALUES(?,?)",(sha,writer.name))
                                    result["new_blobs"] += 1
                                db.execute("INSERT OR IGNORE INTO blobs VALUES(?,?,?)", (sha,size,crc_text))
                                db.execute("INSERT OR IGNORE INTO origins(archive,member,sha1,size,crc32) VALUES(?,?,?,?,?)",
                                           (str(archive),name,sha,size,crc_text))
                                origins.append({"archive":str(archive),"member":name,"sha1":sha,"size":size,"crc32":crc_text})
                                result["members"] += 1
                            except OperationCancelled:
                                raise
                            except sqlite3.Error:
                                raise
                            except PackWriteError:
                                raise
                            except Exception as exc:
                                result["rejected"].append(f"{archive}:{name}: {exc}")
                            finally:
                                if os.path.exists(tmp_name): os.unlink(tmp_name)
                    if storage == "pack" and (writer is not None or origins):
                        if writer is None: writer = PackWriter(pool)
                        writer.finish(origins)
                    if len(result["rejected"]) == rejected_before:
                        db.execute("INSERT OR REPLACE INTO ingested_archives VALUES(?,?,?)",
                                   (str(archive),stat.st_size,stat.st_mtime_ns))
            except OperationCancelled:
                discard_new_content()
                raise
            except (zipfile.BadZipFile, OSError) as exc:
                discard_new_content()
                result.update(counts_before)
                result["rejected"].append(f"{archive}: {exc}")
            except Exception:
                discard_new_content()
                raise
    return result

def part_rows(db, rid):
    return db.execute("SELECT * FROM parts WHERE rom_id=? ORDER BY ordinal", (rid,)).fetchall()

def is_external(part):
    return bool(part["name"] or part["crc32"] or json.loads(part["attrs"]).get("ignore_crc") is not None)

class OriginIndex:
    """Index pool content by source ZIP and globally by CRC for safe fallback."""

    def __init__(self, db, cancel_requested=None):
        self.by_crc = {}
        self.by_crc_global = {}
        self.by_name = {}
        self.archives = {}
        seen_archives = {}
        seen_sha = set()
        for row in db.execute("SELECT * FROM origins ORDER BY id"):
            check_cancel(cancel_requested)
            archive = row["archive"]
            full_path = canonical_zip(archive)
            path_parts = full_path.split("/")
            # MRA ZIP references may include a directory, for example
            # "/sound/journey.zip". Index path suffixes so an archive found
            # recursively under an input root can match that relative path.
            archive_keys = {canonical_zip(Path(archive).name), full_path}
            archive_keys.update("/".join(path_parts[start:])
                                for start in range(1, len(path_parts)))
            member_name = PurePosixPath(row["member"]).name.lower()
            for key in archive_keys:
                if archive not in seen_archives.setdefault(key, set()):
                    self.archives.setdefault(key, []).append(archive)
                    seen_archives[key].add(archive)
                self.by_crc.setdefault((key, row["crc32"]), []).append(row)
                self.by_name.setdefault((key, member_name), []).append(row)
            # MRA ZIP names constrain preferred lookup locations, but identical
            # ROM bytes are reusable from any imported archive.
            if row["sha1"] not in seen_sha:
                seen_sha.add(row["sha1"])
                self.by_crc_global.setdefault(row["crc32"], []).append(row)

        # Keep pool objects whose original archive record was removed as
        # searchable too. Their output filename still comes from the MRA part.
        for row in db.execute("SELECT sha1,size,crc32 FROM blobs"):
            check_cancel(cancel_requested)
            if row["sha1"] in seen_sha:
                continue
            seen_sha.add(row["sha1"])
            self.by_crc_global.setdefault(row["crc32"], []).append({
                "sha1": row["sha1"], "size": row["size"], "crc32": row["crc32"],
                "archive": "", "member": ""})

    def hits(self, zip_name, crc32=None, member_name=None):
        key = canonical_zip(zip_name)
        if crc32:
            return self.by_crc.get((key, crc32), ())
        if member_name and member_name.lower() != "none":
            return self.by_name.get((key, member_name.lower()), ())
        return ()

    def global_crc_hits(self, crc32):
        return self.by_crc_global.get(crc32, ())

    def registered_archives(self, zip_name):
        return self.archives.get(canonical_zip(zip_name), ())


def choices(db, part, origin_index=None):
    origin_index = origin_index or OriginIndex(db)
    desired_name = part["name"]
    candidates = [r["zip_name"] for r in db.execute("SELECT zip_name FROM zip_candidates WHERE part_id=? ORDER BY ordinal",(part["id"],))]
    if not candidates:
        return [], "unsupported:no ZIP search path"
    for zname in candidates:
        hits = origin_index.hits(zname, part["crc32"], desired_name)
        if hits:
            by_sha = {}
            for h in hits: by_sha.setdefault(h["sha1"],h)
            return list(by_sha.values()), "crc" if part["crc32"] else "low-trust-name"
    # CRC-bearing MRA parts identify the required bytes independently of the
    # source archive name. Accept a global pool match only when its content hash
    # is unique; distinct blobs sharing a CRC remain ambiguous in rom_plan.
    if part["crc32"]:
        hits = origin_index.global_crc_hits(part["crc32"])
        if hits:
            by_sha = {}
            for hit in hits:
                by_sha.setdefault(hit["sha1"], hit)
            return list(by_sha.values()), "global-crc"
    return [], "missing"

def output_zip_for_part(db, part, item):
    """Return the MRA ZIP candidate that actually supplied this part."""
    source = canonical_zip(Path(item["source_archive"]).name) if item else None
    candidates = [r["zip_name"] for r in db.execute(
        "SELECT zip_name FROM zip_candidates WHERE part_id=? ORDER BY ordinal", (part["id"],))]
    for candidate in candidates:
        if source is not None and canonical_zip(candidate) == source:
            return canonical_zip(candidate)
    # Older catalog rows and direct callers may not retain the source archive.
    return canonical_zip(candidates[0]) if candidates else ""

def rom_plan(db, rom, origin_index=None):
    origin_index = origin_index or OriginIndex(db)
    members = {}; selected = []; missing = []; ambiguous = []; unsupported = []
    selectors = {}
    for part in part_rows(db,rom["id"]):
        if not is_external(part): continue
        found, mode = choices(db,part,origin_index)
        if not found:
            (unsupported if mode.startswith("unsupported") else missing).append({"part":part["id"],"name":part["name"],"crc32":part["crc32"],"zip":part["zip_spec"],"reason":mode})
            continue
        if len(found) > 1:
            ambiguous.append({"part":part["id"],"name":part["name"],"crc32":part["crc32"],"sha1":[r["sha1"] for r in found]}); continue
        row = found[0]
        output_name = part["name"] if part["name"] and part["name"].lower() != "none" else PurePosixPath(row["member"]).name
        if not safe_name(output_name):
            unsupported.append({"part":part["id"],"reason":"unsafe output member name"}); continue
        item = {"part":part["id"],"name":output_name,"source_archive":row["archive"],"source_member":row["member"],"crc32":row["crc32"],"lookup_crc32":part["crc32"],"sha1":row["sha1"],"size":row["size"],"match":mode}
        selector = (output_name, part["crc32"] if part["crc32"] else "by-name")
        if selector in selectors and selectors[selector] != row["sha1"]:
            ambiguous.append({"part":part["id"],"name":output_name,"crc32":part["crc32"],
                              "reason":"same MRA lookup key resolves to different content"})
            continue
        selectors[selector] = row["sha1"]
        # MiSTer searches by CRC first, so ZIPs may contain multiple entries
        # with the same member name when the MRA requires different revisions.
        members[(output_name, row["sha1"])] = item
        selected.append(item)
    return {"rom_id":rom["id"],"rom_index":rom["rom_index"],"zip_spec":rom["zip_spec"],"md5_expected":rom["md5"],
            "members":list(members.values()),"selected_parts":selected,"missing":missing,"ambiguous":ambiguous,"unsupported":unsupported}

def plans_for_mra(db, mra, origin_index=None):
    origin_index = origin_index or OriginIndex(db)
    roms = db.execute("SELECT * FROM roms WHERE mra_id=? ORDER BY ordinal",(mra["id"],)).fetchall()
    groups = {}; plans = []
    for rom in roms:
        if not rom["zip_spec"]: continue
        plan = rom_plan(db,rom,origin_index)
        groups.setdefault(rom["rom_index"],[]).append(plan)
    for index, variants in groups.items():
        ranked = sorted(variants,key=lambda p:(bool(p["missing"] or p["ambiguous"] or p["unsupported"]),variants.index(p)))
        best = ranked[0]
        best["alternatives"] = len(variants)
        plans.append(best)
    return plans

def status(plan, built=False):
    if plan["ambiguous"]: return "ambiguous"
    if plan["unsupported"]: return "unsupported"
    if plan["missing"]: return "missing"
    md5 = plan["md5_expected"]
    if md5 and md5.lower() != "none":
        return "md5-matched" if built and plan.get("md5_status") == "matched" else "parts-ready-md5-unverified"
    return "parts-ready"
