"""Transfer generated ZIP files to MiSTer over FTP."""
from __future__ import annotations
import contextlib
import ftplib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import zipfile
from arcade_build import verify_zip
from arcade_common import CHUNK, canonical_zip, check_cancel, digest_file
from arcade_mra import mra_files_match_catalog


class CancellableReader:
    def __init__(self, source, cancel_requested):
        self.source = source
        self.cancel_requested = cancel_requested

    def read(self, size=-1):
        check_cancel(self.cancel_requested)
        return self.source.read(size)


def _load_send_cache(work):
    try:
        data = json.loads((work / "send-cache.json").read_text(encoding="utf-8"))
        if data.get("version") == 1 and isinstance(data.get("entries"), dict):
            return data["entries"]
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def _save_send_cache(work, entries):
    work.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="send-cache-", suffix=".tmp", dir=work)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "entries": entries}, stream, ensure_ascii=False, indent=2)
        os.replace(temp, work / "send-cache.json")
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def _remember_send_cache(work, entries):
    # Cache persistence must never turn a verified upload into a failed Send.
    with contextlib.suppress(OSError):
        _save_send_cache(work, entries)


def _members_signature(members):
    """Stable fingerprint of the ROM members a ZIP must contain."""
    rows = sorted([m["name"], m["sha1"], m["crc32"], m["size"]] +
                  (["by-name"] if "lookup_crc32" in m and not m["lookup_crc32"] else [])
                  for m in members)
    return hashlib.sha1(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def _remote_mdtm(ftp, name):
    try:
        reply = ftp.sendcmd("MDTM " + name)
    except ftplib.all_errors + (AttributeError,):
        return None
    if not reply.startswith("213 "):
        return None
    timestamp = reply[4:].strip()
    return timestamp if len(timestamp) >= 14 and timestamp[:14].isdigit() else None


def _ensure_remote_directory(ftp, path):
    """Change to an absolute remote directory, creating missing components."""
    try:
        ftp.cwd(path)
        return
    except ftplib.error_perm:
        pass
    if not path.startswith("/"):
        raise ftplib.error_perm(f"remote directory does not exist: {path}")
    ftp.cwd("/")
    for component in (part for part in path.split("/") if part):
        try:
            ftp.cwd(component)
        except ftplib.error_perm:
            try:
                ftp.mkd(component)
            except ftplib.error_perm:
                # A concurrent Send may have created it after cwd failed.
                ftp.cwd(component)
            else:
                ftp.cwd(component)

def _replace_remote(ftp, temp, name, progress):
    """Swap a verified upload in for the existing remote ZIP, restoring the old one on failure."""
    spare = name + ".replaced"
    with contextlib.suppress(ftplib.all_errors):
        ftp.delete(spare)
    ftp.rename(name, spare)
    try:
        ftp.rename(temp, name)
    except Exception:
        with contextlib.suppress(Exception):
            ftp.rename(spare, name)
        raise
    with contextlib.suppress(ftplib.all_errors):
        ftp.delete(spare)
    progress(f"既存ZIPを置換: {name}")


def _delete_held_entry(ftp, entry, work, cache, server, progress, cancel_requested=None):
    """Remove a held ZIP from MiSTer and out so it cannot be sent again stale."""
    check_cancel(cancel_requested)
    dest = entry["destination"]
    parent, name = dest.rsplit("/", 1)
    progress(f"保留ZIP削除中: {dest}")
    try:
        ftp.cwd(parent)
        names = {PurePosixPath(item).name for item in ftp.nlst()}
        if name in names:
            ftp.delete(name)
            remote = "deleted"
        else:
            remote = "absent"
    except ftplib.all_errors + (OSError,) as exc:
        entry["cleanup"] = {"remote": "failed", "local": "retained",
                            "error": str(exc)}
        progress(f"保留ZIP削除失敗: {dest}: {exc}")
        return

    local = Path(entry["source"])
    try:
        local.unlink(missing_ok=True)
    except OSError as exc:
        entry["cleanup"] = {"remote": remote, "local": "failed", "error": str(exc)}
        progress(f"保留ZIP削除失敗: {local}: {exc}")
        return

    entry["cleanup"] = {"remote": remote, "local": "deleted"}
    cache_key = json.dumps([server, dest], ensure_ascii=False)
    if cache.pop(cache_key, None) is not None:
        _remember_send_cache(work, cache)
    progress(f"保留ZIP削除完了: {dest} (out=削除, FTP={remote})")

def current_mra_requirements(db, work, mra_dir=None, mra_categories=None, cancel_requested=None):
    """Load Run's requirements only when they match the current MRA catalog."""
    path = work / "send-manifest.json"
    if not path.is_file():
        return {}, {}, "Run is required before Send"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}, "Run is required to refresh the send manifest"
    catalog = [[row["relative_path"], row["file_sha1"]] for row in db.execute(
        "SELECT relative_path,file_sha1 FROM mras WHERE status IN ('parsed','corrected') ORDER BY relative_path")]
    if manifest.get("catalog") != catalog:
        return {}, {}, "MRA catalog changed; Run is required before Send"
    if mra_dir is not None and not mra_files_match_catalog(
            db, Path(mra_dir), frozenset(mra_categories or ()), cancel_requested):
        return {}, {}, "MRA files changed since Run; Run is required before Send"
    return manifest["required"], manifest["blocked"], None


def current_mra_exclusions(work):
    """MRAs left out of each ZIP because some of their ROM parts are not in the pool."""
    try:
        manifest = json.loads((work / "send-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    excluded = manifest.get("excluded")
    return excluded if isinstance(excluded, dict) else {}


def transfer(db, work, config, dry_run=True, skip_matching=False, require_mra_match=False,
             cancel_requested=None, progress=None, replace_different=False,
             mra_dir=None, mra_categories=None):
    progress = progress or (lambda message: None)
    check_cancel(cancel_requested)
    t={**config.get("mister",{}),**config.get("transfer",{})}
    if t.get("protocol","ftp").lower() != "ftp":
        raise ValueError("only FTP is supported")
    entries=[]
    member_needs = {}
    required, blocked, manifest_error = current_mra_requirements(
        db, work, mra_dir, mra_categories, cancel_requested) if require_mra_match else ({}, {}, None)
    exclusions = current_mra_exclusions(work) if require_mra_match and manifest_error is None else {}
    for p in sorted((work/"out").rglob("*.zip")) if (work/"out").exists() else []:
        check_cancel(cancel_requested)
        rel=p.relative_to(work/"out").as_posix()
        root=t.get("rom_root","/games/mame")
        if rel.startswith("sound/"):
            root=t.get("sound_root","/media/fat/_Arcade/sound")
            rel=rel[6:]
        elif rel.startswith("hbmame/"): root=t.get("hbmame_root","/games/hbmame"); rel=rel[7:]
        dest=root.rstrip("/")+"/"+rel
        entry={"source":str(p),"destination":dest,"size":p.stat().st_size,
               "sha1":digest_file(p,cancel_requested=cancel_requested),"collision":"unchecked"}
        if require_mra_match:
            target = canonical_zip(p.relative_to(work/"out").as_posix())
            reason = manifest_error or blocked.get(target)
            if reason is None and target not in required:
                reason = "no current MRA requires this ZIP"
            if reason is None:
                try:
                    verify_zip(p, required[target],cancel_requested)
                except (ValueError, OSError, zipfile.BadZipFile) as exc:
                    reason = f"current MRA mismatch: {exc}"
            if reason is not None:
                entry.update({"collision":"local-mra-mismatch", "action":"conflict", "reason":reason})
            else:
                if exclusions.get(target):
                    entry["excluded_mras"] = exclusions[target]
                if required.get(target):
                    member_needs[dest] = (required[target], _members_signature(required[target]))
        entries.append(entry)
    check_cancel(cancel_requested)
    if require_mra_match and manifest_error is not None and entries:
        # The send manifest is missing or stale (for example Run was cancelled).  That says
        # nothing about the individual ZIPs, so do not connect and do not delete anything.
        return {"dry_run":dry_run,"entries":entries,"connection_configured":bool(t.get("host")),
                "blocked_reason":manifest_error}
    if not entries:
        return {"dry_run":dry_run,"entries":entries,"connection_configured":bool(t.get("host"))}
    if dry_run and not t.get("host"):
        return {"dry_run":True,"entries":entries,"connection_configured":False,
                "note":"remote collision check unavailable without connection configuration"}
    if not t.get("host"): raise ValueError("transfer host is not configured")
    password=t.get("password") or os.environ.get(t.get("password_env","MISTER_FTP_PASSWORD"),"")
    cache = _load_send_cache(work) if not dry_run else {}
    server = f"{t['host']}:{int(t.get('port', 21))}:{t.get('username', 'anonymous')}"
    with ftplib.FTP() as ftp:
        ftp.connect(t["host"],int(t.get("port",21)),timeout=30)
        ftp.login(t.get("username","anonymous"),password)
        ftp.voidcmd("TYPE I")
        listings = {}
        current_dir = None
        for e in entries:
            check_cancel(cancel_requested)
            if e.get("action") == "conflict":
                if not dry_run:
                    _delete_held_entry(ftp, e, work, cache, server, progress, cancel_requested)
                    ftp.voidcmd("TYPE I")
                    listings.pop(e["destination"].rsplit("/", 1)[0], None)
                    current_dir = None
                continue
            dest=e["destination"]
            progress(f"送信先を確認中: {dest}")
            parent,name=dest.rsplit("/",1)
            if current_dir != parent or parent not in listings:
                folder_exists = True
                if dry_run:
                    # A dry run must not create anything on the MiSTer.
                    try:
                        ftp.cwd(parent)
                    except ftplib.error_perm:
                        folder_exists = False
                else:
                    _ensure_remote_directory(ftp, parent)
                current_dir = parent
                if parent not in listings:
                    # One listing per folder is enough; Send keeps it current as it changes files.
                    listings[parent] = ({PurePosixPath(x).name for x in ftp.nlst()}
                                        if folder_exists else set())
                    ftp.voidcmd("TYPE I")
            names = listings[parent]
            e["collision"]="existing" if name in names else "none"
            temp=name+".uploading"
            cache_key = json.dumps([server, dest], ensure_ascii=False)
            if e["collision"]=="existing" and skip_matching:
                needed = member_needs.get(dest)
                remote_size = ftp.size(name)
                remote_mdtm = _remote_mdtm(ftp, name)
                cached = cache.get(cache_key)
                if (remote_mdtm is not None and isinstance(cached, dict)
                        and cached.get("mdtm") == remote_mdtm and cached.get("size") == remote_size):
                    if remote_size == e["size"] and cached.get("sha1") == e["sha1"]:
                        e["collision"]="matching"
                        e["match_source"]="send_cache"
                    elif needed and cached.get("members") == needed[1]:
                        e["collision"]="satisfied"
                        e["match_source"]="send_cache"
                if e["collision"] == "existing":
                    if remote_size != e["size"] and not needed:
                        e["collision"]="different"
                    else:
                        remote_hash=hashlib.sha1()
                        with tempfile.TemporaryFile(dir=work) as downloaded:
                            def hash_remote(block):
                                check_cancel(cancel_requested)
                                remote_hash.update(block)
                                downloaded.write(block)
                            ftp.retrbinary("RETR "+name,hash_remote,blocksize=CHUNK)
                            remote_sha1 = remote_hash.hexdigest()
                            if remote_sha1 == e["sha1"]:
                                e["collision"]="matching"
                            elif needed:
                                downloaded.seek(0)
                                try:
                                    verify_zip(downloaded, needed[0], cancel_requested)
                                    e["collision"]="satisfied"
                                except (ValueError, OSError, zipfile.BadZipFile):
                                    e["collision"]="different"
                            else:
                                e["collision"]="different"
                        if (e["collision"] in ("matching", "satisfied") and remote_mdtm is not None
                                and _remote_mdtm(ftp, name) == remote_mdtm):
                            cache[cache_key] = {"size":remote_size, "sha1":remote_sha1, "mdtm":remote_mdtm}
                            if e["collision"] == "satisfied":
                                cache[cache_key]["members"] = needed[1]
                            if not dry_run: _remember_send_cache(work, cache)
                if e["collision"] == "different" and cache_key in cache:
                    del cache[cache_key]
                    if not dry_run: _remember_send_cache(work, cache)
            if temp in names: e["stale_temp"]=True
            replacing = replace_different and e["collision"]=="different"
            if replacing: e["will_replace"]=True
            if dry_run: continue
            if e["collision"]=="matching":
                e["action"]="already_present"
                continue
            if e["collision"]=="satisfied":
                e["action"]="already_satisfied"
                continue
            if skip_matching and e["collision"]=="different" and not replacing:
                e["action"]="conflict"
                continue
            if e["collision"]!="none" and not replacing:
                raise ValueError(f"remote collision: {dest}: {e['collision']}")
            if e.get("stale_temp"):
                # An interrupted earlier upload of ours; the real ZIP is never touched by it.
                with contextlib.suppress(ftplib.all_errors):
                    ftp.delete(temp)
                names.discard(temp)
                progress(f"中断された一時ファイルを削除: {temp}")
            try:
                progress(f"ZIP送信中: {dest}")
                with open(e["source"],"rb") as f:
                    ftp.storbinary("STOR "+temp,CancellableReader(f,cancel_requested),blocksize=CHUNK)
                check_cancel(cancel_requested)
                if ftp.size(temp)!=e["size"]: raise ValueError("remote size mismatch")
                h=hashlib.sha1()
                progress(f"送信ZIP検証中: {dest}")
                def hash_uploaded(block):
                    check_cancel(cancel_requested)
                    h.update(block)
                ftp.retrbinary("RETR "+temp,hash_uploaded,blocksize=CHUNK)
                if h.hexdigest()!=e["sha1"]: raise ValueError("remote content mismatch")
                check_cancel(cancel_requested)
                if replacing:
                    _replace_remote(ftp, temp, name, progress)
                    e["action"]="replaced"
                else:
                    ftp.rename(temp,name)
                    e["action"]="uploaded"
                    spare = name + ".replaced"
                    if spare in names:  # left by an interrupted replacement
                        with contextlib.suppress(ftplib.all_errors):
                            ftp.delete(spare)
                        names.discard(spare)
                names.discard(temp)
                names.add(name)
                remote_mdtm = _remote_mdtm(ftp, name)
                if remote_mdtm is not None:
                    cache[cache_key] = {"size":e["size"], "sha1":e["sha1"], "mdtm":remote_mdtm}
                    _remember_send_cache(work, cache)
                progress(f"ZIP送信完了: {dest}")
            except Exception:
                with contextlib.suppress(Exception): ftp.delete(temp)
                raise
    return {"dry_run":dry_run,"entries":entries,"connection_configured":True}
