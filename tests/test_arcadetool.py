import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
import zlib
from unittest import mock

import arcadetool as a


def crc(data):
    return f"{zlib.crc32(data):08x}"


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.work = self.base / "work"
        self.mras = self.base / "mra"
        self.mras.mkdir()
        self.db = a.connect(self.work)
        self.addCleanup(self.db.close)

    def mra(self, name, body):
        (self.mras / name).write_text(f"<misterromdescription><name>{name}</name>{body}</misterromdescription>")
        a.scan_mras(self.db, self.mras)
        return self.db.execute("SELECT * FROM mras WHERE relative_path=?", (name,)).fetchone()

    def zip(self, folder, name, items):
        folder = self.base / folder
        folder.mkdir(exist_ok=True)
        path = folder / name
        with zipfile.ZipFile(path, "w") as z:
            for member, data in items.items():
                z.writestr(member, data)
        a.ingest(self.db, self.work, [path])
        return path

    def test_zip_search_order_and_part_override(self):
        self.zip("a", "first.zip", {"x": b"same"})
        self.zip("b", "second.zip", {"renamed": b"same", "override": b"other"})
        m = self.mra("Search.mra", f'<rom index="0" zip="first.zip|second.zip"><part name="x" crc="{crc(b"same")}"/><part name="y" crc="{crc(b"other")}" zip="second.zip"/></rom>')
        p = a.plans_for_mra(self.db, m)[0]
        self.assertEqual([Path(x["source_archive"]).parent.name for x in p["selected_parts"]], ["a", "b"])
        self.assertEqual(p["missing"], [])

    def test_crc_match_different_name_and_ambiguous_sha(self):
        self.zip("a", "one.zip", {"actual": b"abc"})
        m = self.mra("CRC.mra", f'<rom index="0" zip="one.zip"><part name="wanted" crc="{crc(b"abc")}"/></rom>')
        p = a.plans_for_mra(self.db, m)[0]
        self.assertEqual(p["selected_parts"][0]["source_member"], "actual")
        # Inject a synthetic CRC collision: two distinct SHA-1 values in the same ZIP scope.
        sha = hashlib.sha1(b"different").hexdigest()
        self.db.execute("INSERT INTO blobs VALUES(?,?,?)", (sha, 9, crc(b"abc")))
        self.db.execute("INSERT INTO origins(archive,member,sha1,size,crc32) VALUES(?,?,?,?,?)",
                        (str(self.base / "a" / "one.zip"), "collision", sha, 9, crc(b"abc")))
        self.db.commit()
        p = a.plans_for_mra(self.db, m)[0]
        self.assertEqual(len(p["ambiguous"][0]["sha1"]), 2)

    def test_crc_fallback_finds_unique_pool_content_outside_mra_zip_names(self):
        self.zip("imported", "asukaj.zip", {"b68-09-1.ic23": b"rev1 program"})
        expected_crc = crc(b"rev1 program")
        m = self.mra("Asuka Rev 1.mra",
                     f'<rom index="0" zip="asuka.zip"><part name="b68-09-1.ic23" crc="{expected_crc}"/></rom>')
        plan = a.plans_for_mra(self.db, m)[0]
        self.assertEqual(plan["missing"], [])
        self.assertEqual(plan["selected_parts"][0]["match"], "global-crc")
        self.assertEqual(Path(plan["selected_parts"][0]["source_archive"]).name, "asukaj.zip")
        result = a.build_ready(self.db, self.work)
        self.assertEqual(len(result["built"]), 1)
        self.assertEqual(Path(result["built"][0]).name, "asuka.zip")
        with zipfile.ZipFile(result["built"][0]) as archive:
            self.assertEqual(archive.read("b68-09-1.ic23"), b"rev1 program")

    def test_global_crc_fallback_holds_distinct_content_collision_as_ambiguous(self):
        self.zip("imported", "foreign.zip", {"source": b"expected"})
        expected_crc = crc(b"expected")
        m = self.mra("Ambiguous.mra",
                     f'<rom index="0" zip="requested.zip"><part name="rom" crc="{expected_crc}"/></rom>')
        sha = hashlib.sha1(b"collision").hexdigest()
        self.db.execute("INSERT INTO blobs VALUES(?,?,?)", (sha, 9, expected_crc))
        self.db.execute("INSERT INTO origins(archive,member,sha1,size,crc32) VALUES(?,?,?,?,?)",
                        (str(self.base / "other" / "collision.zip"), "other",
                         sha, 9, expected_crc))
        self.db.commit()
        plan = a.plans_for_mra(self.db, m)[0]
        self.assertEqual(len(plan["ambiguous"]), 1)
        self.assertEqual(len(plan["ambiguous"][0]["sha1"]), 2)

    def test_nested_mra_zip_path_matches_and_preserves_output_directory(self):
        source = self.base / "roms" / "sound" / "journey.zip"
        source.parent.mkdir(parents=True)
        with zipfile.ZipFile(source, "w") as z:
            z.writestr("sepways.wav", b"sample data")
        a.ingest(self.db, self.work, [source.parent.parent])
        m = self.mra("Journey.mra", '<rom index="2" zip="/sound/journey.zip"><part name="sepways.wav" ignore_crc=""/></rom>')
        plan = a.plans_for_mra(self.db, m)[0]
        self.assertEqual(plan["missing"], [])
        self.assertEqual(plan["selected_parts"][0]["source_member"], "sepways.wav")
        result = a.build(self.db, self.work, "Journey.mra")
        self.assertEqual(Path(result["built"][0]).relative_to(self.work / "out").as_posix(),
                         "sound/journey.zip")
        with zipfile.ZipFile(self.work / "out" / "sound" / "journey.zip") as z:
            self.assertEqual(z.read("sepways.wav"), b"sample data")

    def test_alternative_rom_zero_and_duplicate_part(self):
        self.zip("a", "second.zip", {"x": b"123"})
        m = self.mra("Alt.mra", f'<rom index="0" zip="first.zip"><part name="x" crc="{crc(b"123")}"/></rom><rom index="0" zip="second.zip"><part name="x" crc="{crc(b"123")}"/><part name="x" crc="{crc(b"123")}"/></rom>')
        p = a.plans_for_mra(self.db, m)[0]
        self.assertEqual(p["zip_spec"], "second.zip")
        self.assertEqual(p["alternatives"], 2)
        self.assertEqual(len(p["members"]), 1)
        self.assertEqual(len(p["selected_parts"]), 2)
        result = a.build(self.db, self.work, "Alt.mra")
        with zipfile.ZipFile(result["built"][0]) as z:
            self.assertEqual(z.namelist(), ["x"])

    def test_reparse_only_changed_file(self):
        self.mra("A.mra", '<rom index="0" zip="a.zip"><part name="x" crc="01234567"/></rom>')
        self.mra("B.mra", '<rom index="0" zip="b.zip"/>')
        before = {r["relative_path"]: r["id"] for r in self.db.execute("SELECT * FROM mras")}
        result = a.scan_mras(self.db, self.mras)
        self.assertEqual(result["counts"]["unchanged"], 2)
        (self.mras / "A.mra").write_text('<misterromdescription><rom index="0" zip="new.zip"/></misterromdescription>')
        result = a.scan_mras(self.db, self.mras)
        self.assertEqual(result["counts"]["unchanged"], 1)
        after = {r["relative_path"]: r["id"] for r in self.db.execute("SELECT * FROM mras")}
        self.assertEqual(before, after)
        self.assertEqual(self.db.execute("SELECT zip_spec FROM roms WHERE mra_id=?", (before["A.mra"],)).fetchone()[0], "new.zip")

    def test_cross_mra_name_collision_before_output(self):
        self.zip("a", "shared.zip", {"x": b"one"})
        self.zip("b", "shared.zip", {"x": b"two"})
        self.mra("One.mra", f'<rom index="0" zip="shared.zip"><part name="x" crc="{crc(b"one")}"/></rom>')
        self.mra("Two.mra", f'<rom index="0" zip="shared.zip"><part name="x" crc="{crc(b"two")}"/></rom>')
        with self.assertRaisesRegex(ValueError, "cross-MRA output collision"):
            a.build(self.db, self.work, "One.mra")
        self.assertFalse((self.work / "out" / "shared.zip").exists())

    def test_failed_verification_leaves_no_completed_zip(self):
        self.zip("a", "test.zip", {"x": b"abc"})
        self.mra("Fail.mra", f'<rom index="0" zip="test.zip" md5="00000000000000000000000000000000"><part name="x" crc="{crc(b"abc")}"/></rom>')
        with self.assertRaisesRegex(ValueError, "MD5 mismatch"):
            a.build(self.db, self.work, "Fail.mra")
        self.assertFalse((self.work / "out" / "test.zip").exists())

    def test_multizip_stage_failure_leaves_no_completed_zip(self):
        self.zip("a", "first.zip", {"x": b"one"})
        self.zip("b", "second.zip", {"y": b"two"})
        self.mra("Multi.mra", f'<rom index="0" zip="first.zip"><part name="x" crc="{crc(b"one")}"/></rom><rom index="2" zip="second.zip"><part name="y" crc="{crc(b"two")}"/></rom>')
        # Corrupt the selected pool object after intake; the second staged ZIP must fail verification.
        sha = hashlib.sha1(b"two").hexdigest()
        (self.work / "pool" / sha[:2] / sha).write_bytes(b"bad")
        with self.assertRaisesRegex(ValueError, "ZIP member mismatch"):
            a.build(self.db, self.work, "Multi.mra")
        self.assertFalse((self.work / "out" / "first.zip").exists())
        self.assertFalse((self.work / "out" / "second.zip").exists())

    def test_build_progress_reports_current_title_zip_and_verification(self):
        self.zip("input", "game.zip", {"rom": b"data"})
        self.mra("Game.mra", f'<rom index="0" zip="game.zip"><part name="rom" crc="{crc(b"data")}"/></rom>')
        messages = []
        result = a.build_ready(self.db, self.work, progress=messages.append)
        self.assertEqual(len(result["built"]), 1)
        self.assertTrue(any(m.startswith("MRA照合中 (1/1): Game.mra") for m in messages))
        self.assertLess(next(i for i,m in enumerate(messages) if m.startswith("ZIP作成中: game.zip")),
                        next(i for i,m in enumerate(messages) if m.startswith("ZIP検証中: game.zip")))
        self.assertLess(next(i for i,m in enumerate(messages) if m.startswith("ZIP検証中: game.zip")),
                        next(i for i,m in enumerate(messages) if m.startswith("ZIP完成: game.zip")))
        self.assertIn("ZIP処理完了 (1/1): Game.mra", messages)
        self.assertEqual(messages[-1], "送信用ZIPリストを更新しています")

    def test_cancel_during_second_zip_keeps_first_and_removes_temp(self):
        self.zip("input", "a.zip", {"rom": b"a"})
        self.zip("input", "b.zip", {"rom": b"b"})
        self.mra("A.mra", f'<rom index="0" zip="a.zip"><part name="rom" crc="{crc(b"a")}"/></rom>')
        self.mra("B.mra", f'<rom index="0" zip="b.zip"><part name="rom" crc="{crc(b"b")}"/></rom>')
        state = {"cancel": False}
        def progress(message):
            if message.startswith("ZIP検証中: b.zip"):
                state["cancel"] = True
        with self.assertRaises(a.OperationCancelled):
            a.build_ready(self.db, self.work, progress=progress,
                          cancel_requested=lambda: state["cancel"])
        self.assertTrue((self.work / "out" / "a.zip").is_file())
        self.assertFalse((self.work / "out" / "b.zip").exists())
        self.assertEqual(list((self.work / "out").glob("build-*.zip")), [])

    def test_cancel_during_ingest_removes_partial_pool_file(self):
        source = self.base / "input"
        source.mkdir()
        with zipfile.ZipFile(source / "large.zip", "w") as z:
            z.writestr("rom", b"data")
        state = {"checks": 0}
        def cancel_requested():
            state["checks"] += 1
            return state["checks"] == 3
        with self.assertRaises(a.OperationCancelled):
            a.ingest(self.db, self.work, [source], cancel_requested=cancel_requested)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM ingested_archives").fetchone()[0], 0)
        self.assertEqual(list((self.work / "pool").rglob("ingest-*")), [])

    def test_cancelled_reingest_preserves_previous_archive_record(self):
        source = self.zip("input", "change.zip", {"old": b"old"})
        original = self.db.execute("SELECT member FROM origins WHERE archive=?",
                                   (str(source.resolve()),)).fetchone()[0]
        with zipfile.ZipFile(source, "w") as z:
            z.writestr("new1", b"new one")
            z.writestr("new2", b"new two")
        checks = {"count": 0}
        def cancel_requested():
            checks["count"] += 1
            return checks["count"] == 4
        with self.assertRaises(a.OperationCancelled):
            a.ingest(self.db, self.work, [source], cancel_requested=cancel_requested)
        members = [row[0] for row in self.db.execute(
            "SELECT member FROM origins WHERE archive=?", (str(source.resolve()),))]
        self.assertEqual(members, [original])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM ingested_archives").fetchone()[0], 1)
        new_sha = hashlib.sha1(b"new one").hexdigest()
        self.assertFalse((self.work / "pool" / new_sha[:2] / new_sha).exists())

    def test_plain_assembled_md5_matched_and_content_deduplicated(self):
        self.zip("a", "one.zip", {"x": b"abc"})
        self.zip("b", "other.zip", {"different-name": b"abc"})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM origins").fetchone()[0], 2)
        md5 = hashlib.md5(b"\x01abc").hexdigest()
        self.mra("MD5.mra", f'<rom index="0" zip="one.zip" md5="{md5}"><part>01</part><part name="x" crc="{crc(b"abc")}"/></rom>')
        result = a.build(self.db, self.work, "MD5.mra")
        self.assertEqual(result["plans"][0]["md5_status"], "matched")

    def test_member_path_and_duplicate_rejected(self):
        path = self.base / "bad.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("../escape", b"x")
            z.writestr("safe", b"a")
            z.writestr("safe", b"b")
        result = a.ingest(self.db, self.work, [path])
        self.assertEqual(result["members"], 1)
        self.assertEqual(len(result["rejected"]), 2)
        self.assertFalse((self.base / "escape").exists())

    def test_transfer_dry_run_reports_remote_collision(self):
        out = self.work / "out"
        out.mkdir()
        (out / "existing.zip").write_bytes(b"zip")
        class FakeFTP:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def connect(self, *args, **kwargs): pass
            def login(self, *args, **kwargs): pass
            def voidcmd(self,command): pass
            def cwd(self, path): self.cwd_path = path
            def nlst(self): return ["existing.zip"]
        with mock.patch.object(a.ftplib, "FTP", FakeFTP):
            result = a.transfer(self.db, self.work, {"transfer":{"host":"example.test"}}, True)
        self.assertEqual(result["entries"][0]["destination"], "/games/mame/existing.zip")
        self.assertEqual(result["entries"][0]["collision"], "existing")

    def test_transfer_rejects_non_ftp_protocol(self):
        with self.assertRaisesRegex(ValueError,"only FTP is supported"):
            a.transfer(self.db,self.work,{"transfer":{"protocol":"sftp"}})

    def test_send_holds_zip_that_no_longer_matches_current_mra(self):
        self.zip("input", "game.zip", {"rom": b"old"})
        self.mra("Game.mra", f'<rom index="0" zip="game.zip"><part name="rom" crc="{crc(b"old")}"/></rom>')
        a.build_ready(self.db, self.work)
        self.mra("Game.mra", f'<rom index="0" zip="game.zip"><part name="rom" crc="{crc(b"new")}"/></rom>')
        class FakeFTP:
            files = {"game.zip": b"stale remote zip"}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def connect(self, *args, **kwargs): pass
            def login(self, *args, **kwargs): pass
            def voidcmd(self, *args): pass
            def cwd(self, *args): pass
            def nlst(self): return list(self.files)
            def delete(self, name): del self.files[name]
        with mock.patch.object(a.ftplib, "FTP", FakeFTP):
            result = a.transfer(self.db, self.work, {"mister":{"host":"example.test"}},
                                dry_run=False, require_mra_match=True)
        entry = result["entries"][0]
        self.assertEqual(entry["action"], "conflict")
        self.assertEqual(entry["collision"], "local-mra-mismatch")
        self.assertIn("MRA catalog changed", entry["reason"])
        self.assertEqual(entry["cleanup"], {"remote":"deleted", "local":"deleted"})
        self.assertFalse((self.work / "out" / "game.zip").exists())
        self.assertEqual(FakeFTP.files, {})

    def test_send_requires_all_parts_of_shared_zip(self):
        self.zip("input", "shared.zip", {"one": b"one", "two": b"two"})
        self.mra("One.mra", f'<rom index="0" zip="shared.zip"><part name="one" crc="{crc(b"one")}"/></rom>')
        self.mra("Two.mra", f'<rom index="0" zip="shared.zip"><part name="two" crc="{crc(b"two")}"/></rom>')
        a.build_ready(self.db, self.work)
        result = a.transfer(self.db, self.work, {}, dry_run=True, require_mra_match=True)
        self.assertEqual(result["entries"][0]["action"], "conflict")
        self.assertIn("ZIP member set mismatch", result["entries"][0]["reason"])
        with zipfile.ZipFile(self.work / "out" / "shared.zip", "w") as z:
            z.writestr("one", b"one")
            z.writestr("two", b"two")
        result = a.transfer(self.db, self.work, {}, dry_run=True, require_mra_match=True)
        self.assertNotIn("action", result["entries"][0])

    def test_send_uploads_valid_zip_and_holds_unrecognized_zip(self):
        self.zip("input", "good.zip", {"rom": b"valid"})
        self.mra("Good.mra", f'<rom index="0" zip="good.zip"><part name="rom" crc="{crc(b"valid")}"/></rom>')
        a.build_ready(self.db, self.work)
        with zipfile.ZipFile(self.work / "out" / "old.zip", "w") as z:
            z.writestr("rom", b"old")

        class FakeFTP:
            uploaded = []
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def connect(self, *args, **kwargs): pass
            def login(self, *args, **kwargs): pass
            def voidcmd(self, *args): pass
            def cwd(self, *args): pass
            def nlst(self): return []
            def storbinary(self, command, source, blocksize=None):
                self.data = source.read()
                self.uploaded.append(command)
            def size(self, name): return len(self.data)
            def retrbinary(self, command, receive, blocksize=None): receive(self.data)
            def rename(self, *args): pass

        with mock.patch.object(a.ftplib, "FTP", FakeFTP):
            result = a.transfer(self.db, self.work, {"mister":{"host":"example.test"}},
                                dry_run=False, skip_matching=True, require_mra_match=True)
        actions = {Path(e["source"]).name: e["action"] for e in result["entries"]}
        self.assertEqual(actions, {"good.zip":"uploaded", "old.zip":"conflict"})
        self.assertEqual(FakeFTP.uploaded, ["STOR good.zip.uploading"])
        self.assertFalse((self.work / "out" / "old.zip").exists())
        held = next(e for e in result["entries"] if Path(e["source"]).name == "old.zip")
        self.assertEqual(held["cleanup"], {"remote":"absent", "local":"deleted"})

    def test_send_creates_missing_sound_directory(self):
        out = self.work / "out" / "sound"
        out.mkdir(parents=True)
        (out / "journey.zip").write_bytes(b"zip data")

        class FakeFTP:
            dirs = {"/", "/media", "/media/fat"}
            files = {}
            current = "/"
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def connect(self, *args, **kwargs): pass
            def login(self, *args, **kwargs): pass
            def voidcmd(self, *args): pass
            def cwd(self, path):
                target = path if path.startswith("/") else self.current.rstrip("/") + "/" + path
                target = str(Path(target).as_posix())
                if target not in self.dirs:
                    raise a.ftplib.error_perm("550 no such directory")
                self.current = target
            def mkd(self, name):
                target = self.current.rstrip("/") + "/" + name
                self.dirs.add(target)
            def nlst(self): return []
            def storbinary(self, command, source, blocksize=None):
                self.data = source.read()
            def size(self, name): return len(self.data)
            def retrbinary(self, command, receive, blocksize=None): receive(self.data)
            def rename(self, source, target): self.files[target] = self.data

        with mock.patch.object(a.ftplib, "FTP", FakeFTP):
            result = a.transfer(self.db, self.work, {"mister":{"host":"example.test"},
                                                     "transfer":{"sound_root":"/media/fat/_Arcade/sound"}},
                                dry_run=False, skip_matching=True)
        self.assertEqual(result["entries"][0]["action"], "uploaded")
        self.assertIn("/media/fat/_Arcade/sound", FakeFTP.dirs)

    def test_cancel_during_ftp_upload_removes_temporary_remote_file(self):
        out = self.work / "out"
        out.mkdir()
        (out / "game.zip").write_bytes(b"x" * (a.CHUNK + 1))
        state = {"cancel": False}
        class FakeFTP:
            deleted = []
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def connect(self, *args, **kwargs): pass
            def login(self, *args, **kwargs): pass
            def voidcmd(self, *args): pass
            def cwd(self, *args): pass
            def nlst(self): return []
            def storbinary(self, command, source, blocksize=None):
                source.read(blocksize)
                state["cancel"] = True
                source.read(blocksize)
            def delete(self, name): self.deleted.append(name)
        with mock.patch.object(a.ftplib, "FTP", FakeFTP):
            with self.assertRaises(a.OperationCancelled):
                a.transfer(self.db, self.work, {"mister":{"host":"example.test"}},
                           dry_run=False, cancel_requested=lambda: state["cancel"])
        self.assertEqual(FakeFTP.deleted, ["game.zip.uploading"])

    def test_send_recognizes_matching_remote_zip(self):
        out = self.work / "out"
        out.mkdir()
        (out / "existing.zip").write_bytes(b"zip")
        class FakeFTP:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def connect(self,*args,**kwargs): pass
            def login(self,*args,**kwargs): pass
            def voidcmd(self,command): pass
            def cwd(self,path): pass
            def nlst(self): return ["existing.zip"]
            def size(self,name): return 3
            def retrbinary(self,command,receive,blocksize=None): receive(b"zip")
        with mock.patch.object(a.ftplib,"FTP",FakeFTP):
            result = a.transfer(self.db,self.work,{"mister":{"host":"example.test"}},
                                dry_run=False,skip_matching=True)
        self.assertEqual(result["entries"][0]["collision"],"matching")

    def test_send_cache_skips_remote_download_until_mdtm_changes(self):
        out = self.work / "out"
        out.mkdir()
        (out / "existing.zip").write_bytes(b"zip")
        class FakeFTP:
            data = b"zip"
            mdtm = "20260930010000"
            downloads = 0
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def connect(self, *args, **kwargs): pass
            def login(self, *args, **kwargs): pass
            def voidcmd(self, command): pass
            def sendcmd(self, command): return "213 " + self.mdtm
            def cwd(self, path): pass
            def nlst(self): return ["existing.zip"]
            def size(self, name): return len(self.data)
            def retrbinary(self, command, receive, blocksize=None):
                FakeFTP.downloads += 1
                receive(self.data)
        config = {"mister": {"host": "example.test"}}
        with mock.patch.object(a.ftplib, "FTP", FakeFTP):
            first = a.transfer(self.db, self.work, config, dry_run=False, skip_matching=True)
            second = a.transfer(self.db, self.work, config, dry_run=False, skip_matching=True)
            self.assertEqual(FakeFTP.downloads, 1)
            self.assertEqual(second["entries"][0]["match_source"], "send_cache")
            FakeFTP.mdtm = "20260930010001"
            FakeFTP.data = b"bad"
            third = a.transfer(self.db, self.work, config, dry_run=False, skip_matching=True)
        self.assertEqual(first["entries"][0]["action"], "already_present")
        self.assertEqual(FakeFTP.downloads, 2)
        self.assertEqual(third["entries"][0]["action"], "conflict")

    def test_send_cache_records_verified_upload(self):
        out = self.work / "out"
        out.mkdir()
        (out / "game.zip").write_bytes(b"zip")
        class FakeFTP:
            files = {}
            downloads = 0
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def connect(self, *args, **kwargs): pass
            def login(self, *args, **kwargs): pass
            def voidcmd(self, command): pass
            def sendcmd(self, command): return "213 20260930010000"
            def cwd(self, path): pass
            def nlst(self): return list(self.files)
            def size(self, name): return len(self.files[name])
            def storbinary(self, command, source, blocksize=None):
                self.files[command[5:]] = source.read()
            def retrbinary(self, command, receive, blocksize=None):
                FakeFTP.downloads += 1
                receive(self.files[command[5:]])
            def rename(self, old, new): self.files[new] = self.files.pop(old)
        config = {"mister": {"host": "example.test"}}
        with mock.patch.object(a.ftplib, "FTP", FakeFTP):
            first = a.transfer(self.db, self.work, config, dry_run=False, skip_matching=True)
            second = a.transfer(self.db, self.work, config, dry_run=False, skip_matching=True)
        self.assertEqual(first["entries"][0]["action"], "uploaded")
        self.assertEqual(second["entries"][0]["action"], "already_present")
        self.assertEqual(second["entries"][0]["match_source"], "send_cache")
        self.assertEqual(FakeFTP.downloads, 1)

    def test_send_skips_different_zip_and_continues_with_new_zip(self):
        out = self.work / "out"
        out.mkdir()
        (out / "a.zip").write_bytes(b"wanted")
        (out / "b.zip").write_bytes(b"new")
        class FakeFTP:
            files = {"a.zip":b"other"}
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def connect(self,*args,**kwargs): pass
            def login(self,*args,**kwargs): pass
            def voidcmd(self,command): pass
            def cwd(self,path): pass
            def nlst(self): return list(self.files)
            def size(self,name): return len(self.files[name])
            def retrbinary(self,command,receive,blocksize=None): receive(self.files[command[5:]])
            def storbinary(self,command,source,blocksize=None): self.files[command[5:]] = source.read()
            def rename(self,old,new): self.files[new] = self.files.pop(old)
            def delete(self,name): self.files.pop(name,None)
        with mock.patch.object(a.ftplib,"FTP",FakeFTP):
            result = a.transfer(self.db,self.work,{"mister":{"host":"example.test"}},
                                dry_run=False,skip_matching=True)
        self.assertEqual([e["action"] for e in result["entries"]],["conflict","uploaded"])
        self.assertEqual(FakeFTP.files["a.zip"],b"other")
        self.assertEqual(FakeFTP.files["b.zip"],b"new")

    def test_missing_report_distinguishes_unregistered_and_incomplete_zip(self):
        self.zip("a", "present.zip", {"other": b"wrong"})
        m = self.mra("Missing.mra", f'<rom index="0" zip="present.zip|absent.zip"><part name="needed" crc="{crc(b"right")}"/></rom>')
        report = a.missing_report(self.db, "Missing")
        self.assertEqual(len(report), 1)
        part = report[0]["roms"][0]["missing"][0]
        self.assertEqual(part["name"], "needed")
        self.assertEqual([z["state"] for z in part["zip_candidates"]],
                         ["registered-no-match", "not-registered"])
        self.assertIn("present.zip（登録済み・該当ファイルなし）", a.format_missing_report(report))
        self.assertEqual(a.missing_zip_names(report), ["absent.zip"])
        self.zip("b", "absent.zip", {"renamed": b"right"})
        self.assertEqual(a.missing_report(self.db, "Missing"), [])

    def test_zip_list_can_be_written_from_saved_report(self):
        report = [{"mra":"Example.mra", "roms":[{"missing":[
            {"zip_candidates":[{"zip":"one.zip","state":"not-registered"},
                                 {"zip":"two.zip","state":"not-registered"}]},
            {"zip_candidates":[{"zip":"ONE.zip","state":"not-registered"},
                                 {"zip":"present.zip","state":"registered-no-match"}]}
        ]}]}]
        saved = self.base / "missing-report.json"
        output = self.base / "missing-zips.txt"
        saved.write_text(json.dumps(report), encoding="utf-8")
        self.assertEqual(a.main(["--work",str(self.work),"missing","--report",str(saved),
                                 "--zip","--output",str(output)]), 0)
        self.assertEqual(output.read_text(encoding="utf-8"), "one.zip\ntwo.zip\n")

    def test_build_without_mra_builds_only_complete_titles_and_reuses_outputs(self):
        self.zip("a", "ready.zip", {"x": b"ready"})
        self.zip("b", "partial.zip", {"y": b"partial"})
        self.mra("Ready.mra", f'<rom index="0" zip="ready.zip"><part name="x" crc="{crc(b"ready")}"/></rom>')
        self.mra("Partial.mra", f'<rom index="0" zip="partial.zip"><part name="y" crc="{crc(b"partial")}"/></rom>'
                 '<rom index="2" zip="absent.zip"><part name="z" crc="12345678"/></rom>')
        first = a.build_ready(self.db, self.work)
        self.assertEqual(first["ready_titles"], 1)
        self.assertEqual([Path(p).name for p in first["built"]], ["ready.zip"])
        self.assertFalse((self.work / "out" / "partial.zip").exists())
        second = a.build_ready(self.db, self.work)
        self.assertEqual(second["built"], [])
        self.assertEqual([Path(p).name for p in second["already_present"]], ["ready.zip"])

    def test_build_without_mra_reports_existing_zip_mismatch(self):
        self.zip("a", "ready.zip", {"x": b"ready"})
        self.mra("Ready.mra", f'<rom index="0" zip="ready.zip"><part name="x" crc="{crc(b"ready")}"/></rom>')
        out = self.work / "out"
        out.mkdir()
        (out / "ready.zip").write_bytes(b"not a ZIP")
        result = a.build_ready(self.db, self.work)
        self.assertEqual(result["built"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("existing output differs", result["errors"][0]["error"])
        self.assertEqual((out / "ready.zip").read_bytes(), b"not a ZIP")

    def test_run_pipeline_creates_zip_reports_and_skips_unchanged_input(self):
        (self.mras / "Ready.mra").write_text(
            f'<misterromdescription><rom index="0" zip="ready.zip">'
            f'<part name="x" crc="{crc(b"ready")}"/></rom></misterromdescription>')
        source = self.base / "input"
        source.mkdir()
        with zipfile.ZipFile(source / "ready.zip", "w") as z:
            z.writestr("x", b"ready")
        origin_queries = []
        self.db.set_trace_callback(
            lambda statement: origin_queries.append(statement)
            if "SELECT * FROM origins ORDER BY id" in statement else None)
        try:
            first = a.run_pipeline(self.db,self.work,self.mras,[source])
        finally:
            self.db.set_trace_callback(None)
        self.assertEqual(len(origin_queries), 1)
        self.assertEqual(first["ingest"]["archives"], 1)
        self.assertEqual(first["build"]["ready_titles"], 1)
        self.assertEqual(len(first["build"]["built"]), 1)
        self.assertEqual(first["missing_titles"], 0)
        self.assertEqual(first["mra_ftp"]["status"], "skipped-no-host")
        self.assertEqual(first["missing_output_zip_count"], 0)
        log = Path(first["run_log"]).read_text(encoding="utf-8")
        self.assertIn("New output ZIP: 1", log)
        self.assertIn("Progress details:", log)
        self.assertIn("ROM ZIP取込中 (1/1): ready.zip", log)
        self.assertIn("ZIP検証中: ready.zip", log)
        self.assertEqual(json.loads(Path(first["missing_report"]).read_text(encoding="utf-8")), [])
        second = a.run_pipeline(self.db,self.work,self.mras,[source])
        self.assertEqual(second["ingest"]["archives"], 0)
        self.assertEqual(second["ingest"]["skipped_unchanged"], 1)
        self.assertEqual(len(second["build"]["already_present"]), 1)

    def test_cancelled_run_invalidates_send_manifest_but_keeps_completed_zip(self):
        (self.mras / "Game.mra").write_text(
            f'<misterromdescription><rom index="0" zip="game.zip">'
            f'<part name="rom" crc="{crc(b"data")}"/></rom></misterromdescription>')
        source = self.base / "input"
        source.mkdir()
        with zipfile.ZipFile(source / "game.zip", "w") as z:
            z.writestr("rom", b"data")
        a.run_pipeline(self.db, self.work, self.mras, [source])
        manifest = self.work / "send-manifest.json"
        self.assertTrue(manifest.exists())
        state = {"cancel": False}
        def progress(message):
            if message == "送信用ZIPリストを更新しています":
                state["cancel"] = True
        with self.assertRaises(a.OperationCancelled):
            a.run_pipeline(self.db, self.work, self.mras, [source], progress=progress,
                           cancel_requested=lambda: state["cancel"])
        self.assertFalse(manifest.exists())
        self.assertTrue((self.work / "out" / "game.zip").exists())

    def test_changed_zip_replaces_old_origin(self):
        source = self.zip("input", "change.zip", {"old": b"old"})
        previous = source.stat()
        with zipfile.ZipFile(source, "w") as z:
            z.writestr("new", b"new")
        os.utime(source,ns=(previous.st_atime_ns,previous.st_mtime_ns+1_000_000_000))
        result = a.ingest(self.db,self.work,[source],skip_unchanged=True)
        self.assertEqual(result["archives"], 1)
        names = [r[0] for r in self.db.execute("SELECT member FROM origins WHERE archive=?",(str(source.resolve()),))]
        self.assertEqual(names, ["new"])

    def test_ftp_sync_downloads_only_changed_mras(self):
        class FakeFTP:
            data = b'<misterromdescription><name>Game</name></misterromdescription>'
            modified = "20260928000000"
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def connect(self,*args,**kwargs): pass
            def login(self,*args): pass
            def cwd(self,path): self.folder = path
            def mlsd(self):
                if self.folder == "/media/fat/_Arcade":
                    return iter([("Game.mra",{"type":"file","size":str(len(self.data)),"modify":self.modified}),
                                 ("_alternatives",{"type":"dir"})])
                return iter([("Alternative.mra",{"type":"file","size":str(len(self.data)),"modify":self.modified})])
            def retrbinary(self,command,receive,blocksize=None): receive(self.data)
        config = {"mister":{"host":"example.test","username":"root"}}
        with mock.patch.object(a.ftplib,"FTP",FakeFTP):
            first = a.sync_mras_ftp(self.db,self.mras,config)
            second = a.sync_mras_ftp(self.db,self.mras,config)
        self.assertEqual(first["downloaded"],["Game.mra"])
        self.assertEqual(first["remote_files"],1)
        self.assertEqual(second["unchanged"],1)
        self.assertEqual(second["downloaded"],[])
        self.assertEqual((self.mras / "Game.mra").read_bytes(),FakeFTP.data)
        self.assertFalse((self.mras / "_alternatives" / "Alternative.mra").exists())

    def test_ftp_sync_only_downloads_selected_alternatives(self):
        class FakeFTP:
            data = b'<misterromdescription><name>Game</name></misterromdescription>'
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def connect(self,*args,**kwargs): pass
            def login(self,*args): pass
            def cwd(self,path): self.folder = path
            def mlsd(self):
                if self.folder == "/media/fat/_Arcade":
                    return iter([("_alternatives",{"type":"dir"})])
                return iter([(name,{"type":"file","size":str(len(self.data)),
                                    "modify":"20260928000000"})
                             for name in ("Game (Japan).mra","Game (USA).mra",
                                          "Game (Japan, bootleg).mra")])
            def retrbinary(self,command,receive,blocksize=None): receive(self.data)
        config = {"mister":{"host":"example.test","username":"root"}}
        with mock.patch.object(a.ftplib,"FTP",FakeFTP):
            result = a.sync_mras_ftp(self.db,self.mras,config,
                                     alternative_categories={"japan"})
        self.assertEqual(result["remote_alternative_files"],1)
        self.assertEqual(result["downloaded"],["_alternatives/Game (Japan).mra"])

    def test_run_removes_alternative_mras_from_catalog_without_deleting_files(self):
        self.mra("Main.mra", '<rom index="0" zip="main.zip"><part name="x" crc="12345678"/></rom>')
        alt = self.mras / "_alternatives"
        alt.mkdir()
        (alt / "Other.mra").write_text(
            '<misterromdescription><rom index="0" zip="other.zip">'
            '<part name="y" crc="12345678"/></rom></misterromdescription>')
        a.scan_mras(self.db,self.mras)
        self.db.execute("INSERT INTO remote_mras VALUES(?,?,?,?)",
                        ("/media/fat/_Arcade/_alternatives/Other.mra",1,"20260928000000","abc"))
        self.db.commit()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM mras").fetchone()[0],2)
        source = self.base / "input"
        source.mkdir()
        result = a.run_pipeline(self.db,self.work,self.mras,[source])
        self.assertEqual(result["mra"]["total"],1)
        self.assertEqual(result["mra"]["removed_from_catalog"],1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM mras").fetchone()[0],1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM remote_mras").fetchone()[0],1)
        self.assertTrue((alt / "Other.mra").exists())
        included = a.run_pipeline(self.db,self.work,self.mras,[source],include_alternatives=True)
        self.assertEqual(included["mra"]["total"],2)
        self.assertEqual(included["mra"]["main_mras"],1)
        self.assertEqual(included["mra"]["alternative_mras"],1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM mras").fetchone()[0],2)

    def test_alternative_categories_keep_only_explicit_japan_by_default_choice(self):
        self.mra("Main.mra", '<rom index="0" zip="main.zip"><part name="x" crc="12345678"/></rom>')
        alt = self.mras / "_alternatives"
        alt.mkdir()
        for name in ("Game (Japan, version 2).mra", "Game (USA).mra",
                     "Game (revision 3).mra", "Game (Japan, bootleg).mra",
                     "Game (JP).mra", "Game Other.mra"):
            (alt / name).write_text('<misterromdescription><name>Game</name></misterromdescription>')
        source = self.base / "input"
        source.mkdir()
        result = a.run_pipeline(self.db,self.work,self.mras,[source],
                                alternative_categories={"japan"})
        self.assertEqual(result["mra"]["alternative_mras"],2)
        selected = {row[0] for row in self.db.execute("SELECT relative_path FROM mras")}
        self.assertIn("_alternatives/Game (Japan, version 2).mra",selected)
        self.assertIn("_alternatives/Game (JP).mra",selected)
        self.assertNotIn("_alternatives/Game (Japan, bootleg).mra",selected)
        report = json.loads(Path(result["alternative_selection_report"]).read_text(encoding="utf-8"))
        self.assertEqual(len(report["files"]),6)
        self.assertEqual(sum(row["selected"] for row in report["files"]),2)
        self.assertEqual(a.alternative_category("Game (Japan, bootleg).mra"),"modified")

    def test_japan_zip_pair_requires_preferred_child_and_parent(self):
        def raw(spec):
            return (f'<misterromdescription><name>Game</name>'
                    f'<rom zip="{spec}"/></misterromdescription>').encode()
        self.assertEqual(a.alternative_category("Mystery.mra",raw("klaxj3.zip|klax.zip")),"japan")
        self.assertEqual(a.alternative_category("Mystery.mra",raw("bagman.zip|bagmanj.zip")),"unclassified")
        self.assertEqual(a.alternative_category("Mystery.mra",raw("bnj.zip|brubber.zip")),"unclassified")
        self.assertEqual(a.alternative_category("Mystery.mra",raw("klaxj3.zip")),"unclassified")
        bagman = (b'<misterromdescription><setname>bagmanj</setname><parent>bagman</parent>'
                  b'<rom zip="bagman.zip|bagmanj.zip"/></misterromdescription>')
        self.assertEqual(a.alternative_category("Bagman (Taito).mra",bagman),"japan")
        unrelated = (b'<misterromdescription><setname>bnj</setname><parent>bnj</parent>'
                     b'<rom zip="brubber.zip|bnj.zip"/></misterromdescription>')
        self.assertEqual(a.alternative_category("Bump n Jump.mra",unrelated),"unclassified")
        self.assertEqual(a.alternative_category("Salamander (version J).mra"),"japan")
        self.assertEqual(a.alternative_category("Mystery.mra",b'<misterromdescription><region>Japan</region></misterromdescription>'),"japan")
        self.assertEqual(a.alternative_category("Mystery (bootleg).mra",raw("klaxj3.zip|klax.zip")),"modified")
        self.assertEqual(a.alternative_category("Game (Japan).mra",b'<misterromdescription><bootleg>yes</bootleg></misterromdescription>'),"modified")

    def test_ftp_inspects_unlabelled_alternative_zip_pair(self):
        class FakeFTP:
            data = b'<misterromdescription><rom zip="klaxj3.zip|klax.zip"/></misterromdescription>'
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def connect(self,*args,**kwargs): pass
            def login(self,*args): pass
            def cwd(self,path): self.folder = path
            def mlsd(self):
                if self.folder == "/media/fat/_Arcade":
                    return iter([("_alternatives",{"type":"dir"})])
                return iter([("Mystery.mra",{"type":"file","size":str(len(self.data)),
                                             "modify":"20260928000000"})])
            def retrbinary(self,command,receive,blocksize=None): receive(self.data)
        with mock.patch.object(a.ftplib,"FTP",FakeFTP):
            result = a.sync_mras_ftp(self.db,self.mras,{"mister":{"host":"example.test"}},
                                     alternative_categories={"japan"})
        self.assertEqual(result["downloaded"],["_alternatives/Mystery.mra"])
        self.assertEqual((self.mras / "_alternatives" / "Mystery.mra").read_bytes(),FakeFTP.data)

    def test_offline_ftp_uses_saved_mras(self):
        (self.mras / "Saved.mra").write_text('<misterromdescription><name>Saved</name></misterromdescription>')
        source = self.base / "input"
        source.mkdir()
        class OfflineFTP:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def connect(self,*args,**kwargs): raise OSError("offline")
        with mock.patch.object(a.ftplib,"FTP",OfflineFTP):
            result = a.run_pipeline(self.db,self.work,self.mras,[source],
                                    {"mister":{"host":"example.test"}})
        self.assertEqual(result["mra_ftp"]["status"],"offline")
        self.assertEqual(result["mra"]["counts"]["parsed"],1)

    def test_run_log_lists_mra_zip_names_absent_from_out(self):
        (self.mras / "Missing.mra").write_text(
            '<misterromdescription><rom index="0" zip="needed.zip|alternative.zip">'
            '<part name="x" crc="12345678"/></rom></misterromdescription>')
        source = self.base / "input"
        source.mkdir()
        result = a.run_pipeline(self.db,self.work,self.mras,[source])
        self.assertEqual(result["missing_output_zip_count"],1)
        self.assertEqual(Path(result["missing_output_zip_list"]).read_text(encoding="utf-8"),"needed.zip\n")
        self.assertIn("  needed.zip",Path(result["run_log"]).read_text(encoding="utf-8"))
        causes = Path(result["missing_output_causes"]).read_text(encoding="utf-8")
        self.assertIn("needed.zip | missing ROM parts | MRA: Missing.mra", causes)
        self.assertIn("x (CRC 12345678; needed.zip|alternative.zip)", causes)
        self.assertIn("Missing output causes report:", Path(result["run_log"]).read_text(encoding="utf-8"))

    def test_run_records_existing_shared_zip_as_cause_of_absent_game(self):
        (self.mras / "Game.mra").write_text(
            '<misterromdescription>'
            f'<rom index="0" zip="game.zip"><part name="game" crc="{crc(b"game")}"/></rom>'
            f'<rom index="1" zip="bios.zip"><part name="bios-a" crc="{crc(b"a")}"/>'
            f'<part name="bios-b" crc="{crc(b"b")}"/></rom>'
            '</misterromdescription>')
        (self.mras / "Game2.mra").write_text(
            '<misterromdescription>'
            f'<rom index="0" zip="game2.zip"><part name="game2" crc="{crc(b"game2")}"/></rom>'
            f'<rom index="1" zip="bios.zip"><part name="bios-a" crc="{crc(b"a")}"/></rom>'
            '</misterromdescription>')
        source = self.base / "input"
        source.mkdir()
        with zipfile.ZipFile(source / "game.zip", "w") as z:
            z.writestr("game", b"game")
        with zipfile.ZipFile(source / "game2.zip", "w") as z:
            z.writestr("game2", b"game2")
        with zipfile.ZipFile(source / "bios.zip", "w") as z:
            z.writestr("bios-a", b"a")
            z.writestr("bios-b", b"b")
        out = self.work / "out"
        out.mkdir()
        with zipfile.ZipFile(out / "bios.zip", "w") as z:
            z.writestr("bios-a", b"a")
        result = a.run_pipeline(self.db, self.work, self.mras, [source])
        self.assertEqual(result["missing_output_zip_count"], 1)
        self.assertFalse((out / "game.zip").exists())
        causes = Path(result["missing_output_causes"]).read_text(encoding="utf-8")
        self.assertIn("game.zip | build error | MRA: Game.mra", causes)
        self.assertIn("existing output differs:", causes)
        self.assertIn("bios.zip: ZIP member set mismatch", causes)
        self.assertIn("Required bios.zip members for this MRA: bios-a, bios-b", causes)
        self.assertIn("Current bios.zip members: bios-a", causes)
        self.assertIn("Missing from current ZIP: bios-b", causes)
        self.assertIn("Required member: bios-b | CRC32", causes)
        self.assertIn("Required variant (1 MRA): bios-a, bios-b", causes)
        self.assertIn("Required variant (1 MRA): bios-a", causes)

    def test_stvbios_union_upgrades_valid_subset_and_builds_all_games(self):
        (self.mras / "A.mra").write_text(
            '<misterromdescription>'
            f'<rom index="0" zip="a.zip"><part name="a" crc="{crc(b"game-a")}"/></rom>'
            f'<rom index="1" zip="stvbios.zip"><part name="bios-a" crc="{crc(b"bios-a")}"/></rom>'
            '</misterromdescription>')
        (self.mras / "B.mra").write_text(
            '<misterromdescription>'
            f'<rom index="0" zip="b.zip"><part name="b" crc="{crc(b"game-b")}"/></rom>'
            f'<rom index="1" zip="stvbios.zip"><part name="bios-b" crc="{crc(b"bios-b")}"/></rom>'
            '</misterromdescription>')
        source = self.base / "input"
        source.mkdir()
        for name, member, data in (("a.zip", "a", b"game-a"), ("b.zip", "b", b"game-b")):
            with zipfile.ZipFile(source / name, "w") as archive:
                archive.writestr(member, data)
        with zipfile.ZipFile(source / "stvbios.zip", "w") as archive:
            archive.writestr("bios-a", b"bios-a")
            archive.writestr("bios-b", b"bios-b")
        out = self.work / "out"
        out.mkdir()
        with zipfile.ZipFile(out / "stvbios.zip", "w") as archive:
            archive.writestr("bios-a", b"bios-a")
        dat = self.base / "mame.dat"
        dat.write_text('<datafile><machine name="stvbios" isbios="yes"/></datafile>',
                       encoding="utf-8")
        result = a.run_pipeline(self.db, self.work, self.mras, [source], mame_dat=dat)
        self.assertEqual(result["missing_output_zip_count"], 0)
        self.assertEqual(result["build"]["errors"], [])
        self.assertEqual({Path(path).name for path in result["build"]["built"]},
                         {"a.zip", "b.zip", "stvbios.zip"})
        with zipfile.ZipFile(out / "stvbios.zip") as archive:
            self.assertEqual(set(archive.namelist()), {"bios-a", "bios-b"})
        report = Path(result["missing_output_causes"]).read_text(encoding="utf-8")
        self.assertIn("stvbios.zip | referenced by 2 MRAs", report)
        self.assertIn("Current members: bios-a, bios-b", report)
        backups = list((self.work / "backups").glob("stvbios-*.zip"))
        self.assertEqual(len(backups), 1)
        with zipfile.ZipFile(backups[0]) as archive:
            self.assertEqual(archive.namelist(), ["bios-a"])
        sent = a.transfer(self.db, self.work, {}, dry_run=True, require_mra_match=True)
        self.assertFalse(any(item.get("action") == "conflict" for item in sent["entries"]))

    def test_stvbios_union_rejects_conflicting_member_content(self):
        self.zip("first", "stvbios.zip", {"bios": b"first"})
        self.zip("second", "other.zip", {"bios": b"second"})
        self.mra("A.mra", f'<rom index="0" zip="stvbios.zip"><part name="bios" crc="{crc(b"first")}"/></rom>')
        self.mra("B.mra", f'<rom index="0" zip="stvbios.zip|other.zip"><part name="bios" crc="{crc(b"second")}"/></rom>')
        without_dat = a.build_ready(self.db, self.work)
        self.assertEqual(without_dat["bios_unions"], [])
        result = a.build_ready(self.db, self.work, bios_zips=frozenset({"stvbios.zip"}))
        self.assertFalse((self.work / "out" / "stvbios.zip").exists())
        self.assertTrue(any("shared BIOS member conflict" in item["error"]
                            for item in result["errors"]))

    def test_run_uses_dat_isbios_to_merge_a_nonhardcoded_bios_zip(self):
        dat = self.base / "mame.dat"
        dat.write_text('<?xml version="1.0"?><datafile>'
                       '<machine name="custombios" isbios="yes"/>'
                       '<machine name="parent"/>'
                       '</datafile>', encoding="utf-8")
        self.assertEqual(a.bios_zip_names(dat), frozenset({"custombios.zip"}))
        (self.mras / "A.mra").write_text(
            f'<misterromdescription><rom index="0" zip="custombios.zip">'
            f'<part name="one" crc="{crc(b"one")}"/></rom></misterromdescription>')
        (self.mras / "B.mra").write_text(
            f'<misterromdescription><rom index="0" zip="custombios.zip">'
            f'<part name="none" crc="{crc(b"two")}"/></rom></misterromdescription>')
        source = self.base / "input"
        source.mkdir()
        with zipfile.ZipFile(source / "custombios.zip", "w") as archive:
            archive.writestr("one", b"one")
            archive.writestr("two", b"two")
        result = a.run_pipeline(self.db, self.work, self.mras, [source], mame_dat=dat)
        self.assertEqual(result["build"]["bios_unions"], ["custombios.zip"])
        self.assertEqual(result["build"]["errors"], [])
        with zipfile.ZipFile(self.work / "out" / "custombios.zip") as archive:
            self.assertEqual(set(archive.namelist()), {"one", "two"})
        report = Path(result["missing_output_causes"]).read_text(encoding="utf-8")
        self.assertIn("custombios.zip | referenced by 2 MRAs", report)
        self.assertIn("Required member: two | CRC32", report)
        self.assertNotIn("Required member: none |", report)

    def test_invalid_mame_dat_is_rejected_before_run(self):
        dat = self.base / "not-a-dat.dat"
        dat.write_text("<other><machine name='x' isbios='yes'/></other>", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Not a MAME DAT"):
            a.run_pipeline(self.db, self.work, self.mras, [self.mras], mame_dat=dat)


if __name__ == "__main__":
    unittest.main()
