import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


HAS_QT = importlib.util.find_spec("PyQt6") is not None
if HAS_QT:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication
    import arcadegui


@unittest.skipUnless(HAS_QT, "PyQt6 GUI dependency is not installed")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_window_shows_run_send_and_report_names(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            (work / "run-log.txt").write_text(
                "MRA FTP: connected\nAlternative categories: japan\nNew output ZIP: 1\n",
                encoding="utf-8")
            (work / "missing-out-zips.txt").write_text("missing.zip\n",encoding="utf-8")
            (work / "alternative-selection-report.json").write_text(
                '{"files":[{"mra":"_alternatives/Game (Japan).mra",'
                '"category":"japan","selected":true}]}',encoding="utf-8")
            settings = QSettings(str(work / "settings.ini"),QSettings.Format.IniFormat)
            with (mock.patch.object(arcadegui,"WORK_DIR",work),
                  mock.patch.object(arcadegui,"QSettings",return_value=settings)):
                window = arcadegui.MainWindow()
                self.assertIn("自動セットアップ",window.windowTitle())
                self.assertTrue(window.run_button.isEnabled())
                self.assertTrue(window.send_button.isEnabled())
                self.assertFalse(window.cancel_button.isEnabled())
                self.assertIsNotNone(window.japan_alternatives)
                self.assertIsNotNone(window.other_region_alternatives)
                self.assertIsNotNone(window.version_alternatives)
                self.assertIsNotNone(window.modified_alternatives)
                self.assertFalse(window.compress_output.isChecked())
                self.assertEqual(window.mame_dat.objectName(), "mame_dat")
                self.assertEqual(window.content_row.indexOf(window.tabs.parentWidget()),1)
                self.assertEqual(window.tabs.count(),5)
                self.assertEqual(window.tabs.indexOf(window.missing_roms),4)
                self.assertEqual(window.tabs.indexOf(window.log),0)
                self.assertEqual(window.tabs.indexOf(window.new_zips),1)
                self.assertEqual(window.tabs.indexOf(window.missing_zips),2)
                self.assertEqual(window.tabs.indexOf(window.alternative_list),3)
                self.assertEqual(window.tabs.tabToolTip(1),"今回追加したZIP")
                self.assertEqual(window.tabs.tabToolTip(3),"代替MRAの判定")
                window.show()
                self.app.processEvents()
                left = window.content_row.itemAt(0).widget()
                right = window.content_row.itemAt(1).widget()
                self.assertGreater(right.geometry().left(),left.geometry().right())
                cards = left.layout().itemAt(0).widget().widget()
                self.assertLess(cards.layout().itemAt(0).widget().geometry().bottom(),
                                cards.layout().itemAt(1).widget().geometry().top())
                self.assertEqual(window.height(),860)
                window.mra_dir.clear()
                window.rom_dir.clear()
                window._start("run")
                self.assertIsNone(window.worker)
                self.assertIn("MRA の保存先を選択",window.status.text())
                window.language_selector.setCurrentIndex(1)
                self.assertIn("Choose an MRA folder",window.status.text())
                window._append_log("MRAを解析しています")
                self.assertIn("Parsing MRA files",window.status.text())
                window._append_log("MRAを確認中: 標準版 50件、代替版 10件")
                self.assertIn("50 main, 10 alternatives",window.status.text())
                window._append_log("ZIP処理中 (2/5): Game.mra")
                self.assertIn("Processing ZIPs for (2/5)",window.status.text())
                self.assertEqual((window.progress.minimum(), window.progress.maximum(), window.progress.value()),
                                 (0, 5, 1))
                window._append_log("ZIP検証中: game.zip (Game.mra)")
                self.assertIn("Verifying ZIP: game.zip",window.status.text())
                window._append_log("ZIP処理完了 (2/5): Game.mra")
                self.assertEqual(window.progress.value(), 2)
                window.language_selector.setCurrentIndex(0)
                self.assertIn("MRAを解析しています",window.log.toPlainText())
                self.assertEqual(window.missing_zips.count(),1)
                self.assertIn("未生成ZIP (1)",window.tabs.tabText(2))
                self.assertIn("missing.zip",window.missing_zips.item(0).text())
                window._show_result("run",{
                    "run_log":str(work / "run-log.txt"),
                    "missing_output_zip_list":str(work / "missing-out-zips.txt"),
                    "build":{"built":[str(work / "out" / "new.zip")]},
                    "mra":{"total":1,"main_mras":1,"alternative_mras":0},
                    "mra_ftp":{"status":"connected"},
                })
                self.assertEqual(window.new_zips.item(0).text(),"new.zip")
                self.assertIn("新規ZIP 1件",window.status.text())
                self.assertIn("対象 | 日本版と判定",window.alternative_list.item(0).text())
                window.language_selector.setCurrentIndex(1)
                self.assertIn("Done: 1 main MRAs",window.status.text())
                self.assertIn("New output ZIP: 1",window.log.toPlainText())
                window.language_selector.setCurrentIndex(0)
                self.assertIn("新規生成ZIP: 1",window.log.toPlainText())
                self.assertIn("MRA FTP接続: 接続済み",window.log.toPlainText())
                window._show_result("send",{"entries":[
                    {"action":"uploaded","destination":"/media/fat/games/mame/new.zip"},
                    {"action":"conflict","destination":"/media/fat/games/mame/old.zip"},
                ]})
                self.assertIn("保留: 1件",window.status.text())
                window.language_selector.setCurrentIndex(1)
                self.assertEqual(window.windowTitle(),"MiSTer Arcade Auto Setup")
                self.assertEqual(window.run_button.text(),"Build ROM ZIPs")
                self.assertEqual(window.tabs.tabText(0),"Log")
                self.assertIn("New ZIPs",window.tabs.tabText(1))
                self.assertIn("Transfer complete",window.status.text())
                self.assertIn("Included | Japan region",window.alternative_list.item(0).text())
                self.assertEqual(settings.value("language"),"en")
                window.mame_dat.setText(str(work / "mame.dat"))
                window._save_settings()
                restarted = arcadegui.MainWindow()
                self.assertEqual(restarted.language,"en")
                self.assertEqual(restarted.windowTitle(),"MiSTer Arcade Auto Setup")
                self.assertEqual(restarted.mame_dat.text(), str(work / "mame.dat"))
                restarted.deleteLater()
                window.language_selector.setCurrentIndex(0)
                self.assertIn("自動セットアップ",window.windowTitle())
                window.new_zips.clear()
                window.worker = mock.Mock()
                window.worker.isRunning.return_value = True
                window.worker.isInterruptionRequested.return_value = True
                window.worker.operation = "run"
                window.cancel_button.setEnabled(True)
                window._cancel()
                window.worker.requestInterruption.assert_called_once_with()
                self.assertFalse(window.cancel_button.isEnabled())
                self.assertIn("停止を要求しました", window.status.text())
                window._append_log("ZIP作成中: game.zip (Game.mra)")
                self.assertIn("停止を要求しました", window.status.text())
                window._append_log("ZIP完成: completed.zip (Game.mra)")
                window._show_cancelled()
                self.assertEqual(window.new_zips.item(0).text(), "completed.zip")
                self.assertIn("処理を中止しました", window.status.text())
                window.worker = None
                window.hide()
                window.deleteLater()

    def test_run_passes_selected_dat_to_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dat = root / "mame.dat"
            dat.write_text('<datafile><machine name="bios" isbios="yes"/></datafile>',
                           encoding="utf-8")
            settings = QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat)
            with (mock.patch.object(arcadegui, "WORK_DIR", root),
                  mock.patch.object(arcadegui, "QSettings", return_value=settings),
                  mock.patch.object(arcadegui.WorkThread, "start")):
                window = arcadegui.MainWindow()
                window.mra_dir.setText(str(root))
                window.rom_dir.setText(str(root))
                window.mame_dat.setText(str(dat))
                window._start("run")
                self.assertEqual(window.worker.config["mame_dat"], str(dat))
                window.worker = None
                window.deleteLater()

    def test_missing_roms_tab_lists_parts_and_copies_text(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            (work / "send-manifest.json").write_text(json.dumps({
                "required": {"b.zip": []},
                "excluded": {
                    "b.zip": [{"mra": "A.mra", "missing": ["x (CRC 11111111)"]}],
                    "v.zip": [{"mra": "V.mra", "missing": ["p (CRC 22222222)", "q"]}]}}),
                encoding="utf-8")
            settings = QSettings(str(work / "settings.ini"), QSettings.Format.IniFormat)
            with (mock.patch.object(arcadegui, "WORK_DIR", work),
                  mock.patch.object(arcadegui, "QSettings", return_value=settings)):
                window = arcadegui.MainWindow()
                tree = window.missing_roms
                self.assertEqual(window.tabs.tabText(4), "不足ROM (2)")
                self.assertEqual(tree.topLevelItem(0).text(0), "v.zip")
                self.assertEqual(tree.topLevelItem(0).text(1), "未生成")
                clipboard = QApplication.clipboard()
                tree.topLevelItem(0).child(0).child(0).setSelected(True)
                window._copy_missing_roms()
                self.assertEqual(clipboard.text(), "p (CRC 22222222)")
                tree.clearSelection()
                tree.topLevelItem(0).setSelected(True)
                tree.topLevelItem(0).child(0).setSelected(True)
                window._copy_missing_roms()
                self.assertEqual(clipboard.text(), "v.zip\nV.mra\np (CRC 22222222)\nq")
                window._copy_missing_roms(everything=True)
                self.assertEqual(clipboard.text().splitlines()[:3],
                                 ["v.zip", "  V.mra", "    p (CRC 22222222)"])
                self.assertEqual(len(clipboard.text().splitlines()), 7)
                window.deleteLater()

    def test_blocked_send_shows_one_clear_message(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            settings = QSettings(str(work / "settings.ini"), QSettings.Format.IniFormat)
            with (mock.patch.object(arcadegui, "WORK_DIR", work),
                  mock.patch.object(arcadegui, "QSettings", return_value=settings)):
                window = arcadegui.MainWindow()
                window._last_result = ("send", {"entries": [{"destination": "/x/a.zip", "action": "conflict"}],
                                                "blocked_reason": "Run is required before Send"})
                window._log_mode = "send"
                window._render_log()
                window._render_status()
                text = window.log.toPlainText()
                self.assertIn("Run is required before Send", text)
                self.assertIn("削除", text)
                self.assertNotIn("a.zip", text)
                self.assertIn("ROM ZIPを作成", window.status.text())
                window.deleteLater()


if __name__ == "__main__":
    unittest.main()
