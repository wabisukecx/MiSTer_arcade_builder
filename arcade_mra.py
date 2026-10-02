"""MRA classification, parsing, catalog, and FTP synchronization."""
from __future__ import annotations
import contextlib
import ftplib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
from arcade_common import CHUNK, check_cancel, digest_file, safe_name, normalize_crc

FIXABLE = {"Space Demon.mra", "Space Firebird.mra"}
TAG = re.compile(rb"(<\s*/?\s*)([A-Za-z][A-Za-z0-9_-]*)(?=[\s/>])")
ALTERNATIVE_CATEGORIES = frozenset({"japan", "other_region", "version", "modified", "unclassified"})
JAPAN_MARKER = re.compile(r"(?i)(?<![a-z])(?:japan(?:ese)?|jp)(?![a-z])|日本|[\[(]j[\])]|\b(?:version|ver\.?)\s*j\b")
MODIFIED_MARKER = re.compile(r"(?i)\b(?:hack|hacked|bootleg|homebrew|patch|patched|fix|fixed|cheat|trainer|decrypted|translation|mod(?:ified)?)\b")
OTHER_REGION_MARKER = re.compile(r"(?i)\b(?:usa?|us|europe|eur|world|asia|korea|korean|china|chinese|taiwan|hong\s*kong|germany|german|france|french|spain|spanish|italy|italian|uk|australia|brazil|russia)\b")
VERSION_MARKER = re.compile(r"(?i)\b(?:version|ver\.?|revision|rev\.?|set|prototype|proto|beta|alpha|alternate|alternative|older|newer|v\d+)\b")

def japan_zip_pair(root: ET.Element) -> bool:
    """Recognize a preferred fooj[digits].zip followed by its foo.zip parent."""
    for rom in root.findall("rom"):
        names = [PurePosixPath(item.strip()).name.lower().removesuffix(".zip")
                 for item in rom.get("zip", "").split("|") if item.strip()]
        if len(names) < 2:
            continue
        match = re.fullmatch(r"(.+?)j\d*", names[0])
        if match and match.group(1) in names[1:]:
            return True
    return False

def japan_setname_parent(root: ET.Element) -> bool:
    """Recognize a j-suffixed child set even when its ZIP is a later fallback."""
    setname = (root.findtext("setname") or "").strip().lower()
    parent = (root.findtext("parent") or "").strip().lower()
    if not parent or not re.fullmatch(re.escape(parent) + r"j\d*", setname):
        return False
    return any(PurePosixPath(item.strip()).name.lower() == setname + ".zip"
               for rom in root.findall("rom")
               for item in rom.get("zip", "").split("|") if item.strip())

def alternative_category(relative: str | PurePosixPath, raw: bytes | None = None) -> str:
    """Classify using the filename, explicit MRA metadata, and a strong ZIP pair."""
    stem = PurePosixPath(str(relative)).stem.replace("_", " ")
    if MODIFIED_MARKER.search(stem):
        return "modified"
    root = None
    if raw is not None:
        try:
            root, _ = parse_tree(raw, PurePosixPath(str(relative)).name)
            if (root.findtext("bootleg") or "").strip().lower() == "yes" or (root.findtext("homebrew") or "").strip().lower() == "yes":
                return "modified"
        except ET.ParseError:
            pass
    if JAPAN_MARKER.search(stem):
        return "japan"
    if OTHER_REGION_MARKER.search(stem):
        return "other_region"
    if root is not None and (JAPAN_MARKER.search(root.findtext("region") or "")
                             or JAPAN_MARKER.search(root.findtext("name") or "")
                             or japan_zip_pair(root)
                             or japan_setname_parent(root)):
        return "japan"
    if VERSION_MARKER.search(stem):
        return "version"
    return "unclassified"

def selected_categories(include_alternatives=False, alternative_categories=None) -> frozenset[str]:
    categories = (ALTERNATIVE_CATEGORIES if include_alternatives else frozenset()) if alternative_categories is None else frozenset(alternative_categories)
    invalid = categories - ALTERNATIVE_CATEGORIES
    if invalid:
        raise ValueError(f"Unknown alternative categories: {', '.join(sorted(invalid))}")
    return categories

def selected_mra(relative: str | PurePosixPath, categories: frozenset[str], raw: bytes | None = None) -> bool:
    parts = PurePosixPath(str(relative)).parts
    if len(parts) == 1:
        return True
    return parts[0].casefold() == "_alternatives" and alternative_category(relative, raw) in categories

def connect(work: Path) -> sqlite3.Connection:
    work.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(work / "arcade.db")
    db.row_factory = sqlite3.Row
    db.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS mras(id INTEGER PRIMARY KEY, relative_path TEXT UNIQUE, file_sha1 TEXT,
      status TEXT, error TEXT, title TEXT, setname TEXT, rbf TEXT, mameversion TEXT, has_external TEXT);
    CREATE TABLE IF NOT EXISTS roms(id INTEGER PRIMARY KEY, mra_id INTEGER REFERENCES mras(id) ON DELETE CASCADE,
      ordinal INTEGER, rom_index TEXT, zip_spec TEXT, md5 TEXT, attrs TEXT);
    CREATE TABLE IF NOT EXISTS parts(id INTEGER PRIMARY KEY, rom_id INTEGER REFERENCES roms(id) ON DELETE CASCADE,
      ordinal INTEGER, name TEXT, crc32 TEXT, zip_spec TEXT, attrs TEXT, text_data TEXT, parent_tag TEXT, parent_attrs TEXT);
    CREATE TABLE IF NOT EXISTS zip_candidates(part_id INTEGER REFERENCES parts(id) ON DELETE CASCADE,
      ordinal INTEGER, zip_name TEXT, PRIMARY KEY(part_id,ordinal));
    CREATE TABLE IF NOT EXISTS nodes(rom_id INTEGER REFERENCES roms(id) ON DELETE CASCADE,
      ordinal INTEGER, tag TEXT, attrs TEXT, text_data TEXT, parent_ordinal INTEGER,
      PRIMARY KEY(rom_id,ordinal));
    CREATE TABLE IF NOT EXISTS blobs(sha1 TEXT PRIMARY KEY, size INTEGER, crc32 TEXT);
    CREATE TABLE IF NOT EXISTS blob_packs(sha1 TEXT PRIMARY KEY, pack TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS origins(id INTEGER PRIMARY KEY, archive TEXT, member TEXT, sha1 TEXT REFERENCES blobs(sha1),
      size INTEGER, crc32 TEXT, UNIQUE(archive,member,sha1));
    CREATE TABLE IF NOT EXISTS ingested_archives(path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER);
    CREATE TABLE IF NOT EXISTS remote_mras(remote_path TEXT PRIMARY KEY, size INTEGER, modified TEXT, sha1 TEXT);
    CREATE TABLE IF NOT EXISTS builds(mra_id INTEGER, rom_id INTEGER, zip_name TEXT, plan TEXT, md5_status TEXT,
      PRIMARY KEY(mra_id,rom_id,zip_name));
    """)
    return db

def parse_tree(raw: bytes, filename: str):
    try:
        return ET.fromstring(raw), "parsed"
    except ET.ParseError:
        if filename not in FIXABLE:
            raise
        fixed = TAG.sub(lambda m: m.group(1) + m.group(2).lower(), raw)
        return ET.fromstring(fixed), "corrected"

def scan_mras(db, root: Path, recursive=True, prune_unseen=False, alternative_categories=None,
              cancel_requested=None):
    counts = {"parsed": 0, "corrected": 0, "unsupported": 0, "failed": 0, "unchanged": 0}
    corrected = []
    paths = sorted(root.rglob("*.mra") if recursive else root.glob("*.mra"))
    if alternative_categories is not None:
        categories = selected_categories(alternative_categories=alternative_categories)
        selected = []
        for path in paths:
            check_cancel(cancel_requested)
            if selected_mra(path.relative_to(root).as_posix(), categories, path.read_bytes()):
                selected.append(path)
        paths = selected
    seen = set()
    for path in paths:
        check_cancel(cancel_requested)
        rel = path.relative_to(root).as_posix()
        seen.add(rel)
        sha = digest_file(path, cancel_requested=cancel_requested)
        old = db.execute("SELECT id,file_sha1,status FROM mras WHERE relative_path=?", (rel,)).fetchone()
        if old and old["file_sha1"] == sha:
            counts["unchanged"] += 1
            continue
        try:
            root_node, status = parse_tree(path.read_bytes(), path.name)
            if root_node.tag.lower() != "misterromdescription":
                status, error = "unsupported", "unexpected root element"
            else:
                error = None
        except (ET.ParseError, ValueError, UnicodeError) as exc:
            root_node, status, error = None, "failed", str(exc)
        title = (root_node.findtext("name") or root_node.findtext("description") or path.stem) if root_node is not None else path.stem
        vals = [root_node.findtext(x) if root_node is not None else None for x in ("setname", "rbf", "mameversion")]
        with db:
            if old:
                mid = old["id"]
                db.execute("DELETE FROM roms WHERE mra_id=?", (mid,))
                db.execute("DELETE FROM builds WHERE mra_id=?", (mid,))
                db.execute("UPDATE mras SET file_sha1=?,status=?,error=?,title=?,setname=?,rbf=?,mameversion=? WHERE id=?",
                           (sha,status,error,title,*vals,mid))
            else:
                mid = db.execute("INSERT INTO mras(relative_path,file_sha1,status,error,title,setname,rbf,mameversion) VALUES(?,?,?,?,?,?,?,?)",
                                 (rel,sha,status,error,title,*vals)).lastrowid
            if status in ("parsed", "corrected"):
                for ri, rom in enumerate(root_node.findall("rom")):
                    rid = db.execute("INSERT INTO roms(mra_id,ordinal,rom_index,zip_spec,md5,attrs) VALUES(?,?,?,?,?,?)",
                                     (mid,ri,rom.get("index","0"),rom.get("zip",""),rom.get("md5",""),json.dumps(rom.attrib))).lastrowid
                    ordinal = 0
                    def walk(node, parent_tag="rom", parent_attrs=None, parent_ordinal=None):
                        nonlocal ordinal
                        for child in node:
                            ordinal += 1
                            no = ordinal
                            attrs = json.dumps(child.attrib)
                            text_data = (child.text or "").strip()
                            db.execute("INSERT INTO nodes VALUES(?,?,?,?,?,?)", (rid,no,child.tag,attrs,text_data,parent_ordinal))
                            if child.tag == "part":
                                crc = normalize_crc(child.get("crc"))
                                spec = child.get("zip") or rom.get("zip", "")
                                pid = db.execute("INSERT INTO parts(rom_id,ordinal,name,crc32,zip_spec,attrs,text_data,parent_tag,parent_attrs) VALUES(?,?,?,?,?,?,?,?,?)",
                                                 (rid,no,child.get("name"),crc,spec,attrs,text_data,parent_tag,json.dumps(parent_attrs or {}))).lastrowid
                                for zi, z in enumerate(x.strip() for x in spec.split("|") if x.strip()):
                                    db.execute("INSERT INTO zip_candidates VALUES(?,?,?)", (pid,zi,z))
                            walk(child,child.tag,child.attrib,no)
                    walk(rom)
        counts[status] += 1
        if status == "corrected": corrected.append(rel)
    removed = 0
    check_cancel(cancel_requested)
    if prune_unseen:
        with db:
            for row in db.execute("SELECT id,relative_path FROM mras").fetchall():
                if row["relative_path"] not in seen:
                    db.execute("DELETE FROM builds WHERE mra_id=?",(row["id"],))
                    db.execute("DELETE FROM mras WHERE id=?",(row["id"],))
                    removed += 1
    main_count = sum("/" not in rel for rel in seen)
    alternative_count = sum(rel.startswith("_alternatives/") for rel in seen)
    return {"counts": counts, "total": sum(counts.values()), "corrected": corrected,
            "main_mras": main_count, "alternative_mras": alternative_count,
            "removed_from_catalog": removed}

def ftp_mra_files(ftp, remote_root, recursive=False):
    """List MRA files in the arcade directory; subfolders are optional."""
    def walk(folder, relative):
        ftp.cwd(folder)
        try:
            entries = list(ftp.mlsd())
        except ftplib.all_errors:
            entries = [(name,{}) for name in ftp.nlst()]
        entries.sort(key=lambda item:(not PurePosixPath(item[0]).name.lower().endswith(".mra"),
                                      PurePosixPath(item[0]).name.casefold()))
        for raw_name,facts in entries:
            name = PurePosixPath(raw_name).name
            if name in ("", ".", "..") or not safe_name(name):
                continue
            kind = facts.get("type","").lower()
            if kind in ("cdir","pdir"):
                continue
            if kind == "dir" and name.lower() in ("cores","mame","hbmame"):
                continue
            path = folder.rstrip("/") + "/" + name
            rel = relative / name
            if name.lower().endswith(".mra") and kind != "dir":
                yield path,rel,facts
            elif recursive and (kind == "dir" or not kind):
                try:
                    ftp.cwd(path)
                except ftplib.all_errors:
                    continue
                yield from walk(path,rel)
                ftp.cwd(folder)
    yield from walk(remote_root,PurePosixPath())

def sync_mras_ftp(db, local_root: Path, config, progress=None, include_alternatives=False,
                  alternative_categories=None, cancel_requested=None):
    """Fetch changed MRA files; connection failures leave the local cache usable."""
    progress = progress or (lambda message: None)
    categories = selected_categories(include_alternatives, alternative_categories)
    connection = config.get("mister",{})
    host = connection.get("host","")
    remote_root = connection.get("mra_root","/media/fat/_Arcade")
    result = {"status":"skipped-no-host","remote_files":0,"remote_main_files":0,
              "remote_alternative_files":0,"downloaded":[],"unchanged":0,"errors":[]}
    check_cancel(cancel_requested)
    if not host:
        return result
    password = connection.get("password") or os.environ.get(connection.get("password_env","MISTER_FTP_PASSWORD"),"")
    try:
        with ftplib.FTP() as ftp:
            ftp.connect(host,int(connection.get("port",21)),timeout=int(connection.get("timeout",5)))
            ftp.login(connection.get("username","root"),password)
            result["status"] = "connected"
            for remote_path,relative,facts in ftp_mra_files(ftp,remote_root,recursive=bool(categories)):
                check_cancel(cancel_requested)
                target = local_root.joinpath(*relative.parts)
                preferred = selected_mra(relative, categories)
                inspect_alternative = (relative.parts[0].casefold() == "_alternatives"
                                       and categories != ALTERNATIVE_CATEGORIES)
                if not preferred and not inspect_alternative:
                    continue
                size = int(facts["size"]) if facts.get("size","").isdigit() else None
                modified = facts.get("modify")
                if size is None:
                    with contextlib.suppress(*ftplib.all_errors): size = ftp.size(remote_path)
                if not modified:
                    with contextlib.suppress(*ftplib.all_errors):
                        reply = ftp.sendcmd("MDTM " + remote_path)
                        if reply.startswith("213 "): modified = reply[4:].strip()
                previous = db.execute("SELECT size,modified,sha1 FROM remote_mras WHERE remote_path=?",
                                      (remote_path,)).fetchone()
                prefetched = None
                if inspect_alternative:
                    if (previous and size is not None and modified and target.is_file()
                            and (previous["size"],previous["modified"]) == (size,modified)
                            and digest_file(target, cancel_requested=cancel_requested) == previous["sha1"]):
                        raw = target.read_bytes()
                    else:
                        blocks = []
                        try:
                            def receive_inspected(block):
                                check_cancel(cancel_requested)
                                blocks.append(block)
                            ftp.retrbinary("RETR " + remote_path,receive_inspected,blocksize=CHUNK)
                        except ftplib.all_errors as exc:
                            result["errors"].append({"mra":str(relative),"error":str(exc)})
                            continue
                        raw = prefetched = b"".join(blocks)
                        if size is not None and len(raw) != size:
                            result["errors"].append({"mra":str(relative),"error":"MRA size mismatch"})
                            continue
                    if not selected_mra(relative,categories,raw):
                        continue
                result["remote_files"] += 1
                if len(relative.parts) == 1:
                    result["remote_main_files"] += 1
                elif relative.parts[0] == "_alternatives":
                    result["remote_alternative_files"] += 1
                if result["remote_files"] % 50 == 0:
                    progress(f"MRAを確認中: 標準版 {result['remote_main_files']}件、"
                             f"代替版 {result['remote_alternative_files']}件")
                if (previous and size is not None and modified and target.is_file()
                        and (previous["size"],previous["modified"]) == (size,modified)
                        and digest_file(target, cancel_requested=cancel_requested) == previous["sha1"]):
                    result["unchanged"] += 1
                    continue
                target.parent.mkdir(parents=True,exist_ok=True)
                fd,temp_name = tempfile.mkstemp(prefix="mra-",suffix=".tmp",dir=target.parent)
                try:
                    sha = hashlib.sha1()
                    with os.fdopen(fd,"wb") as output:
                        def receive(block):
                            check_cancel(cancel_requested)
                            output.write(block)
                            sha.update(block)
                        if prefetched is not None:
                            receive(prefetched)
                        else:
                            ftp.retrbinary("RETR " + remote_path,receive,blocksize=CHUNK)
                    if size is not None and Path(temp_name).stat().st_size != size:
                        raise ValueError(f"MRA size mismatch: {remote_path}")
                    check_cancel(cancel_requested)
                    if target.is_file() and digest_file(target, cancel_requested=cancel_requested) == sha.hexdigest():
                        result["unchanged"] += 1
                    else:
                        os.replace(temp_name,target)
                        result["downloaded"].append(str(relative))
                    with db:
                        db.execute("INSERT OR REPLACE INTO remote_mras VALUES(?,?,?,?)",
                                   (remote_path,size,modified,sha.hexdigest()))
                except (OSError,ValueError,*ftplib.all_errors) as exc:
                    result["errors"].append({"mra":str(relative),"error":str(exc)})
                finally:
                    with contextlib.suppress(OSError): os.unlink(temp_name)
    except ftplib.all_errors as exc:
        result["status"] = "offline" if result["status"] == "skipped-no-host" else "interrupted"
        result["errors"].append({"error":str(exc)})
    return result
