import html
import os
import sys
import traceback

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QLabel, QLineEdit, QPushButton,
    QComboBox, QSpinBox, QDoubleSpinBox, QFileDialog,
    QTextEdit, QStatusBar, QProgressBar, QSplitter, QSizePolicy,
    QMessageBox, QCheckBox,
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QFont, QPixmap

import common
import fa_download
import ib_download


# ── Background download worker ─────────────────────────────────────────────────

class DownloadWorker(QThread):
    log           = pyqtSignal(str)
    error         = pyqtSignal(str)
    done          = pyqtSignal(bool, str)
    progress      = pyqtSignal(int, int)        # (done_count, total)
    file_progress = pyqtSignal(str, int, int)   # (filename, bytes_done, bytes_total)
    preview       = pyqtSignal(str)             # filepath

    def __init__(self, cfg: dict, verbose: bool = False):
        super().__init__()
        self.cfg     = cfg
        self.verbose = verbose
        self._stop   = False

    def cancel(self):
        self._stop = True

    def _log(self, msg: str):
        if self.verbose:
            print(msg, flush=True)
        self.log.emit(msg)

    def _err(self, msg: str):
        if self.verbose:
            print(msg, file=sys.stderr, flush=True)
        self.error.emit(msg)

    def run(self):
        cfg  = self.cfg
        site = cfg["site"]
        try:
            self._log(f"Logging in to {site} as {cfg['username']}…")

            if site == "FurAffinity":
                self._run_fa(cfg)
            else:
                self._run_ib(cfg)

        except Exception as exc:
            if self.verbose:
                tb = traceback.format_exc()
                print(tb, file=sys.stderr, flush=True)
                self.error.emit(tb)
            self.done.emit(False, f"{type(exc).__name__}: {exc}")

    # ── FurAffinity flow ───────────────────────────────────────────────────────

    def _run_fa(self, cfg: dict):
        session = fa_download.fa_resume_session()
        if session:
            self._log("Resumed previous FurAffinity session from saved cookies.")
        else:
            self._log("Opening Firefox for FurAffinity login — a browser window will appear.")
            session = fa_download.fa_login(cfg["username"], cfg["password"], log_fn=self._log)
            self._log("Login successful. Cookies saved for next run.")

        out_dir   = os.path.join(cfg["output"], "FurAffinity")
        mode_text = cfg["mode"]

        if mode_text == "Submission Notifications":
            self._log("Fetching submission notifications…")
            sub_ids = fa_download.fa_fetch_notification_ids(
                session,
                max_pages=cfg["pages"],
                log_fn=self._log,
                cancel_fn=lambda: self._stop,
            )
        else:
            target = cfg["target"] or cfg["username"]
            mode   = "favourites" if mode_text == "User Favourites" else "gallery"
            sub_ids = fa_download.fa_fetch_submission_ids(
                session, target, mode,
                max_pages=cfg["pages"],
                log_fn=self._log,
                cancel_fn=lambda: self._stop,
            )

        if not sub_ids:
            self.done.emit(False, "No submissions found.")
            return

        self._log(f"Found {len(sub_ids)} submissions. Starting download…")

        stats = fa_download.download_fa_submissions(
            session, sub_ids, out_dir,
            log_fn=self._log,
            error_fn=self._err,
            cancel_fn=lambda: self._stop,
            progress_fn=lambda d, t: self.progress.emit(d, t),
            file_progress_fn=lambda fn, d, t: self.file_progress.emit(fn, d, t),
            preview_fn=self.preview.emit,
            delay_min=cfg["delay_min"],
            delay_max=cfg["delay_max"],
            max_workers=cfg["workers"],
        )

        # Clear notifications only AFTER downloads finish, and only for items that
        # actually landed on disk (done_ids) — never clear a failed download.
        if cfg.get("clear_notifications") and not self._stop:
            done_ids = stats.get("done_ids", [])
            if done_ids:
                self._log(f"Clearing {len(done_ids)} downloaded notifications (page by page)…")
                fa_download.fa_clear_notifications(
                    session, done_ids,
                    log_fn=self._log,
                    cancel_fn=lambda: self._stop,
                )
            else:
                self._log("No successfully-downloaded notifications to clear.")

        self._finish(stats)

    # ── Inkbunny flow ──────────────────────────────────────────────────────────

    def _run_ib(self, cfg: dict):
        sid, user_id, session = ib_download.ib_login(cfg["username"], cfg["password"])
        self._log(f"Login successful — username: {cfg['username']}  user_id: {user_id}")

        mode_text = cfg["mode"]
        out_dir   = os.path.join(cfg["output"], "Inkbunny")

        if mode_text == "Submission Notifications":
            self._log("Fetching new submissions from watched artists…")
            sub_ids = ib_download.ib_fetch_unread_submission_ids(
                session,
                max_pages=cfg["pages"],
                log_fn=self._log,
                cancel_fn=lambda: self._stop,
            )
        elif mode_text == "User Favourites":
            target = cfg["target"].strip()
            if not target or target == cfg["username"]:
                # Own favourites — use the exact same web endpoint as the browser
                # (submissionsviewall.php?mode=userfavs&user_id=X&orderby=fav_datetime)
                self._log(
                    f"Fetching your favourites via web scrape"
                    f" (user_id={user_id}, orderby=fav_datetime)…"
                )
                sub_ids = ib_download.ib_fetch_favourite_ids(
                    session, user_id,
                    max_pages=cfg["pages"],
                    log_fn=self._log,
                    cancel_fn=lambda: self._stop,
                )
            else:
                # Another user's favourites — use the search API
                self._log(f"Fetching favourites of '{target}' via API…")
                sub_ids = ib_download.ib_fetch_submission_ids(
                    sid, target, "favourites",
                    max_pages=cfg["pages"],
                    log_fn=self._log,
                    cancel_fn=lambda: self._stop,
                )
        else:  # User Gallery
            target = cfg["target"] or cfg["username"]
            self._log(f"Fetching gallery of '{target}'…")
            sub_ids = ib_download.ib_fetch_submission_ids(
                sid, target, "gallery",
                max_pages=cfg["pages"],
                log_fn=self._log,
                cancel_fn=lambda: self._stop,
            )

        if not sub_ids:
            self.done.emit(False, "No submissions found.")
            return

        self._log(f"Found {len(sub_ids)} submissions. Resolving file URLs…")
        file_infos = ib_download.ib_get_file_infos(sid, sub_ids, log_fn=self._log)

        if not file_infos:
            self.done.emit(False, "No downloadable files found.")
            return

        self._log(f"Found {len(file_infos)} files. Starting download…")

        stats = ib_download.download_ib_files(
            file_infos, out_dir,
            log_fn=self._log,
            error_fn=self._err,
            cancel_fn=lambda: self._stop,
            progress_fn=lambda d, t: self.progress.emit(d, t),
            file_progress_fn=lambda fn, d, t: self.file_progress.emit(fn, d, t),
            preview_fn=self.preview.emit,
            delay_min=cfg["delay_min"],
            delay_max=cfg["delay_max"],
            max_workers=cfg["workers"],
        )

        if cfg.get("clear_notifications") and not self._stop:
            done_ids = stats.get("done_ids", [])
            if done_ids:
                self._log(f"Marking {len(done_ids)} submissions as read…")
                ib_download.ib_mark_submissions_read(
                    session, done_ids,
                    log_fn=self._log,
                    cancel_fn=lambda: self._stop,
                )
            else:
                self._log("No successfully-downloaded submissions to mark as read.")

        self._finish(stats)

    # ── Shared finish ──────────────────────────────────────────────────────────

    def _finish(self, stats: dict):
        nb       = stats["bytes"]
        n        = stats["images"]
        size_str = (
            f"{nb / 1_048_576:.1f} MB" if nb >= 1_048_576
            else f"{nb / 1024:.1f} KB"
        )
        self._log(f"── Summary ──  Files: {n}  │  {size_str}")
        if self._stop:
            self.done.emit(False, "Cancelled.")
        else:
            self.done.emit(True, f"Done — {n} files · {size_str}")


# ── Background helpers ─────────────────────────────────────────────────────────

class _UpdateChecker(QThread):
    """Checks GitHub releases for a newer version. Runs once at startup."""
    update_found = pyqtSignal(str)   # emits latest version string

    def run(self):
        available, latest = common.check_for_updates()
        if available:
            self.update_found.emit(latest)


class _CamoufoxSetup(QThread):
    """Downloads the Camoufox browser binary in the background."""
    finished = pyqtSignal(bool, str)   # (success, error_message)

    def run(self):
        try:
            from camoufox.pkgman import camoufox_path
            camoufox_path(download_if_missing=True)
            self.finished.emit(True, "")
        except Exception as exc:
            self.finished.emit(False, str(exc))


# ── Main window ────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"FA & Inkbunny Downloader v{common.VERSION}")
        self.setMinimumWidth(640)
        self.worker: DownloadWorker | None          = None
        self._current_preview_pixmap: QPixmap | None = None
        self._prev_cred_site: str | None             = None
        self._target_per_mode: dict[str, str]        = {}
        self._prev_mode: str | None                  = None

        self._build_ui()
        self._load_saved_credentials()
        self._load_ui_state()

        self._update_checker = _UpdateChecker()
        self._update_checker.update_found.connect(self._on_update_available)
        self._update_checker.start()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        mb        = self.menuBar()
        help_menu = mb.addMenu("&Help")
        help_menu.addAction("Setup Camoufox Browser…").triggered.connect(self._setup_camoufox)
        help_menu.addSeparator()
        help_menu.addAction("Check for Updates").triggered.connect(self._check_updates_manual)
        self._act_update_available = help_menu.addAction("")
        self._act_update_available.triggered.connect(self._open_releases)
        self._act_update_available.setVisible(False)
        help_menu.addSeparator()
        help_menu.addAction(f"Version {common.VERSION}").setEnabled(False)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setSpacing(10)
        layout.setContentsMargins(14, 14, 14, 14)

        layout.addWidget(self._credentials_group())
        layout.addWidget(self._options_group())
        layout.addWidget(self._output_group())
        layout.addLayout(self._buttons_row())
        layout.addWidget(self._progress_group())

        self._bottom_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._bottom_splitter.setChildrenCollapsible(False)
        self._bottom_splitter.addWidget(self._preview_group())
        self._bottom_splitter.addWidget(self._log_group())
        layout.addWidget(self._bottom_splitter, 1)

        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)
        self.statusbar.showMessage("Ready")

    def _credentials_group(self) -> QGroupBox:
        g     = QGroupBox("Credentials")
        outer = QVBoxLayout(g)
        outer.setSpacing(6)

        # Site selector
        site_row = QHBoxLayout()
        site_row.addWidget(QLabel("Site:"))
        self.cb_site = QComboBox()
        self.cb_site.addItems(common.SITES)
        self.cb_site.setFixedWidth(140)
        site_row.addWidget(self.cb_site)
        site_row.addStretch()
        outer.addLayout(site_row)

        # Hint label — changes per site
        self.lbl_site_hint = QLabel()
        self.lbl_site_hint.setWordWrap(True)
        self.lbl_site_hint.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        outer.addWidget(self.lbl_site_hint)

        f = QFormLayout()
        f.setContentsMargins(0, 4, 0, 0)
        self.le_username = QLineEdit(placeholderText="Username")
        self.le_password = QLineEdit(placeholderText="Password")
        self.le_password.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow("Username:", self.le_username)
        f.addRow("Password:", self.le_password)
        outer.addLayout(f)

        self.cb_site.currentTextChanged.connect(self._on_site_changed)
        self._on_site_changed(self.cb_site.currentText())   # set initial hint
        return g

    def _options_group(self) -> QGroupBox:
        g = QGroupBox("Download Options")
        f = QFormLayout(g)

        # Clear-notifications checkbox is created before the mode signal is wired,
        # so _on_mode_changed can safely toggle it the first time it fires.
        self.chk_clear_notif = QCheckBox("Clear notifications after download")
        self.chk_clear_notif.setToolTip(
            "After all downloads finish, mark the downloaded items as read in your\n"
            "submission-notification inbox. Only items that downloaded successfully\n"
            "are cleared/marked, in page-sized batches for safety."
        )
        self.chk_clear_notif.setEnabled(False)

        self.cb_mode = QComboBox()
        self.cb_mode.addItems(self._modes_for_site(self.cb_site.currentText()))
        self.cb_mode.currentTextChanged.connect(self._on_mode_changed)

        self.le_target = QLineEdit(placeholderText="Username to download (blank = your own account for Favourites)")

        self.sp_pages = QSpinBox()
        self.sp_pages.setRange(1, 500)
        self.sp_pages.setValue(25)
        self.sp_pages.setSuffix("  pages")

        self.sp_workers = QSpinBox()
        self.sp_workers.setRange(1, 5)
        self.sp_workers.setValue(2)
        self.sp_workers.setSuffix("  concurrent")
        self.sp_workers.setToolTip(
            "Number of simultaneous downloads (1–5).\n"
            "Keep low to avoid rate-limiting or bans."
        )

        f.addRow("Mode:",                 self.cb_mode)
        f.addRow("Target Username:",      self.le_target)
        f.addRow("Max Pages:",            self.sp_pages)
        f.addRow("Concurrent Downloads:", self.sp_workers)
        f.addRow("Post Delay:",           self._build_delay_widget())
        f.addRow("",                      self.chk_clear_notif)
        return g

    @staticmethod
    def _modes_for_site(site: str) -> list[str]:
        return ["User Gallery", "User Favourites", "Submission Notifications"]

    def _output_group(self) -> QGroupBox:
        g = QGroupBox("Output Folder")
        h = QHBoxLayout(g)
        self.le_output = QLineEdit(common.DEFAULT_DOWNLOAD_DIR)
        btn = QPushButton("Browse…")
        btn.setFixedWidth(80)
        btn.clicked.connect(self._browse_output)
        h.addWidget(self.le_output)
        h.addWidget(btn)
        return g

    def _buttons_row(self) -> QHBoxLayout:
        h = QHBoxLayout()
        self.btn_start  = QPushButton("Start Download")
        self.btn_cancel = QPushButton("Cancel")
        for btn in (self.btn_start, self.btn_cancel):
            btn.setFixedHeight(34)
        self.btn_cancel.setEnabled(False)
        self.btn_start.clicked.connect(self._start)
        self.btn_cancel.clicked.connect(self._cancel)
        h.addWidget(self.btn_start)
        h.addWidget(self.btn_cancel)
        return h

    def _progress_group(self) -> QGroupBox:
        g = QGroupBox("Progress")
        v = QVBoxLayout(g)
        v.setSpacing(4)

        h_overall = QHBoxLayout()
        lbl_total = QLabel("Total:")
        lbl_total.setFixedWidth(42)
        self.pb_overall = QProgressBar()
        self.pb_overall.setTextVisible(False)
        self.pb_overall.setFixedHeight(16)
        self.lbl_overall_count = QLabel("–")
        self.lbl_overall_count.setFixedWidth(100)
        self.lbl_overall_count.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        h_overall.addWidget(lbl_total)
        h_overall.addWidget(self.pb_overall, 1)
        h_overall.addWidget(self.lbl_overall_count)

        h_current = QHBoxLayout()
        lbl_file = QLabel("File:")
        lbl_file.setFixedWidth(42)
        self.pb_current = QProgressBar()
        self.pb_current.setTextVisible(False)
        self.pb_current.setFixedHeight(16)
        h_current.addWidget(lbl_file)
        h_current.addWidget(self.pb_current, 1)

        self.lbl_current_file = QLabel("")
        self.lbl_current_file.setFont(QFont("Monospace", 8))
        self.lbl_current_file.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )

        v.addLayout(h_overall)
        v.addLayout(h_current)
        v.addWidget(self.lbl_current_file)
        return g

    def _preview_group(self) -> QGroupBox:
        g = QGroupBox("Preview")
        g.setMinimumWidth(150)
        v = QVBoxLayout(g)
        self.lbl_preview = QLabel("No preview")
        self.lbl_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_preview.setMinimumSize(100, 150)
        self.lbl_preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.lbl_preview.setStyleSheet(
            "background-color: #1a1a2e; color: #666; border-radius: 4px;"
        )
        v.addWidget(self.lbl_preview)
        return g

    def _log_group(self) -> QGroupBox:
        g = QGroupBox("Log")
        v = QVBoxLayout(g)

        toolbar = QHBoxLayout()
        self.chk_verbose = QCheckBox("Verbose (console)")
        self.chk_verbose.setToolTip(
            "Mirror all messages to stdout/stderr and show full error tracebacks"
        )
        toolbar.addWidget(self.chk_verbose)
        toolbar.addStretch()
        v.addLayout(toolbar)

        self.te_log = QTextEdit()
        self.te_log.setReadOnly(True)
        self.te_log.setFont(QFont("Monospace", 9))
        self.te_log.setMinimumHeight(160)
        v.addWidget(self.te_log)
        return g

    def _build_delay_widget(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)

        self.cb_delay_type = QComboBox()
        self.cb_delay_type.addItems(["Fixed", "Variable"])
        self.cb_delay_type.setFixedWidth(84)

        self.dsb_delay_fixed = QDoubleSpinBox()
        self.dsb_delay_fixed.setRange(0.0, 60.0)
        self.dsb_delay_fixed.setSingleStep(0.5)
        self.dsb_delay_fixed.setValue(2.0)
        self.dsb_delay_fixed.setSuffix(" s")
        self.dsb_delay_fixed.setFixedWidth(72)

        self.dsb_delay_min = QDoubleSpinBox()
        self.dsb_delay_min.setRange(0.0, 60.0)
        self.dsb_delay_min.setSingleStep(0.5)
        self.dsb_delay_min.setValue(1.0)
        self.dsb_delay_min.setSuffix(" s")
        self.dsb_delay_min.setFixedWidth(72)
        self.dsb_delay_min.setVisible(False)

        self._lbl_delay_to = QLabel("to")
        self._lbl_delay_to.setVisible(False)

        self.dsb_delay_max = QDoubleSpinBox()
        self.dsb_delay_max.setRange(0.0, 60.0)
        self.dsb_delay_max.setSingleStep(0.5)
        self.dsb_delay_max.setValue(4.0)
        self.dsb_delay_max.setSuffix(" s")
        self.dsb_delay_max.setFixedWidth(72)
        self.dsb_delay_max.setVisible(False)

        h.addWidget(self.cb_delay_type)
        h.addWidget(self.dsb_delay_fixed)
        h.addWidget(self.dsb_delay_min)
        h.addWidget(self._lbl_delay_to)
        h.addWidget(self.dsb_delay_max)
        h.addStretch()

        self.cb_delay_type.currentTextChanged.connect(self._on_delay_type_changed)
        return w

    # ── Window events ──────────────────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_splitter_ratio()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale_preview()

    def _apply_splitter_ratio(self):
        screen   = QApplication.primaryScreen()
        screen_h = screen.size().height() if screen else 1080

        if screen_h <= 1080:
            self._bottom_splitter.setStretchFactor(0, 3)
            self._bottom_splitter.setStretchFactor(1, 7)
            preview_ratio = 0.30
        else:
            self._bottom_splitter.setStretchFactor(0, 1)
            self._bottom_splitter.setStretchFactor(1, 1)
            preview_ratio = 0.50

        total = self._bottom_splitter.width()
        if total > 0:
            pw = int(total * preview_ratio)
            self._bottom_splitter.setSizes([pw, total - pw])

    # ── Update / Camoufox helpers ──────────────────────────────────────────────

    def _on_update_available(self, latest: str):
        self._act_update_available.setText(f"Update available: v{latest} — open releases…")
        self._act_update_available.setVisible(True)
        self.statusbar.showMessage(
            f"Update available: v{latest} — see Help menu.", 12000
        )

    def _check_updates_manual(self):
        self.statusbar.showMessage("Checking for updates…")
        available, latest = common.check_for_updates()
        if available:
            self._on_update_available(latest)
            QMessageBox.information(
                self, "Update Available",
                f"Version v{latest} is available on GitHub.\nYou are running v{common.VERSION}.",
            )
        else:
            self.statusbar.showMessage("You are up to date.", 5000)
            QMessageBox.information(
                self, "Up to Date",
                f"You are running the latest version (v{common.VERSION}).",
            )

    def _open_releases(self):
        import webbrowser
        webbrowser.open(common.GITHUB_RELEASES_URL)

    def _setup_camoufox(self):
        reply = QMessageBox.question(
            self, "Setup Camoufox",
            "Download the Camoufox browser required for FurAffinity login (~100 MB).\n"
            "This is a one-time setup stored in your home directory.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.statusbar.showMessage("Downloading Camoufox browser… (may take a few minutes)")
        self._camoufox_setup = _CamoufoxSetup()
        self._camoufox_setup.finished.connect(self._on_camoufox_setup_done)
        self._camoufox_setup.start()

    def _on_camoufox_setup_done(self, ok: bool, err: str):
        if ok:
            self.statusbar.showMessage("Camoufox browser ready.", 6000)
            QMessageBox.information(self, "Camoufox Ready", "Camoufox browser installed successfully.")
        else:
            self.statusbar.showMessage("Camoufox setup failed.", 6000)
            QMessageBox.critical(self, "Setup Failed", f"Could not install Camoufox:\n{err}")

    # ── Slots ──────────────────────────────────────────────────────────────────

    def _on_site_changed(self, site: str):
        hints = {
            "FurAffinity": (
                "Tip: for NSFW content, enable 'Show NSFW' in your FA account settings."
            ),
            "Inkbunny": (
                "Inkbunny has a native API — login is fast and reliable."
            ),
        }
        self.lbl_site_hint.setText(hints.get(site, ""))

        # Persist the previous site's typed credentials before swapping them out,
        # so FA and Inkbunny each keep their own username+password as you switch.
        if self._prev_cred_site and self._prev_cred_site != site:
            u = self.le_username.text().strip()
            p = self.le_password.text().strip()
            if u and p:
                common.save_config(self._prev_cred_site, u, p)
        self._prev_cred_site = site

        # Rebuild the mode list (preserving selection) when the site changes.
        if hasattr(self, "cb_mode"):
            self.cb_mode.blockSignals(True)
            prev_mode = self.cb_mode.currentText()
            self.cb_mode.clear()
            self.cb_mode.addItems(self._modes_for_site(site))
            idx = self.cb_mode.findText(prev_mode)
            self.cb_mode.setCurrentIndex(idx if idx >= 0 else 0)
            self.cb_mode.blockSignals(False)
            self._on_mode_changed(self.cb_mode.currentText())

        # Load credentials for the newly selected site
        cfg = common.load_config()
        username, password = common.get_credentials(cfg, site)
        self.le_username.setText(username)
        self.le_password.setText(password or "")

    def _on_mode_changed(self, mode: str):
        # Stash the target typed for the departing mode, then restore the one
        # previously used for the arriving mode.  Without this, a Gallery target
        # (e.g. "MishaJeans") would bleed into Favourites mode and download that
        # artist's favourites instead of the logged-in user's own.
        if self._prev_mode is not None:
            self._target_per_mode[self._prev_mode] = self.le_target.text()
        self._prev_mode = mode
        self.le_target.setText(self._target_per_mode.get(mode, ""))

        is_notif = mode == "Submission Notifications"
        self.le_target.setEnabled(not is_notif)
        self.chk_clear_notif.setEnabled(is_notif)
        if is_notif:
            self.le_target.setPlaceholderText("(not used — notifications come from your own inbox)")
        elif mode == "User Gallery":
            self.le_target.setPlaceholderText("Username whose gallery to download (required)")
        else:
            self.le_target.setPlaceholderText("Username to download favourites from (blank = your own account)")

    def _on_delay_type_changed(self, mode: str):
        fixed = mode == "Fixed"
        self.dsb_delay_fixed.setVisible(fixed)
        self.dsb_delay_min.setVisible(not fixed)
        self._lbl_delay_to.setVisible(not fixed)
        self.dsb_delay_max.setVisible(not fixed)

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select Output Folder", self.le_output.text()
        )
        if path:
            self.le_output.setText(path)

    # ── Config persistence ─────────────────────────────────────────────────────

    def _load_saved_credentials(self):
        cfg  = common.load_config()
        site = self.cb_site.currentText()
        username, password = common.get_credentials(cfg, site)
        self.le_username.setText(username)
        if password:
            self.le_password.setText(password)
        elif cfg.has_option(common._site_section(site), "password"):
            self.statusBar().showMessage(
                "Saved password could not be decrypted — please re-enter it.", 8000
            )

    def _load_ui_state(self):
        cfg = common.load_config()
        if not cfg.has_section("last_run"):
            return
        lr = cfg["last_run"]

        if "site" in lr:
            idx = self.cb_site.findText(lr["site"])
            if idx >= 0:
                self.cb_site.setCurrentIndex(idx)
        if "mode" in lr:
            idx = self.cb_mode.findText(lr["mode"])
            if idx >= 0:
                self.cb_mode.setCurrentIndex(idx)
        if "target" in lr:
            self.le_target.setText(lr["target"])
            # Attribute the saved target to the restored mode so that switching
            # modes doesn't accidentally carry it over to a different mode.
            self._target_per_mode[self.cb_mode.currentText()] = lr["target"]
            self._prev_mode = self.cb_mode.currentText()
        if "pages" in lr:
            try:
                self.sp_pages.setValue(int(lr["pages"]))
            except ValueError:
                pass
        if "workers" in lr:
            try:
                self.sp_workers.setValue(int(lr["workers"]))
            except ValueError:
                pass
        if "output" in lr:
            self.le_output.setText(lr["output"])
        if "clear_notif" in lr:
            self.chk_clear_notif.setChecked(lr["clear_notif"] == "True")
        if "delay_type" in lr:
            idx = self.cb_delay_type.findText(lr["delay_type"])
            if idx >= 0:
                self.cb_delay_type.setCurrentIndex(idx)
        for field, spinbox in [
            ("delay_fixed", self.dsb_delay_fixed),
            ("delay_min",   self.dsb_delay_min),
            ("delay_max",   self.dsb_delay_max),
        ]:
            if field in lr:
                try:
                    spinbox.setValue(float(lr[field]))
                except ValueError:
                    pass

    def _save_ui_state(self):
        common.save_ui_state({
            "site":        self.cb_site.currentText(),
            "mode":        self.cb_mode.currentText(),
            "target":      self.le_target.text().strip(),
            "pages":       str(self.sp_pages.value()),
            "workers":     str(self.sp_workers.value()),
            "output":      self.le_output.text().strip(),
            "delay_type":  self.cb_delay_type.currentText(),
            "delay_fixed": str(self.dsb_delay_fixed.value()),
            "delay_min":   str(self.dsb_delay_min.value()),
            "delay_max":   str(self.dsb_delay_max.value()),
            "clear_notif": str(self.chk_clear_notif.isChecked()),
        })

    # ── Log helpers ────────────────────────────────────────────────────────────

    def _append_log(self, msg: str):
        self.te_log.append(html.escape(msg))
        self._scroll_log()

    def _append_error(self, msg: str):
        self.te_log.append(
            f'<span style="color: #ff5555;">{html.escape(msg)}</span>'
        )
        self._scroll_log()

    def _scroll_log(self):
        sb = self.te_log.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ── Progress helpers ───────────────────────────────────────────────────────

    def _update_progress(self, done: int, total: int):
        self.pb_overall.setMaximum(max(total, 1))
        self.pb_overall.setValue(done)
        self.lbl_overall_count.setText(f"{done} / {total} files")

    def _update_file_progress(self, fname: str, done: int, total: int):
        if total > 0:
            self.pb_current.setMaximum(total)
            self.pb_current.setValue(done)
            size_str = (
                f"{done / 1_048_576:.1f} / {total / 1_048_576:.1f} MB"
                if total >= 1_048_576
                else f"{done / 1024:.1f} / {total / 1024:.1f} KB"
            )
            self.lbl_current_file.setText(f"{fname}  ({size_str})")
        else:
            self.pb_current.setMaximum(0)
            self.pb_current.setValue(0)
            self.lbl_current_file.setText(fname)

    # ── Preview helpers ────────────────────────────────────────────────────────

    def _rescale_preview(self):
        if self._current_preview_pixmap and not self._current_preview_pixmap.isNull():
            scaled = self._current_preview_pixmap.scaled(
                self.lbl_preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.lbl_preview.setPixmap(scaled)

    def _update_preview(self, filepath: str):
        pixmap = QPixmap(filepath)
        if not pixmap.isNull():
            self._current_preview_pixmap = pixmap
            self._rescale_preview()
        else:
            self._current_preview_pixmap = None
            self.lbl_preview.setText("(non-image file)")

    # ── Start / Cancel ─────────────────────────────────────────────────────────

    def _start(self):
        site     = self.cb_site.currentText()
        username = self.le_username.text().strip()
        password = self.le_password.text().strip()

        if not username or not password:
            self._append_error("Username and password are required.")
            return

        mode   = self.cb_mode.currentText()
        target = self.le_target.text().strip()

        if mode == "User Gallery" and not target:
            self._append_error("Target username is required for User Gallery mode.")
            return

        common.save_config(site, username, password)
        self._save_ui_state()

        if self.cb_delay_type.currentText() == "Fixed":
            d = self.dsb_delay_fixed.value()
            delay_min, delay_max = d, d
        else:
            delay_min = self.dsb_delay_min.value()
            delay_max = max(self.dsb_delay_max.value(), delay_min)

        cfg = {
            "site":      site,
            "username":  username,
            "password":  password,
            "mode":      mode,
            "target":    target,
            "pages":     self.sp_pages.value(),
            "workers":   self.sp_workers.value(),
            "output":    self.le_output.text().strip(),
            "delay_min": delay_min,
            "delay_max": delay_max,
            "clear_notifications": (
                mode == "Submission Notifications" and self.chk_clear_notif.isChecked()
            ),
        }

        self.te_log.clear()
        self.pb_overall.setMaximum(100)
        self.pb_overall.setValue(0)
        self.pb_current.setMaximum(100)
        self.pb_current.setValue(0)
        self.lbl_overall_count.setText("–")
        self.lbl_current_file.setText("")
        self._current_preview_pixmap = None
        self.lbl_preview.setText("No preview")

        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.statusbar.showMessage(f"Downloading from {site}…")

        self.worker = DownloadWorker(cfg, verbose=self.chk_verbose.isChecked())
        self.worker.log.connect(self._append_log)
        self.worker.error.connect(self._append_error)
        self.worker.done.connect(self._on_done)
        self.worker.progress.connect(self._update_progress)
        self.worker.file_progress.connect(self._update_file_progress)
        self.worker.preview.connect(self._update_preview)
        self.worker.start()

    def _cancel(self):
        if self.worker:
            self.worker.cancel()
        self.btn_cancel.setEnabled(False)
        self.statusbar.showMessage("Cancelling…")

    def _on_done(self, ok: bool, msg: str):
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        if ok or msg == "Cancelled.":
            self._append_log(msg)
        else:
            self._append_error(msg)
        self.statusbar.showMessage(msg)
        self.lbl_current_file.setText("")
        self.pb_current.setMaximum(100)
        self.pb_current.setValue(0)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("FA & Inkbunny Downloader")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
