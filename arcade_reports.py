"""Catalog and missing-ROM reports."""
from __future__ import annotations
from collections import Counter
import json
from pathlib import Path
import re
import zipfile
from arcade_common import canonical_zip, safe_name
from arcade_pool import OriginIndex, is_external, output_zip_for_part, part_rows, plans_for_mra, status

def list_mras(db, needle="", only=None):
    origin_index = OriginIndex(db)
    output=[]
    for mra in db.execute("SELECT * FROM mras ORDER BY relative_path"):
        if needle and needle.lower() not in mra["relative_path"].lower(): continue
        plans=plans_for_mra(db,mra,origin_index) if mra["status"] in ("parsed","corrected") else []
        for p in plans:
            b=db.execute("SELECT md5_status FROM builds WHERE mra_id=? AND rom_id=? LIMIT 1",(mra["id"],p["rom_id"])).fetchone()
            if b: p["md5_status"]=b["md5_status"]
        states=[status(p,True) for p in plans]
        state=("parse-"+mra["status"]) if mra["status"] not in ("parsed","corrected") else (" / ".join(states) or "embedded-only")
        if only and only not in state: continue
        output.append({"mra":mra["relative_path"],"status":state,"plans":plans})
    return output

def missing_report(db, needle="", origin_index=None, plan_cache=None):
    """Report unresolved parts and the state of each ZIP search candidate."""
    origin_index = origin_index or OriginIndex(db)
    output = []
    for mra in db.execute("SELECT * FROM mras ORDER BY relative_path"):
        if needle and not any(needle.lower() in str(mra[key] or "").lower()
                              for key in ("relative_path", "title", "setname")):
            continue
        if mra["status"] not in ("parsed", "corrected"):
            output.append({"mra":mra["relative_path"], "status":"parse-"+mra["status"],
                           "error":mra["error"], "roms":[]})
            continue
        roms = []
        plans = plan_cache[mra["id"]] if plan_cache is not None and mra["id"] in plan_cache else plans_for_mra(db,mra,origin_index)
        for plan in plans:
            if not (plan["missing"] or plan["ambiguous"] or plan["unsupported"]):
                continue
            missing = []
            for item in plan["missing"]:
                candidates = []
                for z in (x.strip() for x in item["zip"].split("|") if x.strip()):
                    registered = list(origin_index.registered_archives(z))
                    candidates.append({"zip":z,
                                       "state":"registered-no-match" if registered else "not-registered",
                                       "registered_archives":registered})
                missing.append({"name":item["name"], "crc32":item["crc32"],
                                "zip_candidates":candidates})
            roms.append({"rom_index":plan["rom_index"],"zip_spec":plan["zip_spec"],
                         "alternatives":plan["alternatives"],"missing":missing,
                         "ambiguous":plan["ambiguous"],"unsupported":plan["unsupported"]})
        if roms:
            output.append({"mra":mra["relative_path"],"status":"unresolved","roms":roms})
    return output

def format_missing_report(report):
    if not report: return "不足はありません（指定範囲の解析済みMRA）。"
    lines = []
    for entry in report:
        lines.append(f"[{entry['mra']}] {entry['status']}")
        if entry["status"].startswith("parse-"):
            lines.append(f"  解析エラー: {entry['error']}")
            continue
        for rom in entry["roms"]:
            suffix = f"（代替構成 {rom['alternatives']}件から選択）" if rom["alternatives"] > 1 else ""
            lines.append(f"  ROM index={rom['rom_index']} {suffix}".rstrip())
            for item in rom["missing"]:
                label = item["name"] or "名前なし"
                crc = item["crc32"] or "CRC指定なし"
                lines.append(f"    不足: {label}  CRC={crc}")
                if item["zip_candidates"]:
                    desc = " | ".join(f"{z['zip']}（{'登録済み・該当ファイルなし' if z['state']=='registered-no-match' else 'プールに未登録'}）"
                                      for z in item["zip_candidates"])
                    lines.append(f"      探索先ZIP（左から順に検索）: {desc}")
            for item in rom["ambiguous"]:
                lines.append(f"    候補が複数: {item.get('name') or item.get('reason')}  CRC={item.get('crc32') or '指定なし'}")
            for item in rom["unsupported"]:
                lines.append(f"    未対応: {item.get('name') or item.get('reason')}  {item.get('reason','')}")
    lines.append("ZIPの状態は投入済みプールの記録に基づきます。A.zip|B.zipは代替検索先で、両方が必須という意味ではありません。")
    return "\n".join(lines)

def missing_zip_names(report):
    """List ZIP search candidates with no members registered in the pool."""
    names = {}
    for entry in report:
        for rom in entry.get("roms", []):
            for part in rom.get("missing", []):
                for candidate in part.get("zip_candidates", []):
                    if candidate.get("state") == "not-registered":
                        name = candidate["zip"]
                        names.setdefault(canonical_zip(name), name)
    return sorted(names.values(), key=str.casefold)

def missing_output_zips(db, work: Path, origin_index=None, plan_cache=None):
    """List preferred MRA ZIP names that are absent from the local output folder."""
    origin_index = origin_index or OriginIndex(db)
    expected = set()
    for mra in db.execute("SELECT * FROM mras WHERE status IN ('parsed','corrected')"):
        plans = plan_cache[mra["id"]] if plan_cache is not None and mra["id"] in plan_cache else plans_for_mra(db,mra,origin_index)
        for plan in plans:
            for part in part_rows(db,plan["rom_id"]):
                if not is_external(part):
                    continue
                item = next((entry for entry in plan["selected_parts"]
                             if entry["part"] == part["id"]), None)
                if item is None:
                    continue
                target = output_zip_for_part(db, part, item)
                if target and safe_name(target):
                    expected.add(canonical_zip(target))
    return sorted((name for name in expected if not (work / "out" / name).is_file()),key=str.casefold)

def missing_output_causes(db, absent, build_result, plan_cache):
    """Explain absent output names using the plans and errors from this Run."""
    absent_set = set(absent)
    references = {name: [] for name in absent}
    errors = {item["mra"]: item["error"] for item in build_result["errors"]}
    for mra in db.execute("SELECT id,relative_path FROM mras WHERE status IN ('parsed','corrected')"):
        plans = plan_cache[mra["id"]]
        targets = set()
        for plan in plans:
            for part in part_rows(db, plan["rom_id"]):
                if is_external(part):
                    item = next((entry for entry in plan["selected_parts"]
                                 if entry["part"] == part["id"]), None)
                    if item is None:
                        continue
                    target = output_zip_for_part(db, part, item)
                    if target in absent_set:
                        targets.add(target)
        if not targets:
            continue
        missing = [item for plan in plans for item in plan["missing"]]
        ambiguous = [item for plan in plans for item in plan["ambiguous"]]
        unsupported = [item for plan in plans for item in plan["unsupported"]]
        for target in targets:
            references[target].append((mra["relative_path"], missing, ambiguous,
                                       unsupported, errors.get(mra["relative_path"])))

    output = []
    for name in absent:
        candidates = references[name]
        failed = [ref for ref in candidates if ref[4]]
        if failed:
            mra, _, _, _, error = min(failed, key=lambda ref: ("existing output differs" not in ref[4], ref[0]))
            output.append({"zip": name, "reason": "build error", "mra": mra, "detail": error})
            continue
        unresolved = [ref for ref in candidates if ref[1] or ref[2] or ref[3]]
        if unresolved:
            mra, missing, ambiguous, unsupported, _ = min(
                unresolved, key=lambda ref: (len(ref[1]) + len(ref[2]) + len(ref[3]), ref[0]))
            if missing:
                examples = ", ".join(
                    f"{item['name'] or '(unnamed)'} (CRC {item['crc32'] or 'none'}; {item['zip']})"
                    for item in missing[:3])
                reason, detail = "missing ROM parts", f"{len(missing)} missing; examples: {examples}"
            elif ambiguous:
                examples = ", ".join(item.get("name") or item.get("reason", "unknown")
                                     for item in ambiguous[:3])
                reason, detail = "ambiguous ROM parts", f"{len(ambiguous)} ambiguous; examples: {examples}"
            else:
                reason = "unsupported ROM parts"
                detail = f"{len(unsupported)} unsupported; first: {unsupported[0].get('reason', 'unknown')}"
            output.append({"zip": name, "reason": reason, "mra": mra, "detail": detail})
            continue
        output.append({"zip": name, "reason": "unexplained", "mra": candidates[0][0] if candidates else "",
                       "detail": "No unresolved plan or build error was recorded"})
    return output

def existing_output_diagnostics(db, work: Path, causes, plan_cache, bios_zips=None):
    """Compare shared BIOS and ZIPs blocking absent outputs with MRA requirements."""
    blocked = {name: work / "out" / name for name in (bios_zips or ())}
    for item in causes:
        match = re.fullmatch(r"existing output differs: (.+\.zip): .+", item["detail"])
        if match:
            path = Path(match[1])
            try:
                name = canonical_zip(path.resolve().relative_to((work / "out").resolve()).as_posix())
            except ValueError:
                name = canonical_zip(path.name)
            if safe_name(name):
                blocked[name] = work / "out" / name
    if not blocked:
        return {}

    requirements = {name: {} for name in blocked}
    for mra in db.execute("SELECT id,relative_path FROM mras WHERE status IN ('parsed','corrected')"):
        for plan in plan_cache[mra["id"]]:
            selected = {item["part"]: item for item in plan["selected_parts"]}
            for part in part_rows(db, plan["rom_id"]):
                if not is_external(part):
                    continue
                item = selected.get(part["id"])
                target = output_zip_for_part(db, part, item)
                if target not in blocked:
                    continue
                name = part["name"]
                if not name or name.lower() == "none":
                    name = item.get("name") if item else None
                if not name:
                    continue
                key = (name, part["crc32"], item.get("sha1") if item else None)
                requirements[target].setdefault(mra["relative_path"], {})[key] = {
                    "crc32": part["crc32"], "size": item.get("size") if item else None,
                    "sha1": item.get("sha1") if item else None}

    diagnostics = {}
    for target, path in blocked.items():
        if not requirements[target]:
            continue
        actual = []
        try:
            with zipfile.ZipFile(path) as archive:
                actual = [{"name":item.filename, "crc32":f"{item.CRC:08x}", "size":item.file_size}
                          for item in archive.infolist()]
        except (OSError, zipfile.BadZipFile):
            pass
        by_mra = requirements[target]
        variants = Counter(tuple(sorted((name, item["crc32"]) for (name, _, _), item in members.items()))
                           for members in by_mra.values())
        catalog = {}
        for members in by_mra.values():
            for (name, _, _), item in members.items():
                key = (name, item["crc32"], item["sha1"], item["size"])
                catalog[key] = catalog.get(key, 0) + 1
        diagnostics[target] = {"actual": actual, "by_mra": by_mra,
                               "variants": variants, "catalog": catalog}
    return diagnostics

def format_missing_output_causes(causes, diagnostics=None):
    diagnostics = diagnostics or {}
    lines = [f"Output ZIP names absent from out: {len(causes)}", ""]
    for item in causes:
        lines.append(f"{item['zip']} | {item['reason']} | MRA: {item['mra']}")
        lines.append(f"  {item['detail']}")
        match = re.fullmatch(r"existing output differs: (.+\.zip): .+", item["detail"])
        target = canonical_zip(Path(match[1]).name) if match else None
        if target in diagnostics:
            diagnostic = diagnostics[target]
            expected = diagnostic["by_mra"].get(item["mra"], {})
            actual = diagnostic["actual"]
            expected_names = {key[0] for key in expected}
            actual_names = {item["name"] for item in actual}
            lines.append(f"  Required {target} members for this MRA: {', '.join(sorted(expected_names)) or '(none)'}")
            lines.append(f"  Current {target} members: {', '.join(sorted(actual_names)) or '(none)'}")
            lines.append(f"  Missing names in current ZIP: {', '.join(sorted(expected_names - actual_names)) or '(none)'}")
    if diagnostics:
        lines.extend(["", "Existing output ZIP requirements across selected MRAs:"])
        for target, diagnostic in sorted(diagnostics.items()):
            lines.append(f"{target} | referenced by {len(diagnostic['by_mra'])} MRAs")
            for (name, crc, sha, size), count in sorted(
                    diagnostic["catalog"].items(),
                    key=lambda entry: (entry[0][0], entry[0][1] or "", entry[0][2] or "",
                                       entry[0][3] if entry[0][3] is not None else -1)):
                lines.append(f"  Required member: {name} | CRC32 {crc or 'unknown'} | "
                             f"SHA1 {sha or 'unresolved'} | size {size if size is not None else 'unknown'} | "
                             f"MRAs {count}")
            lines.append(f"  Current members: {', '.join(sorted({item['name'] for item in diagnostic['actual']})) or '(none)'}")
            for item in sorted(diagnostic["actual"], key=lambda x: (x["name"].casefold(), x["crc32"])):
                lines.append(f"  Current member: {item['name']} | CRC32 {item['crc32']} | size {item['size']}")
            for members, count in sorted(diagnostic["variants"].items(),
                                         key=lambda entry: (-entry[1], str(entry[0]))):
                lines.append(f"  Required variant ({count} {'MRA' if count == 1 else 'MRAs'}): "
                             + (", ".join(name for name, _ in members) or "(none)"))
            if len(diagnostic["variants"]) > 1:
                lines.append("  Note: Required member sets vary by MRA; one ZIP can satisfy them when it contains every required CRC/content variant.")
    return "\n".join(lines) + "\n"

def missing_overview(work: Path):
    """Group the ROMs missing from the latest Run by output ZIP, then by MRA.

    Reads send-manifest.json, which Run rewrites.  A ZIP is "built" when at least one
    MRA that needs it is complete; the excluded MRAs are the ones still missing parts.
    """
    try:
        manifest = json.loads((work / "send-manifest.json").read_text(encoding="utf-8-sig"))
        excluded = manifest.get("excluded") or {}
        required = manifest.get("required") or {}
    except (OSError, ValueError, AttributeError):
        return {"zips": [], "mra_count": 0, "part_count": 0}
    zips = []
    for target, items in excluded.items():
        mras = [{"mra": item["mra"], "missing": list(item["missing"])} for item in items]
        zips.append({"zip": target, "built": target in required,
                     "mras": sorted(mras, key=lambda x: x["mra"].casefold())})
    zips.sort(key=lambda x: (x["built"], x["zip"].casefold()))
    return {"zips": zips,
            "mra_count": sum(len(z["mras"]) for z in zips),
            "part_count": sum(len(m["missing"]) for z in zips for m in z["mras"])}
