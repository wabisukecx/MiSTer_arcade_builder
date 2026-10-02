"""Construct the MiSTer Arcade desktop window."""
from __future__ import annotations
from pathlib import Path
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QPlainTextEdit, QProgressBar, QPushButton,
    QTabWidget, QVBoxLayout, QWidget,
)

def build_ui(self, work_dir: Path):
    root = QWidget()
    root.setObjectName("central_widget")
    self.setCentralWidget(root)
    outer = QVBoxLayout(root)
    outer.setContentsMargins(24, 18, 24, 18)
    outer.setSpacing(10)

    header = QHBoxLayout()
    self.title_label = QLabel(self._t("title"))
    self.title_label.setObjectName("title")
    self.language_label = QLabel(self._t("language"))
    self.language_selector = QComboBox()
    self.language_selector.setObjectName("language_selector")
    self.language_selector.addItem("日本語", "ja")
    self.language_selector.addItem("English", "en")
    self.language_selector.setCurrentIndex(0 if self.language == "ja" else 1)
    self.language_selector.currentIndexChanged.connect(self._change_language)
    header.addWidget(self.title_label)
    header.addStretch(1)
    header.addWidget(self.language_label)
    header.addWidget(self.language_selector)
    outer.addLayout(header)
    self.subtitle_label = QLabel(self._t("subtitle"))
    self.subtitle_label.setObjectName("subtitle")
    outer.addWidget(self.subtitle_label)

    self.content_row = QHBoxLayout()
    self.content_row.setSpacing(18)
    left = QWidget()
    left_layout = QVBoxLayout(left)
    left_layout.setContentsMargins(0, 0, 0, 0)
    left_layout.setSpacing(12)
    self.content_row.addWidget(left, 1)
    result_panel, result_layout = self._section_card("result")
    self.content_row.addWidget(result_panel, 1)
    outer.addLayout(self.content_row, 1)

    connection, connection_layout = self._section_card("connection")
    conn_form = QFormLayout()
    conn_form.setSpacing(10)
    connection_layout.addLayout(conn_form)
    self.host = QLineEdit()
    self.host.setObjectName("mister_host")
    self.host.setPlaceholderText("MiSTer の IP アドレス")
    self.username = QLineEdit()
    self.username.setObjectName("mister_username")
    self.password = QLineEdit()
    self.password.setObjectName("mister_password")
    self.password.setEchoMode(QLineEdit.EchoMode.Password)
    self.password.setPlaceholderText("OS の資格情報ストアから読み込みます")
    self.host.editingFinished.connect(self._load_password)
    self.username.editingFinished.connect(self._load_password)
    conn_form.addRow(self._form_label("ip"), self.host)
    conn_form.addRow(self._form_label("user"), self.username)
    conn_form.addRow(self._form_label("password"), self.password)
    self.remote_hint = QLabel(self._t("remote_paths"))
    self.remote_hint.setObjectName("hint")
    conn_form.addRow("", self.remote_hint)
    left_layout.addWidget(connection)

    folders, folders_layout = self._section_card("folders")
    folder_form = QFormLayout()
    folder_form.setSpacing(10)
    folders_layout.addLayout(folder_form)
    self.mra_dir = QLineEdit()
    self.mra_dir.setObjectName("mra_dir")
    self.mra_dir.setPlaceholderText("MRA を保存するフォルダーを選択")
    self.rom_dir = QLineEdit()
    self.rom_dir.setObjectName("rom_dir")
    self.mame_dat = QLineEdit()
    self.mame_dat.setObjectName("mame_dat")
    self.mame_dat.setPlaceholderText(self._t("dat_placeholder"))
    self.rom_dir.setPlaceholderText("元 ROM ZIP のフォルダーを選択")
    folder_form.addRow(self._form_label("mra_folder"), self._folder_row(self.mra_dir))
    self.japan_alternatives = QCheckBox("地域違い：日本版と判定されたもの")
    self.japan_alternatives.setObjectName("japan_alternatives")
    self.other_region_alternatives = QCheckBox("地域違い：その他の地域")
    self.other_region_alternatives.setObjectName("other_region_alternatives")
    self.version_alternatives = QCheckBox("別バージョン")
    self.version_alternatives.setObjectName("version_alternatives")
    self.modified_alternatives = QCheckBox("改造版・非公式版")
    self.modified_alternatives.setObjectName("modified_alternatives")
    alternatives = QWidget()
    alternative_grid = QGridLayout(alternatives)
    alternative_grid.setContentsMargins(0, 0, 0, 0)
    alternative_grid.setHorizontalSpacing(16)
    alternative_grid.setVerticalSpacing(8)
    alternative_grid.addWidget(self.japan_alternatives, 0, 0)
    alternative_grid.addWidget(self.other_region_alternatives, 0, 1)
    alternative_grid.addWidget(self.version_alternatives, 1, 0)
    alternative_grid.addWidget(self.modified_alternatives, 1, 1)
    folder_form.addRow(self._form_label("alternatives"), alternatives)
    folder_form.addRow(self._form_label("mame_dat"), self._file_row(self.mame_dat))
    folder_form.addRow(self._form_label("rom_folder"), self._folder_row(self.rom_dir))
    self.compress_output = QCheckBox("圧縮する(既定は無圧縮)")
    self.compress_output.setObjectName("compress_output")
    folder_form.addRow(self._form_label("output_zip"), self.compress_output)
    out_row = QWidget()
    out_layout = QHBoxLayout(out_row)
    out_layout.setContentsMargins(0, 0, 0, 0)
    self.out_dir_display = QLineEdit(str(work_dir / "out"))
    self.out_dir_display.setReadOnly(True)
    out_layout.addWidget(self.out_dir_display, 1)
    self.open_out = QPushButton(self._t("open"))
    self.open_out.setObjectName("open_out")
    self.open_out.clicked.connect(self._open_out)
    out_layout.addWidget(self.open_out)
    folder_form.addRow(self._form_label("output_folder"), out_row)
    left_layout.addWidget(folders)

    actions = QHBoxLayout()
    self.run_button = QPushButton(self._t("run"))
    self.run_button.setObjectName("run_button")
    self.run_button.clicked.connect(lambda: self._start("run"))
    self.send_button = QPushButton(self._t("send"))
    self.send_button.setObjectName("send_button")
    self.send_button.clicked.connect(lambda: self._start("send"))
    self.cancel_button = QPushButton(self._t("cancel"))
    self.cancel_button.setObjectName("cancel_button")
    self.cancel_button.setEnabled(False)
    self.cancel_button.clicked.connect(self._cancel)
    actions.addWidget(self.run_button)
    actions.addWidget(self.send_button)
    actions.addWidget(self.cancel_button)
    left_layout.addLayout(actions)

    status_row = QHBoxLayout()
    self.status = QLabel(self._t("idle"))
    self.status.setObjectName("status")
    self.status.setWordWrap(True)
    self.progress = QProgressBar()
    self.progress.setTextVisible(False)
    self.progress.setFixedWidth(150)
    self.progress.hide()
    status_row.addWidget(self.status, 1)
    status_row.addWidget(self.progress)
    left_layout.addLayout(status_row)
    left_layout.addStretch(1)

    self.tabs = QTabWidget()
    self.log = QPlainTextEdit()
    self.log.setObjectName("run_log")
    self.log.setReadOnly(True)
    self.new_zips = QListWidget()
    self.new_zips.setObjectName("new_zips")
    self.missing_zips = QListWidget()
    self.missing_zips.setObjectName("missing_zips")
    self.alternative_list = QListWidget()
    self.alternative_list.setObjectName("alternative_list")
    self.tabs.addTab(self.log, self._t("tab_log"))
    self.tabs.addTab(self.new_zips, self._t("tab_new", count=0))
    self.tabs.addTab(self.missing_zips, self._t("tab_missing", count=0))
    self.tabs.addTab(self.alternative_list, self._t("tab_alternative", count=0))
    result_layout.addWidget(self.tabs, 1)

    self.setStyleSheet("""
        QMainWindow, QWidget#central_widget { background: #f6f7fb; color: #202635; font-size: 13px; }
        QLabel#title { font-size: 23px; font-weight: 700; color: #18243d; }
        QLabel#subtitle, QLabel#hint { color: #626d80; }
        QLabel#status { color: #355575; }
        QFrame#section_card { background: white; border: 1px solid #dce2ec;
                              border-radius: 8px; }
        QLabel#section_heading { font-size: 15px; font-weight: 700; color: #18243d; }
        QLineEdit { background: white; border: 1px solid #cbd4e3; border-radius: 5px;
                    padding: 7px 9px; min-height: 22px; }
        QLineEdit:focus { border-color: #3478c8; }
        QComboBox { background: white; border: 1px solid #cbd4e3; border-radius: 5px;
                    padding: 5px 9px; min-width: 100px; }
        QPushButton { background: white; border: 1px solid #bdc9da; border-radius: 6px;
                      padding: 8px 14px; min-height: 24px; }
        QPushButton:hover { background: #eef4fc; }
        QPushButton:disabled { color: #9aa3b2; background: #e9edf3; }
        QPushButton#run_button { background: #1768bd; color: white; border: 0;
                                 font-weight: 700; min-height: 32px; }
        QPushButton#run_button:hover { background: #1059a8; }
        QPushButton#send_button { background: #eff5fc; color: #195b9e;
                                  border-color: #9ebddd; font-weight: 700; min-height: 32px; }
        QTabWidget::pane { border: 1px solid #dce2ec; background: white; }
        QTabBar::tab { background: #e9edf4; padding: 9px 12px; }
        QTabBar::tab:selected { background: white; color: #155fae; }
        QPlainTextEdit, QListWidget { background: white; border: 0; padding: 10px; }
    """)
