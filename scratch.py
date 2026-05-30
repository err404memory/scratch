#!/usr/bin/env python3
"""scratch — always-on-top sticky pad: Quill editor, split panes, global terminal, Ollama."""

import fcntl
import json
import logging
import os
import pty
import shlex
import shutil
import socket as _socket
import struct
import subprocess
import sys
import termios
import tempfile
import threading
import time
import urllib.request
import urllib.parse
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, QPoint, QSize, QTimer, QUrl, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QCursor, QIcon, QKeySequence, QShortcut
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineScript, QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMenu, QMessageBox, QPushButton, QSizePolicy, QSpacerItem, QSpinBox, QSplitter, QSystemTrayIcon, QTabWidget,
    QTextEdit, QVBoxLayout, QWidget, QInputDialog,
)

from scratch_core import (
    SHORTCUTS,
    Rect,
    create_shortcuts,
    is_livecodes_content_config,
    livecodes_config_from_source,
    livecodes_source_from_config,
    livecodes_url,
    ollama_generate_payload,
    ollama_stream_chunks,
    plain_text_from_html,
    preformatted_html,
    resize_rect,
    start_livecodes_server,
)

logger = logging.getLogger(__name__)

DATA_FILE    = Path.home() / ".scratch-notes" / "notes.json"
CONFIG_FILE  = Path.home() / ".scratch-notes" / "config.json"
BACKUP_DIR   = DATA_FILE.parent / "backups"
MAX_NOTE_BACKUPS = 100
ASSETS_DIR   = Path(__file__).parent / "assets"
EDITOR_URL   = QUrl.fromLocalFile(str(ASSETS_DIR / "editor.html"))
LIVECODES_URL = QUrl.fromLocalFile(str(ASSETS_DIR / "livecodes_pane.html"))
TERMINAL_URL = QUrl.fromLocalFile(str(ASSETS_DIR / "terminal.html"))
SOCKET_PATH  = f"/tmp/scratch-{os.getuid()}.sock"

DEFAULT_UI_SETTINGS = {
    "window_color": "#1a1a2e",
    "title_bar_color": "#16213e",
    "page_rail_color": "#16213e",
    "border_color": "#2d2d4e",
    "button_color": "#c5d0e0",
    "button_hover": "#263451",
    "button_border": "#3b4764",
    "pin_glow_color": "#42d9ff",
    "border_radius": 8,
    "button_radius": 5,
    "toolbar_padding": 6,
    "toolbar_button_spacing": 3,
    "toolbar_group_spacing": 10,
    "page_rail_padding": 6,
    "button_size": 27,
    "button_height": 25,
    "start_pinned": True,
    "ctrl_wheel_pages": True,
    "hide_on_close": True,
}


def _hex_color(value, fallback):
    value = str(value or "").strip()
    if len(value) == 7 and value[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in value[1:]):
        return value.lower()
    return fallback


def _bounded_int(value, fallback, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return fallback


def normalized_ui_settings(raw):
    ui = DEFAULT_UI_SETTINGS.copy()
    if isinstance(raw, dict):
        ui.update(raw)
    for key in (
        "window_color",
        "title_bar_color",
        "page_rail_color",
        "border_color",
        "button_color",
        "button_hover",
        "button_border",
        "pin_glow_color",
    ):
        ui[key] = _hex_color(ui.get(key), DEFAULT_UI_SETTINGS[key])
    ui["border_radius"] = _bounded_int(ui.get("border_radius"), 8, 0, 24)
    ui["button_radius"] = _bounded_int(ui.get("button_radius"), 5, 0, 14)
    ui["toolbar_padding"] = _bounded_int(ui.get("toolbar_padding"), 6, 0, 18)
    ui["toolbar_button_spacing"] = _bounded_int(ui.get("toolbar_button_spacing"), 3, 0, 12)
    ui["toolbar_group_spacing"] = _bounded_int(ui.get("toolbar_group_spacing"), 10, 0, 24)
    ui["page_rail_padding"] = _bounded_int(ui.get("page_rail_padding"), 6, 0, 18)
    ui["button_size"] = _bounded_int(ui.get("button_size"), 27, 24, 34)
    ui["button_height"] = _bounded_int(ui.get("button_height"), 25, 22, 32)
    ui["start_pinned"] = bool(ui.get("start_pinned"))
    ui["ctrl_wheel_pages"] = bool(ui.get("ctrl_wheel_pages"))
    ui["hide_on_close"] = bool(ui.get("hide_on_close"))
    return ui


def _hex_to_rgb(value):
    value = _hex_color(value, DEFAULT_UI_SETTINGS["pin_glow_color"])
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def style_from_ui(raw):
    ui = normalized_ui_settings(raw)
    glow_r, glow_g, glow_b = _hex_to_rgb(ui["pin_glow_color"])
    radius = ui["border_radius"]
    button_radius = ui["button_radius"]
    return f"""
QWidget#root {{ background: {ui["window_color"]}; border: 1px solid {ui["border_color"]}; border-radius: {radius}px; }}
QFrame#topbar {{ background: {ui["title_bar_color"]}; border-top-left-radius: {radius}px; border-top-right-radius: {radius}px; }}
QFrame#navbar {{ background: {ui["page_rail_color"]}; border-bottom-left-radius: {radius}px; border-bottom-right-radius: {radius}px; }}
QSplitter::handle {{ background: {ui["border_color"]}; }}
QSplitter::handle:horizontal {{ width: 3px; }}
QSplitter::handle:vertical   {{ height: 4px; }}
QPushButton {{
    background: rgba(255,255,255,.04); color: {ui["button_color"]};
    border: 1px solid {ui["button_border"]}; font-size: 16px;
    font-weight: 650; padding: 0; border-radius: {button_radius}px;
}}
QPushButton:hover {{ background: {ui["button_hover"]}; color: #ffffff; }}
QPushButton#command {{ background: rgba(255,255,255,.04); color: {ui["button_color"]}; }}
QPushButton#mode-on {{ background: #7ec8a4; color: #0f1720; font-weight: 700; }}
QPushButton#tool-on {{ background: #7cc4ff; color: #0f1720; font-weight: 700; }}
QPushButton#pin-on  {{
    color: #dffcff;
    background: qradialgradient(cx:.5, cy:.52, radius:.42,
        stop:0 rgba({glow_r},{glow_g},{glow_b},150), stop:.46 rgba({glow_r},{glow_g},{glow_b},56), stop:1 rgba({glow_r},{glow_g},{glow_b},0));
}}
QPushButton#pin-on:hover {{
    color: #ffffff;
    background: qradialgradient(cx:.5, cy:.52, radius:.45,
        stop:0 rgba({glow_r},{glow_g},{glow_b},190), stop:.48 rgba({glow_r},{glow_g},{glow_b},70), stop:1 rgba({glow_r},{glow_g},{glow_b},0));
}}
QPushButton#pin-off {{ color: #6f7894; background: rgba(255,255,255,.04); }}
QPushButton#danger  {{ color: #ff8f9a; }}
QPushButton#add     {{ color: #7ec8a4; }}
QPushButton#del     {{ color: #ff5f6d; }}
QPushButton#nav     {{ color: #9aacd0; font-size: 14px; background: transparent; border: none; }}
QPushButton#nav:hover    {{ background: {ui["button_hover"]}; color: #e6edf3; }}
QPushButton#nav:disabled {{ color: #2d2d4e; background: transparent; }}
QLabel#page-label {{ color: #9aacd0; font-size: 11px; font-weight: 700; }}
"""

# ── single-instance ──────────────────────────────────────────────────────────

def _acquire_instance_lock():
    """
    Try to bind a Unix socket as an instance lock.
    Returns the bound socket if we are the first instance.
    If another instance is running, sends it a 'show' signal and exits.
    """
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        sock.bind(SOCKET_PATH)
        sock.listen(5)
        return sock
    except OSError:
        try:
            client = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            client.settimeout(1.0)
            client.connect(SOCKET_PATH)
            client.sendall(b"show")
            client.close()
        except OSError:
            try:
                os.unlink(SOCKET_PATH)
            except OSError:
                pass
            return _acquire_instance_lock()
        sys.exit(0)


def _start_instance_listener(lock_sock, on_show):
    """Background thread: accept show-signals from future invocations."""
    def _run():
        while True:
            try:
                conn, _ = lock_sock.accept()
                data = conn.recv(64)
                conn.close()
                if data == b"show":
                    on_show()
            except OSError:
                break
    threading.Thread(target=_run, daemon=True).start()


# ── PTY ─────────────────────────────────────────────────────────────────────

class PtyManager:
    """Manages a pseudo-terminal (PTY) subprocess with asynchronous I/O."""
    def __init__(self, on_data, on_exit):
        self._on_data   = on_data
        self._on_exit   = on_exit
        self._master_fd = None
        self._pid       = None
        self._thread    = None

    def start(self, cwd=None):
        self.stop()
        shell = os.environ.get("SHELL", "/bin/bash")
        env   = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
        pid, master_fd = pty.fork()
        if pid == 0:
            if cwd:
                try: os.chdir(cwd)
                except OSError: pass
            os.execvpe(shell, [shell], env)
        self._pid, self._master_fd = pid, master_fd
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self):
        while self._master_fd is not None:
            try:
                data = os.read(self._master_fd, 4096)
                if data:
                    self._on_data(data.decode("utf-8", errors="replace"))
            except OSError:
                break
        self._on_exit()

    def write(self, data):
        if self._master_fd is not None:
            try: os.write(self._master_fd, data.encode())
            except OSError: pass

    def resize(self, cols, rows):
        if self._master_fd is not None:
            try:
                fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", rows, cols, 0, 0))
            except OSError: pass

    def stop(self):
        if self._pid:
            try:
                os.kill(self._pid, 9)
            except ProcessLookupError:
                pass
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
        self._pid = self._master_fd = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)


# ── bridges ──────────────────────────────────────────────────────────────────

class QuillBridge(QObject):
    """Bridge exposing Qt slots for Quill editor events to Python."""
    def __init__(self, pane):
        super().__init__()
        self._pane = pane

    @pyqtSlot()
    def ready(self):
        self._pane.on_editor_ready()

    @pyqtSlot(str)
    def contentChanged(self, html):
        self._pane.on_content_changed(html)

    @pyqtSlot()
    def requestFocus(self):
        # Give Qt focus to WebEngine first, then put Chromium focus back into
        # the textarea. Focusing the view after editor.focus() can steal typing.
        self._pane._edit_mode = True
        self._pane.view.setFocus(Qt.FocusReason.OtherFocusReason)
        self._pane._pad._sync_command_states()
        QTimer.singleShot(0, lambda: self._pane.view.page().runJavaScript("focusEditor()"))
        QTimer.singleShot(80, lambda: self._pane.view.page().runJavaScript("focusEditor()"))

    @pyqtSlot()
    def snapshotDone(self):
        self._pane._on_snapshot_done()

    @pyqtSlot()
    def openContextMenu(self):
        self._pane._pad._open_context_menu_at(QCursor.pos())


class TerminalBridge(QObject):
    """Bridge exposing Qt slots for terminal I/O between Python PTY and JS."""
    terminalOutputSignal = pyqtSignal(str)
    cwdSignal            = pyqtSignal(str)
    fitSignal            = pyqtSignal()

    def __init__(self, window):
        super().__init__()
        self._window = window

    @pyqtSlot()
    def ready(self):
        self._window._on_terminal_ready()

    @pyqtSlot(int, int)
    def terminalReady(self, cols, rows):
        self._window.global_pty.resize(cols, rows)

    @pyqtSlot(str)
    def terminalInput(self, data):
        self._window.global_pty.write(data)

    @pyqtSlot(int, int)
    def terminalResize(self, cols, rows):
        self._window.global_pty.resize(cols, rows)

    @pyqtSlot()
    def closeTerminal(self):
        self._window._toggle_terminal()


# ── Quill pane ───────────────────────────────────────────────────────────────

class QuillPane(QWidget):
    """Container pane hosting the LiveCodes-backed editor/preview."""
    content_changed = pyqtSignal(int, str)

    def __init__(self, pad, initial_page=0):
        super().__init__()
        self._pad          = pad
        self._page_index   = initial_page
        self._editor_ready = False
        self._pending_html = None
        self._edit_mode    = False

        self._snapshot_callback = None
        self._snapshot_fallback = QTimer(self)
        self._snapshot_fallback.setSingleShot(True)
        self._snapshot_fallback.timeout.connect(self._on_snapshot_done)

        self.bridge  = QuillBridge(self)
        self.channel = QWebChannel(self)
        self.channel.registerObject("bridge", self.bridge)
        self.view = QWebEngineView(self)
        self.view.setMinimumSize(QSize(0, 0))
        s = self.view.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.view.page().setWebChannel(self.channel)
        self._install_context_menu_bridge()
        livecodes_url_obj = QUrl(LIVECODES_URL)
        livecodes_url_obj.setQuery(urllib.parse.urlencode({"appUrl": self._pad.livecodes_app_url}))
        self.view.setUrl(livecodes_url_obj)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.view.installEventFilter(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

    @property
    def page_index(self):
        return self._page_index

    def load_page(self, index):
        self._page_index = index
        html = self._pad.notes["pages"][index]
        if self._editor_ready:
            self._send_content(html)
        else:
            self._pending_html = html

    def on_editor_ready(self):
        self._editor_ready = True
        if self._pending_html is not None:
            self._send_content(self._pending_html)
            self._pending_html = None

    def on_content_changed(self, content):
        target_page = self._page_index
        allow_empty = False
        try:
            payload = json.loads(content)
            if isinstance(payload, dict) and "config" in payload:
                target_page = int(payload.get("scratchPageId", target_page))
                allow_empty = bool(payload.get("allowEmpty"))
                payload = payload.get("config", {})
                if not is_livecodes_content_config(payload):
                    logger.warning("Ignored invalid LiveCodes save payload for page %s", target_page + 1)
                    return
                source = livecodes_source_from_config(payload)
            elif is_livecodes_content_config(payload):
                source = livecodes_source_from_config(payload)
            else:
                source = content
        except Exception:
            source = content
        if target_page < 0 or target_page >= len(self._pad.notes["pages"]):
            return
        current_source = self._pad.notes["pages"][target_page]
        if source == "" and current_source.strip() and not allow_empty:
            logger.warning("Ignored empty LiveCodes save over non-empty page %s", target_page + 1)
            return
        self._pad.notes["pages"][target_page] = source
        self._pad.schedule_save()
        self.content_changed.emit(target_page, source)

    def _send_content(self, source):
        config = livecodes_config_from_source(source)
        self.view.page().runJavaScript(
            f"loadNoteSource({json.dumps(source)}, {json.dumps(json.dumps(config))}, {self._page_index})")

    def capture_current_content(self):
        if self._editor_ready:
            self.view.page().runJavaScript(f"captureLiveCodesConfig({self._page_index})")

    def capture_and_then(self, callback, fallback_ms=500):
        """Capture editor content, then invoke callback once JS confirms done (or timeout)."""
        self._snapshot_callback = callback
        self._snapshot_fallback.start(fallback_ms)
        if self._editor_ready:
            self.view.page().runJavaScript(f"captureLiveCodesConfig({self._page_index})")
        else:
            self._on_snapshot_done()

    def _on_snapshot_done(self):
        self._snapshot_fallback.stop()
        cb = self._snapshot_callback
        self._snapshot_callback = None
        if cb:
            cb()

    def get_share_payload(self, callback):
        self.view.page().runJavaScript("getSharePayload()", callback)

    def run_livecodes_command(self, method, args=None):
        self.view.page().runJavaScript(
            f"callLiveCodesApi({json.dumps(method)}, {json.dumps(args or [])})"
        )

    def run_livecodes_edit_command(self, command):
        self.view.page().runJavaScript(
            f"callLiveCodesEditCommand({json.dumps(command)})"
        )

    def _install_context_menu_bridge(self):
        script = QWebEngineScript()
        script.setName("scratch-context-menu-bridge")
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(True)
        script.setSourceCode(
            """
            document.addEventListener('contextmenu', function(event) {
                event.preventDefault();
                event.stopPropagation();
                event.stopImmediatePropagation();
                window.top.postMessage({ type: 'scratch-open-context-menu' }, '*');
            }, true);
            window.addEventListener('message', function(event) {
                var data = event.data || {};
                if (data.type !== 'scratch-edit-command' || !data.command) return;
                document.execCommand(data.command);
            });
            """
        )
        self.view.page().scripts().insert(script)

    def eventFilter(self, obj, event):
        if obj is self.view and event.type() == QEvent.Type.MouseButtonPress:
            self.window().activateWindow()
            self.window().raise_()
        return super().eventFilter(obj, event)

    def toggle_edit_mode(self):
        self.set_edit_mode(not self._edit_mode)

    def set_edit_mode(self, enabled):
        self._edit_mode = bool(enabled)
        self.view.page().runJavaScript(
            f"setEditMode({json.dumps(self._edit_mode)})")


# ── drag handle ──────────────────────────────────────────────────────────────

class DragHandle(QFrame):
    """Draggable top bar for moving the frameless window."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_pos = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (e.globalPosition().toPoint()
                              - self.window().frameGeometry().topLeft())
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() == Qt.MouseButton.LeftButton:
            self.window().move(e.globalPosition().toPoint() - self._drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag_pos = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        e.accept()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and hasattr(self.window(), "_toggle_pin"):
            self.window()._toggle_pin()


class ResizeHandle(QFrame):
    """Transparent frameless-window resize handle for one edge or corner."""

    CURSORS = {
        "top": Qt.CursorShape.SizeVerCursor,
        "bottom": Qt.CursorShape.SizeVerCursor,
        "left": Qt.CursorShape.SizeHorCursor,
        "right": Qt.CursorShape.SizeHorCursor,
        "top-left": Qt.CursorShape.SizeFDiagCursor,
        "bottom-right": Qt.CursorShape.SizeFDiagCursor,
        "top-right": Qt.CursorShape.SizeBDiagCursor,
        "bottom-left": Qt.CursorShape.SizeBDiagCursor,
    }

    def __init__(self, edge, parent=None):
        super().__init__(parent)
        self.edge = edge
        self._drag_pos = None
        self._start_geom = None
        self.setMouseTracking(True)
        self.setCursor(self.CURSORS.get(edge, Qt.CursorShape.ArrowCursor))
        self.setToolTip(f"Resize window ({edge})")
        self.setStyleSheet("background: transparent;")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint()
            self._start_geom = self.window().geometry()
            e.accept()

    def mouseMoveEvent(self, e):
        if not self._drag_pos or not (e.buttons() & Qt.MouseButton.LeftButton):
            return
        current = e.globalPosition().toPoint()
        geom = resize_rect(
            Rect(
                self._start_geom.x(),
                self._start_geom.y(),
                self._start_geom.width(),
                self._start_geom.height(),
            ),
            (self._drag_pos.x(), self._drag_pos.y()),
            (current.x(), current.y()),
            self.edge,
            minimum_size=(420, 260),
        )
        self.window().setGeometry(geom.x, geom.y, geom.width, geom.height)
        e.accept()

    def mouseReleaseEvent(self, e):
        self._drag_pos = None
        self._start_geom = None
        if hasattr(self.window(), "_flush_save"):
            self.window()._flush_save()
        e.accept()


class ResizeGrip(ResizeHandle):
    """Visible bottom-right resize cue used in the page rail."""

    def __init__(self, parent=None):
        super().__init__("bottom-right", parent)
        self.setFixedSize(QSize(18, 18))
        self.setToolTip("Resize window")


class ConfigDialog(QDialog):
    """Small configuration panel for AI and share targets."""

    def __init__(self, parent, config):
        super().__init__(parent)
        self.setWindowTitle("Scratch configuration")
        self.resize(560, 560)
        self._config = config
        root = QVBoxLayout(self)
        tabs = QTabWidget(self)
        root.addWidget(tabs)

        telegram = config.get("telegram", {})
        tg_tab = QWidget(self)
        tg_form = QFormLayout(tg_tab)
        self.telegram_token = QLineEdit(telegram.get("bot_token", ""))
        self.telegram_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.telegram_chat = QLineEdit(telegram.get("default_chat_id", ""))
        self.telegram_parse_mode = QComboBox()
        self.telegram_parse_mode.addItems(["", "HTML", "MarkdownV2"])
        self.telegram_parse_mode.setCurrentText(telegram.get("parse_mode", ""))
        self.telegram_recent = QListWidget()
        for chat in telegram.get("recent_chats", []):
            label = chat.get("title") or chat.get("username") or str(chat.get("id", ""))
            self.telegram_recent.addItem(f"{label} :: {chat.get('id', '')}")
        refresh_btn = QPushButton("Fetch recent chats")
        refresh_btn.clicked.connect(self._fetch_recent_chats)
        tg_form.addRow("Bot token", self.telegram_token)
        tg_form.addRow("Default chat id", self.telegram_chat)
        tg_form.addRow("Parse mode", self.telegram_parse_mode)
        tg_form.addRow(refresh_btn)
        tg_form.addRow("Recent chats", self.telegram_recent)
        tabs.addTab(tg_tab, "Telegram")

        ollama = config.get("ollama", {})
        ollama_tab = QWidget(self)
        ollama_form = QFormLayout(ollama_tab)
        self.ollama_base_url = QLineEdit(ollama.get("base_url", "http://localhost:11434"))
        self.ollama_model = QLineEdit(ollama.get("model", "llama3.2"))
        self.ollama_system = QTextEdit(ollama.get("system", ""))
        self.ollama_system.setMinimumHeight(120)
        ollama_form.addRow("Base URL", self.ollama_base_url)
        ollama_form.addRow("Model", self.ollama_model)
        ollama_form.addRow("System prompt", self.ollama_system)
        tabs.addTab(ollama_tab, "Ollama")

        targets_tab = QWidget(self)
        targets_layout = QVBoxLayout(targets_tab)
        targets_help = QLabel(
            "Share targets JSON. Kinds: local_folder, scp, sftp, taildrop, ntfy, command.\n"
            "Examples:\n"
            "[{\"name\":\"Nova inbox\",\"kind\":\"scp\",\"destination\":\"nova:/home/ash/inbox/\"},\n"
            " {\"name\":\"Atlas Taildrop\",\"kind\":\"taildrop\",\"device\":\"atlas\"},\n"
            " {\"name\":\"AI clipboard\",\"kind\":\"command\",\"command\":[\"xclip\",\"-selection\",\"clipboard\"]}]"
        )
        targets_help.setWordWrap(True)
        self.share_targets = QTextEdit(json.dumps(config.get("share_targets", []), indent=2))
        self.share_targets.setMinimumHeight(240)
        targets_layout.addWidget(targets_help)
        targets_layout.addWidget(self.share_targets, 1)
        tabs.addTab(targets_tab, "Share targets")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _fetch_recent_chats(self):
        token = self.telegram_token.text().strip()
        if not token:
            QMessageBox.warning(self, "Telegram", "Add a bot token first.")
            return
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            with urllib.request.urlopen(url, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            chats = {}
            for update in data.get("result", []):
                message = update.get("message") or update.get("channel_post") or {}
                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                if chat_id is None:
                    continue
                chats[str(chat_id)] = {
                    "id": chat_id,
                    "title": chat.get("title") or " ".join(
                        p for p in [chat.get("first_name"), chat.get("last_name")] if p
                    ),
                    "username": chat.get("username", ""),
                }
            self.telegram_recent.clear()
            for chat in chats.values():
                label = chat.get("title") or chat.get("username") or str(chat.get("id"))
                self.telegram_recent.addItem(f"{label} :: {chat.get('id')}")
            QMessageBox.information(self, "Telegram", f"Loaded {len(chats)} recent chats.")
        except Exception as e:
            QMessageBox.warning(self, "Telegram", f"Could not fetch chats:\n{e}")

    def value(self):
        try:
            share_targets = json.loads(self.share_targets.toPlainText() or "[]")
            if not isinstance(share_targets, list):
                raise ValueError("share_targets must be a list")
        except Exception as e:
            raise ValueError(f"Invalid share targets JSON: {e}") from e

        recent = []
        for row in range(self.telegram_recent.count()):
            text = self.telegram_recent.item(row).text()
            if "::" not in text:
                continue
            label, chat_id = [part.strip() for part in text.rsplit("::", 1)]
            recent.append({"title": label, "id": chat_id})

        return {
            "telegram": {
                "bot_token": self.telegram_token.text().strip(),
                "default_chat_id": self.telegram_chat.text().strip(),
                "parse_mode": self.telegram_parse_mode.currentText().strip(),
                "recent_chats": recent,
            },
            "ollama": {
                "base_url": self.ollama_base_url.text().strip() or "http://localhost:11434",
                "model": self.ollama_model.text().strip() or "llama3.2",
                "system": self.ollama_system.toPlainText(),
            },
            "share_targets": share_targets,
            "ui": self._config.get("ui", normalized_ui_settings({})),
        }


class UiSettingsDialog(QDialog):
    """Context-menu panel for window appearance and low-friction behaviors."""

    def __init__(self, parent, ui_config):
        super().__init__(parent)
        self.setWindowTitle("Scratch window UI")
        self.resize(460, 440)
        ui = normalized_ui_settings(ui_config)
        root = QVBoxLayout(self)
        form = QFormLayout()
        root.addLayout(form, 1)

        def color_row(label, key):
            field = QLineEdit(ui[key])
            field.setPlaceholderText("#16213e")
            form.addRow(label, field)
            return field

        def int_row(label, key, minimum, maximum):
            field = QSpinBox(self)
            field.setRange(minimum, maximum)
            field.setValue(ui[key])
            form.addRow(label, field)
            return field

        self.window_color = color_row("Window background", "window_color")
        self.title_bar_color = color_row("Title bar color", "title_bar_color")
        self.page_rail_color = color_row("Page rail color", "page_rail_color")
        self.border_color = color_row("Border color", "border_color")
        self.button_color = color_row("Button icon color", "button_color")
        self.button_hover = color_row("Button hover color", "button_hover")
        self.button_border = color_row("Button border color", "button_border")
        self.pin_glow_color = color_row("Pin glow color", "pin_glow_color")

        self.border_radius = int_row("Window radius", "border_radius", 0, 24)
        self.button_radius = int_row("Button radius", "button_radius", 0, 14)
        self.toolbar_padding = int_row("Toolbar side padding", "toolbar_padding", 0, 18)
        self.toolbar_button_spacing = int_row("Button spacing", "toolbar_button_spacing", 0, 12)
        self.toolbar_group_spacing = int_row("Group spacing", "toolbar_group_spacing", 0, 24)
        self.page_rail_padding = int_row("Page rail side padding", "page_rail_padding", 0, 18)
        self.button_size = int_row("Button width", "button_size", 24, 34)
        self.button_height = int_row("Button height", "button_height", 22, 32)

        self.start_pinned = QCheckBox("Start pinned / always on top")
        self.start_pinned.setChecked(ui["start_pinned"])
        self.ctrl_wheel_pages = QCheckBox("Ctrl + mouse wheel changes pages")
        self.ctrl_wheel_pages.setChecked(ui["ctrl_wheel_pages"])
        self.hide_on_close = QCheckBox("Close hides to tray instead of quitting")
        self.hide_on_close.setChecked(ui["hide_on_close"])
        form.addRow("Startup", self.start_pinned)
        form.addRow("Navigation", self.ctrl_wheel_pages)
        form.addRow("Close behavior", self.hide_on_close)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.RestoreDefaults |
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self._restore_defaults)
        root.addWidget(buttons)

    def _restore_defaults(self):
        defaults = normalized_ui_settings({})
        for key, widget in self._fields().items():
            if isinstance(widget, QLineEdit):
                widget.setText(defaults[key])
            elif isinstance(widget, QSpinBox):
                widget.setValue(defaults[key])
        self.start_pinned.setChecked(defaults["start_pinned"])
        self.ctrl_wheel_pages.setChecked(defaults["ctrl_wheel_pages"])
        self.hide_on_close.setChecked(defaults["hide_on_close"])

    def _fields(self):
        return {
            "window_color": self.window_color,
            "title_bar_color": self.title_bar_color,
            "page_rail_color": self.page_rail_color,
            "border_color": self.border_color,
            "button_color": self.button_color,
            "button_hover": self.button_hover,
            "button_border": self.button_border,
            "pin_glow_color": self.pin_glow_color,
            "border_radius": self.border_radius,
            "button_radius": self.button_radius,
            "toolbar_padding": self.toolbar_padding,
            "toolbar_button_spacing": self.toolbar_button_spacing,
            "toolbar_group_spacing": self.toolbar_group_spacing,
            "page_rail_padding": self.page_rail_padding,
            "button_size": self.button_size,
            "button_height": self.button_height,
        }

    def value(self):
        values = {}
        for key, widget in self._fields().items():
            if isinstance(widget, QLineEdit):
                values[key] = widget.text().strip()
            elif isinstance(widget, QSpinBox):
                values[key] = widget.value()
        values["start_pinned"] = self.start_pinned.isChecked()
        values["ctrl_wheel_pages"] = self.ctrl_wheel_pages.isChecked()
        values["hide_on_close"] = self.hide_on_close.isChecked()
        return normalized_ui_settings(values)


# ── main window ──────────────────────────────────────────────────────────────

class ScratchPad(QWidget):
    """Main application window containing panes, terminal, and controls."""
    _ollama_chunk = pyqtSignal(int, str)
    _ollama_done  = pyqtSignal(int, str)

    def __init__(self):
        super().__init__()
        self.notes = self._load()
        self.config = self._load_config()
        self._ui_settings = normalized_ui_settings(self.config.get("ui", {}))
        self.pinned = self._ui_settings["start_pinned"]
        self._panes: list[QuillPane] = []
        self._active_pane_index = 0
        self._shortcuts = []
        self._term_height = 200

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._flush_save)

        self.livecodes_app_url = livecodes_url()
        try:
            self._livecodes_thread, livecodes_port = start_livecodes_server()
            self.livecodes_app_url = livecodes_url(livecodes_port)
        except Exception as e:
            logger.warning("LiveCodes server was not started by Scratch: %s", e)

        self._ollama_chunk.connect(self._on_ollama_chunk)
        self._ollama_done.connect(self._on_ollama_done)

        self._build_window()
        self._build_ui()
        self._build_global_terminal()
        ws = self.notes.get("window", {})
        start_page = min(ws.get("active_page", 0), max(0, len(self.notes["pages"]) - 1))
        self._add_pane(page=start_page)
        self._update_nav()

        x, y, w, h = self._init_geometry
        self.resize(w, h)
        self.move(x, y)
        # Reapply after the event loop starts in case the layout fights back
        QTimer.singleShot(0, lambda: (self.resize(w, h), self.move(x, y)))

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self):
        if DATA_FILE.exists():
            try:
                raw = json.loads(DATA_FILE.read_text())
            except Exception as e:
                logger.warning("Failed to parse notes file %s: %s — starting with fresh notes", DATA_FILE, e)
                raw = {}
            if isinstance(raw, dict):
                raw_pages = raw.get("pages", [""])
                window = raw.get("window", {})
                pages = []
                for page in raw_pages:
                    if isinstance(page, dict):
                        # Migrate from LiveCodes v2 format
                        content = page.get("markup", {}).get("content", "")
                        pages.append(content)
                    elif isinstance(page, str):
                        pages.append(page)
                    else:
                        pages.append("")
                return {"pages": pages or [""], "window": window}
            if isinstance(raw, list):
                pages = [p if isinstance(p, str) else "" for p in raw]
                return {"pages": pages or [""], "window": {}}
        return {"pages": [""], "window": {}}

    def _default_config(self):
        return {
            "telegram": {
                "bot_token": "",
                "default_chat_id": "",
                "parse_mode": "",
                "recent_chats": [],
            },
            "ollama": {
                "base_url": "http://localhost:11434",
                "model": "llama3.2",
                "system": "",
            },
            "share_targets": [
                {
                    "name": "Local Scratch shares",
                    "kind": "local_folder",
                    "path": str(Path.home() / ".scratch-notes" / "shares"),
                    "format": "html",
                }
            ],
            "ui": normalized_ui_settings({}),
        }

    def _load_config(self):
        config = self._default_config()
        if CONFIG_FILE.exists():
            try:
                raw = json.loads(CONFIG_FILE.read_text())
            except Exception as e:
                logger.warning("Failed to parse config file %s: %s", CONFIG_FILE, e)
                raw = {}
            if isinstance(raw, dict):
                for section, defaults in config.items():
                    if isinstance(defaults, dict) and isinstance(raw.get(section), dict):
                        defaults.update(raw[section])
                    elif section in raw:
                        config[section] = raw[section]
        return config

    def _save_config(self):
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(self.config, indent=2, ensure_ascii=False))
        CONFIG_FILE.chmod(0o600)

    def schedule_save(self):
        self._save_timer.start(400)

    def _backup_notes_file(self, next_payload):
        if not DATA_FILE.exists():
            return
        try:
            current_payload = DATA_FILE.read_text()
        except OSError as e:
            logger.warning("Failed to read notes file before backup: %s", e)
            return
        if not current_payload.strip() or current_payload == next_payload:
            return
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            millis = int((time.time() % 1) * 1000)
            backup_path = BACKUP_DIR / f"notes-{timestamp}-{millis:03d}.json"
            backup_path.write_text(current_payload)
            backup_path.chmod(0o600)
            backups = sorted(
                BACKUP_DIR.glob("notes-*.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for old_backup in backups[MAX_NOTE_BACKUPS:]:
                old_backup.unlink()
        except OSError as e:
            logger.warning("Failed to create Scratch notes backup: %s", e)

    def _flush_save(self):
        current_geometry = (self.x(), self.y(), self.width(), self.height())
        self._init_geometry = current_geometry
        self.notes["window"] = {
            "x": current_geometry[0],
            "y": current_geometry[1],
            "width": current_geometry[2],
            "height": current_geometry[3],
            "h_split_sizes": self.h_split.sizes(),
            "term_height": self._term_height,
            "active_page": self._active_pane().page_index,
        }
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.notes, indent=2, ensure_ascii=False)
        self._backup_notes_file(payload)
        temp_file = DATA_FILE.with_suffix(DATA_FILE.suffix + ".tmp")
        temp_file.write_text(payload)
        temp_file.chmod(0o600)
        os.replace(temp_file, DATA_FILE)
        DATA_FILE.chmod(0o600)

    # ── window ───────────────────────────────────────────────────────────────

    def _build_window(self):
        ws = self.notes.get("window", {})
        self.setWindowTitle("Scratch")
        self.setObjectName("root")
        self.setWindowFlags(self._window_flags_for_pin(self.pinned))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(420, 260)
        self.setStyleSheet(style_from_ui(self._ui_settings))

        w = ws.get("width", 440)
        h = ws.get("height", 460)

        if "x" in ws and "y" in ws:
            x, y = ws["x"], ws["y"]
            screen = (QApplication.screenAt(QPoint(x + w // 2, y + h // 2))
                      or QApplication.primaryScreen())
            avail = screen.availableGeometry()
            x = max(avail.left() + 10, min(x, avail.right()  - w - 10))
            y = max(avail.top()  + 10, min(y, avail.bottom() - h - 10))
        else:
            avail = QApplication.primaryScreen().availableGeometry()
            pad = 40
            x = avail.right()  - w - pad
            y = avail.top()    +     pad

        # Store and apply AFTER all child widgets are built so the layout
        # cannot expand the window past the intended size.
        self._init_geometry = (x, y, w, h)

    def _window_flags_for_pin(self, pinned):
        flags = Qt.WindowType.FramelessWindowHint
        if pinned:
            flags |= Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        return flags

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        topbar = DragHandle(self)
        topbar.setObjectName("topbar")
        topbar.setFixedHeight(max(36, self._ui_settings["button_height"] + 12))
        self._topbar = topbar
        top = QHBoxLayout(topbar)
        top.setContentsMargins(self._ui_settings["toolbar_padding"], 0, self._ui_settings["toolbar_padding"], 0)
        top.setSpacing(self._ui_settings["toolbar_button_spacing"])
        self._top_layout = top
        self._toolbar_group_spacers = []

        def btn(label, obj_name="command", tip=None):
            b = QPushButton(label)
            b.setFixedSize(QSize(self._ui_settings["button_size"], self._ui_settings["button_height"]))
            if obj_name: b.setObjectName(obj_name)
            if tip:      b.setToolTip(tip)
            self._toolbar_buttons.append(b)
            return b

        self._toolbar_buttons = []

        self.pin_btn = btn("📌", obj_name="pin-on", tip="Pin window  (Ctrl+P, double-click top bar)")
        self.pin_btn.clicked.connect(self._toggle_pin)

        self.page_label = QLabel("1 / 1")
        self.page_label.setObjectName("page-label")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.add_btn = btn("+", obj_name="add", tip="New page  (Ctrl+N)")
        self.del_btn = btn("🗑", obj_name="del", tip="Delete current page  (Ctrl+W)")
        self.ask_btn = btn("🤖", tip="Ask Ollama  (Ctrl+Shift+A)")
        self.term_btn = btn("⌨", tip="Toggle terminal  (Ctrl+T)")
        self.split_btn = btn("◫", tip="Split pane  (Ctrl+\\); close splits with Ctrl+Shift+\\")
        self.hide_btn = btn("–", tip="Hide to tray  (Ctrl+H)")
        self.quit_btn = btn("×", obj_name="danger", tip="Quit  (Ctrl+Q)")

        self.add_btn.clicked.connect(self._new_page)
        self.del_btn.clicked.connect(self._delete_page)
        self.ask_btn.clicked.connect(self._ask_ollama)
        self.term_btn.clicked.connect(self._toggle_terminal)
        self.split_btn.clicked.connect(self._toggle_split_panes)
        self.hide_btn.clicked.connect(self.hide)
        self.quit_btn.clicked.connect(self._quit)

        def add_group(*widgets):
            if top.count():
                spacer = QSpacerItem(
                    self._ui_settings["toolbar_group_spacing"],
                    0,
                    QSizePolicy.Policy.Fixed,
                    QSizePolicy.Policy.Minimum,
                )
                self._toolbar_group_spacers.append(spacer)
                top.addItem(spacer)
            for widget in widgets:
                top.addWidget(widget)

        add_group(self.pin_btn)
        add_group(self.add_btn, self.del_btn)
        add_group(self.ask_btn, self.term_btn, self.split_btn)
        top.addStretch()
        add_group(self.hide_btn, self.quit_btn)

        self.h_split = QSplitter(Qt.Orientation.Horizontal, self)
        self.h_split.setChildrenCollapsible(False)

        navbar = QFrame(self)
        navbar.setObjectName("navbar")
        navbar.setFixedHeight(34)
        nav = QGridLayout(navbar)
        nav.setContentsMargins(self._ui_settings["page_rail_padding"], 0, self._ui_settings["page_rail_padding"], 0)
        nav.setHorizontalSpacing(4)
        self._nav_layout = nav
        nav.setColumnStretch(0, 1)
        nav.setColumnStretch(1, 1)
        nav.setColumnStretch(2, 1)

        self.prev_btn = btn("◀", obj_name="nav", tip="Previous page  (Ctrl+Left, Ctrl+wheel up)")
        self.prev_btn.clicked.connect(self._prev_page)

        self.next_btn = btn("▶", obj_name="nav", tip="Next page  (Ctrl+Right, Ctrl+wheel down)")
        self.next_btn.clicked.connect(self._next_page)

        right_nav = QWidget(self)
        right_nav_layout = QHBoxLayout(right_nav)
        right_nav_layout.setContentsMargins(0, 0, 0, 0)
        right_nav_layout.setSpacing(4)
        right_nav_layout.addStretch()
        right_nav_layout.addWidget(self.next_btn)

        nav.addWidget(self.prev_btn, 0, 0, Qt.AlignmentFlag.AlignLeft)
        nav.addWidget(self.page_label, 0, 1, Qt.AlignmentFlag.AlignCenter)
        nav.addWidget(right_nav, 0, 2)

        root.addWidget(topbar)
        root.addWidget(self.h_split, 1)
        root.addWidget(navbar)

        self._install_resize_handles()

        self._shortcuts = create_shortcuts(
            self,
            SHORTCUTS,
            shortcut_cls=QShortcut,
            keyseq_cls=QKeySequence,
        )

    def _install_resize_handles(self):
        edges = (
            "top",
            "bottom",
            "left",
            "right",
            "top-left",
            "top-right",
            "bottom-left",
            "bottom-right",
        )
        self._resize_handles = {edge: ResizeHandle(edge, self) for edge in edges}
        self._position_resize_handles()

    def _position_resize_handles(self):
        handles = getattr(self, "_resize_handles", {})
        if not handles:
            return
        margin = 8
        corner = 16
        width = self.width()
        height = self.height()
        horizontal_width = max(0, width - (corner * 2))
        vertical_height = max(0, height - (corner * 2))

        handles["top"].setGeometry(corner, 0, horizontal_width, margin)
        handles["bottom"].setGeometry(corner, height - margin, horizontal_width, margin)
        handles["left"].setGeometry(0, corner, margin, vertical_height)
        handles["right"].setGeometry(width - margin, corner, margin, vertical_height)
        handles["top-left"].setGeometry(0, 0, corner, corner)
        handles["top-right"].setGeometry(width - corner, 0, corner, corner)
        handles["bottom-left"].setGeometry(0, height - corner, corner, corner)
        handles["bottom-right"].setGeometry(width - corner, height - corner, corner, corner)

        for handle in handles.values():
            handle.raise_()
            handle.show()

    def _build_global_terminal(self):
        self.global_term = QWebEngineView(self)
        self.global_term.setMinimumSize(QSize(0, 0))
        s = self.global_term.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        self.global_term_bridge = TerminalBridge(self)
        self.global_term_channel = QWebChannel(self)
        self.global_term_channel.registerObject("bridge", self.global_term_bridge)
        self.global_term.page().setWebChannel(self.global_term_channel)
        self.global_term.hide()
        self._terminal_loaded    = False
        self._pending_pty_cwd   = None

        self.global_pty = PtyManager(
            on_data=lambda d: self.global_term_bridge.terminalOutputSignal.emit(d),
            on_exit=self._on_global_pty_exit,
        )

    # ── pane management ──────────────────────────────────────────────────────

    def _active_pane(self) -> QuillPane:
        return self._panes[self._active_pane_index]

    def _add_pane(self, page):
        pane = QuillPane(self, page)
        pane.content_changed.connect(
            lambda pi, html, origin=pane: self._on_any_content_changed(pi, html, origin))
        self._panes.append(pane)
        self.h_split.addWidget(pane)
        pane.load_page(page)
        return pane

    def _on_any_content_changed(self, page_index, html, origin_pane=None):
        for pane in self._panes:
            if pane is origin_pane:
                continue
            if pane.page_index == page_index and pane._editor_ready:
                pane._send_content(html)
        self._update_nav()

    def _after_content_snapshot(self, callback):
        self._active_pane().capture_and_then(callback)

    def _flush_after_content_snapshot(self, callback=None):
        def finish():
            self._flush_save()
            if callback:
                callback()
        self._active_pane().capture_and_then(finish)

    def _split_pane(self):
        if len(self._panes) >= 3:
            return
        total   = len(self.notes["pages"])
        current = self._active_pane().page_index
        self._add_pane((current + 1) % total)
        self._active_pane_index = len(self._panes) - 1
        self._update_nav()

    def _toggle_split_panes(self):
        if len(self._panes) > 1:
            self._close_extra_panes()
        else:
            self._split_pane()

    def _close_extra_panes(self):
        while len(self._panes) > 1:
            pane = self._panes.pop()
            pane.setParent(None)
            pane.deleteLater()
        self._active_pane_index = 0
        self._update_nav()

    # ── page navigation ──────────────────────────────────────────────────────

    def _update_nav(self):
        total = len(self.notes["pages"])
        idx   = self._active_pane().page_index
        self.page_label.setText(f"{idx + 1} / {total}")
        self.prev_btn.setEnabled(idx > 0)
        self.next_btn.setEnabled(idx < total - 1)
        self._sync_command_states()

    def _set_button_object_name(self, button, name):
        if button.objectName() == name:
            return
        button.setObjectName(name)
        button.style().unpolish(button)
        button.style().polish(button)

    def _apply_ui_settings(self):
        self._ui_settings = normalized_ui_settings(self.config.get("ui", {}))
        self.setStyleSheet(style_from_ui(self._ui_settings))
        if hasattr(self, "_topbar"):
            self._topbar.setFixedHeight(max(36, self._ui_settings["button_height"] + 12))
        if hasattr(self, "_top_layout"):
            pad = self._ui_settings["toolbar_padding"]
            self._top_layout.setContentsMargins(pad, 0, pad, 0)
            self._top_layout.setSpacing(self._ui_settings["toolbar_button_spacing"])
        for spacer in getattr(self, "_toolbar_group_spacers", []):
            spacer.changeSize(
                self._ui_settings["toolbar_group_spacing"],
                0,
                QSizePolicy.Policy.Fixed,
                QSizePolicy.Policy.Minimum,
            )
        if hasattr(self, "_nav_layout"):
            pad = self._ui_settings["page_rail_padding"]
            self._nav_layout.setContentsMargins(pad, 0, pad, 0)
        if hasattr(self, "_top_layout"):
            self._top_layout.invalidate()
        for button in getattr(self, "_toolbar_buttons", []):
            button.setFixedSize(QSize(self._ui_settings["button_size"], self._ui_settings["button_height"]))
            button.style().unpolish(button)
            button.style().polish(button)
        self.updateGeometry()

    def _sync_command_states(self):
        if hasattr(self, "term_btn"):
            self._set_button_object_name(
                self.term_btn,
                "tool-on" if self.global_term.isVisible() else "command",
            )
        if hasattr(self, "split_btn"):
            self.split_btn.setText("▣" if len(self._panes) > 1 else "◫")
            self.split_btn.setToolTip(
                "Close split panes  (Ctrl+Shift+\\)"
                if len(self._panes) > 1
                else "Split pane  (Ctrl+\\)"
            )

    def _prev_page(self):
        pane = self._active_pane()
        if pane.page_index > 0:
            self._after_content_snapshot(lambda target=pane.page_index - 1: self._load_active_page(target))

    def _next_page(self):
        pane = self._active_pane()
        if pane.page_index < len(self.notes["pages"]) - 1:
            self._after_content_snapshot(lambda target=pane.page_index + 1: self._load_active_page(target))

    def _load_active_page(self, index):
        self._active_pane().load_page(index)
        self._flush_save()
        self._update_nav()

    def _new_page(self):
        idx = self._active_pane().page_index
        self._after_content_snapshot(lambda idx=idx: self._new_page_after_snapshot(idx))

    def _new_page_after_snapshot(self, idx):
        self._flush_save()
        self.notes["pages"].insert(idx + 1, "")
        self._active_pane().load_page(idx + 1)
        self._flush_save()
        self._update_nav()

    def _remove_blank_pages(self):
        non_blank = [p for p in self.notes["pages"] if str(p).strip()]
        removed = len(self.notes["pages"]) - len(non_blank)
        if removed == 0:
            QMessageBox.information(self, "Clean up", "No blank pages found.")
            return
        reply = QMessageBox.question(
            self, "Remove blank pages",
            f"Remove {removed} blank page{'s' if removed != 1 else ''}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.notes["pages"] = non_blank or [""]
        new_max = len(self.notes["pages"]) - 1
        for pane in self._panes:
            pane.load_page(min(pane.page_index, new_max))
        self._flush_save()
        self._update_nav()

    def _delete_page(self):
        self._flush_save()
        pane = self._active_pane()
        if len(self.notes["pages"]) == 1:
            self.notes["pages"][0] = ""
            pane.load_page(0)
            self._flush_save()
            return
        idx = pane.page_index
        self.notes["pages"].pop(idx)
        pane.load_page(min(idx, len(self.notes["pages"]) - 1))
        self._flush_save()
        self._update_nav()

    # ── actions ───────────────────────────────────────────────────────────────

    def _toggle_terminal(self):
        if self.global_term.isVisible():
            self._close_terminal()
        else:
            self._open_terminal()
        self._sync_command_states()

    def _open_terminal(self, cwd=None):
        if not self.global_term.isVisible():
            if not hasattr(self, '_main_split'):
                self._main_split = QSplitter(Qt.Orientation.Vertical, self)
                self._main_split.setChildrenCollapsible(False)
                self._main_split.addWidget(self.h_split)
                self._main_split.addWidget(self.global_term)
                layout = self.layout()
                layout.removeWidget(self.h_split)
                layout.insertWidget(1, self._main_split, 1)
            self.global_term.show()
            total = self._main_split.height()
            self._main_split.setSizes([total - self._term_height, self._term_height])

        if not self._terminal_loaded:
            # First open: load URL now that the view is visible and sized so
            # xterm.js gets real dimensions for term.open() and fit.
            self._pending_pty_cwd = cwd or str(Path.home())
            self.global_term.setUrl(TERMINAL_URL)
            self._terminal_loaded = True
            # PTY starts in _on_terminal_ready() once QWebChannel is ready.
            return

        if self.global_pty._pid is None:
            self.global_pty.start(cwd=cwd or str(Path.home()))
            self.global_term_bridge.cwdSignal.emit(cwd or str(Path.home()))
        self.global_term_bridge.fitSignal.emit()

    def _on_terminal_ready(self):
        """Called by TerminalBridge.ready() when the terminal page is fully loaded."""
        cwd = self._pending_pty_cwd
        self._pending_pty_cwd = None
        if self.global_pty._pid is None and cwd is not None:
            self.global_pty.start(cwd=cwd)
            self.global_term_bridge.cwdSignal.emit(cwd)
        self.global_term_bridge.fitSignal.emit()

    def _close_terminal(self):
        if self.global_term.isVisible():
            if hasattr(self, '_main_split'):
                sizes = self._main_split.sizes()
                if len(sizes) == 2 and sizes[1] > 0:
                    self._term_height = sizes[1]
        self.global_pty.stop()
        self.global_term.hide()
        self._sync_command_states()

    def _on_global_pty_exit(self):
        self.global_pty.stop()
        self.global_term.hide()
        self._sync_command_states()

    def _toggle_edit_mode(self):
        self._active_pane().toggle_edit_mode()
        self._sync_command_states()

    def _toggle_pin(self):
        self._set_pin_state(not self.pinned)

    def _set_pin_state(self, pinned):
        self.pinned = bool(pinned)
        self.setWindowFlags(self._window_flags_for_pin(self.pinned))
        self.pin_btn.setObjectName("pin-on" if self.pinned else "pin-off")
        self.pin_btn.style().unpolish(self.pin_btn)
        self.pin_btn.style().polish(self.pin_btn)
        self.show()
        if self.pinned:
            self.raise_()

    def _ask_ollama(self):
        prompt, ok = QInputDialog.getText(self, "Ask Ollama", "Prompt:")
        if not ok or not prompt.strip():
            return
        self._send_to_ollama(prompt.strip())

    def _send_to_ollama(self, prompt):
        page_idx = self._active_pane().page_index
        text_content = plain_text_from_html(self.notes["pages"][page_idx])
        ollama = self.config.get("ollama", {})

        full_prompt = f"Context:\n{text_content}\n\nUser request:\n{prompt}"
        payload = json.dumps(
            ollama_generate_payload(
                full_prompt,
                model=ollama.get("model", "llama3.2") or "llama3.2",
                system=ollama.get("system") or None,
            )
        ).encode()
        base_url = (ollama.get("base_url") or "http://localhost:11434").rstrip("/")

        req = urllib.request.Request(
            f"{base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        def _after_snapshot():
            self._flush_save()
            response_idx = self._active_pane().page_index + 1
            self.notes["pages"].insert(response_idx, "")
            self._active_pane().load_page(response_idx)
            self._flush_save()
            self._update_nav()

            def _stream():
                chunks = []
                try:
                    with urllib.request.urlopen(req, timeout=300) as resp:
                        for chunk in ollama_stream_chunks(resp):
                            chunks.append(chunk)
                            self._ollama_chunk.emit(response_idx, chunk)
                    response_text = "".join(chunks)
                except Exception as e:
                    response_text = f"Error: {e}"
                self._ollama_done.emit(response_idx, response_text)

            threading.Thread(target=_stream, daemon=True).start()

        self._after_content_snapshot(_after_snapshot)

    def _on_ollama_chunk(self, page_index, chunk):
        self.notes["pages"][page_index] += chunk
        text = self.notes["pages"][page_index]
        for pane in self._panes:
            if pane.page_index == page_index and pane._editor_ready:
                pane.view.page().runJavaScript(
                    f"setPreviewHtml({json.dumps(preformatted_html(text))})")
        self._update_nav()

    def _on_ollama_done(self, page_index, full_text):
        html = preformatted_html(full_text)
        self.notes["pages"][page_index] = html
        self._flush_save()
        for pane in self._panes:
            if pane.page_index == page_index and pane._editor_ready:
                pane._send_content(html)
        self._update_nav()

    def _context_menu_style(self):
        return (
            "QMenu { background:#16213e; color:#e6edf3; border:1px solid #2d2d4e; }"
            "QMenu::item { padding:6px 24px; }"
            "QMenu::item:selected { background:#2d2d4e; }"
        )

    def _build_context_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(self._context_menu_style())

        def add_action(label, shortcut=None):
            action = menu.addAction(label)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            return action

        lc_editor_act = add_action("LiveCodes: show editor")
        lc_result_act = add_action("LiveCodes: show result")
        lc_toggle_result_act = add_action("LiveCodes: toggle result")
        lc_run_act = add_action("LiveCodes: run project")
        lc_format_act = add_action("LiveCodes: format code")
        lc_cut_act = add_action("Cut")
        lc_copy_act = add_action("Copy")
        lc_paste_act = add_action("Paste")
        lc_select_all_act = add_action("Select all")
        menu.addSeparator()
        ask_act = add_action("Ask Ollama", "Ctrl+Shift+A")
        term_act = add_action("Toggle terminal", "Ctrl+T")
        split_act = add_action("Split / unsplit", "Ctrl+\\")
        menu.addSeparator()
        share_menu = menu.addMenu("Share")
        share_copy_act = share_menu.addAction("Copy share text")
        share_copy_act.triggered.connect(lambda: self._share_current("clipboard", {}))
        share_ai_act = share_menu.addAction("Copy for AI")
        share_ai_act.triggered.connect(lambda: self._share_current("ai_clipboard", {}))
        telegram = self.config.get("telegram", {})
        default_chat = telegram.get("default_chat_id", "")
        share_tg_act = share_menu.addAction("Telegram default" if default_chat else "Configure Telegram...")
        share_tg_act.triggered.connect(
            (lambda: self._share_current("telegram", {"chat_id": default_chat}))
            if default_chat
            else self._open_config_panel
        )
        for target in self.config.get("share_targets", []):
            if not isinstance(target, dict):
                continue
            action = share_menu.addAction(str(target.get("name") or target.get("kind") or "Target"))
            action.triggered.connect(
                lambda checked=False, t=target: self._share_current(str(t.get("kind", "")), t)
            )
        share_menu.addSeparator()
        share_menu.addAction("Configure sharing...").triggered.connect(self._open_config_panel)
        ui_settings_act = add_action("Window UI settings...")
        config_act = add_action("Configure sharing/Ollama...")
        menu.addSeparator()
        export_act = add_action("Export page...")
        pin_act = add_action("Toggle pin", "Ctrl+P")
        menu.addSeparator()
        delete_act = add_action("Delete page", "Ctrl+W")
        remove_blanks_act = add_action("Remove all blank pages")
        hide_act = add_action("Hide to tray", "Ctrl+H")
        quit_act = add_action("Quit", "Ctrl+Q")

        return menu, {
            ask_act: self._ask_ollama,
            term_act: self._toggle_terminal,
            split_act: self._toggle_split_panes,
            ui_settings_act: self._open_ui_settings_panel,
            config_act: self._open_config_panel,
            export_act: self._export_page,
            pin_act: self._toggle_pin,
            delete_act: self._delete_page,
            remove_blanks_act: self._remove_blank_pages,
            hide_act: self.hide,
            quit_act: self._quit,
            lc_editor_act: lambda: self._run_livecodes_action("show", ["editor"]),
            lc_result_act: lambda: self._run_livecodes_action("show", ["result"]),
            lc_toggle_result_act: lambda: self._run_livecodes_action("show", ["toggle-result"]),
            lc_run_act: lambda: self._run_livecodes_action("run"),
            lc_format_act: lambda: self._run_livecodes_action("format"),
            lc_cut_act: lambda: self._run_livecodes_edit_action("cut"),
            lc_copy_act: lambda: self._run_livecodes_edit_action("copy"),
            lc_paste_act: lambda: self._run_livecodes_edit_action("paste"),
            lc_select_all_act: lambda: self._run_livecodes_edit_action("selectAll"),
        }

    def _open_context_menu_at(self, global_pos):
        menu, actions = self._build_context_menu()
        action = menu.exec(global_pos)
        handler = actions.get(action)
        if handler:
            handler()

    def contextMenuEvent(self, event):
        menu, actions = self._build_context_menu()
        action = menu.exec(event.globalPos())
        handler = actions.get(action)
        if handler:
            handler()

    def _run_livecodes_action(self, method, args=None):
        pane = self._active_pane()
        if pane and pane._editor_ready:
            pane.run_livecodes_command(method, args or [])

    def _run_livecodes_edit_action(self, command):
        pane = self._active_pane()
        if not pane or not pane._editor_ready:
            return
        action_map = {
            "cut": QWebEnginePage.WebAction.Cut,
            "copy": QWebEnginePage.WebAction.Copy,
            "paste": QWebEnginePage.WebAction.Paste,
            "selectAll": QWebEnginePage.WebAction.SelectAll,
        }
        web_action = action_map.get(command)
        if web_action is not None:
            page = pane.view.page()
            QTimer.singleShot(0, lambda: page.triggerAction(web_action))

    def _open_config_panel(self):
        dialog = ConfigDialog(self, self.config)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.config = dialog.value()
            self._save_config()
        except ValueError as e:
            QMessageBox.warning(self, "Scratch configuration", str(e))
            return
        QMessageBox.information(self, "Scratch configuration", "Configuration saved.")

    def _open_ui_settings_panel(self):
        dialog = UiSettingsDialog(self, self.config.get("ui", {}))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.config["ui"] = dialog.value()
        self._save_config()
        self._apply_ui_settings()
        self._set_pin_state(self._ui_settings["start_pinned"])
        QMessageBox.information(self, "Scratch window UI", "Window UI settings saved.")

    def _share_current(self, kind, target):
        self._active_pane().get_share_payload(
            lambda payload, k=kind, t=target: self._share_payload(k, t, payload or {})
        )

    def _share_payload_text(self, payload):
        text = (payload.get("text") or "").strip()
        if text:
            return text
        html_text = payload.get("html") or ""
        return plain_text_from_html(html_text).strip() or html_text

    def _share_payload_file(self, payload, preferred_format="html"):
        html_text = payload.get("html") or ""
        text = self._share_payload_text(payload)
        suffix = ".txt" if preferred_format == "text" else ".html"
        handle = tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            suffix=suffix,
            prefix="scratch-share-",
            encoding="utf-8",
        )
        with handle:
            if suffix == ".txt":
                handle.write(text)
            else:
                handle.write(
                    "<!DOCTYPE html><html><head><meta charset='UTF-8'></head><body>"
                    + html_text
                    + "</body></html>"
                )
        return Path(handle.name)

    def _share_payload(self, kind, target, payload):
        text = self._share_payload_text(payload)
        if not text and not payload.get("html"):
            QMessageBox.information(self, "Share", "Nothing to share.")
            return

        try:
            if kind == "clipboard":
                QApplication.clipboard().setText(text)
                QMessageBox.information(self, "Share", "Copied share text.")
            elif kind == "ai_clipboard":
                label = "selected content" if payload.get("selected") else "full Scratch note"
                QApplication.clipboard().setText(
                    f"Use this {label} as context:\n\n{text}"
                )
                QMessageBox.information(self, "Share", "Copied AI handoff prompt.")
            elif kind == "telegram":
                self._share_to_telegram(target.get("chat_id", ""), text)
            elif kind == "ntfy":
                self._share_to_ntfy(target, text)
            elif kind == "local_folder":
                path = Path(target.get("path", "")).expanduser()
                path.mkdir(parents=True, exist_ok=True)
                file_path = self._share_payload_file(payload, target.get("format", "html"))
                dest = path / file_path.name
                shutil.copy2(file_path, dest)
                QMessageBox.information(self, "Share", f"Shared to {dest}")
            elif kind in {"scp", "sftp"}:
                destination = target.get("destination", "")
                if not destination:
                    raise ValueError("Missing destination, for example nova:/home/ash/inbox/")
                file_path = self._share_payload_file(payload, target.get("format", "html"))
                subprocess.run(["scp", str(file_path), destination], check=True, timeout=45)
                QMessageBox.information(self, "Share", f"Shared to {destination}")
            elif kind == "taildrop":
                device = target.get("device", "")
                if not device:
                    raise ValueError("Missing Taildrop device name")
                file_path = self._share_payload_file(payload, target.get("format", "html"))
                subprocess.run(["tailscale", "file", "cp", str(file_path), f"{device}:"], check=True, timeout=45)
                QMessageBox.information(self, "Share", f"Sent to {device} with Taildrop.")
            elif kind == "command":
                self._share_to_command(target, payload, text)
            else:
                raise ValueError(f"Unsupported share target kind: {kind or 'blank'}")
        except Exception as e:
            QMessageBox.warning(self, "Share failed", str(e))

    def _share_to_telegram(self, chat_id, text):
        telegram = self.config.get("telegram", {})
        token = telegram.get("bot_token", "")
        chat_id = chat_id or telegram.get("default_chat_id", "")
        if not token or not chat_id:
            self._open_config_panel()
            return
        values = {"chat_id": chat_id, "text": text[:4096]}
        parse_mode = telegram.get("parse_mode", "")
        if parse_mode:
            values["parse_mode"] = parse_mode
        data = urllib.parse.urlencode(values).encode("utf-8")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        with urllib.request.urlopen(url, data=data, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(result)
        QMessageBox.information(self, "Share", "Sent to Telegram.")

    def _share_to_ntfy(self, target, text):
        url = target.get("url") or target.get("topic_url") or self.config.get("ntfy", {}).get("url", "")
        if not url:
            raise ValueError("Missing ntfy URL")
        headers = {}
        token = target.get("token") or self.config.get("ntfy", {}).get("token", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=text.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=20):
            pass
        QMessageBox.information(self, "Share", "Sent to ntfy.")

    def _share_to_command(self, target, payload, text):
        command = target.get("command")
        if not command:
            raise ValueError("Missing command")
        file_path = self._share_payload_file(payload, target.get("format", "html"))
        if isinstance(command, str):
            command = shlex.split(command)
        if not isinstance(command, list):
            raise ValueError("Command must be a string or list")
        command = [
            str(part).replace("{file}", str(file_path)).replace("{text}", text)
            for part in command
        ]
        if any("{file}" in str(part) or "{text}" in str(part) for part in command):
            raise ValueError("Unresolved command placeholder")
        subprocess.run(command, input=text, text=True, check=True, timeout=45)
        QMessageBox.information(self, "Share", f"Ran share command: {command[0]}")

    def _quit(self):
        self._flush_after_content_snapshot(self._quit_after_snapshot)

    def _quit_after_snapshot(self):
        self.global_pty.stop()
        for pane in self._panes:
            pane.view.page().deleteLater()
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        QApplication.quit()

    def _export_page(self):
        html = self.notes["pages"][self._active_pane().page_index]

        path, sel = QFileDialog.getSaveFileName(
            self, "Export page", str(Path.home()),
            "HTML (*.html);;Plain text (*.txt)",
        )
        if not path:
            return
        path = Path(path)
        if sel.startswith("Plain") or path.suffix == ".txt":
            path.write_text(plain_text_from_html(html), encoding="utf-8")
        else:
            wrapper = (
                "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
                "<style>body{font-family:sans-serif;max-width:800px;margin:2em auto;}</style>"
                "</head><body>" + html + "</body></html>"
            )
            path.write_text(wrapper, encoding="utf-8")

    # ── window events ─────────────────────────────────────────────────────────

    def sizeHint(self):
        _, _, w, h = getattr(self, '_init_geometry', (0, 0, 440, 460))
        return QSize(w, h)

    def wheelEvent(self, event):
        if self._ui_settings["ctrl_wheel_pages"] and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.angleDelta().y() < 0:
                self._next_page()
            elif event.angleDelta().y() > 0:
                self._prev_page()
            event.accept()
            return
        super().wheelEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        x, y, w, h = self._init_geometry
        self.setGeometry(x, y, w, h)
        self._position_resize_handles()
        QTimer.singleShot(50, self.activateWindow)

    def hide(self):
        self._flush_after_content_snapshot(self._hide_after_snapshot)

    def _hide_after_snapshot(self):
        super().hide()

    def moveEvent(self, event):
        self.schedule_save()
        super().moveEvent(event)

    def resizeEvent(self, event):
        self._position_resize_handles()
        self.schedule_save()
        super().resizeEvent(event)

    def closeEvent(self, event):
        if self._ui_settings["hide_on_close"]:
            self.global_pty.stop()
            event.ignore()
            self.hide()
            return
        self._quit()

    def show_and_raise(self):
        self.show()
        self.raise_()
        self.activateWindow()


# ── entrypoint ────────────────────────────────────────────────────────────────

def _detach_from_terminal():
    if os.environ.get("SCRATCH_DETACHED") or not sys.stdin.isatty():
        return
    import subprocess as _sp
    _sp.Popen(
        [sys.executable] + sys.argv,
        env={**os.environ, "SCRATCH_DETACHED": "1"},
        start_new_session=True,
        stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        close_fds=True,
    )
    sys.exit(0)


def main():
    # Configure logging for stderr output
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Override-redirect windows require XCB. Force it before QApplication.
    os.environ["QT_QPA_PLATFORM"] = "xcb"

    _detach_from_terminal()

    lock_sock = _acquire_instance_lock()

    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

    app = QApplication(sys.argv)
    app.setApplicationName("scratch")
    app.setDesktopFileName("scratch")
    app.setQuitOnLastWindowClosed(False)

    win = ScratchPad()
    win.show()

    _start_instance_listener(
        lock_sock,
        lambda: QTimer.singleShot(0, win.show_and_raise),
    )

    icon_path = Path.home() / ".local/share/icons/scratch.svg"
    icon = (QIcon(str(icon_path)) if icon_path.exists()
            else QIcon.fromTheme("accessories-text-editor"))
    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("Scratch")

    menu = QMenu()
    show_act   = menu.addAction("Show Scratch")
    menu.addSeparator()
    quit_act   = menu.addAction("Quit")
    tray.setContextMenu(menu)

    show_act.triggered.connect(win.show_and_raise)
    quit_act.triggered.connect(win._quit)
    tray.activated.connect(
        lambda r: win.show_and_raise()
        if r == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()

    app.aboutToQuit.connect(win._flush_save)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
