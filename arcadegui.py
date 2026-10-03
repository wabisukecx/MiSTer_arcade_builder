"""PyQt6 desktop interface for MiSTer Arcade Auto Setup."""
from __future__ import annotations

from pathlib import Path
import json
import re
import sys

from PyQt6.QtCore import QSettings, QThread, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont, QFontDatabase, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QMenu, QTreeWidgetItem, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QVBoxLayout, QWidget,
)

import arcadetool
from gui_i18n import MRA_REMOTE, PROGRESS_EN, ROM_REMOTE, RUN_LOG_JA, TEXT
from gui_layout import build_ui

try:
    import keyring
except ImportError:
    keyring = None


APP_DIR = Path(__file__).resolve().parent
WORK_DIR = APP_DIR / ".local-work"
KEYRING_SERVICE = "MiSTerArcadeBuilder"

class WorkThread(QThread):
    message = pyqtSignal(str)
    result = pyqtSignal(dict)
    error = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, operation: str, mra_dir: Path, rom_dir: Path, config: dict,
                 alternative_categories: frozenset[str], parent=None):
        super().__init__(parent)
        self.operation = operation
        self.mra_dir = mra_dir
        self.rom_dir = rom_dir
        self.config = config
        self.alternative_categories = alternative_categories

    def run(self):
        db = None
        try:
            db = arcadetool.connect(WORK_DIR)
            if self.operation == "run":
                result = arcadetool.run_pipeline(
                    db, WORK_DIR, self.mra_dir, [self.rom_dir], self.config,
                    progress=self.message.emit,
                    alternative_categories=self.alternative_categories,
                    cancel_requested=self.isInterruptionRequested,
                )
            else:
                self.message.emit("MiSTerの転送先を確認しています")
                result = arcadetool.transfer(db, WORK_DIR, self.config,
                                             dry_run=False, skip_matching=True,
                                             require_mra_match=True,
                                             replace_different=True,
                                             cancel_requested=self.isInterruptionRequested,
                                             progress=self.message.emit)
            self.result.emit(result)
        except arcadetool.OperationCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            if db is not None:
                db.close()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("MiSTerArcadeBuilder", "ArcadeBuilder")
        saved_language = self.settings.value("language", "ja")
        self.language = saved_language if saved_language in ("ja", "en") else "ja"
        self.worker: WorkThread | None = None
        self._headings = {}
        self._form_labels = {}
        self._browse_buttons = []
        self._alternative_report = None
        self._log_mode = "progress"
        self._progress_messages = []
        self._run_log_raw = ""
        self._last_result = None
        self._last_status_raw = None
        self.setWindowTitle(self._t("title"))
        self.resize(1440, 860)
        self.setMinimumSize(1160, 720)
        build_ui(self, WORK_DIR)
        self.missing_roms.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.missing_roms.customContextMenuRequested.connect(self._missing_roms_menu)
        self.missing_roms.setSelectionMode(self.missing_roms.SelectionMode.ExtendedSelection)
        QShortcut(QKeySequence.StandardKey.Copy, self.missing_roms).activated.connect(
            self._copy_missing_roms)
        self._load_settings()
        self._load_previous_report()
        self._retranslate_ui()

    def _t(self, key: str, **values) -> str:
        return TEXT[key][0 if self.language == "ja" else 1].format(**values)


    def _section_card(self, key: str):
        card = QFrame()
        card.setObjectName("section_card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(12)
        heading = QLabel(self._t(key))
        heading.setObjectName("section_heading")
        layout.addWidget(heading)
        self._headings[key] = heading
        return card, layout

    def _form_label(self, key: str):
        label = QLabel(self._t(key))
        self._form_labels[key] = label
        return label

    def _change_language(self, index: int):
        self.language = self.language_selector.itemData(index)
        self.settings.setValue("language", self.language)
        self._retranslate_ui()

    def _retranslate_ui(self):
        self.setWindowTitle(self._t("title"))
        self.title_label.setText(self._t("title"))
        self.subtitle_label.setText(self._t("subtitle"))
        self.language_label.setText(self._t("language"))
        for key, heading in self._headings.items():
            heading.setText(self._t(key))
        for key, label in self._form_labels.items():
            label.setText(self._t(key))
        self.host.setPlaceholderText(self._t("host_placeholder"))
        self.password.setPlaceholderText(self._t("password_placeholder"))
        self.mra_dir.setPlaceholderText(self._t("mra_placeholder"))
        self.rom_dir.setPlaceholderText(self._t("rom_placeholder"))
        self.mame_dat.setPlaceholderText(self._t("dat_placeholder"))
        self.remote_hint.setText(self._t("remote_paths"))
        self.japan_alternatives.setText(self._t("japan"))
        self.other_region_alternatives.setText(self._t("other_region"))
        self.version_alternatives.setText(self._t("version"))
        self.modified_alternatives.setText(self._t("modified"))
        self.compress_output.setText(self._t("compress_output"))
        self.open_out.setText(self._t("open"))
        for browse in self._browse_buttons:
            browse.setText(self._t("browse"))
        self.run_button.setText(self._t("run"))
        self.send_button.setText(self._t("send"))
        self.cancel_button.setText(self._t("cancel"))
        self._render_alternative_list()
        self._render_missing_roms()
        self._refresh_tabs()
        self._render_log()
        self._render_status()

    def _missing_rom_lines(self, items=None, indent=False):
        lines = []
        def walk(item, depth):
            lines.append(("  " * depth if indent else "") + item.text(0))
            for index in range(item.childCount()):
                walk(item.child(index), depth + 1)
        tree = self.missing_roms
        if items is None:
            for index in range(tree.topLevelItemCount()):
                walk(tree.topLevelItem(index), 0)
        else:
            selected = set(map(id, items))
            def has_selected_ancestor(item):
                parent = item.parent()
                while parent is not None:
                    if id(parent) in selected:
                        return True
                    parent = parent.parent()
                return False
            for item in items:
                if not has_selected_ancestor(item):
                    walk(item, 0)
        return lines

    def _copy_missing_roms(self, everything=False):
        selected = self.missing_roms.selectedItems()
        if everything or not selected:
            lines = self._missing_rom_lines(indent=True)
        else:
            lines = self._missing_rom_lines(selected)
        if lines:
            QApplication.clipboard().setText("\n".join(lines))

    def _missing_roms_menu(self, position):
        menu = QMenu(self.missing_roms)
        copy = menu.addAction(self._t("copy"))
        copy.setEnabled(bool(self.missing_roms.selectedItems()))
        copy.triggered.connect(lambda: self._copy_missing_roms())
        copy_all = menu.addAction(self._t("copy_all"))
        copy_all.setEnabled(self.missing_roms.topLevelItemCount() > 0)
        copy_all.triggered.connect(lambda: self._copy_missing_roms(everything=True))
        menu.exec(self.missing_roms.viewport().mapToGlobal(position))

    def _render_missing_roms(self):
        overview = arcadetool.missing_overview(WORK_DIR)
        tree = self.missing_roms
        tree.clear()
        tree.setHeaderLabels([self._t("col_missing_zip"), self._t("col_missing_state")])
        for zip_info in overview["zips"]:
            top = QTreeWidgetItem([zip_info["zip"], self._t("state_partial" if zip_info["built"]
                                                              else "state_unbuilt")])
            tree.addTopLevelItem(top)
            for mra in zip_info["mras"]:
                child = QTreeWidgetItem([mra["mra"], self._t("state_mra_missing",
                                                              count=len(mra["missing"]))])
                top.addChild(child)
                for part in mra["missing"]:
                    child.addChild(QTreeWidgetItem([part, ""]))
        tree.expandToDepth(1)
        def add_tooltips(item):
            item.setToolTip(0, item.text(0))
            for index in range(item.childCount()):
                add_tooltips(item.child(index))
        for index in range(tree.topLevelItemCount()):
            add_tooltips(tree.topLevelItem(index))

    def _refresh_tabs(self):
        self.tabs.setTabText(0, self._t("tab_log"))
        self.tabs.setTabText(1, self._t("tab_new", count=self.new_zips.count()))
        self.tabs.setTabText(2, self._t("tab_missing", count=self.missing_zips.count()))
        self.tabs.setTabText(3, self._t("tab_alternative", count=self.alternative_list.count()))
        self.tabs.setTabText(4, self._t("tab_missing_roms", count=self.missing_roms.topLevelItemCount()))
        for index, key in ((1, "tip_new"), (2, "tip_missing"), (3, "tip_alternative"),
                           (4, "tip_missing_roms")):
            self.tabs.setTabToolTip(index, self._t(key))

    def _translate_progress(self, raw: str) -> str:
        if self.language == "ja":
            return raw
        if raw.startswith("MAME DATを解析中: "):
            return "Reading MAME DAT: " + raw.removeprefix("MAME DATを解析中: ")
        if raw.startswith("MAME DATのBIOSセット: "):
            return "BIOS sets in MAME DAT: " + raw.removeprefix("MAME DATのBIOSセット: ")
        if raw in PROGRESS_EN:
            return PROGRESS_EN[raw]
        count = re.fullmatch(r"(ROM ZIP取込中|MRA照合中|MRA照合完了|ZIP処理中|ZIP処理完了) \((\d+)/(\d+)\): (.+)", raw)
        if count:
            action = {"ROM ZIP取込中": "Importing ROM ZIP", "MRA照合中": "Matching MRA",
                      "MRA照合完了": "Finished matching MRAs", "ZIP処理中": "Processing ZIPs for",
                      "ZIP処理完了": "Finished ZIPs for"}[count[1]]
            return f"{action} ({count[2]}/{count[3]}): {count[4]}"
        detail = re.fullmatch(r"(MRAのハッシュ検証中|既存ZIP検証中|既存ZIP再利用|ZIP作成中|ZIP検証中|ZIP完成|ZIP生成エラー): (.+)", raw)
        if detail:
            action = {"MRAのハッシュ検証中": "Checking MRA hash",
                      "既存ZIP検証中": "Checking existing ZIP", "既存ZIP再利用": "Reusing existing ZIP",
                      "ZIP作成中": "Creating temporary ZIP", "ZIP検証中": "Verifying ZIP",
                      "ZIP完成": "Completed ZIP", "ZIP生成エラー": "ZIP build error"}[detail[1]]
            return f"{action}: {detail[2]}"
        transfer = re.fullmatch(r"(送信先を確認中|ZIP送信中|送信ZIP検証中|ZIP送信完了): (.+)", raw)
        if transfer:
            action = {"送信先を確認中": "Checking destination", "ZIP送信中": "Uploading ZIP",
                      "送信ZIP検証中": "Verifying uploaded ZIP", "ZIP送信完了": "Uploaded ZIP"}[transfer[1]]
            return f"{action}: {transfer[2]}"
        match = re.fullmatch(r"MRAを確認中: 標準版 (\d+)件、代替版 (\d+)件", raw)
        if match:
            return f"Checking MRAs: {match[1]} main, {match[2]} alternatives"
        if raw.startswith("元ROMのフォルダーが見つかりません: "):
            return "Source ROM folder not found: " + raw.split(": ", 1)[1]
        if raw.startswith("エラー: "):
            return "Error: " + raw[5:]
        return raw

    def _render_log(self):
        if self._log_mode == "run":
            lines = self._run_log_raw.splitlines()
            if self.language == "ja":
                lines = [self._translate_run_log_line(line) for line in lines]
            else:
                lines = [self._translate_progress(line) for line in lines]
            self.log.setPlainText("\n".join(lines))
        elif self._log_mode == "send" and self._last_result and self._last_result[1].get("blocked_reason"):
            self.log.setPlainText(self._t("send_needs_run", reason=self._last_result[1]["blocked_reason"]))
        elif self._log_mode == "send" and self._last_result:
            entries = self._last_result[1]["entries"]
            sent = sum(e.get("action") in ("uploaded", "replaced") for e in entries)
            matched = sum(e.get("action") in ("already_present", "already_satisfied") for e in entries)
            conflicts = sum(e.get("action") == "conflict" for e in entries)
            labels = {"uploaded":"send_uploaded", "replaced":"send_replaced", "already_present":"send_present",
                      "already_satisfied":"send_satisfied",
                      "conflict":"send_conflict"}
            self.log.setPlainText("\n".join(
                [self._t("send_summary", sent=sent, matched=matched, conflicts=conflicts)] +
                [f"{self._t('send_local_mra_mismatch' if e.get('collision') == 'local-mra-mismatch' else labels.get(e.get('action'), 'send_check'))}  {e['destination']}"
                 + (f" — {e['reason']}" if e.get("reason") else "")
                 + self._held_cleanup_text(e.get("cleanup"))
                 + self._excluded_text(e.get("excluded_mras"))
                 for e in entries]
            ))
        else:
            self.log.setPlainText("\n".join(self._translate_progress(raw)
                                            for raw in self._progress_messages))

    def _excluded_text(self, excluded):
        if not excluded:
            return ""
        head = "\n    除外MRA(ROM不足): " if self.language == "ja" else "\n    Excluded MRAs (missing ROMs): "
        return head + "; ".join(f"{item['mra']} / {', '.join(item['missing'])}" for item in excluded)

    def _held_cleanup_text(self, cleanup):
        if not cleanup:
            return ""
        local, remote = cleanup.get("local"), cleanup.get("remote")
        if self.language == "ja":
            local_label = {"deleted":"削除済み", "retained":"保持", "failed":"失敗"}.get(local, local)
            remote_label = {"deleted":"削除済み", "absent":"元からなし", "failed":"失敗"}.get(remote, remote)
            text = f" | 保留ZIP削除: out={local_label}, FTP={remote_label}"
        else:
            local_label = {"deleted":"deleted", "retained":"retained", "failed":"failed"}.get(local, local)
            remote_label = {"deleted":"deleted", "absent":"already absent", "failed":"failed"}.get(remote, remote)
            text = f" | Held ZIP cleanup: out={local_label}, FTP={remote_label}"
        if cleanup.get("error"):
            text += f" ({cleanup['error']})"
        return text

    def _translate_run_log_line(self, line: str) -> str:
        cause = re.fullmatch(r"(\s+[^|]+\.zip \| )([^|]+)( \| .+)", line)
        if cause:
            reasons = {"build error": "生成エラー", "missing ROM parts": "ROM部品不足",
                       "ambiguous ROM parts": "ROM部品が一意に決まらない",
                       "unsupported ROM parts": "未対応のROM部品", "unexplained": "原因未特定"}
            return cause[1] + reasons.get(cause[2].strip(), cause[2]) + cause[3]
        if line.startswith("MRA FTP: "):
            status = line.removeprefix("MRA FTP: ")
            states = {"connected":"接続済み", "offline":"オフライン",
                      "interrupted":"接続中断", "skipped-no-host":"接続先未設定"}
            return "MRA FTP接続: " + states.get(status, status)
        if line.startswith("Alternative categories: "):
            categories = line.removeprefix("Alternative categories: ")
            names = {"japan":"日本版", "other_region":"その他の地域", "version":"別バージョン",
                     "modified":"改造版", "unclassified":"分類不能", "none":"なし"}
            return "代替MRAの選択分類: " + ", ".join(
                names.get(item.strip(), item.strip()) for item in categories.split(","))
        catalog = re.fullmatch(r"MRA catalog: (\d+) main \+ (\d+) alternatives = (\d+)", line)
        if catalog:
            return f"MRA登録数: 標準 {catalog[1]}件 + 代替 {catalog[2]}件 = 合計 {catalog[3]}件"
        return next((line.replace(prefix, translated, 1)
                     for prefix, translated in RUN_LOG_JA.items()
                     if line.startswith(prefix)), line)

    def _render_status(self):
        if self._last_result:
            operation, result = self._last_result
            if operation == "run":
                built = len(result["build"]["built"])
                missing = self.missing_zips.count()
                summary = self._t("done", main=result["mra"]["main_mras"],
                                  alt=result["mra"]["alternative_mras"],
                                  new=built, missing=missing)
                if result["mra_ftp"]["status"] in ("offline", "interrupted", "skipped-no-host"):
                    summary += self._t("saved_mra")
            else:
                entries = result["entries"]
                if result.get("blocked_reason"):
                    summary = self._t("send_stopped")
                else:
                    summary = self._t("sent", sent=sum(e.get("action") in ("uploaded", "replaced") for e in entries),
                                      conflicts=sum(e.get("action") == "conflict" for e in entries))
            self.status.setText(summary)
        elif self._last_status_raw:
            self.status.setText(self._translate_progress(self._last_status_raw))
        else:
            self.status.setText(self._t("idle"))

    def _render_alternative_list(self):
        if self._alternative_report is None:
            return
        labels = {"japan":"alt_japan", "other_region":"alt_other_region",
                  "version":"alt_version", "modified":"alt_modified",
                  "unclassified":"alt_unclassified"}
        self.alternative_list.clear()
        self.alternative_list.addItems(
            f"{self._t('alt_selected' if item['selected'] else 'alt_excluded')} | "
            f"{self._t(labels[item['category']])} | {item['mra']}"
            for item in self._alternative_report.get("files", [])
        )

    def _folder_row(self, edit: QLineEdit):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        browse = QPushButton(self._t("browse"))
        browse.clicked.connect(lambda: self._choose_folder(edit))
        layout.addWidget(browse)
        self._browse_buttons.append(browse)
        return row

    def _file_row(self, edit: QLineEdit):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        browse = QPushButton(self._t("browse"))
        browse.clicked.connect(lambda: self._choose_dat(edit))
        layout.addWidget(browse)
        self._browse_buttons.append(browse)
        return row

    def _choose_dat(self, edit: QLineEdit):
        current = Path(edit.text()).expanduser()
        start = current if current.is_file() else current.parent
        chosen, _ = QFileDialog.getOpenFileName(
            self, self._t("choose_dat"), str(start), "MAME DAT (*.dat *.xml);;All files (*)")
        if chosen:
            edit.setText(chosen)

    def _choose_folder(self, edit: QLineEdit):
        current = Path(edit.text()).expanduser()
        start = current if current.is_dir() else current.parent
        chosen = QFileDialog.getExistingDirectory(self, self._t("choose_folder"), str(start))
        if chosen:
            edit.setText(chosen)

    def _open_out(self):
        path = WORK_DIR / "out"
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _load_settings(self):
        self.host.setText(str(self.settings.value("host", "")))
        self.username.setText(str(self.settings.value("username", "root")))
        self.mra_dir.setText(str(self.settings.value("mra_dir", "")))
        self.rom_dir.setText(str(self.settings.value("rom_dir", "")))
        self.mame_dat.setText(str(self.settings.value("mame_dat", "")))
        for key, widget, default in (
            ("japan_alternatives", self.japan_alternatives, True),
            ("other_region_alternatives", self.other_region_alternatives, False),
            ("version_alternatives", self.version_alternatives, False),
            ("modified_alternatives", self.modified_alternatives, False),
        ):
            widget.setChecked(self.settings.value(key, default, type=bool))
        self.compress_output.setChecked(self.settings.value("compress_output", False, type=bool))
        self._load_password()

    def _load_password(self):
        if keyring is None:
            return
        try:
            account = f"{self.username.text().strip()}@{self.host.text().strip()}"
            self.password.setText(keyring.get_password(KEYRING_SERVICE, account) or "")
        except Exception:
            pass

    def _save_settings(self):
        for key, widget in (("host", self.host), ("username", self.username),
                            ("mra_dir", self.mra_dir), ("rom_dir", self.rom_dir),
                            ("mame_dat", self.mame_dat)):
            self.settings.setValue(key, widget.text().strip())
        for key, widget in (
            ("japan_alternatives", self.japan_alternatives),
            ("other_region_alternatives", self.other_region_alternatives),
            ("version_alternatives", self.version_alternatives),
            ("modified_alternatives", self.modified_alternatives),
        ):
            self.settings.setValue(key, widget.isChecked())
        self.settings.setValue("compress_output", self.compress_output.isChecked())
        self.settings.setValue("language", self.language)

    def _alternative_categories(self) -> frozenset[str]:
        return frozenset(key for key, widget in (
            ("japan", self.japan_alternatives),
            ("other_region", self.other_region_alternatives),
            ("version", self.version_alternatives),
            ("modified", self.modified_alternatives),
        ) if widget.isChecked())

    def _save_password(self):
        if keyring is not None and self.password.text():
            try:
                account = f"{self.username.text().strip()}@{self.host.text().strip()}"
                keyring.set_password(KEYRING_SERVICE, account, self.password.text())
            except Exception:
                self._append_log("パスワードを資格情報ストアへ保存できませんでした。")

    def _load_previous_report(self):
        path = WORK_DIR / "missing-out-zips.txt"
        if path.is_file():
            names = path.read_text(encoding="utf-8").splitlines()
            self.missing_zips.addItems(names)
        path = WORK_DIR / "run-log.txt"
        if path.is_file():
            self._run_log_raw = path.read_text(encoding="utf-8")
            self._log_mode = "run"
        self._load_alternative_report()
        self._render_missing_roms()
        self._refresh_tabs()
        self._render_log()

    def _load_alternative_report(self):
        path = WORK_DIR / "alternative-selection-report.json"
        if not path.is_file():
            return
        self._alternative_report = json.loads(path.read_text(encoding="utf-8"))
        self._render_alternative_list()
        self._refresh_tabs()

    def _append_log(self, message: str):
        if self._log_mode != "progress":
            self._log_mode = "progress"
            self._progress_messages.clear()
            self.log.clear()
        self._last_result = None
        self._progress_messages.append(message)
        waiting_to_stop = (self.worker is not None and self.worker.isInterruptionRequested()
                           and message not in ("停止を要求しました。安全な区切りで中止します", "処理を中止しました"))
        if not waiting_to_stop:
            self._last_status_raw = message
        count = re.fullmatch(r"(ROM ZIP取込中|MRA照合中|MRA照合完了|ZIP処理中|ZIP処理完了) \((\d+)/(\d+)\): .+", message)
        if count:
            current, total = int(count[2]), int(count[3])
            self.progress.setRange(0, total)
            self.progress.setValue(current if count[1] in ("MRA照合完了", "ZIP処理完了") else current - 1)
            self.progress.setFormat("%v/%m")
            self.progress.setTextVisible(True)
        self.log.appendPlainText(self._translate_progress(message))
        self._render_status()

    def _start(self, operation: str):
        if self.worker is not None:
            return
        host = self.host.text().strip()
        username = self.username.text().strip()
        mra_text = self.mra_dir.text().strip()
        rom_text = self.rom_dir.text().strip()
        dat_text = self.mame_dat.text().strip()
        if operation == "run" and not mra_text:
            self._append_log("MRA の保存先を選択してください。")
            return
        if operation == "run" and not rom_text:
            self._append_log("元 ROM のフォルダーを選択してください。")
            return
        mra_dir = Path(mra_text).expanduser() if mra_text else Path()
        rom_dir = Path(rom_text).expanduser() if rom_text else Path()
        if operation == "run" and not rom_dir.is_dir():
            self._append_log(f"元ROMのフォルダーが見つかりません: {rom_dir}")
            return
        if operation == "run" and dat_text and not Path(dat_text).expanduser().is_file():
            self._append_log(self._t("dat_missing", path=dat_text))
            return
        if operation == "send" and not host:
            self._append_log("MiSTerへ送信するにはIPアドレスが必要です。")
            return
        self._save_settings()
        self._save_password()
        config = {
            "mame_dat": dat_text,
            "output": {"compression": "deflate" if self.compress_output.isChecked() else "stored"},
            "mister": {"host": host, "port": 21, "username": username,
                       "password": self.password.text(), "mra_root": MRA_REMOTE},
            "transfer": {"rom_root": ROM_REMOTE,
                         "hbmame_root": "/media/fat/games/hbmame",
                         "sound_root": "/media/fat/_Arcade/sound"},
        }
        self.log.clear()
        self._log_mode = "progress"
        self._progress_messages.clear()
        self._last_result = None
        if operation == "run":
            self.new_zips.clear()
            self.missing_zips.clear()
            self.missing_roms.clear()
            self._refresh_tabs()
        self.tabs.setCurrentIndex(0)
        self._append_log("MRAとROMの確認を開始しました" if operation == "run" else "MiSTerへ送信しています")
        self.run_button.setEnabled(False)
        self.send_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.show()
        self.worker = WorkThread(operation, mra_dir, rom_dir, config,
                                 self._alternative_categories(), self)
        self.worker.message.connect(self._append_log)
        self.worker.result.connect(lambda result: self._show_result(operation, result))
        self.worker.error.connect(self._show_error)
        self.worker.cancelled.connect(self._show_cancelled)
        self.worker.finished.connect(self._thread_finished)
        self.worker.start()

    def _cancel(self):
        if self.worker is None or not self.worker.isRunning():
            return
        self.worker.requestInterruption()
        self.cancel_button.setEnabled(False)
        self._append_log("停止を要求しました。安全な区切りで中止します")

    def _show_cancelled(self):
        if self.worker is not None and self.worker.operation == "run":
            completed = [message.removeprefix("ZIP完成: ").rsplit(" (", 1)[0]
                         for message in self._progress_messages if message.startswith("ZIP完成: ")]
            self.new_zips.addItems(completed)
            self._refresh_tabs()
        self._append_log("処理を中止しました")

    def _show_result(self, operation: str, result: dict):
        if operation == "run":
            log_path = Path(result["run_log"])
            self._run_log_raw = log_path.read_text(encoding="utf-8")
            self._log_mode = "run"
            self._load_alternative_report()
            built = [Path(name).name for name in result["build"]["built"]]
            self.new_zips.clear()
            self.new_zips.addItems(built)
            missing = Path(result["missing_output_zip_list"]).read_text(encoding="utf-8").splitlines()
            self.missing_zips.clear()
            self.missing_zips.addItems(missing)
            self._render_missing_roms()
        else:
            self._log_mode = "send"
        self._last_result = (operation, result)
        self._refresh_tabs()
        self._render_log()
        self._render_status()
        self.tabs.setCurrentIndex(0)

    def _show_error(self, error: str):
        self._append_log("エラー: " + error)

    def _thread_finished(self):
        self.progress.hide()
        self.run_button.setEnabled(True)
        self.send_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.worker = None

    def closeEvent(self, event):
        if self.worker is not None:
            self._append_log("処理が完了してからウィンドウを閉じてください。")
            event.ignore()
        else:
            self._save_settings()
            super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("MiSTer Arcade Auto Setup")
    if sys.platform == "win32":
        font_path = Path("C:/Windows/Fonts/meiryo.ttc")
        if font_path.is_file() and QFontDatabase.addApplicationFont(str(font_path)) >= 0:
            app.setFont(QFont("Meiryo", 10))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
