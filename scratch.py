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
import tempfile
import termios
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QSize,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QCursor, QIcon, QKeySequence, QShortcut
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineCore import QWebEngineScript, QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QSystemTrayIcon,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from scratch_core import (
    OLLAMA_PARAM_GROUPS,
    SHORTCUTS,
    Rect,
    chat_page_html,
    create_shortcuts,
    is_chat_page_html,
    is_livecodes_content_config,
    livecodes_config_from_source,
    livecodes_source_from_config,
    livecodes_url,
    migrate_ollama_config,
    ollama_chat_payload,
    ollama_chat_stream_chunks,
    ollama_loaded_model_names,
    ollama_model_is_loaded,
    ollama_stream_chunks,
    page_title_from_config,
    page_title_from_html,
    parse_chat_page_html,
    parse_ollama_model_params,
    plain_text_from_html,
    preformatted_html,
    reindex_page_map_after_delete,
    reindex_page_map_after_insert,
    resize_rect,
    start_livecodes_server,
)

logger = logging.getLogger(__name__)


def _load_injection_content(inj: dict) -> str:
    """Read content for a context injection item (file or inline text)."""
    if inj.get("type") == "file":
        try:
            return Path(inj["path"]).expanduser().read_text(errors="replace")
        except Exception as e:
            logger.warning("Injection file unreadable %s: %s", inj.get("path"), e)
            return ""
    return inj.get("content", "")


def _ollama_request_timeout() -> float | None:
    """Return Ollama request timeout seconds; 0/none/off disables it."""
    raw = os.environ.get(OLLAMA_REQUEST_TIMEOUT_ENV, "").strip().lower()
    if raw in {"0", "none", "off", "false", "disable", "disabled"}:
        return None
    if not raw:
        return DEFAULT_OLLAMA_REQUEST_TIMEOUT
    try:
        return max(1.0, float(raw))
    except ValueError:
        logger.warning(
            "Invalid %s=%r; using default %.0fs",
            OLLAMA_REQUEST_TIMEOUT_ENV,
            raw,
            DEFAULT_OLLAMA_REQUEST_TIMEOUT,
        )
        return DEFAULT_OLLAMA_REQUEST_TIMEOUT


def _ollama_keep_alive() -> str | None:
    """Return Ollama keep_alive value; default/omit leaves server default behavior."""
    raw = os.environ.get(OLLAMA_KEEP_ALIVE_ENV, "").strip()
    if not raw:
        return DEFAULT_OLLAMA_KEEP_ALIVE
    if raw.lower() in {"default", "omit", "none"}:
        return None
    return raw


DATA_FILE = Path.home() / ".scratch-notes" / "notes.json"
CONFIG_FILE = Path.home() / ".scratch-notes" / "config.json"
BACKUP_DIR = DATA_FILE.parent / "backups"
MAX_NOTE_BACKUPS = 100
ASSETS_DIR = Path(__file__).parent / "assets"
EDITOR_URL = QUrl.fromLocalFile(str(ASSETS_DIR / "editor.html"))
LIVECODES_URL = QUrl.fromLocalFile(str(ASSETS_DIR / "livecodes_pane.html"))
TERMINAL_URL = QUrl.fromLocalFile(str(ASSETS_DIR / "terminal.html"))
SOCKET_PATH = f"/tmp/scratch-{os.getuid()}.sock"

OLLAMA_REQUEST_TIMEOUT_ENV = "SCRATCH_OLLAMA_TIMEOUT"
DEFAULT_OLLAMA_REQUEST_TIMEOUT = 300.0
OLLAMA_KEEP_ALIVE_ENV = "SCRATCH_OLLAMA_KEEP_ALIVE"
DEFAULT_OLLAMA_KEEP_ALIVE = "30m"

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
    if (
        len(value) == 7
        and value[0] == "#"
        and all(c in "0123456789abcdefABCDEF" for c in value[1:])
    ):
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
    ui["toolbar_button_spacing"] = _bounded_int(
        ui.get("toolbar_button_spacing"), 3, 0, 12
    )
    ui["toolbar_group_spacing"] = _bounded_int(
        ui.get("toolbar_group_spacing"), 10, 0, 24
    )
    ui["page_rail_padding"] = _bounded_int(ui.get("page_rail_padding"), 6, 0, 18)
    ui["button_size"] = _bounded_int(ui.get("button_size"), 27, 24, 34)
    ui["button_height"] = _bounded_int(ui.get("button_height"), 25, 22, 32)
    ui["start_pinned"] = bool(ui.get("start_pinned"))
    ui["ctrl_wheel_pages"] = bool(ui.get("ctrl_wheel_pages"))
    ui["hide_on_close"] = bool(ui.get("hide_on_close"))
    return ui


def _hex_to_rgb(value):
    value = _hex_color(value, DEFAULT_UI_SETTINGS["pin_glow_color"])
    return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))


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
QPushButton#ai-unknown {{ color: #7f8aa6; }}
QPushButton#ai-checking {{ color: #f8d66d; background: rgba(248,214,109,.10); }}
QPushButton#ai-connected {{ color: #f8d66d; }}
QPushButton#ai-loaded {{ color: #7ec8a4; background: rgba(126,200,164,.12); }}
QPushButton#ai-processing {{ color: #7cc4ff; background: rgba(124,196,255,.16); }}
QPushButton#ai-offline {{ color: #ff8f9a; background: rgba(255,143,154,.10); }}
QPushButton#ai-error {{ color: #ff5f6d; background: rgba(255,95,109,.16); }}
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
QPushButton#nav-counter {{ color: #9aacd0; font-size: 11px; font-weight: 700; background: transparent; border: none; }}
QPushButton#nav-counter:hover {{ color: #e6edf3; background: {ui["button_hover"]}; border-radius: 4px; }}
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
        self._on_data = on_data
        self._on_exit = on_exit
        self._master_fd = None
        self._pid = None
        self._thread = None

    def start(self, cwd=None):
        self.stop()
        shell = os.environ.get("SHELL", "/bin/bash")
        env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
        pid, master_fd = pty.fork()
        if pid == 0:
            if cwd:
                try:
                    os.chdir(cwd)
                except OSError:
                    pass
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
            try:
                os.write(self._master_fd, data.encode())
            except OSError:
                pass

    def resize(self, cols, rows):
        if self._master_fd is not None:
            try:
                fcntl.ioctl(
                    self._master_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0),
                )
            except OSError:
                pass

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
        QTimer.singleShot(
            0, lambda: self._pane.view.page().runJavaScript("focusEditor()")
        )
        QTimer.singleShot(
            80, lambda: self._pane.view.page().runJavaScript("focusEditor()")
        )

    @pyqtSlot()
    def snapshotDone(self):
        self._pane._on_snapshot_done()

    @pyqtSlot()
    def openContextMenu(self):
        self._pane._pad._open_context_menu_at(QCursor.pos())


class TerminalBridge(QObject):
    """Bridge exposing Qt slots for terminal I/O between Python PTY and JS."""

    terminalOutputSignal = pyqtSignal(str)
    cwdSignal = pyqtSignal(str)
    fitSignal = pyqtSignal()

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

    chat_message_sent = pyqtSignal(int, str)  # page_index, text
    page_loaded = pyqtSignal(int)  # page_index
    restore_history_requested = pyqtSignal(int)  # page_index
    stop_requested = pyqtSignal(int)  # page_index

    _SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, pad, initial_page=0):
        super().__init__()
        self._pad = pad
        self._page_index = initial_page
        self._editor_ready = False
        self._pending_html = None
        self._edit_mode = False
        self._chat_state = "idle"  # idle | connecting | streaming
        self._spinner_frame = 0

        self._snapshot_callback = None
        self._snapshot_fallback = QTimer(self)
        self._snapshot_fallback.setSingleShot(True)
        self._snapshot_fallback.timeout.connect(self._on_snapshot_done)

        self.bridge = QuillBridge(self)
        self.channel = QWebChannel(self)
        self.channel.registerObject("bridge", self.bridge)
        self.view = QWebEngineView(self)
        self.view.setMinimumSize(QSize(0, 0))
        s = self.view.settings()
        s.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
        )
        s.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True
        )
        self.view.page().setWebChannel(self.channel)
        self._install_context_menu_bridge()
        livecodes_url_obj = QUrl(LIVECODES_URL)
        livecodes_url_obj.setQuery(
            urllib.parse.urlencode({"appUrl": self._pad.livecodes_app_url})
        )
        self.view.setUrl(livecodes_url_obj)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.view.installEventFilter(self)

        # Dedicated chat view — bypasses LiveCodes entirely for AI responses
        self._chat_view = QWebEngineView(self)
        self._chat_view.setMinimumSize(QSize(0, 0))
        self._chat_view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self._chat_view_ready = False
        self._chat_view_initializing = False
        self._pending_chat_body: str | None = None
        self._chat_view.loadFinished.connect(self._on_chat_view_ready)

        self._view_stack = QStackedWidget(self)
        self._view_stack.addWidget(self.view)  # index 0 — LiveCodes
        self._view_stack.addWidget(self._chat_view)  # index 1 — AI chat

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view_stack, 1)

        self._chat_bar = QWidget(self)
        chat_hl = QHBoxLayout(self._chat_bar)
        chat_hl.setContentsMargins(6, 4, 6, 6)
        chat_hl.setSpacing(6)
        self._chat_input = QTextEdit(self._chat_bar)
        self._chat_input.setMaximumHeight(58)
        self._chat_input.setPlaceholderText(
            "Reply to Ollama… (Enter to send, Shift+Enter for newline)"
        )
        self._chat_input.installEventFilter(self)
        self._send_btn = QPushButton("Send", self._chat_bar)
        self._send_btn.setFixedWidth(56)
        self._send_btn.clicked.connect(self._on_chat_send)
        self._stop_btn = QPushButton("Stop", self._chat_bar)
        self._stop_btn.setFixedWidth(56)
        self._stop_btn.clicked.connect(lambda: self.stop_requested.emit(self._page_index))
        self._stop_btn.hide()
        self._restore_btn = QPushButton("Restore history", self._chat_bar)
        self._restore_btn.clicked.connect(
            lambda: self.restore_history_requested.emit(self._page_index)
        )
        self._restore_btn.hide()
        chat_hl.addWidget(self._restore_btn)
        chat_hl.addWidget(self._chat_input, 1)
        chat_hl.addWidget(self._stop_btn)
        chat_hl.addWidget(self._send_btn)
        self._chat_bar.hide()
        layout.addWidget(self._chat_bar)

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
        self.page_loaded.emit(index)

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
                    logger.warning(
                        "Ignored invalid LiveCodes save payload for page %s",
                        target_page + 1,
                    )
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
            logger.warning(
                "Ignored empty LiveCodes save over non-empty page %s", target_page + 1
            )
            return
        self._pad.notes["pages"][target_page] = source
        self._pad.schedule_save()
        self.content_changed.emit(target_page, source)

    def _send_content(self, source):
        config = livecodes_config_from_source(source)
        self.view.page().runJavaScript(
            f"loadNoteSource({json.dumps(source)}, {json.dumps(json.dumps(config))}, {self._page_index})"
        )

    def capture_current_content(self):
        if self._editor_ready:
            self.view.page().runJavaScript(
                f"captureLiveCodesConfig({self._page_index})"
            )

    def capture_and_then(self, callback, fallback_ms=500):
        """Capture editor content, then invoke callback once JS confirms done (or timeout)."""
        self._snapshot_callback = callback
        self._snapshot_fallback.start(fallback_ms)
        if self._editor_ready:
            self.view.page().runJavaScript(
                f"captureLiveCodesConfig({self._page_index})"
            )
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
            var _scratchFocused = null;
            document.addEventListener('focusin', function(e) {
                _scratchFocused = e.target;
            }, true);
            document.addEventListener('contextmenu', function(event) {
                event.preventDefault();
                event.stopPropagation();
                event.stopImmediatePropagation();
                window.top.postMessage({ type: 'scratch-open-context-menu' }, '*');
            }, true);
            window.addEventListener('message', function(event) {
                var data = event.data || {};
                if (data.type !== 'scratch-edit-command' || !data.command) return;
                if (_scratchFocused) { try { _scratchFocused.focus(); } catch(e) {} }
                document.execCommand(data.command);
                for (var i = 0; i < window.frames.length; i++) {
                    try { window.frames[i].postMessage(data, '*'); } catch(e) {}
                }
            });
            """
        )
        self.view.page().scripts().insert(script)

    def set_chat_mode(self, enabled: bool, *, restore_available: bool = False):
        if enabled:
            self._chat_bar.show()
            self._view_stack.setCurrentIndex(1)
            self._chat_input.setEnabled(not restore_available)
            self._send_btn.setEnabled(not restore_available)
            self._stop_btn.hide()
            self._restore_btn.setVisible(restore_available)
        else:
            self._chat_bar.hide()
            self._view_stack.setCurrentIndex(0)
            self.set_edit_mode(True)  # reset editor view when leaving chat

    def set_history_restored(self):
        """Called after chat history has been restored; re-enables the reply bar."""
        self._restore_btn.hide()
        self._chat_input.setEnabled(True)
        self._send_btn.setEnabled(True)
        self._stop_btn.hide()

    def set_connecting(self):
        self._chat_state = "connecting"
        self._chat_input.setEnabled(False)
        self._send_btn.setEnabled(False)
        self._stop_btn.show()
        self._start_spinner()

    def set_streaming(self):
        self._chat_state = "streaming"
        self._stop_spinner()
        self._send_btn.setText("●")
        self._send_btn.setEnabled(False)
        self._stop_btn.show()

    def set_idle(self):
        self._chat_state = "idle"
        self._stop_spinner()
        self._send_btn.setText("Send")
        self._send_btn.setEnabled(True)
        self._chat_input.setEnabled(True)
        self._stop_btn.hide()

    def _start_spinner(self):
        self._spinner_frame = 0
        if not hasattr(self, "_spinner_timer"):
            self._spinner_timer = QTimer(self)
            self._spinner_timer.timeout.connect(self._tick_spinner)
        self._spinner_timer.start(80)
        self._tick_spinner()

    def _stop_spinner(self):
        if hasattr(self, "_spinner_timer"):
            self._spinner_timer.stop()

    def _tick_spinner(self):
        self._send_btn.setText(
            self._SPINNER_FRAMES[self._spinner_frame % len(self._SPINNER_FRAMES)]
        )
        self._spinner_frame += 1

    def _on_chat_view_ready(self, ok: bool):
        self._chat_view_ready = True
        self._chat_view_initializing = False
        if self._pending_chat_body is not None:
            body = self._pending_chat_body
            self._pending_chat_body = None
            self._update_chat_body(body)

    def _update_chat_body(self, html_fragment: str):
        self._chat_view.page().runJavaScript(
            "document.body.innerHTML = " + json.dumps(html_fragment) + ";"
            "window.scrollTo(0, document.body.scrollHeight);"
        )

    def show_chat_html(self, html_fragment: str):
        """Render chat content in the dedicated chat view and switch to it."""
        self._view_stack.setCurrentIndex(1)
        if not self._chat_view_ready:
            self._pending_chat_body = html_fragment
            if not self._chat_view_initializing:
                self._chat_view_initializing = True
                self._chat_view.setHtml(
                    "<!DOCTYPE html><html><head><meta charset='UTF-8'></head>"
                    "<body style='margin:0;padding:0;'></body></html>"
                )
        else:
            self._update_chat_body(html_fragment)

    def _on_chat_send(self):
        text = self._chat_input.toPlainText().strip()
        if text:
            self._chat_input.clear()
            self.chat_message_sent.emit(self._page_index, text)

    def eventFilter(self, obj, event):
        try:
            if obj is self._chat_input and event.type() == QEvent.Type.KeyPress:
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
                    event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                ):
                    self._on_chat_send()
                    return True
            if obj is self.view and event.type() == QEvent.Type.MouseButtonPress:
                self.window().activateWindow()
                self.window().raise_()
            return super().eventFilter(obj, event)
        except Exception:
            logger.exception("Unhandled exception in Scratch event filter")
            return False

    def toggle_edit_mode(self):
        self.set_edit_mode(not self._edit_mode)

    def set_edit_mode(self, enabled):
        self._edit_mode = bool(enabled)
        self.view.page().runJavaScript(f"setEditMode({json.dumps(self._edit_mode)})")


# ── drag handle ──────────────────────────────────────────────────────────────


class DragHandle(QFrame):
    """Draggable top bar for moving the frameless window."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_pos = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                e.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            )
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
        if e.button() == Qt.MouseButton.LeftButton and hasattr(
            self.window(), "_toggle_pin"
        ):
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


class OllamaPromptDialog(QDialog):
    """Prompt dialog for asking Ollama about the current note."""

    def __init__(self, parent, profiles: list | None = None, active_profile: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Ask Ollama")
        self.resize(500, 220)
        root = QVBoxLayout(self)
        profiles = profiles or []
        self._profile_combo: QComboBox | None = None
        if len(profiles) > 1:
            prof_row = QHBoxLayout()
            prof_row.addWidget(QLabel("Profile:"))
            self._profile_combo = QComboBox()
            for p in profiles:
                self._profile_combo.addItem(p.get("name", "?"))
            if active_profile:
                self._profile_combo.setCurrentText(active_profile)
            prof_row.addWidget(self._profile_combo, 1)
            root.addLayout(prof_row)
        root.addWidget(QLabel("What would you like to ask about this note?"))
        self.text = QTextEdit(self)
        self.text.setMinimumHeight(110)
        self.text.setPlaceholderText(
            "e.g. Summarize this, fix the bugs, explain this code, translate to Spanish…"
        )
        root.addWidget(self.text, 1)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def value(self) -> str:
        return self.text.toPlainText().strip()

    def selected_profile(self) -> str | None:
        return self._profile_combo.currentText() if self._profile_combo else None


class InjectionEditDialog(QDialog):
    """Add or edit a single context injection entry."""

    def __init__(self, parent, item=None):
        super().__init__(parent)
        item = item or {}
        self.setWindowTitle("Edit injection" if item else "Add injection")
        self.resize(480, 320)
        root = QVBoxLayout(self)
        form = QFormLayout()
        root.addLayout(form)

        self._label = QLineEdit(item.get("label", ""))
        self._label.setPlaceholderText("Short name shown in the list")
        form.addRow("Label", self._label)

        type_row = QWidget()
        type_hl = QHBoxLayout(type_row)
        type_hl.setContentsMargins(0, 0, 0, 0)
        self._rb_file = QRadioButton("File")
        self._rb_text = QRadioButton("Inline text")
        type_hl.addWidget(self._rb_file)
        type_hl.addWidget(self._rb_text)
        type_hl.addStretch()
        form.addRow("Type", type_row)

        path_row = QWidget()
        path_hl = QHBoxLayout(path_row)
        path_hl.setContentsMargins(0, 0, 0, 0)
        self._path = QLineEdit(item.get("path", ""))
        self._path.setPlaceholderText("Absolute path to file")
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(72)
        browse_btn.clicked.connect(self._browse)
        path_hl.addWidget(self._path, 1)
        path_hl.addWidget(browse_btn)
        self._path_row_w = path_row
        form.addRow("Path", path_row)

        self._text_content = QTextEdit(item.get("content", ""))
        self._text_content.setMinimumHeight(80)
        self._text_content.setPlaceholderText("Paste text to inject directly…")
        self._text_row_label = QLabel("Content")
        form.addRow(self._text_row_label, self._text_content)

        role_row = QWidget()
        role_hl = QHBoxLayout(role_row)
        role_hl.setContentsMargins(0, 0, 0, 0)
        self._rb_system = QRadioButton("System")
        self._rb_user = QRadioButton("User context")
        role_hl.addWidget(self._rb_system)
        role_hl.addWidget(self._rb_user)
        role_hl.addStretch()
        form.addRow("Inject as", role_row)

        help_lbl = QLabel(
            "System: appended to the system prompt (invisible to user turn).\n"
            "User context: added as an acknowledged context exchange before your prompt."
        )
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet("color: gray; font-size: 11px;")
        root.addWidget(help_lbl)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

        (
            self._rb_file if item.get("type", "file") == "file" else self._rb_text
        ).setChecked(True)
        (
            self._rb_system if item.get("role", "system") == "system" else self._rb_user
        ).setChecked(True)
        self._rb_file.toggled.connect(self._on_type_changed)
        self._on_type_changed()

    def _on_type_changed(self):
        is_file = self._rb_file.isChecked()
        self._path_row_w.setVisible(is_file)
        self._text_content.setVisible(not is_file)
        self._text_row_label.setVisible(not is_file)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select file to inject")
        if path:
            self._path.setText(path)
            if not self._label.text():
                self._label.setText(Path(path).name)

    def value(self) -> dict:
        typ = "file" if self._rb_file.isChecked() else "text"
        d = {
            "label": self._label.text().strip()
            or (self._path.text().split("/")[-1] if typ == "file" else "text"),
            "type": typ,
            "role": "system" if self._rb_system.isChecked() else "user",
        }
        if typ == "file":
            d["path"] = self._path.text().strip()
        else:
            d["content"] = self._text_content.toPlainText()
        return d


_OLLAMA_PARAM_HELP: dict[str, str] = {
    "temperature": (
        "Controls how unpredictable the output is.\n\n"
        "Low (0.1–0.3): Near-identical results each run. Best for code, factual Q&A, "
        "structured output where consistency matters.\n\n"
        "High (1.2–1.8): Word choices vary noticeably between runs — useful for creative "
        "writing or brainstorming where you want variety.\n\n"
        "Example: At 0.2, rewriting a sentence gives almost the same phrasing every time. "
        "At 1.4, each run finds a different angle."
    ),
    "top_p": (
        "Nucleus sampling: only tokens whose combined probability reaches this fraction "
        "are candidates for the next word.\n\n"
        "Low (0.5): Very focused pool — the model stays safe and on-topic.\n"
        "High (0.95): Nearly the full vocabulary is on the table.\n\n"
        "Example: A factual assistant at 0.7 stays grounded. "
        "Creative brainstorming at 0.95 opens up unusual vocabulary. "
        "Works alongside temperature — lowering one often makes the other less necessary."
    ),
    "top_k": (
        "Hard cap on how many token candidates are considered at each step, "
        "regardless of their probability.\n\n"
        "Low (10–20): Only the most likely words are ever chosen — very predictable.\n"
        "High (100–200): Wide vocabulary, more room for creative choices.\n"
        "0: Disables the limit entirely.\n\n"
        "Example: Coding tasks benefit from top_k 20–40. "
        "Open-ended storytelling from 80–150."
    ),
    "min_p": (
        "Filters out any token less probable than (min_p × probability of the top token). "
        "Clips the tail of the distribution more precisely than top_k alone.\n\n"
        "0.0: Disabled — no tokens filtered by this rule.\n"
        "0.1: Drops any word less than 10% as likely as the best option.\n\n"
        "Example: min_p=0.05 quietly removes weird tangential words without affecting "
        "the most natural choices. Pairs well with a higher temperature."
    ),
    "repeat_penalty": (
        "Divides the probability of tokens that appeared recently, "
        "discouraging the model from looping.\n\n"
        "1.0: No penalty — the model may repeat phrases freely.\n"
        "1.1–1.2: Gentle nudge, fixes most looping without side effects.\n"
        "1.5+: Aggressively avoids recent words, which can make common words "
        "feel strangely absent.\n\n"
        "Example: If the model keeps writing 'furthermore, furthermore' — "
        "bumping from 1.0 to 1.15 usually cures it."
    ),
    "repeat_last_n": (
        "How many tokens back the repeat penalty scans when deciding what to penalise.\n\n"
        "-1: Scans the entire context — catches echoes from anywhere in the conversation.\n"
        "64: Only looks at the last ~64 tokens (recent output).\n"
        "0: Disables repeat penalty entirely.\n\n"
        "Example: For a short answer, 64 is fine. For long-form writing where you want "
        "to avoid re-using a word from three paragraphs ago, use -1."
    ),
    "presence_penalty": (
        "Applies a flat penalty to any token that has appeared at all, "
        "encouraging new topics and vocabulary regardless of frequency.\n\n"
        "Positive (0.2–0.6): Pushes the model to introduce new ideas and vary phrasing.\n"
        "Negative (−0.2 to −0.5): Allows the model to revisit the same words freely — "
        "useful for structured output with repeated labels or chant-like formats.\n\n"
        "Example: 0.3 noticeably diversifies a long response. "
        "-0.3 is handy for markdown tables or bullet lists where repeated words are intentional."
    ),
    "frequency_penalty": (
        "Like presence penalty, but scales with how many times a token has already appeared — "
        "the more a word has been used, the harder it is penalised.\n\n"
        "Positive (0.3–0.8): Increasingly strong pressure to avoid over-used words, "
        "keeping long responses fresh.\n"
        "Negative: Allows and even encourages high-frequency repetition.\n\n"
        "Example: 0.5 on a long essay noticeably reduces filler phrases like "
        "'it is important to note that'."
    ),
    "num_ctx": (
        "The total token window the model can see at once, covering your system prompt, "
        "injected context, conversation history, and the response.\n\n"
        "Small (2048): Fast, lower VRAM, fine for short notes and quick questions.\n"
        "Large (8192–32768): Needed for multi-turn conversations, large injected files, "
        "or when the model seems to 'forget' something from earlier in the session.\n\n"
        "Example: A single-note summary works fine at 2048. "
        "A coding session where you paste a whole file needs at least 8192."
    ),
    "num_predict": (
        "Maximum tokens the model will generate in a single response. "
        "-1 means no limit — the model stops when it naturally finishes.\n\n"
        "-1: Unlimited. Model decides when it's done.\n"
        "256–512: Caps at roughly 2–3 short paragraphs. Good for quick answers.\n"
        "If a response feels like it was cut off mid-sentence, "
        "this limit (or num_ctx) was hit.\n\n"
        "Example: Set 300 for a model that over-explains everything. "
        "Leave at -1 when writing long documents."
    ),
    "seed": (
        "A fixed integer seed makes the same prompt produce identical output every time. "
        "-1 picks a new random seed on each run.\n\n"
        "-1: Random — different result each run (default).\n"
        "Any fixed integer: Fully reproducible output for that exact prompt + settings.\n\n"
        "Example: You got a perfect response and want to regenerate it exactly — "
        "note the seed Ollama logs (or set one yourself), paste it here. "
        "Set back to -1 when you want variety again."
    ),
    "tfs_z": (
        "Tail-free sampling removes tokens at the very bottom of the probability "
        "distribution before sampling, reducing 'noise' tokens without affecting likely words.\n\n"
        "1.0: Disabled.\n"
        "0.95: Gently trims the tail — barely noticeable in output but removes "
        "occasional odd word choices.\n"
        "0.5: More aggressive, may start affecting fluency.\n\n"
        "Example: Leave near 1.0 unless you're seeing random rare words pop up at "
        "higher temperatures. Usually doesn't need adjusting."
    ),
    "typical_p": (
        "Keeps tokens that are 'typically probable' given context — discarding both "
        "the most obvious choices and long-shot surprises, to mimic natural writing patterns.\n\n"
        "1.0: Disabled.\n"
        "0.85: Reduces filler phrases and over-confident completions without sacrificing variety.\n"
        "0.5: Tight, constrained — almost never surprises you.\n\n"
        "Example: 0.85 can clean up a model that pads responses with "
        "'It is worth noting that...' type filler."
    ),
    "mirostat": (
        "A perplexity-targeting algorithm that dynamically adjusts sampling to maintain "
        "consistent coherence across a response.\n\n"
        "0: Off — use top_p/top_k/temperature instead.\n"
        "1: Mirostat v1 (original).\n"
        "2: Mirostat v2 — generally recommended, often outperforms manual top-p/top-k "
        "for sustained long-form writing.\n\n"
        "Example: With mirostat=2 a long story stays coherent and engaged throughout "
        "instead of drifting into repetition. When active, set tau and eta; "
        "top_k/top_p have less effect."
    ),
    "mirostat_tau": (
        "Target entropy ('surprise level') when Mirostat is active. "
        "Think of it as how adventurous the model is allowed to be.\n\n"
        "Low (2–4): Focused, predictable output that stays on topic.\n"
        "High (7–9): Varied, imaginative — the model takes more unexpected turns.\n\n"
        "Example: tau=3.5 for an essay that must stay on-point. "
        "tau=7.0 for a story you want to surprise you. "
        "Default 5.0 is a reasonable middle ground for most tasks."
    ),
    "mirostat_eta": (
        "Learning rate for Mirostat — how quickly it corrects when output drifts "
        "away from the target tau.\n\n"
        "Low (0.05–0.1): Gradual corrections, smooth output — recommended for most uses.\n"
        "High (0.3–0.5): Reacts fast but can cause choppy swings between dull and chaotic "
        "within a single response.\n\n"
        "Example: Default 0.1 is right for almost everything. "
        "Only increase if you need very tight real-time perplexity control."
    ),
}


class _HelpPopup(QFrame):
    """Small popup panel shown when a parameter help button is clicked."""

    _STYLE = (
        "QFrame {"
        "  background: #1c2128;"
        "  border: 1px solid #444c56;"
        "  border-radius: 6px;"
        "  padding: 2px;"
        "}"
        "QLabel { color: #cdd9e5; font-size: 12px; background: transparent; }"
    )

    def __init__(self, text: str):
        super().__init__(None, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setStyleSheet(self._STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setMaximumWidth(320)
        layout.addWidget(lbl)

    def show_near(self, global_pos: QPoint):
        self.adjustSize()
        screen = QApplication.primaryScreen().availableGeometry()
        x = min(global_pos.x(), screen.right() - self.width() - 8)
        y = global_pos.y() + 6
        if y + self.height() > screen.bottom() - 8:
            y = global_pos.y() - self.height() - 6
        self.move(x, y)
        self.show()


class OllamaParamsDialog(QDialog):
    """Advanced Ollama generation parameters and context injection editor."""

    def __init__(
        self,
        parent,
        model_params: dict,
        injections: list,
        base_url: str = "http://localhost:11434",
        model: str = "",
    ):
        super().__init__(parent)
        self.setWindowTitle("Model parameters")
        self.resize(500, 580)
        self._model_params = dict(model_params)
        self._injections = list(injections)
        self._base_url = base_url.rstrip("/")
        self._model = model

        root = QVBoxLayout(self)
        tabs = QTabWidget()
        root.addWidget(tabs, 1)

        # ── Generation tab ────────────────────────────────────────────────────
        gen_tab = QWidget()
        gen_scroll = QScrollArea()
        gen_scroll.setWidgetResizable(True)
        gen_container = QWidget()
        gen_vbox = QVBoxLayout(gen_container)
        gen_vbox.setContentsMargins(8, 8, 8, 8)
        gen_vbox.setSpacing(2)

        self._param_rows: dict[str, tuple] = {}  # key → (checkbox, spinbox)
        for section, params in OLLAMA_PARAM_GROUPS:
            hdr = QLabel(section)
            hdr.setStyleSheet(
                "font-weight: bold; padding-top: 10px; padding-bottom: 2px;"
            )
            gen_vbox.addWidget(hdr)
            form = QFormLayout()
            form.setContentsMargins(0, 0, 0, 4)
            form.setHorizontalSpacing(8)
            gen_vbox.addLayout(form)
            for key, label, typ, default, lo, hi, step, tip in params:
                row_w = QWidget()
                row_hl = QHBoxLayout(row_w)
                row_hl.setContentsMargins(0, 0, 0, 0)
                row_hl.setSpacing(6)
                cb = QCheckBox()
                cb.setToolTip("Check to override the model default")
                if typ is float:
                    sb = QDoubleSpinBox()
                    sb.setRange(lo, hi)
                    sb.setSingleStep(step)
                    sb.setDecimals(3 if step < 0.01 else 2)
                    sb.setValue(float(self._model_params.get(key, default)))
                else:
                    sb = QSpinBox()
                    sb.setRange(lo, hi)
                    sb.setSingleStep(step)
                    sb.setValue(int(self._model_params.get(key, default)))
                sb.setFixedWidth(120)
                cb.setChecked(key in self._model_params)
                sb.setEnabled(cb.isChecked())
                cb.toggled.connect(sb.setEnabled)
                help_btn = QPushButton("?")
                help_btn.setFixedSize(17, 17)
                help_btn.setFlat(True)
                help_btn.setStyleSheet(
                    "QPushButton { border: 1px solid #555; border-radius: 8px;"
                    " color: #888; font-size: 10px; font-weight: bold; padding: 0; }"
                    "QPushButton:hover { background: #333; color: #ccc; }"
                )
                help_text = _OLLAMA_PARAM_HELP.get(key, tip)
                help_btn.clicked.connect(
                    lambda _checked, btn=help_btn, txt=help_text: self._show_help(
                        btn, txt
                    )
                )
                row_hl.addWidget(cb)
                row_hl.addWidget(sb)
                row_hl.addStretch()
                row_hl.addWidget(help_btn)
                form.addRow(label, row_w)
                self._param_rows[key] = (cb, sb)

        gen_vbox.addStretch()
        gen_scroll.setWidget(gen_container)
        reset_btn = QPushButton("Reset all to model defaults")
        reset_btn.clicked.connect(self._reset_all)
        load_btn = QPushButton(
            f"Load from model{': ' + self._model if self._model else ''}…"
        )
        load_btn.setToolTip(
            "Fetch the model's built-in recommended parameters from Ollama"
        )
        load_btn.clicked.connect(self._load_from_model)
        gen_root = QVBoxLayout(gen_tab)
        gen_root.setContentsMargins(0, 0, 0, 0)
        gen_root.addWidget(gen_scroll, 1)
        btn_row = QHBoxLayout()
        btn_row.addWidget(load_btn)
        btn_row.addStretch()
        btn_row.addWidget(reset_btn)
        gen_root.addLayout(btn_row)
        tabs.addTab(gen_tab, "Generation")

        # ── Injection tab ─────────────────────────────────────────────────────
        inj_tab = QWidget()
        inj_root = QVBoxLayout(inj_tab)
        inj_root.setContentsMargins(8, 8, 8, 8)

        inj_help = QLabel(
            "Inject files or text into every chat started from this profile.\n"
            "Files are read fresh each time a new chat begins."
        )
        inj_help.setWordWrap(True)
        inj_help.setStyleSheet("color: gray; font-size: 11px; padding-bottom: 6px;")
        inj_root.addWidget(inj_help)

        self._inj_list = QListWidget()
        self._inj_list.itemDoubleClicked.connect(self._edit_injection)
        inj_root.addWidget(self._inj_list, 1)
        self._refresh_inj_list()

        inj_btns = QHBoxLayout()
        add_inj = QPushButton("Add…")
        edit_inj = QPushButton("Edit…")
        del_inj = QPushButton("Remove")
        add_inj.clicked.connect(self._add_injection)
        edit_inj.clicked.connect(self._edit_injection)
        del_inj.clicked.connect(self._delete_injection)
        inj_btns.addWidget(add_inj)
        inj_btns.addWidget(edit_inj)
        inj_btns.addWidget(del_inj)
        inj_btns.addStretch()
        inj_root.addLayout(inj_btns)
        tabs.addTab(inj_tab, "Injection")

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _load_from_model(self):
        if not self._model:
            QMessageBox.information(self, "Load from model", "No model selected.")
            return
        try:
            import urllib.request as _ur

            req = _ur.Request(
                f"{self._base_url}/api/show",
                data=json.dumps({"name": self._model}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _ur.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            params_str = data.get("parameters", "")
        except Exception as e:
            QMessageBox.warning(
                self, "Load from model", f"Could not fetch model info:\n{e}"
            )
            return
        if not params_str:
            QMessageBox.information(
                self,
                "Load from model",
                f"{self._model} has no built-in parameters defined.",
            )
            return
        suggested = parse_ollama_model_params(params_str)
        applied = 0
        for key, val in suggested.items():
            if key not in self._param_rows:
                continue
            cb, sb = self._param_rows[key]
            try:
                sb.setValue(float(val) if isinstance(sb, QDoubleSpinBox) else int(val))
                cb.setChecked(True)
                applied += 1
            except Exception:
                pass
        if applied:
            QMessageBox.information(
                self,
                "Load from model",
                f"Applied {applied} parameter(s) from {self._model}.\n"
                "Review the checked values and uncheck any you don't want.",
            )
        else:
            QMessageBox.information(
                self,
                "Load from model",
                f"No matching parameters found in {self._model}'s definition.",
            )

    def _show_help(self, btn: QPushButton, text: str):
        self._help_popup = _HelpPopup(text)
        self._help_popup.show_near(btn.mapToGlobal(QPoint(0, btn.height())))

    def _reset_all(self):
        for cb, _ in self._param_rows.values():
            cb.setChecked(False)

    def _refresh_inj_list(self):
        self._inj_list.clear()
        for inj in self._injections:
            role = "sys" if inj.get("role") == "system" else "user"
            src = inj.get("path", "") or "(inline text)"
            self._inj_list.addItem(f"[{role}]  {inj.get('label', '?')}  —  {src}")

    def _add_injection(self):
        dlg = InjectionEditDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._injections.append(dlg.value())
            self._refresh_inj_list()

    def _edit_injection(self):
        row = self._inj_list.currentRow()
        if row < 0:
            return
        dlg = InjectionEditDialog(self, self._injections[row])
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._injections[row] = dlg.value()
            self._refresh_inj_list()

    def _delete_injection(self):
        row = self._inj_list.currentRow()
        if row >= 0:
            self._injections.pop(row)
            self._refresh_inj_list()

    def model_params(self) -> dict:
        result = {}
        for key, (cb, sb) in self._param_rows.items():
            if cb.isChecked():
                result[key] = sb.value()
        return result

    def injections(self) -> list:
        return list(self._injections)


class ShareTargetDialog(QDialog):
    """Add or edit a single share target entry."""

    KINDS = ["local_folder", "scp", "sftp", "taildrop", "ntfy", "command", "telegram"]
    FORMATS = ["html", "md", "txt"]

    def __init__(self, parent, target=None):
        super().__init__(parent)
        target = target or {}
        self.setWindowTitle("Edit share target" if target else "Add share target")
        self.resize(480, 300)
        root = QVBoxLayout(self)

        form = QFormLayout()
        root.addLayout(form)

        self.name_field = QLineEdit(target.get("name", ""))
        self.name_field.setPlaceholderText("My share target")
        form.addRow("Name", self.name_field)

        self.kind_combo = QComboBox()
        self.kind_combo.addItems(self.KINDS)
        kind = target.get("kind", "local_folder")
        if kind in self.KINDS:
            self.kind_combo.setCurrentText(kind)
        form.addRow("Kind", self.kind_combo)

        # Per-kind field panels inside a stacked widget
        self._stack = QStackedWidget()
        self._panels = {}
        root.addWidget(self._stack)

        def _panel(kind_key):
            w = QWidget()
            f = QFormLayout(w)
            f.setContentsMargins(0, 4, 0, 0)
            self._panels[kind_key] = (w, f)
            self._stack.addWidget(w)
            return f

        # local_folder
        f = _panel("local_folder")
        self.lf_path = QLineEdit(target.get("path", ""))
        self.lf_path.setPlaceholderText("~/Documents/scratch-shares")
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(self.lf_path)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(lambda: self._browse_folder(self.lf_path))
        path_layout.addWidget(browse_btn)
        self.lf_format = QComboBox()
        self.lf_format.addItems(self.FORMATS)
        self.lf_format.setCurrentText(
            target.get("format", "html") if kind == "local_folder" else "html"
        )
        f.addRow("Folder path", path_row)
        f.addRow("File format", self.lf_format)

        # scp
        f = _panel("scp")
        self.scp_dest = QLineEdit(
            target.get("destination", "") if kind == "scp" else ""
        )
        self.scp_dest.setPlaceholderText("nova:/home/ash/inbox/")
        self.scp_format = QComboBox()
        self.scp_format.addItems(self.FORMATS)
        self.scp_format.setCurrentText(
            target.get("format", "html") if kind == "scp" else "html"
        )
        f.addRow("Destination", self.scp_dest)
        f.addRow("File format", self.scp_format)

        # sftp
        f = _panel("sftp")
        self.sftp_dest = QLineEdit(
            target.get("destination", "") if kind == "sftp" else ""
        )
        self.sftp_dest.setPlaceholderText("user@host:/path/to/folder/")
        self.sftp_format = QComboBox()
        self.sftp_format.addItems(self.FORMATS)
        self.sftp_format.setCurrentText(
            target.get("format", "html") if kind == "sftp" else "html"
        )
        f.addRow("Destination", self.sftp_dest)
        f.addRow("File format", self.sftp_format)

        # taildrop
        f = _panel("taildrop")
        self.td_device = QLineEdit(
            target.get("device", "") if kind == "taildrop" else ""
        )
        self.td_device.setPlaceholderText("atlas  (Tailscale device name)")
        self.td_format = QComboBox()
        self.td_format.addItems(self.FORMATS)
        self.td_format.setCurrentText(
            target.get("format", "html") if kind == "taildrop" else "html"
        )
        f.addRow("Device name", self.td_device)
        f.addRow("File format", self.td_format)

        # ntfy
        f = _panel("ntfy")
        self.ntfy_url = QLineEdit(target.get("url", "") if kind == "ntfy" else "")
        self.ntfy_url.setPlaceholderText("https://ntfy.sh/my-topic")
        self.ntfy_token = QLineEdit(target.get("token", "") if kind == "ntfy" else "")
        self.ntfy_token.setPlaceholderText("optional auth token")
        self.ntfy_token.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow("Topic URL", self.ntfy_url)
        f.addRow("Auth token", self.ntfy_token)

        # command
        f = _panel("command")
        raw_cmd = target.get("command", "") if kind == "command" else ""
        if isinstance(raw_cmd, list):
            raw_cmd = shlex.join(raw_cmd)
        self.cmd_command = QLineEdit(raw_cmd)
        self.cmd_command.setPlaceholderText(
            "xclip -selection clipboard   (use {file} or {text})"
        )
        self.cmd_format = QComboBox()
        self.cmd_format.addItems(self.FORMATS)
        self.cmd_format.setCurrentText(
            target.get("format", "html") if kind == "command" else "html"
        )
        f.addRow("Command", self.cmd_command)
        f.addRow("File format", self.cmd_format)

        # telegram (extra chat as share target)
        f = _panel("telegram")
        self.tg_chat_id = QLineEdit(
            str(target.get("chat_id", "")) if kind == "telegram" else ""
        )
        self.tg_chat_id.setPlaceholderText(
            "numeric chat ID — find it in the Telegram tab"
        )
        f.addRow("Chat ID", self.tg_chat_id)

        self.kind_combo.currentTextChanged.connect(self._on_kind_changed)
        self._on_kind_changed(self.kind_combo.currentText())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _on_kind_changed(self, kind):
        widget = self._panels.get(kind, (None,))[0]
        if widget:
            self._stack.setCurrentWidget(widget)

    def _browse_folder(self, field):
        start = field.text() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Choose folder", start)
        if path:
            field.setText(path)

    def value(self):
        kind = self.kind_combo.currentText()
        name = self.name_field.text().strip() or kind
        t = {"name": name, "kind": kind}
        if kind == "local_folder":
            t["path"] = self.lf_path.text().strip()
            t["format"] = self.lf_format.currentText()
        elif kind == "scp":
            t["destination"] = self.scp_dest.text().strip()
            t["format"] = self.scp_format.currentText()
        elif kind == "sftp":
            t["destination"] = self.sftp_dest.text().strip()
            t["format"] = self.sftp_format.currentText()
        elif kind == "taildrop":
            t["device"] = self.td_device.text().strip()
            t["format"] = self.td_format.currentText()
        elif kind == "ntfy":
            t["url"] = self.ntfy_url.text().strip()
            tok = self.ntfy_token.text().strip()
            if tok:
                t["token"] = tok
        elif kind == "command":
            raw = self.cmd_command.text().strip()
            t["command"] = shlex.split(raw) if raw else []
            t["format"] = self.cmd_format.currentText()
        elif kind == "telegram":
            t["chat_id"] = self.tg_chat_id.text().strip()
        return t


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
        tg_root = QVBoxLayout(tg_tab)
        tg_root.addLayout(
            self._help_header(
                "Telegram setup",
                "Setting up Telegram sharing:\n\n"
                "1. Open Telegram and chat with @BotFather.\n"
                "2. Send /newbot and follow the prompts — you'll receive a bot token.\n"
                "3. Paste the token in 'Bot token' below.\n"
                "4. Send any message to your new bot (or add it to a group/channel).\n"
                "   For a channel: add the bot as an admin, then post something.\n"
                "5. Click 'Fetch recent chats' — discovered chat IDs appear in the list.\n"
                "6. Click a chat in the list to set it as the default.\n\n"
                "Why does 'Fetch recent chats' return 0 results?\n"
                "• getUpdates only sees messages sent TO the bot since it was last polled.\n"
                "  Send the bot a message first, then fetch.\n"
                "• If you have a webhook set on this bot token, getUpdates always returns\n"
                "  empty — webhooks and polling are mutually exclusive in the Bot API.\n"
                "  Delete the webhook with: api.telegram.org/bot<TOKEN>/deleteWebhook\n\n"
                "You can always type a chat ID directly — use @userinfobot to find your own.",
            )
        )
        tg_form = QFormLayout()
        tg_root.addLayout(tg_form)
        self.telegram_token = QLineEdit(telegram.get("bot_token", ""))
        self.telegram_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.telegram_chat = QLineEdit(telegram.get("default_chat_id", ""))
        self.telegram_parse_mode = QComboBox()
        self.telegram_parse_mode.addItems(["", "HTML", "MarkdownV2"])
        self.telegram_parse_mode.setCurrentText(telegram.get("parse_mode", ""))
        self.telegram_recent = QListWidget()
        self.telegram_recent.setMaximumHeight(120)
        for chat in telegram.get("recent_chats", []):
            label = chat.get("title") or chat.get("username") or str(chat.get("id", ""))
            self.telegram_recent.addItem(f"{label} :: {chat.get('id', '')}")
        self.telegram_recent.itemClicked.connect(
            lambda item: self.telegram_chat.setText(
                item.text().rsplit("::", 1)[-1].strip()
            )
        )
        refresh_btn = QPushButton("Fetch recent chats")
        refresh_btn.clicked.connect(self._fetch_recent_chats)
        tg_form.addRow("Bot token", self.telegram_token)
        tg_form.addRow("Default chat id", self.telegram_chat)
        tg_form.addRow("Parse mode", self.telegram_parse_mode)
        tg_form.addRow(refresh_btn)
        tg_form.addRow("Recent chats (click to set default)", self.telegram_recent)
        tabs.addTab(tg_tab, "Telegram")

        ollama = migrate_ollama_config(config.get("ollama", {}))
        self._profiles: list[dict] = list(
            ollama.get("profiles")
            or [
                {
                    "name": "Default",
                    "model": "llama3.2",
                    "system": "",
                    "model_params": {},
                    "context_injections": [],
                }
            ]
        )
        active_name = ollama.get("active_profile", "")
        self._active_profile_idx = next(
            (i for i, p in enumerate(self._profiles) if p.get("name") == active_name), 0
        )
        self._ollama_model_params: dict = {}
        self._ollama_injections: list = []

        ollama_tab = QWidget(self)
        ollama_root = QVBoxLayout(ollama_tab)
        ollama_root.addLayout(
            self._help_header(
                "Ollama setup",
                "1. Install Ollama (ollama.com) and start the service.\n"
                "2. Pull a model:  ollama pull mistral\n"
                "3. Set the base URL and click Test.\n"
                "4. Use Profiles to save different model+prompt+parameter combinations.\n"
                "   Select a profile in 'Ask Ollama' to switch mid-session.",
            )
        )

        # ── global settings ───────────────────────────────────────────────────
        global_form = QFormLayout()
        ollama_root.addLayout(global_form)
        self.ollama_base_url = QLineEdit(
            ollama.get("base_url", "http://localhost:11434")
        )
        url_row = QWidget()
        url_hl = QHBoxLayout(url_row)
        url_hl.setContentsMargins(0, 0, 0, 0)
        url_hl.addWidget(self.ollama_base_url)
        test_btn = QPushButton("Test")
        test_btn.setFixedWidth(54)
        test_btn.clicked.connect(self._test_ollama)
        url_hl.addWidget(test_btn)
        global_form.addRow("Base URL", url_row)

        # ── profile management ────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #444;")
        ollama_root.addWidget(sep)

        prof_hdr_row = QHBoxLayout()
        prof_hdr_row.addWidget(QLabel("Profiles"))
        prof_hdr_row.addStretch()
        ollama_root.addLayout(prof_hdr_row)

        prof_row = QWidget()
        prof_hl = QHBoxLayout(prof_row)
        prof_hl.setContentsMargins(0, 0, 0, 0)
        self._profile_combo = QComboBox()
        for p in self._profiles:
            self._profile_combo.addItem(p.get("name", "?"))
        self._profile_combo.setCurrentIndex(self._active_profile_idx)
        add_p = QPushButton("Add…")
        add_p.setFixedWidth(54)
        ren_p = QPushButton("Rename…")
        ren_p.setFixedWidth(70)
        del_p = QPushButton("Delete")
        del_p.setFixedWidth(54)
        add_p.clicked.connect(self._add_profile)
        ren_p.clicked.connect(self._rename_profile)
        del_p.clicked.connect(self._delete_profile)
        prof_hl.addWidget(self._profile_combo, 1)
        prof_hl.addWidget(add_p)
        prof_hl.addWidget(ren_p)
        prof_hl.addWidget(del_p)
        ollama_root.addWidget(prof_row)

        # ── per-profile settings ──────────────────────────────────────────────
        ollama_form = QFormLayout()
        ollama_root.addLayout(ollama_form)
        self.ollama_model = QComboBox()
        self.ollama_model.setEditable(True)
        model_row = QWidget()
        model_hl = QHBoxLayout(model_row)
        model_hl.setContentsMargins(0, 0, 0, 0)
        model_hl.addWidget(self.ollama_model, 1)
        fetch_btn = QPushButton("Fetch models")
        fetch_btn.clicked.connect(self._fetch_ollama_models)
        model_hl.addWidget(fetch_btn)
        self.ollama_system = QTextEdit()
        self.ollama_system.setMinimumHeight(90)
        params_btn = QPushButton("Model parameters…")
        params_btn.clicked.connect(self._open_ollama_params)
        ollama_form.addRow("Model", model_row)
        ollama_form.addRow("System prompt", self.ollama_system)
        ollama_form.addRow("", params_btn)

        self._load_profile_fields()
        # connect after initial load to avoid spurious save
        self._profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        tabs.addTab(ollama_tab, "Ollama")

        targets_tab = QWidget(self)
        targets_layout = QVBoxLayout(targets_tab)
        self._targets_data = list(config.get("share_targets", []))
        self._targets_list = QListWidget()
        self._targets_list.itemDoubleClicked.connect(self._edit_target)
        self._refresh_targets_list()
        targets_layout.addWidget(self._targets_list, 1)
        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add…")
        edit_btn = QPushButton("Edit…")
        del_btn = QPushButton("Delete")
        up_btn = QPushButton("↑")
        dn_btn = QPushButton("↓")
        add_btn.clicked.connect(self._add_target)
        edit_btn.clicked.connect(self._edit_target)
        del_btn.clicked.connect(self._delete_target)
        up_btn.clicked.connect(self._move_target_up)
        dn_btn.clicked.connect(self._move_target_dn)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        btn_row.addWidget(up_btn)
        btn_row.addWidget(dn_btn)
        targets_layout.addLayout(btn_row)
        tabs.addTab(targets_tab, "Share targets")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _refresh_targets_list(self):
        self._targets_list.clear()
        for t in self._targets_data:
            name = t.get("name") or t.get("kind", "?")
            kind = t.get("kind", "")
            self._targets_list.addItem(f"{name}  [{kind}]")

    def _add_target(self):
        dlg = ShareTargetDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._targets_data.append(dlg.value())
            self._refresh_targets_list()
            self._targets_list.setCurrentRow(len(self._targets_data) - 1)

    def _edit_target(self):
        row = self._targets_list.currentRow()
        if row < 0:
            return
        dlg = ShareTargetDialog(self, self._targets_data[row])
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._targets_data[row] = dlg.value()
            self._refresh_targets_list()
            self._targets_list.setCurrentRow(row)

    def _delete_target(self):
        row = self._targets_list.currentRow()
        if row < 0:
            return
        name = self._targets_data[row].get("name", "this target")
        reply = QMessageBox.question(self, "Delete target", f"Remove '{name}'?")
        if reply == QMessageBox.StandardButton.Yes:
            del self._targets_data[row]
            self._refresh_targets_list()

    def _move_target_up(self):
        row = self._targets_list.currentRow()
        if row > 0:
            self._targets_data[row - 1], self._targets_data[row] = (
                self._targets_data[row],
                self._targets_data[row - 1],
            )
            self._refresh_targets_list()
            self._targets_list.setCurrentRow(row - 1)

    def _move_target_dn(self):
        row = self._targets_list.currentRow()
        if 0 <= row < len(self._targets_data) - 1:
            self._targets_data[row + 1], self._targets_data[row] = (
                self._targets_data[row],
                self._targets_data[row + 1],
            )
            self._refresh_targets_list()
            self._targets_list.setCurrentRow(row + 1)

    def _help_header(self, title, text):
        hl = QHBoxLayout()
        hl.addStretch()
        btn = QPushButton("?")
        btn.setFixedSize(22, 22)
        btn.setToolTip("Click for setup guide")
        btn.clicked.connect(lambda: QMessageBox.information(self, title, text))
        hl.addWidget(btn)
        return hl

    # ── profile helpers ───────────────────────────────────────────────────────

    def _save_current_profile(self):
        if not self._profiles:
            return
        p = self._profiles[self._active_profile_idx]
        p["model"] = self.ollama_model.currentText().strip() or "llama3.2"
        p["system"] = self.ollama_system.toPlainText()
        p["model_params"] = self._ollama_model_params
        p["context_injections"] = self._ollama_injections

    def _load_profile_fields(self):
        p = self._profiles[self._active_profile_idx]
        self._ollama_model_params = dict(p.get("model_params") or {})
        self._ollama_injections = list(p.get("context_injections") or [])
        model = p.get("model", "llama3.2")
        self.ollama_model.blockSignals(True)
        if self.ollama_model.findText(model) < 0:
            self.ollama_model.insertItem(0, model)
        self.ollama_model.setCurrentText(model)
        self.ollama_model.blockSignals(False)
        self.ollama_system.blockSignals(True)
        self.ollama_system.setPlainText(p.get("system", ""))
        self.ollama_system.blockSignals(False)

    def _on_profile_changed(self, idx: int):
        if idx == self._active_profile_idx:
            return
        self._save_current_profile()
        self._active_profile_idx = idx
        self._load_profile_fields()

    def _add_profile(self):
        name, ok = QInputDialog.getText(self, "Add profile", "Profile name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if any(p.get("name") == name for p in self._profiles):
            QMessageBox.warning(self, "Profile", f"'{name}' already exists.")
            return
        self._save_current_profile()
        reply = QMessageBox.question(
            self,
            "Add profile",
            "Copy current profile as starting point?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        new_p = (
            dict(self._profiles[self._active_profile_idx])
            if reply == QMessageBox.StandardButton.Yes
            else {"model_params": {}, "context_injections": []}
        )
        new_p["name"] = name
        self._profiles.append(new_p)
        self._profile_combo.blockSignals(True)
        self._profile_combo.addItem(name)
        self._profile_combo.blockSignals(False)
        self._active_profile_idx = len(self._profiles) - 1
        self._profile_combo.setCurrentIndex(self._active_profile_idx)

    def _rename_profile(self):
        idx = self._profile_combo.currentIndex()
        old = self._profiles[idx].get("name", "")
        name, ok = QInputDialog.getText(self, "Rename profile", "New name:", text=old)
        if not ok or not name.strip() or name.strip() == old:
            return
        name = name.strip()
        if any(p.get("name") == name for p in self._profiles):
            QMessageBox.warning(self, "Profile", f"'{name}' already exists.")
            return
        self._profiles[idx]["name"] = name
        self._profile_combo.setItemText(idx, name)

    def _delete_profile(self):
        if len(self._profiles) <= 1:
            QMessageBox.information(self, "Profile", "Cannot delete the last profile.")
            return
        idx = self._profile_combo.currentIndex()
        name = self._profiles[idx].get("name", "?")
        if (
            QMessageBox.question(
                self,
                "Delete profile",
                f"Delete '{name}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._profiles.pop(idx)
        self._profile_combo.blockSignals(True)
        self._profile_combo.removeItem(idx)
        self._active_profile_idx = min(idx, len(self._profiles) - 1)
        self._profile_combo.setCurrentIndex(self._active_profile_idx)
        self._profile_combo.blockSignals(False)
        self._load_profile_fields()

    def _open_ollama_params(self):
        base_url = self.ollama_base_url.text().strip() or "http://localhost:11434"
        model = self.ollama_model.currentText().strip()
        dlg = OllamaParamsDialog(
            self,
            self._ollama_model_params,
            self._ollama_injections,
            base_url=base_url,
            model=model,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._ollama_model_params = dlg.model_params()
            self._ollama_injections = dlg.injections()

    def _test_ollama(self):
        base_url = (
            self.ollama_base_url.text().strip() or "http://localhost:11434"
        ).rstrip("/")
        try:
            with urllib.request.urlopen(f"{base_url}/api/version", timeout=8) as resp:
                data = json.loads(resp.read().decode())
            version = data.get("version", "unknown")
            QMessageBox.information(self, "Ollama", f"Connected — Ollama {version}")
        except Exception as e:
            QMessageBox.warning(self, "Ollama", f"Could not reach {base_url}:\n{e}")

    def _fetch_ollama_models(self):
        base_url = (
            self.ollama_base_url.text().strip() or "http://localhost:11434"
        ).rstrip("/")
        try:
            with urllib.request.urlopen(f"{base_url}/api/tags", timeout=8) as resp:
                data = json.loads(resp.read().decode())
            models = [m["name"] for m in data.get("models", [])]
        except Exception as e:
            QMessageBox.warning(self, "Ollama", f"Could not fetch models:\n{e}")
            return
        if not models:
            QMessageBox.information(
                self,
                "Ollama models",
                "No models found. Pull one first:\n  ollama pull mistral",
            )
            return
        current = self.ollama_model.currentText()
        self.ollama_model.clear()
        self.ollama_model.addItems(models)
        if current in models:
            self.ollama_model.setCurrentText(current)

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
                    "title": chat.get("title")
                    or " ".join(
                        p for p in [chat.get("first_name"), chat.get("last_name")] if p
                    ),
                    "username": chat.get("username", ""),
                }
            self.telegram_recent.clear()
            for chat in chats.values():
                label = chat.get("title") or chat.get("username") or str(chat.get("id"))
                self.telegram_recent.addItem(f"{label} :: {chat.get('id')}")
            QMessageBox.information(
                self, "Telegram", f"Loaded {len(chats)} recent chats."
            )
        except Exception as e:
            QMessageBox.warning(self, "Telegram", f"Could not fetch chats:\n{e}")

    def value(self):
        self._save_current_profile()
        share_targets = list(self._targets_data)

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
                "base_url": self.ollama_base_url.text().strip()
                or "http://localhost:11434",
                "active_profile": self._profiles[self._active_profile_idx].get("name")
                if self._profiles
                else "Default",
                "profiles": self._profiles,
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
        self.toolbar_button_spacing = int_row(
            "Button spacing", "toolbar_button_spacing", 0, 12
        )
        self.toolbar_group_spacing = int_row(
            "Group spacing", "toolbar_group_spacing", 0, 24
        )
        self.page_rail_padding = int_row(
            "Page rail side padding", "page_rail_padding", 0, 18
        )
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
            QDialogButtonBox.StandardButton.RestoreDefaults
            | QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(
            self._restore_defaults
        )
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


# ── page list dialog ─────────────────────────────────────────────────────────


class PageListDialog(QDialog):
    """Popup listing all page titles; clicking one activates that page."""

    page_selected = pyqtSignal(int)

    _STYLE = (
        "QDialog { background:#16213e; border:1px solid #2d2d4e; }"
        "QPushButton#page-item {"
        "  background:transparent; color:#c9d1d9;"
        "  border:none; border-radius:4px;"
        "  padding:6px 14px; text-align:left; font-size:13px;"
        "}"
        "QPushButton#page-item:hover { background:#2d2d4e; color:#ffffff; }"
        "QPushButton#page-item.current { color:#7ec8a4; font-weight:600; }"
        "QScrollArea { border:none; background:transparent; }"
        "QWidget#scroll-inner { background:transparent; }"
    )

    def __init__(self, parent, pages: list[str], current_idx: int):
        super().__init__(
            parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setStyleSheet(self._STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner.setObjectName("scroll-inner")
        vbox = QVBoxLayout(inner)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(1)

        for i, page_content in enumerate(pages):
            if is_livecodes_content_config(page_content):
                try:
                    title = page_title_from_config(json.loads(page_content))
                except Exception:
                    title = ""
            else:
                title = page_title_from_html(str(page_content))
            label = title.strip() or f"(Page {i + 1})"
            display = f"{i + 1}.  {label}"

            btn = QPushButton(display)
            btn.setObjectName("page-item")
            if i == current_idx:
                btn.setProperty("class", "current")
                btn.setStyleSheet(
                    "QPushButton#page-item { color:#7ec8a4; font-weight:600;"
                    " background:transparent; border:none; border-radius:4px;"
                    " padding:6px 14px; text-align:left; font-size:13px; }"
                    "QPushButton#page-item:hover { background:#2d2d4e; }"
                )
            btn.clicked.connect(lambda _=False, idx=i: self._select(idx))
            vbox.addWidget(btn)

        vbox.addStretch()
        scroll.setWidget(inner)
        scroll.setMaximumHeight(320)
        root.addWidget(scroll)
        self.adjustSize()

    def _select(self, idx: int):
        self.page_selected.emit(idx)
        self.accept()

    def show_near(self, global_pos: QPoint):
        self.adjustSize()
        screen = QApplication.primaryScreen().availableGeometry()
        x = max(
            screen.left() + 4,
            min(global_pos.x() - self.width() // 2, screen.right() - self.width() - 4),
        )
        y = global_pos.y() - self.height() - 4
        if y < screen.top() + 4:
            y = global_pos.y() + 4
        self.move(x, y)
        self.show()


# ── main window ──────────────────────────────────────────────────────────────


class ScratchPad(QWidget):
    """Main application window containing panes, terminal, and controls."""

    _ollama_start = pyqtSignal(int)
    _ollama_chunk = pyqtSignal(int, str)
    _ollama_done = pyqtSignal(int, str)
    _ollama_status = pyqtSignal(str, str)

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
        self._chat_pages: dict[int, list] = {}  # page_index → message history
        self._ollama_probe_running = False
        self._ollama_active_requests: set[int] = set()
        self._ollama_cancel_events: dict[int, threading.Event] = {}
        self._ollama_responses: dict[int, object] = {}
        self._ollama_stream_lock = threading.Lock()

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._flush_save)

        self._ollama_status_timer = QTimer(self)
        self._ollama_status_timer.setInterval(30000)
        self._ollama_status_timer.timeout.connect(self._probe_ollama_status)

        self.livecodes_app_url = livecodes_url()
        try:
            self._livecodes_thread, livecodes_port = start_livecodes_server()
            self.livecodes_app_url = livecodes_url(livecodes_port)
        except Exception as e:
            logger.warning("LiveCodes server was not started by Scratch: %s", e)

        self._ollama_start.connect(self._on_ollama_start)
        self._ollama_chunk.connect(self._on_ollama_chunk)
        self._ollama_done.connect(self._on_ollama_done)
        self._ollama_status.connect(self._apply_ollama_status)

        self._build_window()
        self._build_ui()
        self._build_global_terminal()
        ws = self.notes.get("window", {})
        start_page = min(ws.get("active_page", 0), max(0, len(self.notes["pages"]) - 1))
        self._add_pane(page=start_page)
        self._update_nav()
        self._ollama_status_timer.start()
        QTimer.singleShot(600, self._probe_ollama_status)

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
                logger.warning(
                    "Failed to parse notes file %s: %s — starting with fresh notes",
                    DATA_FILE,
                    e,
                )
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
                    if isinstance(defaults, dict) and isinstance(
                        raw.get(section), dict
                    ):
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
            screen = (
                QApplication.screenAt(QPoint(x + w // 2, y + h // 2))
                or QApplication.primaryScreen()
            )
            avail = screen.availableGeometry()
            x = max(avail.left() + 10, min(x, avail.right() - w - 10))
            y = max(avail.top() + 10, min(y, avail.bottom() - h - 10))
        else:
            avail = QApplication.primaryScreen().availableGeometry()
            pad = 40
            x = avail.right() - w - pad
            y = avail.top() + pad

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
        top.setContentsMargins(
            self._ui_settings["toolbar_padding"],
            0,
            self._ui_settings["toolbar_padding"],
            0,
        )
        top.setSpacing(self._ui_settings["toolbar_button_spacing"])
        self._top_layout = top
        self._toolbar_group_spacers = []

        def btn(label, obj_name="command", tip=None):
            b = QPushButton(label)
            b.setFixedSize(
                QSize(
                    self._ui_settings["button_size"], self._ui_settings["button_height"]
                )
            )
            if obj_name:
                b.setObjectName(obj_name)
            if tip:
                b.setToolTip(tip)
            self._toolbar_buttons.append(b)
            return b

        self._toolbar_buttons = []

        self.pin_btn = btn(
            "📌", obj_name="pin-on", tip="Pin window  (Ctrl+P, double-click top bar)"
        )
        self.pin_btn.clicked.connect(self._toggle_pin)

        self.add_btn = btn("+", obj_name="add", tip="New page  (Ctrl+N)")
        self.del_btn = btn("🗑", obj_name="del", tip="Delete current page  (Ctrl+W)")
        self.ai_status_btn = btn(
            "◌",
            obj_name="ai-unknown",
            tip="Ollama status unknown. Click to check and warm the selected model.",
        )
        self.ask_btn = btn("🤖", tip="Ask Ollama  (Ctrl+Shift+A)")
        self.term_btn = btn("⌨", tip="Toggle terminal  (Ctrl+T)")
        self.split_btn = btn(
            "◫", tip="Split pane  (Ctrl+\\); close splits with Ctrl+Shift+\\"
        )
        self.lc_editor_btn = btn("◧", tip="Show editor only")
        self.lc_split_btn = btn("▥", tip="Show editor + result")
        self.lc_result_btn = btn("◨", tip="Show result only")
        self.hide_btn = btn("–", tip="Hide to tray  (Ctrl+H)")
        self.quit_btn = btn("×", obj_name="danger", tip="Quit  (Ctrl+Q)")

        self.add_btn.clicked.connect(self._new_page)
        self.del_btn.clicked.connect(self._delete_page)
        self.ai_status_btn.clicked.connect(lambda: self._probe_ollama_status(warm=True))
        self.ask_btn.clicked.connect(self._ask_ollama)
        self.term_btn.clicked.connect(self._toggle_terminal)
        self.split_btn.clicked.connect(self._toggle_split_panes)
        self.lc_editor_btn.clicked.connect(
            lambda: self._run_livecodes_action("show", ["editor"])
        )
        self.lc_split_btn.clicked.connect(
            lambda: self._run_livecodes_action("show", ["toggle-result"])
        )
        self.lc_result_btn.clicked.connect(
            lambda: self._run_livecodes_action("show", ["result"])
        )
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
        add_group(self.ai_status_btn, self.ask_btn, self.term_btn, self.split_btn)
        add_group(self.lc_editor_btn, self.lc_split_btn, self.lc_result_btn)
        top.addStretch()
        add_group(self.hide_btn, self.quit_btn)

        self.h_split = QSplitter(Qt.Orientation.Horizontal, self)
        self.h_split.setChildrenCollapsible(False)

        navbar = QFrame(self)
        navbar.setObjectName("navbar")
        navbar.setFixedHeight(34)
        nav = QGridLayout(navbar)
        nav.setContentsMargins(
            self._ui_settings["page_rail_padding"],
            0,
            self._ui_settings["page_rail_padding"],
            0,
        )
        nav.setHorizontalSpacing(4)
        self._nav_layout = nav
        nav.setColumnStretch(0, 1)
        nav.setColumnStretch(1, 1)
        nav.setColumnStretch(2, 1)

        self.prev_btn = btn(
            "◀", obj_name="nav", tip="Previous page  (Ctrl+Left, Ctrl+wheel up)"
        )
        self.prev_btn.clicked.connect(self._prev_page)

        self.next_btn = btn(
            "▶", obj_name="nav", tip="Next page  (Ctrl+Right, Ctrl+wheel down)"
        )
        self.next_btn.clicked.connect(self._next_page)

        self.page_counter_btn = QPushButton("1 / 1")
        self.page_counter_btn.setObjectName("nav-counter")
        self.page_counter_btn.setFlat(True)
        self.page_counter_btn.setToolTip("All pages")
        self.page_counter_btn.clicked.connect(self._show_page_list)

        right_nav = QWidget(self)
        right_nav_layout = QHBoxLayout(right_nav)
        right_nav_layout.setContentsMargins(0, 0, 0, 0)
        right_nav_layout.setSpacing(4)
        right_nav_layout.addStretch()
        right_nav_layout.addWidget(self.next_btn)

        nav.addWidget(self.prev_btn, 0, 0, Qt.AlignmentFlag.AlignLeft)
        nav.addWidget(self.page_counter_btn, 0, 1, Qt.AlignmentFlag.AlignCenter)
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
        handles["bottom-right"].setGeometry(
            width - corner, height - corner, corner, corner
        )

        for handle in handles.values():
            handle.raise_()
            handle.show()

    def _build_global_terminal(self):
        self.global_term = QWebEngineView(self)
        self.global_term.setMinimumSize(QSize(0, 0))
        s = self.global_term.settings()
        s.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
        )
        s.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False
        )
        self.global_term_bridge = TerminalBridge(self)
        self.global_term_channel = QWebChannel(self)
        self.global_term_channel.registerObject("bridge", self.global_term_bridge)
        self.global_term.page().setWebChannel(self.global_term_channel)
        self.global_term.hide()
        self._terminal_loaded = False
        self._pending_pty_cwd = None

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
            lambda pi, html, origin=pane: self._on_any_content_changed(pi, html, origin)
        )
        pane.page_loaded.connect(lambda pi, p=pane: self._on_pane_page_loaded(p, pi))
        pane.chat_message_sent.connect(self._on_chat_message)
        pane.restore_history_requested.connect(self._on_restore_history)
        pane.stop_requested.connect(self._stop_ollama_response)
        self._panes.append(pane)
        self.h_split.addWidget(pane)
        pane.load_page(page)
        return pane

    def _on_pane_page_loaded(self, pane: "QuillPane", page_idx: int):
        if page_idx in self._chat_pages:
            pane.set_chat_mode(True)
            pane.show_chat_html(chat_page_html(self._chat_pages[page_idx]))
        elif is_chat_page_html(self.notes["pages"][page_idx]):
            pane.set_chat_mode(True, restore_available=True)
            pane.show_chat_html(self.notes["pages"][page_idx])
        else:
            pane.set_chat_mode(False)

    def _on_restore_history(self, page_idx: int):
        messages = parse_chat_page_html(self.notes["pages"][page_idx])
        if messages is None:
            return
        self._chat_pages[page_idx] = messages
        rendered = chat_page_html(messages)
        for pane in self._panes:
            if pane.page_index == page_idx:
                pane.set_history_restored()
                pane.show_chat_html(rendered)

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

    def _reindex_chat_pages_after_insert(self, insert_index: int):
        self._chat_pages = reindex_page_map_after_insert(self._chat_pages, insert_index)

    def _reindex_chat_pages_after_delete(self, delete_index: int):
        self._chat_pages = reindex_page_map_after_delete(self._chat_pages, delete_index)

    def _rebuild_chat_pages_from_notes(self):
        self._chat_pages = {
            idx: messages
            for idx, page in enumerate(self.notes["pages"])
            if (messages := parse_chat_page_html(page)) is not None
        }

    def _reindex_open_panes_after_insert(self, insert_index: int):
        for pane in self._panes:
            if pane.page_index >= insert_index:
                pane.load_page(pane.page_index + 1)

    def _reindex_open_panes_after_delete(self, delete_index: int):
        new_max = len(self.notes["pages"]) - 1
        for pane in self._panes:
            if pane.page_index == delete_index:
                pane.load_page(min(delete_index, new_max))
            elif pane.page_index > delete_index:
                pane.load_page(pane.page_index - 1)

    def _split_pane(self):
        if len(self._panes) >= 3:
            return
        total = len(self.notes["pages"])
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
        idx = self._active_pane().page_index
        self.page_counter_btn.setText(f"{idx + 1} / {total}")
        self.prev_btn.setEnabled(idx > 0)
        self.next_btn.setEnabled(idx < total - 1)
        self._sync_command_states()

    def _show_page_list(self):
        pages = self.notes["pages"]
        current_idx = self._active_pane().page_index
        dlg = PageListDialog(self, pages, current_idx)
        dlg.page_selected.connect(self._go_to_page_result_view)
        btn_center = self.page_counter_btn.mapToGlobal(
            QPoint(self.page_counter_btn.width() // 2, 0)
        )
        dlg.show_near(btn_center)

    def _go_to_page_result_view(self, idx: int):
        self._after_content_snapshot(lambda target=idx: self._load_page_result(target))

    def _load_page_result(self, idx: int):
        self._load_active_page(idx)
        self._run_livecodes_action("show", ["result"])

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
            self._topbar.setFixedHeight(
                max(36, self._ui_settings["button_height"] + 12)
            )
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
            button.setFixedSize(
                QSize(
                    self._ui_settings["button_size"], self._ui_settings["button_height"]
                )
            )
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
            self._after_content_snapshot(
                lambda target=pane.page_index - 1: self._load_active_page(target)
            )

    def _next_page(self):
        pane = self._active_pane()
        if pane.page_index < len(self.notes["pages"]) - 1:
            self._after_content_snapshot(
                lambda target=pane.page_index + 1: self._load_active_page(target)
            )

    def _load_active_page(self, index):
        self._active_pane().load_page(index)
        self._flush_save()
        self._update_nav()

    def _new_page(self):
        idx = self._active_pane().page_index
        self._after_content_snapshot(lambda idx=idx: self._new_page_after_snapshot(idx))

    def _new_page_after_snapshot(self, idx):
        self._flush_save()
        insert_idx = idx + 1
        self.notes["pages"].insert(insert_idx, "")
        self._reindex_chat_pages_after_insert(insert_idx)
        self._reindex_open_panes_after_insert(insert_idx)
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
            self,
            "Remove blank pages",
            f"Remove {removed} blank page{'s' if removed != 1 else ''}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.notes["pages"] = non_blank or [""]
        self._rebuild_chat_pages_from_notes()
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
        self._reindex_chat_pages_after_delete(idx)
        self._reindex_open_panes_after_delete(idx)
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
            if not hasattr(self, "_main_split"):
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
            if hasattr(self, "_main_split"):
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

    def _active_ollama_profile(self, override: str | None = None) -> dict:
        ollama = self.config.get("ollama", {})
        profiles = ollama.get("profiles") or []
        if not profiles:
            return ollama  # backward compat with flat config
        name = override or ollama.get("active_profile")
        if name:
            p = next((p for p in profiles if p.get("name") == name), None)
            if p:
                return p
        return profiles[0]

    def _ollama_connection_target(self) -> tuple[str, str]:
        ollama = self.config.get("ollama", {})
        profile = self._active_ollama_profile()
        base_url = (ollama.get("base_url") or "http://localhost:11434").rstrip("/")
        model = profile.get("model", "llama3.2") or "llama3.2"
        return base_url, model

    def _apply_ollama_status(self, state: str, detail: str):
        if not hasattr(self, "ai_status_btn"):
            return
        states = {
            "unknown": ("◌", "ai-unknown"),
            "checking": ("…", "ai-checking"),
            "warming": ("◍", "ai-checking"),
            "connected": ("○", "ai-connected"),
            "loaded": ("●", "ai-loaded"),
            "processing": ("●", "ai-processing"),
            "offline": ("×", "ai-offline"),
            "error": ("!", "ai-error"),
        }
        label, object_name = states.get(state, states["unknown"])
        self.ai_status_btn.setText(label)
        self._set_button_object_name(self.ai_status_btn, object_name)
        self.ai_status_btn.setToolTip(detail)

    def _probe_ollama_status(self, *, warm: bool = False):
        if self._ollama_probe_running:
            return
        if self._ollama_active_requests:
            self._ollama_status.emit(
                "processing",
                "Ollama is processing a Scratch prompt. Click is disabled until it finishes.",
            )
            return
        self._ollama_probe_running = True
        base_url, model = self._ollama_connection_target()
        keep_alive = _ollama_keep_alive()
        self._ollama_status.emit(
            "warming" if warm else "checking",
            f"Checking Ollama at {base_url} for {model}...",
        )

        def _probe():
            try:
                with urllib.request.urlopen(f"{base_url}/api/version", timeout=4) as resp:
                    version_data = json.loads(resp.read().decode())
                version = version_data.get("version", "unknown")
                with urllib.request.urlopen(f"{base_url}/api/ps", timeout=4) as resp:
                    ps_data = json.loads(resp.read().decode())
                loaded_names = ollama_loaded_model_names(ps_data)
                if warm and not ollama_model_is_loaded(ps_data, model):
                    self._ollama_status.emit(
                        "warming",
                        f"Ollama {version} is connected. Loading {model}...",
                    )
                    warm_payload = {"model": model, "prompt": "", "stream": False}
                    if keep_alive:
                        warm_payload["keep_alive"] = keep_alive
                    payload = json.dumps(warm_payload).encode()
                    req = urllib.request.Request(
                        f"{base_url}/api/generate",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=_ollama_request_timeout()) as resp:
                        resp.read()
                    with urllib.request.urlopen(f"{base_url}/api/ps", timeout=4) as resp:
                        ps_data = json.loads(resp.read().decode())
                    loaded_names = ollama_loaded_model_names(ps_data)
                if ollama_model_is_loaded(ps_data, model):
                    detail = f"Ollama {version}: {model} is loaded."
                    if keep_alive:
                        detail += f" keep_alive={keep_alive}."
                    self._ollama_status.emit("loaded", detail)
                else:
                    loaded = ", ".join(loaded_names) if loaded_names else "no models loaded"
                    self._ollama_status.emit(
                        "connected",
                        f"Ollama {version} is connected; {loaded}. Click to warm {model}.",
                    )
            except Exception as e:
                self._ollama_status.emit("offline", f"Ollama is not reachable: {e}")
            finally:
                self._ollama_probe_running = False

        threading.Thread(target=_probe, daemon=True).start()

    def _ask_ollama(self):
        ollama = self.config.get("ollama", {})
        profiles = ollama.get("profiles") or []
        active = ollama.get("active_profile", "")
        dlg = OllamaPromptDialog(self, profiles, active)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.value():
            return
        self._send_to_ollama(dlg.value(), profile_name=dlg.selected_profile())

    def _send_to_ollama(
        self, prompt, *, chat_page_idx=None, profile_name: str | None = None
    ):
        """Start a new chat from the current note, or append a follow-up to an existing chat page."""
        ollama = self.config.get("ollama", {})
        profile = self._active_ollama_profile(override=profile_name)
        model = profile.get("model", "llama3.2") or "llama3.2"
        system = profile.get("system") or None
        base_url = (ollama.get("base_url") or "http://localhost:11434").rstrip("/")
        model_params = profile.get("model_params") or {}
        options = dict(model_params) if model_params else None
        keep_alive = _ollama_keep_alive()

        is_followup = chat_page_idx is not None

        if is_followup:
            history = self._chat_pages[chat_page_idx]
            history.append({"role": "user", "content": prompt})
            payload = json.dumps(
                ollama_chat_payload(
                    history,
                    model=model,
                    system=system,
                    options=options,
                    keep_alive=keep_alive,
                )
            ).encode()
            req = urllib.request.Request(
                f"{base_url}/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            self._stream_ollama_chat(req, chat_page_idx, use_chat_endpoint=True)
            return

        # Process context injections for new chats
        injections = profile.get("context_injections") or []
        extra_system_parts = []
        context_messages: list[dict] = []
        for inj in injections:
            content = _load_injection_content(inj)
            if not content:
                continue
            if inj.get("role") == "system":
                label = inj.get("label", "Context")
                extra_system_parts.append(f"[{label}]\n{content}")
            else:
                label = inj.get("label", "Injected context")
                context_messages.append(
                    {"role": "user", "content": f"[{label}]\n{content}"}
                )
                context_messages.append({"role": "assistant", "content": "Understood."})

        if extra_system_parts:
            system = ((system + "\n\n") if system else "") + "\n\n".join(
                extra_system_parts
            )

        page_idx = self._active_pane().page_index
        text_content = plain_text_from_html(self.notes["pages"][page_idx])
        history: list[dict] = list(context_messages)
        if text_content.strip():
            history.append(
                {"role": "user", "content": f"Context from my note:\n{text_content}"}
            )
            history.append(
                {"role": "assistant", "content": "Got it. What would you like to know?"}
            )
        history.append({"role": "user", "content": prompt})

        payload = json.dumps(
            ollama_chat_payload(
                history,
                model=model,
                system=system,
                options=options,
                keep_alive=keep_alive,
            )
        ).encode()
        req = urllib.request.Request(
            f"{base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        def _after_snapshot():
            self._flush_save()
            response_idx = self._active_pane().page_index + 1
            self.notes["pages"].insert(response_idx, "")
            self._reindex_chat_pages_after_insert(response_idx)
            self._reindex_open_panes_after_insert(response_idx)
            self._chat_pages[response_idx] = history
            self._active_pane().load_page(response_idx)
            self._active_pane().set_edit_mode(False)
            self._active_pane().set_chat_mode(True)
            # loadNoteSource is async in the iframe — retry result-view once it settles
            pane_ref = self._active_pane()
            QTimer.singleShot(350, lambda p=pane_ref: p.set_edit_mode(False))
            self._flush_save()
            self._update_nav()
            self._stream_ollama_chat(req, response_idx, use_chat_endpoint=True)

        self._after_content_snapshot(_after_snapshot)

    def _stream_ollama_chat(self, req, page_index, *, use_chat_endpoint=True):
        chunk_fn = (
            ollama_chat_stream_chunks if use_chat_endpoint else ollama_stream_chunks
        )
        timeout = _ollama_request_timeout()
        cancel_event = threading.Event()
        with self._ollama_stream_lock:
            self._ollama_cancel_events[page_index] = cancel_event
        self._ollama_start.emit(page_index)

        def _stream():
            chunks = []
            stopped = False
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    with self._ollama_stream_lock:
                        self._ollama_responses[page_index] = resp
                    for chunk in chunk_fn(resp):
                        if cancel_event.is_set():
                            stopped = True
                            break
                        chunks.append(chunk)
                        self._ollama_chunk.emit(page_index, chunk)
                response_text = "[Stopped by user.]" if stopped else "".join(chunks)
            except Exception as e:
                response_text = "[Stopped by user.]" if cancel_event.is_set() else f"Error: {e}"
            finally:
                with self._ollama_stream_lock:
                    self._ollama_responses.pop(page_index, None)
                    self._ollama_cancel_events.pop(page_index, None)
            self._ollama_done.emit(page_index, response_text)

        threading.Thread(target=_stream, daemon=True).start()

    def _stop_ollama_response(self, page_index: int):
        with self._ollama_stream_lock:
            cancel_event = self._ollama_cancel_events.get(page_index)
            response = self._ollama_responses.get(page_index)
        if cancel_event is None:
            return
        cancel_event.set()
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        self._ollama_status.emit("error", "Stopping the current Ollama response...")

    def _on_chat_message(self, page_index, prompt):
        if page_index not in self._chat_pages:
            return
        self._send_to_ollama(prompt, chat_page_idx=page_index)

    def _on_ollama_start(self, page_index):
        self._ollama_active_requests.add(page_index)
        self._ollama_status.emit(
            "processing",
            "Ollama is processing this prompt; waiting for the first token.",
        )
        for pane in self._panes:
            if pane.page_index == page_index:
                pane.set_connecting()

    def _on_ollama_chunk(self, page_index, chunk):
        self._ollama_status.emit("processing", "Ollama is streaming a response.")
        for pane in self._panes:
            if pane.page_index == page_index and pane._chat_state == "connecting":
                pane.set_streaming()
        if page_index in self._chat_pages:
            history = self._chat_pages[page_index]
            if not history or history[-1]["role"] != "assistant":
                history.append({"role": "assistant", "content": ""})
            history[-1]["content"] += chunk
            rendered = chat_page_html(history)
            for pane in self._panes:
                if pane.page_index == page_index:
                    pane.show_chat_html(rendered)
        else:
            self.notes["pages"][page_index] += chunk
            rendered = preformatted_html(self.notes["pages"][page_index])
            js = f"setPreviewHtml({json.dumps(rendered)}); setEditMode(false)"
            for pane in self._panes:
                if pane.page_index == page_index and pane._editor_ready:
                    pane.view.page().runJavaScript(js)
        self._update_nav()

    def _on_ollama_done(self, page_index, full_text):
        self._ollama_active_requests.discard(page_index)
        for pane in self._panes:
            if pane.page_index == page_index:
                pane.set_idle()
        if page_index in self._chat_pages:
            if full_text == "[Stopped by user.]":
                history = self._chat_pages[page_index]
                if not history or history[-1].get("role") != "assistant":
                    history.append({"role": "assistant", "content": full_text})
                elif full_text not in history[-1].get("content", ""):
                    history[-1]["content"] = (
                        history[-1].get("content", "").rstrip() + "\n\n" + full_text
                    ).strip()
                self._ollama_status.emit(
                    "loaded",
                    "Stopped the response; Ollama should keep the model warm.",
                )
            elif full_text.startswith("Error:"):
                history = self._chat_pages[page_index]
                if not history or history[-1].get("role") != "assistant":
                    history.append({"role": "assistant", "content": full_text})
                elif not history[-1].get("content"):
                    history[-1]["content"] = full_text
                self._ollama_status.emit("error", full_text)
            else:
                self._ollama_status.emit(
                    "loaded",
                    "Ollama finished the response; the model should remain warm.",
                )
            final_html = chat_page_html(self._chat_pages[page_index])
            self.notes["pages"][page_index] = final_html
            self._flush_save()
            for pane in self._panes:
                if pane.page_index == page_index:
                    pane.show_chat_html(final_html)
        else:
            final_html = preformatted_html(full_text)
            self._ollama_status.emit(
                "error" if full_text.startswith("Error:") else "loaded",
                full_text
                if full_text.startswith("Error:")
                else "Stopped the response."
                if full_text == "[Stopped by user.]"
                else "Ollama finished the response.",
            )
            self.notes["pages"][page_index] = final_html
            self._flush_save()
            for pane in self._panes:
                if pane.page_index == page_index and pane._editor_ready:
                    pane._send_content(final_html)
        self._update_nav()
        if not full_text.startswith("Error:"):
            QTimer.singleShot(1200, self._probe_ollama_status)

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

        lc_cut_act = add_action("Cut")
        lc_copy_act = add_action("Copy")
        lc_paste_act = add_action("Paste")
        lc_select_all_act = add_action("Select all")
        menu.addSeparator()
        ask_act = add_action("Ask Ollama", "Ctrl+Shift+A")
        term_act = add_action("Toggle terminal", "Ctrl+T")
        split_act = add_action("Split / unsplit", "Ctrl+\\")
        menu.addSeparator()
        lc_format_act = add_action("Format code")
        menu.addSeparator()
        share_menu = menu.addMenu("Share")
        share_copy_act = share_menu.addAction("Copy share text")
        share_copy_act.triggered.connect(lambda: self._share_current("clipboard", {}))
        share_ai_act = share_menu.addAction("Copy for AI")
        share_ai_act.triggered.connect(lambda: self._share_current("ai_clipboard", {}))
        telegram = self.config.get("telegram", {})
        default_chat = telegram.get("default_chat_id", "")
        share_tg_act = share_menu.addAction(
            "Telegram default" if default_chat else "Configure Telegram..."
        )
        share_tg_act.triggered.connect(
            (lambda: self._share_current("telegram", {"chat_id": default_chat}))
            if default_chat
            else self._open_config_panel
        )
        for target in self.config.get("share_targets", []):
            if not isinstance(target, dict):
                continue
            action = share_menu.addAction(
                str(target.get("name") or target.get("kind") or "Target")
            )
            action.triggered.connect(
                lambda checked=False, t=target: self._share_current(
                    str(t.get("kind", "")), t
                )
            )
        share_menu.addSeparator()
        share_menu.addAction("Configure sharing...").triggered.connect(
            self._open_config_panel
        )
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
            lc_format_act: lambda: self._run_livecodes_action("format"),
            ui_settings_act: self._open_ui_settings_panel,
            config_act: self._open_config_panel,
            export_act: self._export_page,
            pin_act: self._toggle_pin,
            delete_act: self._delete_page,
            remove_blanks_act: self._remove_blank_pages,
            hide_act: self.hide,
            quit_act: self._quit,
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
        pane.run_livecodes_edit_command(command)

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
                label = (
                    "selected content"
                    if payload.get("selected")
                    else "full Scratch note"
                )
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
                file_path = self._share_payload_file(
                    payload, target.get("format", "html")
                )
                dest = path / file_path.name
                shutil.copy2(file_path, dest)
                QMessageBox.information(self, "Share", f"Shared to {dest}")
            elif kind in {"scp", "sftp"}:
                destination = target.get("destination", "")
                if not destination:
                    raise ValueError(
                        "Missing destination, for example nova:/home/ash/inbox/"
                    )
                file_path = self._share_payload_file(
                    payload, target.get("format", "html")
                )
                subprocess.run(
                    ["scp", str(file_path), destination], check=True, timeout=45
                )
                QMessageBox.information(self, "Share", f"Shared to {destination}")
            elif kind == "taildrop":
                device = target.get("device", "")
                if not device:
                    raise ValueError("Missing Taildrop device name")
                file_path = self._share_payload_file(
                    payload, target.get("format", "html")
                )
                subprocess.run(
                    ["tailscale", "file", "cp", str(file_path), f"{device}:"],
                    check=True,
                    timeout=45,
                )
                QMessageBox.information(
                    self, "Share", f"Sent to {device} with Taildrop."
                )
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
        url = (
            target.get("url")
            or target.get("topic_url")
            or self.config.get("ntfy", {}).get("url", "")
        )
        if not url:
            raise ValueError("Missing ntfy URL")
        headers = {}
        token = target.get("token") or self.config.get("ntfy", {}).get("token", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            url, data=text.encode("utf-8"), headers=headers, method="POST"
        )
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
            self,
            "Export page",
            str(Path.home()),
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
        _, _, w, h = getattr(self, "_init_geometry", (0, 0, 440, 460))
        return QSize(w, h)

    def wheelEvent(self, event):
        if (
            self._ui_settings["ctrl_wheel_pages"]
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
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
        stdin=_sp.DEVNULL,
        stdout=_sp.DEVNULL,
        stderr=_sp.DEVNULL,
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
    icon = (
        QIcon(str(icon_path))
        if icon_path.exists()
        else QIcon.fromTheme("accessories-text-editor")
    )
    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("Scratch")

    menu = QMenu()
    show_act = menu.addAction("Show Scratch")
    menu.addSeparator()
    quit_act = menu.addAction("Quit")
    tray.setContextMenu(menu)

    show_act.triggered.connect(win.show_and_raise)
    quit_act.triggered.connect(win._quit)
    tray.activated.connect(
        lambda r: (
            win.show_and_raise()
            if r == QSystemTrayIcon.ActivationReason.Trigger
            else None
        )
    )
    tray.show()

    app.aboutToQuit.connect(win._flush_save)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
