"""Command-line interface and full run pipeline for MiSTer Arcade Auto Setup."""
from __future__ import annotations
import argparse
import ftplib  # Kept public for existing integrations that patch the FTP client.
import json
from pathlib import Path
import sqlite3
import sys
from arcade_common import (CHUNK, MAX_MEMBER, OperationCancelled, archive_matches,
                           canonical_zip, check_cancel, digest_file, normalize_crc, safe_name)
from arcade_mra import (FIXABLE, TAG, ALTERNATIVE_CATEGORIES, JAPAN_MARKER, MODIFIED_MARKER, OTHER_REGION_MARKER, VERSION_MARKER,
                        japan_zip_pair, japan_setname_parent, alternative_category, selected_categories, selected_mra,
                        connect, parse_tree, scan_mras, ftp_mra_files, sync_mras_ftp)
from arcade_pool import (OriginIndex, ingest, part_rows, is_external, choices, rom_plan,
                         plans_for_mra, output_zip_for_part, status)
from arcade_build import literal_bytes, assembled_md5, build, build_ready, verify_zip, set_output_compression
from arcade_bios import bios_zip_names
from arcade_store import BlobStore, pack_pool, verify_pool, rebuild_index
from arcade_reports import (list_mras, missing_report, format_missing_report,
                            missing_zip_names, missing_output_zips,
                            missing_output_causes, existing_output_diagnostics,
                            format_missing_output_causes)
from arcade_transfer import transfer

def run_pipeline(db, work: Path, mra_root: Path, rom_roots: list[Path], config=None,
                 progress=None, include_alternatives=False, alternative_categories=None,
                 cancel_requested=None, mame_dat=None):
    """One entry point for the GUI's create-ROM action."""
    progress_receiver = progress or (lambda message: None)
    progress_messages = []
    def progress(message):
        progress_messages.append(message)
        progress_receiver(message)
    manifest_path = work / "send-manifest.json"
    def stop_if_requested():
        try:
            check_cancel(cancel_requested)
        except OperationCancelled:
            manifest_path.unlink(missing_ok=True)
            raise
    set_output_compression(((config or {}).get("output") or {}).get("compression"))
    categories = selected_categories(include_alternatives, alternative_categories)
    for root in rom_roots:
        if not root.exists() or not (root.is_dir() or root.suffix.lower() == ".zip"):
            raise ValueError(f"ROM input must be a folder or ZIP file: {root}")
    dat_path = mame_dat or (config or {}).get("mame_dat")
    bios_zips = None
    if dat_path:
        dat_path = Path(dat_path).expanduser()
        progress(f"MAME DATを解析中: {dat_path}")
        bios_zips = bios_zip_names(dat_path, cancel_requested)
        progress(f"MAME DATのBIOSセット: {len(bios_zips)}件")
    stop_if_requested()
    manifest_path.unlink(missing_ok=True)
    progress("MiSTerのMRAを確認しています（標準版と選択した代替版）" if categories
             else "MiSTerの_Arcade直下のMRAを確認しています")
    sync = sync_mras_ftp(db,mra_root,config or {},progress,
                         alternative_categories=categories,cancel_requested=cancel_requested)
    stop_if_requested()
    if not mra_root.is_dir() or not any(mra_root.glob("*.mra")):
        raise ValueError(f"MRA folder is missing or contains no .mra files: {mra_root}; FTP={sync['status']}")
    progress("MRAを解析しています")
    parsed = scan_mras(db,mra_root,recursive=bool(categories),prune_unseen=True,
                       alternative_categories=categories,cancel_requested=cancel_requested)
    alternative_root = mra_root / "_alternatives"
    available_alternatives = sorted(alternative_root.rglob("*.mra")) if alternative_root.is_dir() else []
    classification = []
    for path in available_alternatives:
        stop_if_requested()
        relative = path.relative_to(mra_root).as_posix()
        raw = path.read_bytes()
        category = alternative_category(relative, raw)
        classification.append({"mra":relative,"category":category,
                               "selected":selected_mra(relative,categories,raw)})
    selection_path = work / "alternative-selection-report.json"
    selection_path.write_text(json.dumps({"selected_categories":sorted(categories),
                                          "files":classification},ensure_ascii=False,indent=2)+"\n",
                              encoding="utf-8")
    progress("ROM ZIPを取り込んでいます")
    intake = ingest(db,work,rom_roots,skip_unchanged=True,progress=progress,
                    cancel_requested=cancel_requested)
    progress("プールの検索索引を作成しています")
    origin_index = OriginIndex(db,cancel_requested)
    plan_cache = {}
    progress("作成可能なZIPを照合・生成しています")
    built = build_ready(db,work,progress=progress,cancel_requested=cancel_requested,
                        origin_index=origin_index,plan_cache=plan_cache,bios_zips=bios_zips)
    stop_if_requested()
    progress("不足ファイルとログをまとめています")
    missing = missing_report(db,origin_index=origin_index,plan_cache=plan_cache)
    report_path = work / "missing-report.json"
    names_path = work / "missing-zips.txt"
    report_path.write_text(json.dumps(missing,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    names = missing_zip_names(missing)
    names_path.write_text("".join(name+"\n" for name in names),encoding="utf-8")
    absent = missing_output_zips(db,work,origin_index=origin_index,plan_cache=plan_cache)
    absent_path = work / "missing-out-zips.txt"
    absent_path.write_text("".join(name+"\n" for name in absent),encoding="utf-8")
    causes = missing_output_causes(db,absent,built,plan_cache)
    output_diagnostics = existing_output_diagnostics(db,work,causes,plan_cache,bios_zips)
    causes_path = work / "missing-out-causes.txt"
    causes_path.write_text(format_missing_output_causes(causes,output_diagnostics),encoding="utf-8")
    stop_if_requested()
    log_path = work / "run-log.txt"
    log_lines = [f"MRA FTP: {sync['status']}",
                 f"MAME DAT: {dat_path or 'not specified'}",
                 f"BIOS sets in DAT: {len(bios_zips) if bios_zips is not None else 'not specified'}",
                 f"Shared BIOS ZIPs prepared: {', '.join(built['bios_unions']) or 'none'}",
                 *[f"  BIOS union unavailable: {name}: {error}" for name,error in built['bios_errors'].items()],
                 f"Output ZIP unions failed: {len(built['output_union_errors'])}",
                 *[f"  {name}: {error}" for name,error in built['output_union_errors'].items()],
                 f"Alternative categories: {', '.join(sorted(categories)) or 'none'}",
                 f"Alternative files classified locally: {len(classification)}",
                 f"Alternative files without a known label: {sum(x['category'] == 'unclassified' for x in classification)}",
                 f"Remote main MRA: {sync['remote_main_files']}",
                 f"Remote alternative MRA: {sync['remote_alternative_files']}",
                 f"MRA downloaded: {len(sync['downloaded'])}",
                 *["  " + name for name in sync["downloaded"]],
                 f"MRA catalog: {parsed['main_mras']} main + "
                 f"{parsed['alternative_mras']} alternatives = {parsed['total']}",
                 f"Input ZIP processed: {intake['archives']}",
                 f"Input ZIP unchanged: {intake['skipped_unchanged']}",
                 f"New output ZIP: {len(built['built'])}",
                 *["  " + str(Path(name).relative_to(work / "out")) for name in built["built"]],
                 f"Existing output ZIP reused: {len(built['already_present'])}",
                 f"Build errors: {len(built['errors'])}",
                 *["  " + item["mra"] + ": " + item["error"] for item in built["errors"]],
                 f"MRA output ZIP names absent from out: {len(absent)}",
                 *[f"  {item['zip']} | {item['reason']} | {item['mra']}: {item['detail']}" for item in causes],
                 f"Missing output causes report: {causes_path}",
                 "", "Progress details:", *progress_messages]
    log_path.write_text("\n".join(log_lines)+"\n",encoding="utf-8")
    return {"mra_ftp":sync,"mra":parsed,"ingest":intake,"build":built,
            "alternative_selection_report":str(selection_path),
            "missing_titles":len(missing),"missing_zip_candidates":len(names),
            "missing_report":str(report_path),"missing_zip_list":str(names_path),
            "missing_output_zip_count":len(absent),"missing_output_zip_list":str(absent_path),
            "missing_output_causes":str(causes_path),
            "run_log":str(log_path)}

def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work",type=Path,default=Path(".local-work"))
    parser.add_argument("--config",type=Path)
    sub=parser.add_subparsers(dest="cmd",required=True)
    p=sub.add_parser("run",help="parse MRA, ingest ROM ZIPs, build ready titles, and save missing reports")
    p.add_argument("mra_root",type=Path); p.add_argument("rom_roots",nargs="+",type=Path)
    p.add_argument("--mame-dat",type=Path,help="MAME DAT or -listxml file used to identify BIOS ZIPs")
    p.add_argument("--include-alternatives",action="store_true",
                   help="include every _alternatives MRA, including unclassified files")
    p.add_argument("--japan-alternatives",action="store_true",
                   help="include variants identified as Japanese-region by title, MRA metadata, or ROM set names")
    p.add_argument("--other-region-alternatives",action="store_true")
    p.add_argument("--version-alternatives",action="store_true")
    p.add_argument("--modified-alternatives",action="store_true")
    p.add_argument("--unclassified-alternatives",action="store_true")
    p=sub.add_parser("parse-mra"); p.add_argument("root",type=Path)
    p=sub.add_parser("ingest"); p.add_argument("paths",nargs="+",type=Path)
    p=sub.add_parser("list"); p.add_argument("--filter",default=""); p.add_argument("--status")
    p=sub.add_parser("missing"); p.add_argument("--filter",default=""); p.add_argument("--json",action="store_true")
    p.add_argument("--zip",action="store_true",help="list ZIP candidates not registered in the pool")
    p.add_argument("--report",type=Path,help="read a saved missing-report.json instead of the database")
    p.add_argument("--output",type=Path,help="write the --zip list to a UTF-8 text file")
    p=sub.add_parser("build"); p.add_argument("mra",nargs="?",help="omit to build every ready MRA title")
    p=sub.add_parser("verify"); p.add_argument("zip",type=Path)
    p=sub.add_parser("pack-pool",help="move loose pool files into verified pack ZIPs")
    p.add_argument("--dry-run",action="store_true"); p.add_argument("--keep-loose",action="store_true",help="do not delete loose files after packing")
    p=sub.add_parser("verify-pool",help="check every pool blob can be read back"); p.add_argument("--quick",action="store_true",help="skip SHA-1 re-hash")
    sub.add_parser("rebuild-index",help="restore blob/origin tables from pack manifests")
    p=sub.add_parser("transfer"); p.add_argument("--dry-run",action="store_true")
    p=sub.add_parser("send",help="send output ZIPs to MiSTer using the configured connection")
    p.add_argument("--dry-run",action="store_true")
    args=parser.parse_args(argv)
    config=json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
    set_output_compression((config.get("output") or {}).get("compression"))
    work=args.work
    if args.config and str(work)==".local-work": work=Path(config.get("work_dir",".local-work"))
    db=connect(work)
    try:
        if args.cmd=="run":
            categories = {key for key, enabled in (
                ("japan", args.japan_alternatives),
                ("other_region", args.other_region_alternatives),
                ("version", args.version_alternatives),
                ("modified", args.modified_alternatives),
                ("unclassified", args.unclassified_alternatives),
            ) if enabled}
            if args.include_alternatives:
                categories.update(ALTERNATIVE_CATEGORIES)
            result=run_pipeline(db,work,args.mra_root,args.rom_roots,config,
                                alternative_categories=categories,mame_dat=args.mame_dat)
        elif args.cmd=="parse-mra": result=scan_mras(db,args.root)
        elif args.cmd=="ingest": result=ingest(db,work,args.paths)
        elif args.cmd=="list": result=list_mras(db,args.filter,args.status)
        elif args.cmd=="missing":
            if args.json and args.zip: raise ValueError("--json and --zip cannot be combined")
            if args.output and not args.zip: raise ValueError("--output requires --zip")
            if args.report:
                result=json.loads(args.report.read_text(encoding="utf-8-sig"))
                if not isinstance(result,list): raise ValueError("missing report must be a JSON array")
                if args.filter:
                    result=[r for r in result if args.filter.casefold() in str(r.get("mra","")).casefold()]
            else: result=missing_report(db,args.filter)
            if args.zip:
                names=missing_zip_names(result)
                if args.output:
                    if args.report and args.output.resolve()==args.report.resolve():
                        raise ValueError("--output must differ from --report")
                    args.output.parent.mkdir(parents=True,exist_ok=True)
                    args.output.write_text("".join(name+"\n" for name in names),encoding="utf-8")
                    result={"output":str(args.output),"zip_candidates":len(names)}
                else:
                    print("\n".join(names)); return 0
        elif args.cmd=="pack-pool": result=pack_pool(db,work,progress=print,dry_run=args.dry_run,delete_loose=not args.keep_loose)
        elif args.cmd=="verify-pool":
            result=verify_pool(db,work,deep=not args.quick,progress=print)
            if not result["ok"]: print(json.dumps(result,ensure_ascii=False,indent=2)); return 1
        elif args.cmd=="rebuild-index": result=rebuild_index(db,work,progress=print)
        elif args.cmd=="build": result=build(db,work,args.mra) if args.mra else build_ready(db,work)
        elif args.cmd=="verify":
            plans=[json.loads(r["plan"]) for r in db.execute("SELECT plan FROM builds")]
            target=canonical_zip(args.zip.name)
            members={}
            for plan in plans:
                for item in plan.get("selected_parts",[]):
                    part=db.execute("SELECT * FROM parts WHERE id=?",(item["part"],)).fetchone()
                    if part and output_zip_for_part(db,part,item)==target:
                        members[(item["name"],item["sha1"])]=item
            if not members: raise ValueError("no stored plan for this ZIP")
            verify_zip(args.zip,list(members.values())); result={"zip":str(args.zip),"verified_members":len(members)}
        else: result=transfer(db,work,config,args.dry_run,skip_matching=args.cmd=="send",
                              require_mra_match=True,replace_different=args.cmd=="send")
        if args.cmd=="missing" and not args.json and not args.zip: print(format_missing_report(result))
        else: print(json.dumps(result,ensure_ascii=False,indent=2))
    except (ValueError,OSError,sqlite3.Error) as exc:
        print(json.dumps({"error":str(exc)},ensure_ascii=False),file=sys.stderr); return 1
    finally: db.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
