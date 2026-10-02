"""Build and verify MiSTer ROM ZIP files from matched pool content."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import zipfile
import zlib
from arcade_common import CHUNK, canonical_zip, check_cancel, digest_file, safe_name
from arcade_pool import OriginIndex, is_external, output_zip_for_part, part_rows, plans_for_mra
from arcade_store import BlobStore

def _member_items(members):
    """Normalize legacy name maps and duplicate-name-capable member lists."""
    if isinstance(members, dict):
        result = []
        for name, item in members.items():
            if "name" not in item:
                item = {**item, "name": name}
            result.append(item)
        return result
    return list(members)

def _member_union(target, item):
    """Add one ROM to a target ZIP, preserving same-name CRC variants."""
    bucket = target.setdefault(item["name"], {})
    lookup = item.get("lookup_crc32") or "by-name"
    for prior in bucket.values():
        if (prior.get("lookup_crc32") or "by-name") == lookup and prior["sha1"] != item["sha1"]:
            raise ValueError(f"same MRA lookup key has conflicting content: {item['name']} CRC {lookup}")
    bucket[(lookup, item["sha1"])] = item

def shared_bios_members(db, mras, plan_cache, bios_zips=None):
    """Collect only parts actually found in a BIOS input ZIP."""
    members = {name: {} for name in (bios_zips or ())}
    errors = {}
    for mra in mras:
        for plan in plan_cache[mra["id"]]:
            selected = {item["part"]: item for item in plan["selected_parts"]}
            for part in part_rows(db, plan["rom_id"]):
                if not is_external(part):
                    continue
                item = selected.get(part["id"])
                if item is None:
                    continue
                target = output_zip_for_part(db, part, item)
                if target not in members or not safe_name(target):
                    continue
                try:
                    _member_union(members[target], item)
                except ValueError as exc:
                    errors[target] = str(exc)
    return {name: [item for variants in content.values() for item in variants.values()]
            for name, content in members.items() if content and name not in errors}, errors

def prepare_shared_bios(work, target, members, progress, cancel_requested=None, exact=False, store=None):
    """Write a verified ROM union, preserving replaced output as a backup."""
    if store is None:
        with BlobStore(None, work) as own_store:
            return prepare_shared_bios(work, target, members, progress, cancel_requested, exact, own_store)
    members = _member_items(members)
    if not safe_name(target):
        raise ValueError("unsafe ZIP output path")
    check_cancel(cancel_requested)
    dest = work / "out" / target
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        try:
            verify_zip(dest, members, cancel_requested, exact=exact)
            return "already_present", str(dest)
        except (ValueError, zipfile.BadZipFile):
            pass
    progress(f"ZIP作成中: {target} (MRA ROM union)")
    fd, temporary = tempfile.mkstemp(prefix="bios-", suffix=".zip", dir=dest.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for item in sorted(members, key=lambda x: (x["name"].casefold(), x["crc32"], x["sha1"])):
                check_cancel(cancel_requested)
                with store.open(item["sha1"]) as source, archive.open(item["name"], "w") as output:
                    while block := source.read(CHUNK):
                        check_cancel(cancel_requested)
                        output.write(block)
        progress(f"ZIP検証中: {target} (MRA ROM union)")
        verify_zip(Path(temporary), members, cancel_requested, exact=True)
        check_cancel(cancel_requested)
        if dest.exists():
            backup_dir = work / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"{dest.stem}-{digest_file(dest, cancel_requested=cancel_requested)}.zip"
            if not backup.exists():
                backup_fd, backup_temp = tempfile.mkstemp(prefix="bios-backup-", suffix=".tmp", dir=backup_dir)
                os.close(backup_fd)
                try:
                    shutil.copy2(dest, backup_temp)
                    os.replace(backup_temp, backup)
                finally:
                    with contextlib.suppress(OSError):
                        os.unlink(backup_temp)
        os.replace(temporary, dest)
        progress(f"ZIP完成: {target} (MRA ROM union)")
        return "built", str(dest)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)

def literal_bytes(part):
    data = re.sub(r"\s+", "", part["text_data"] or "")
    if not data: return b""
    if not re.fullmatch(r"[0-9a-fA-F]*",data) or len(data)%2: raise ValueError("invalid embedded hex")
    raw = bytes.fromhex(data)
    repeat = json.loads(part["attrs"]).get("repeat")
    return raw * (int(repeat,0) if repeat else 1)

def assembled_md5(db, work, rom_id, plan, cancel_requested=None, store=None):
    if store is None:
        with BlobStore(db, work) as own_store:
            return assembled_md5(db, work, rom_id, plan, cancel_requested, own_store)
    expected = plan["md5_expected"]
    if not expected or expected.lower() == "none": return "disabled",None
    parts = part_rows(db,rom_id)
    nodes = db.execute("SELECT * FROM nodes WHERE rom_id=?",(rom_id,)).fetchall()
    if any(n["tag"] in ("interleave","patch") for n in nodes): return "unsupported-interleave-or-patch",None
    selected = {x["part"]:x for x in plan["selected_parts"]}
    h = hashlib.md5()
    for part in parts:
        check_cancel(cancel_requested)
        if is_external(part):
            item = selected[part["id"]]
            attrs = json.loads(part["attrs"])
            offset = int(attrs.get("offset","0"),0)
            length = int(attrs["length"],0) if attrs.get("length") else None
            repeat = int(attrs.get("repeat","1"),0)
            with store.open(item["sha1"]) as f:
                for _ in range(repeat):
                    f.seek(offset)
                    remaining = length
                    while remaining is None or remaining > 0:
                        check_cancel(cancel_requested)
                        block = f.read(CHUNK if remaining is None else min(CHUNK,remaining))
                        if not block: break
                        h.update(block)
                        if remaining is not None: remaining -= len(block)
        else: h.update(literal_bytes(part))
    actual = h.hexdigest()
    return ("matched" if actual.lower() == expected.lower() else "mismatch"),actual

def build(db, work, mra_name, skip_existing=False, plan_cache=None, cross_claims=None,
          progress=None, cancel_requested=None, origin_index=None,
          shared_outputs=None, shared_errors=None, store=None):
    if store is None:
        with BlobStore(db, work) as own_store:
            return build(db, work, mra_name, skip_existing, plan_cache, cross_claims, progress,
                         cancel_requested, origin_index, shared_outputs, shared_errors, own_store)
    return _build(db, work, mra_name, skip_existing, plan_cache, cross_claims, progress,
                  cancel_requested, origin_index, shared_outputs, shared_errors, store)

def _build(db, work, mra_name, skip_existing, plan_cache, cross_claims, progress,
           cancel_requested, origin_index, shared_outputs, shared_errors, store):
    progress = progress or (lambda message: None)
    check_cancel(cancel_requested)
    rows = db.execute("SELECT * FROM mras WHERE relative_path=? OR title=? OR setname=? OR relative_path LIKE ?",
                      (mra_name,mra_name,mra_name,"%/"+mra_name)).fetchall()
    if len(rows) != 1: raise ValueError(f"MRA selection matched {len(rows)} rows")
    mra = rows[0]
    if mra["status"] not in ("parsed","corrected"): raise ValueError("MRA is not parsed")
    if plan_cache is None:
        origin_index = origin_index or OriginIndex(db,cancel_requested)
    plans = plan_cache[mra["id"]] if plan_cache is not None else plans_for_mra(db,mra,origin_index)
    ready = [p for p in plans if not (p["missing"] or p["ambiguous"] or p["unsupported"])]
    # Plans are persisted before writing files and include the exact origin of every member.
    with db:
        for p in plans:
            for zip_name in [x.strip() for x in p["zip_spec"].split("|") if x.strip()]:
                db.execute("INSERT OR REPLACE INTO builds VALUES(?,?,?,?,?)",(mra["id"],p["rom_id"],zip_name,json.dumps(p),"not-built"))
    if not ready: return {"mra":mra["relative_path"],"built":[],"plans":plans}
    output_claims = {}
    for p in ready:
        for item in p["selected_parts"]:
            part = db.execute("SELECT * FROM parts WHERE id=?",(item["part"],)).fetchone()
            target = output_zip_for_part(db, part, item)
            if not target or not safe_name(target): raise ValueError("unsafe ZIP output path")
            key = (target,item["name"],item["sha1"])
            output_claims[key] = item
    for target, _, _ in output_claims:
        if target in (shared_errors or {}):
            raise ValueError(shared_errors[target])
    # Same-name ROM variants are valid when their CRCs differ; the MRA loader
    # selects the required bytes by CRC. build_ready supplies a union across MRAs.
    # A real MRA MD5 mismatch means the chosen bytes are wrong. Leave no completed ZIP.
    progress(f"MRAのハッシュ検証中: {mra['relative_path']}")
    for p in ready:
        p["md5_status"],p["md5_actual"] = assembled_md5(db,work,p["rom_id"],p,cancel_requested,store)
        if p["md5_status"] == "mismatch":
            raise ValueError(f"assembled MD5 mismatch for rom {p['rom_id']}: {p['md5_actual']}")
    outputs = {}
    for (target,_,_),item in output_claims.items(): outputs.setdefault(target,[]).append(item)
    for target in outputs:
        if target in (shared_outputs or {}):
            outputs[target] = shared_outputs[target]
    built = []
    already_present = []
    staged = []
    out_dir = work/"out"
    try:
        for target,members in outputs.items():
            check_cancel(cancel_requested)
            dest = out_dir/target
            dest.parent.mkdir(parents=True,exist_ok=True)
            if dest.exists():
                if not skip_existing: raise ValueError(f"output exists: {dest}")
                progress(f"既存ZIP検証中: {target} ({mra['relative_path']})")
                try:
                    verify_zip(dest,members,cancel_requested)
                except (ValueError,zipfile.BadZipFile) as exc:
                    raise ValueError(f"existing output differs: {dest}: {exc}") from exc
                already_present.append(str(dest))
                progress(f"既存ZIP再利用: {target} ({mra['relative_path']})")
                continue
            progress(f"ZIP作成中: {target} ({mra['relative_path']})")
            fd,tmp = tempfile.mkstemp(prefix="build-",suffix=".zip",dir=dest.parent); os.close(fd)
            staged.append((Path(tmp),dest))
            with zipfile.ZipFile(tmp,"w",compression=zipfile.ZIP_STORED) as z:
                for item in sorted(_member_items(members), key=lambda x: (x["name"].casefold(), x["crc32"], x["sha1"])):
                    with store.open(item["sha1"]) as src,z.open(item["name"],"w") as dst:
                        while block := src.read(CHUNK):
                            check_cancel(cancel_requested)
                            dst.write(block)
            progress(f"ZIP検証中: {target} ({mra['relative_path']})")
            verify_zip(Path(tmp),members,cancel_requested)
        check_cancel(cancel_requested)
        for tmp,dest in staged:
            os.replace(tmp,dest)
            built.append(str(dest))
            progress(f"ZIP完成: {dest.relative_to(out_dir).as_posix()} ({mra['relative_path']})")
    except Exception:
        for path in built:
            with contextlib.suppress(OSError): Path(path).unlink()
        raise
    finally:
        for tmp,_ in staged:
            with contextlib.suppress(OSError): tmp.unlink()
    for p in ready:
        with db:
            for zname in [x.strip() for x in p["zip_spec"].split("|") if x.strip()]:
                db.execute("UPDATE builds SET plan=?,md5_status=? WHERE mra_id=? AND rom_id=? AND zip_name=?",
                           (json.dumps(p),p["md5_status"],mra["id"],p["rom_id"],zname))
    return {"mra":mra["relative_path"],"built":built,"already_present":already_present,"plans":plans}

def build_ready(db, work, progress=None, cancel_requested=None, origin_index=None,
                plan_cache=None, bios_zips=None):
    """Build MRA titles whose every external ROM plan is ready."""
    with BlobStore(db, work) as store:
        return _build_ready(db, work, progress, cancel_requested, origin_index,
                            plan_cache, bios_zips, store)

def _build_ready(db, work, progress, cancel_requested, origin_index, plan_cache, bios_zips, store):
    progress = progress or (lambda message: None)
    result = {"ready_titles":0,"built":[],"already_present":[],"errors":[]}
    mras = db.execute("SELECT * FROM mras WHERE status IN ('parsed','corrected') ORDER BY relative_path").fetchall()
    origin_index = origin_index or OriginIndex(db,cancel_requested)
    plan_cache = plan_cache if plan_cache is not None else {}
    for index, mra in enumerate(mras, 1):
        check_cancel(cancel_requested)
        progress(f"MRA照合中 ({index}/{len(mras)}): {mra['relative_path']}")
        if mra["id"] not in plan_cache:
            plan_cache[mra["id"]] = plans_for_mra(db,mra,origin_index)
    if mras:
        progress(f"MRA照合完了 ({len(mras)}/{len(mras)}): {len(mras)}件")
    ready_mras = [mra for mra in mras if plan_cache[mra["id"]] and
                  not any(p["missing"] or p["ambiguous"] or p["unsupported"]
                          for p in plan_cache[mra["id"]])]
    # Build each output ZIP from the union of the selected MRA parts that were
    # actually found in that source ZIP. ZIP search alternatives are not all
    # destinations, and a BIOS candidate is not proof that a part is BIOS data.
    shared_outputs = {}
    shared_errors = {}
    for mra in ready_mras:
        for plan in plan_cache[mra["id"]]:
            selected = {item["part"]: item for item in plan["selected_parts"]}
            for part in part_rows(db, plan["rom_id"]):
                item = selected.get(part["id"])
                if item is None:
                    continue
                target = output_zip_for_part(db, part, item)
                if target and safe_name(target):
                    try:
                        _member_union(shared_outputs.setdefault(target, {}), item)
                    except ValueError as exc:
                        shared_errors[target] = str(exc)
    bios_outputs, bios_errors = shared_bios_members(db, mras, plan_cache, bios_zips)
    shared_errors.update(bios_errors)
    # Include valid BIOS members from incomplete game plans as well, but only
    # when their actual source ZIP is the BIOS set.
    for target, members in bios_outputs.items():
        shared_outputs[target] = members
    for target in shared_errors:
        shared_outputs.pop(target, None)
    shared_outputs = {target: [item for variants in content.values() for item in variants.values()]
                      if isinstance(content, dict) else content
                      for target, content in shared_outputs.items()}
    result["bios_errors"] = {target: error for target, error in shared_errors.items()
                              if target in (bios_zips or ())}
    result["output_union_errors"] = dict(shared_errors)
    for target, members in shared_outputs.items():
        check_cancel(cancel_requested)
        try:
            outcome, path = prepare_shared_bios(work, target, members, progress, cancel_requested,
                                                exact=target in (bios_zips or ()), store=store)
            result[outcome].append(path)
        except (ValueError, OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
            shared_errors[target] = str(exc)
            result["output_union_errors"][target] = str(exc)
            if target in (bios_zips or ()):
                result["bios_errors"][target] = str(exc)
    result["bios_unions"] = sorted(set(bios_outputs) - set(shared_errors))
    for index, mra in enumerate(ready_mras, 1):
        check_cancel(cancel_requested)
        result["ready_titles"] += 1
        progress(f"ZIP処理中 ({index}/{len(ready_mras)}): {mra['relative_path']}")
        try:
            item = build(db,work,mra["relative_path"],skip_existing=True,
                         plan_cache=plan_cache,progress=progress,
                         cancel_requested=cancel_requested,shared_outputs=shared_outputs,
                         shared_errors=shared_errors,store=store)
            result["built"].extend(item["built"])
            result["already_present"].extend(item["already_present"])
        except (ValueError,OSError,sqlite3.Error,zipfile.BadZipFile) as exc:
            result["errors"].append({"mra":mra["relative_path"],"error":str(exc)})
            progress(f"ZIP生成エラー: {mra['relative_path']}: {exc}")
        progress(f"ZIP処理完了 ({index}/{len(ready_mras)}): {mra['relative_path']}")
    result["already_present"] = sorted(set(result["already_present"]) - set(result["built"]))
    check_cancel(cancel_requested)
    progress("送信用ZIPリストを更新しています")
    save_send_manifest(work, mras, plan_cache, db)
    return result

def save_send_manifest(work, mras, plan_cache, db):
    """Save the current MRA member requirements for a fast, safe Send check.

    Only MRAs that are fully resolved (the ones Run builds from) define what a ZIP
    must contain.  An MRA with an unresolved part is excluded instead of blocking the
    shared ZIP; its missing parts are recorded under "excluded" so Send can list them.
    """
    required = {}
    blocked = {}
    excluded = {}
    selectors = {}
    for mra in mras:
        plans = plan_cache[mra["id"]]
        is_ready = bool(plans) and not any(p["missing"] or p["ambiguous"] or p["unsupported"] for p in plans)
        for plan in plans:
            selected = {item["part"]: item for item in plan["selected_parts"]}
            for part in part_rows(db, plan["rom_id"]):
                if not is_external(part):
                    continue
                item = selected.get(part["id"])
                target = output_zip_for_part(db, part, item)
                if not target:
                    continue
                if not is_ready:
                    if item is None:
                        label = part["name"] or "(unnamed)"
                        if part["crc32"]:
                            label += f" (CRC {part['crc32']})"
                        excluded.setdefault(target, {}).setdefault(mra["relative_path"], [])
                        if label not in excluded[target][mra["relative_path"]]:
                            excluded[target][mra["relative_path"]].append(label)
                    continue
                members = required.setdefault(target, {})
                if item is None:
                    blocked[target] = "a required MRA part is unresolved"
                else:
                    selector = (item["name"], item.get("lookup_crc32") or "by-name")
                    prior = selectors.setdefault(target, {}).get(selector)
                    if prior and prior != item["sha1"]:
                        blocked[target] = "MRA lookup key resolves to different content"
                    selectors[target][selector] = item["sha1"]
                    key = (item["name"], item["sha1"])
                    members[key] = {"name": item["name"], **{
                        key: item[key] for key in ("sha1", "crc32", "size")}}
    manifest = {"catalog": [[mra["relative_path"], mra["file_sha1"]] for mra in mras],
                "required": {target: list(members.values()) for target, members in required.items()},
                "blocked": blocked,
                "excluded": {target: [{"mra": mra_name, "missing": names} for mra_name, names in by_mra.items()]
                             for target, by_mra in excluded.items()}}
    work.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="send-manifest-", suffix=".tmp", dir=work)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(manifest, output, ensure_ascii=False)
        os.replace(temp, work / "send-manifest.json")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temp)

def verify_zip(path, members, cancel_requested=None, exact=False):
    members = _member_items(members)
    check_cancel(cancel_requested)
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if exact:
            actual = sorted((info.filename, info.CRC, info.file_size) for info in infos)
            expected = sorted((item["name"], int(item["crc32"], 16), item["size"])
                              for item in members)
            if actual != expected:
                raise ValueError("ZIP member set mismatch")
        for item in members:
            name = item["name"]
            wanted_crc = item.get("crc32")
            candidates = [info for info in infos if
                          (wanted_crc and info.CRC == int(wanted_crc, 16)) or
                          (not wanted_crc and info.filename == name)]
            matched = False
            for info in candidates:
                h=hashlib.sha1(); crc=0; size=0
                with z.open(info) as f:
                    while block := f.read(CHUNK):
                        check_cancel(cancel_requested)
                        h.update(block); crc=zlib.crc32(block,crc); size+=len(block)
                if (h.hexdigest(),f"{crc & 0xffffffff:08x}",size)==(
                        item["sha1"],wanted_crc or f"{crc & 0xffffffff:08x}",item["size"]):
                    matched = True
                    break
            if not matched:
                raise ValueError(f"required ROM not found or mismatched: {name} (CRC {wanted_crc or 'none'})")
