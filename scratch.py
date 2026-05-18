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
import urllib.request
import urllib.parse
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, QPoint, QSize, QTimer, QUrl, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QIcon, QKeySequence, QShortcut
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (
    QApplication, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMenu, QMessageBox, QPushButton, QSplitter, QSystemTrayIcon, QTabWidget,
    QTextEdit, QVBoxLayout, QWidget, QInputDialog,
)

from scratch_core import (
    SHORTCUTS,
    Rect,
    create_shortcuts,
    ollama_generate_payload,
    ollama_stream_chunks,
    page_title_from_html,
    plain_text_from_html,
    preformatted_html,
    resize_rect,
)

logger = logging.getLogger(__name__)

DATA_FILE    = Path.home() / ".scratch-notes" / "notes.json"
CONFIG_FILE  = Path.home() / ".scratch-notes" / "config.json"
ASSETS_DIR   = Path(__file__).parent / "assets"
EDITOR_URL   = QUrl.fromLocalFile(str(ASSETS_DIR / "editor.html"))
TERMINAL_URL = QUrl.fromLocalFile(str(ASSETS_DIR / "terminal.html"))
SOCKET_PATH  = f"/tmp/scratch-{os.getuid()}.sock"

STYLE = """
QWidget#root { background: #1a1a2e; border: 1px solid #2d2d4e; border-radius: 8px; }
QFrame#topbar, QFrame#navbar { background: #16213e; }
QFrame#topbar { border-top-left-radius: 8px; border-top-right-radius: 8px; }
QFrame#navbar { border-bottom-left-radius: 8px; border-bottom-right-radius: 8px; }
QSplitter::handle { background: #2d2d4e; }
QSplitter::handle:horizontal { width: 3px; }
QSplitter::handle:vertical   { height: 4px; }
QPushButton {
    background: transparent; color: #8b8bac; border: none;
    font-size: 12px; padding: 4px 6px; border-radius: 5px;
}
QPushButton:hover { background: #2d2d4e; color: #e6edf3; }
QPushButton#command {
    background: rgba(255,255,255,.04); color: #c5d0e0;
    border: 1px solid rgba(139,139,172,.16); font-weight: 600;
}
QPushButton#command:hover { background: #263451; color: #ffffff; }
QPushButton#mode-on { background: #7ec8a4; color: #0f1720; font-weight: 700; }
QPushButton#tool-on { background: #7cc4ff; color: #0f1720; font-weight: 700; }
QPushButton#pin-on  { color: #ffd700; }
QPushButton#pin-off { color: #4a4a6a; }
QPushButton#danger  { color: #ff8f9a; }
QPushButton#add     { color: #7ec8a4; font-size: 14px; font-weight: bold; }
QPushButton#del     { color: #e06c75; }
QPushButton#nav     { color: #9aacd0; font-size: 12px; }
QPushButton#nav:hover    { color: #e6edf3; }
QPushButton#nav:disabled { color: #2d2d4e; }
QLabel#page-label { color: #9aacd0; font-size: 11px; font-weight: 700; }
QLabel#page-title { color: #7f8ead; font-size: 10px; font-style: italic; }
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
    """Container pane hosting the Quill editor QWebEngineView."""
    content_changed = pyqtSignal(int, str)

    def __init__(self, pad, initial_page=0):
        super().__init__()
        self._pad          = pad
        self._page_index   = initial_page
        self._editor_ready = False
        self._pending_html = None
        self._edit_mode    = False

        self.bridge  = QuillBridge(self)
        self.channel = QWebChannel(self)
        self.channel.registerObject("bridge", self.bridge)
        self.view = QWebEngineView(self)
        self.view.setMinimumSize(QSize(0, 0))
        s = self.view.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        self.view.page().setWebChannel(self.channel)
        self.view.setUrl(EDITOR_URL)
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
        self._pad._apply_bg_color(self)

    def on_content_changed(self, html):
        self._pad.notes["pages"][self._page_index] = html
        self._pad.schedule_save()
        self.content_changed.emit(self._page_index, html)

    def _send_content(self, html):
        self.view.page().runJavaScript(
            f"loadContent({json.dumps(html)}, {json.dumps(html)})")

    def get_share_payload(self, callback):
        self.view.page().runJavaScript("getSharePayload()", callback)

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

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (e.globalPosition().toPoint()
                              - self.window().frameGeometry().topLeft())

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() == Qt.MouseButton.LeftButton:
            self.window().move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

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
        }


# ── main window ──────────────────────────────────────────────────────────────

class ScratchPad(QWidget):
    """Main application window containing panes, terminal, and controls."""
    _ollama_chunk = pyqtSignal(int, str)
    _ollama_done  = pyqtSignal(int, str)

    def __init__(self):
        super().__init__()
        self.notes = self._load()
        self.config = self._load_config()
        self._bg_color = self.notes.get("window", {}).get("bg_color", "")
        self.pinned = True
        self._panes: list[QuillPane] = []
        self._active_pane_index = 0
        self._shortcuts = []
        self._term_height = 200

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._flush_save)

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
            "bg_color": self._bg_color,
            "active_page": self._active_pane().page_index,
        }
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        DATA_FILE.write_text(json.dumps(self.notes, indent=2, ensure_ascii=False))
        DATA_FILE.chmod(0o600)

    # ── window ───────────────────────────────────────────────────────────────

    def _build_window(self):
        ws = self.notes.get("window", {})
        self.setWindowTitle("Scratch")
        self.setObjectName("root")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(420, 260)
        self.setStyleSheet(STYLE)

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

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        topbar = DragHandle(self)
        topbar.setObjectName("topbar")
        topbar.setFixedHeight(40)
        top = QHBoxLayout(topbar)
        top.setContentsMargins(7, 0, 7, 0)
        top.setSpacing(4)

        def btn(label, size=30, obj_name="command", tip=None):
            b = QPushButton(label)
            b.setFixedSize(QSize(size, 28))
            if obj_name: b.setObjectName(obj_name)
            if tip:      b.setToolTip(tip)
            return b

        self.pin_btn = btn("📌", obj_name="pin-on", tip="Pin window  (Ctrl+P, double-click top bar)")
        self.pin_btn.clicked.connect(self._toggle_pin)

        self.page_label = QLabel("1 / 1")
        self.page_label.setObjectName("page-label")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.edit_btn = btn("✎", tip="Edit note  (Ctrl+E)")
        self.add_btn = btn("+", obj_name="add", tip="New page  (Ctrl+N)")
        self.ask_btn = btn("🤖", tip="Ask Ollama  (Ctrl+Shift+A)")
        self.term_btn = btn("⌨", tip="Toggle terminal  (Ctrl+T)")
        self.split_btn = btn("◫", tip="Split pane  (Ctrl+\\); close splits with Ctrl+Shift+\\")
        self.share_btn = btn("⇪", tip="Share selection or note  (Ctrl+Shift+S)")
        self.color_btn = btn("◐", tip="Background color  (Ctrl+B)")
        self.export_btn = btn("⇩", tip="Export page  (Ctrl+S)")
        self.config_btn = btn("⚙", tip="Configure sharing and Ollama  (Ctrl+,)")
        self.hide_btn = btn("–", tip="Hide to tray  (Ctrl+H)")
        self.quit_btn = btn("×", obj_name="danger", tip="Quit  (Ctrl+Q)")

        self.edit_btn.clicked.connect(self._toggle_edit_mode)
        self.add_btn.clicked.connect(self._new_page)
        self.ask_btn.clicked.connect(self._ask_ollama)
        self.term_btn.clicked.connect(self._toggle_terminal)
        self.split_btn.clicked.connect(self._toggle_split_panes)
        self.share_btn.clicked.connect(self._open_share_menu)
        self.color_btn.clicked.connect(self._pick_bg_color)
        self.export_btn.clicked.connect(self._export_page)
        self.config_btn.clicked.connect(self._open_config_panel)
        self.hide_btn.clicked.connect(self.hide)
        self.quit_btn.clicked.connect(self._quit)

        def add_group(*widgets):
            if top.count():
                top.addSpacing(8)
            for widget in widgets:
                top.addWidget(widget)

        add_group(self.pin_btn)
        add_group(self.edit_btn, self.add_btn)
        add_group(self.ask_btn, self.term_btn, self.split_btn)
        add_group(self.share_btn, self.color_btn, self.export_btn, self.config_btn)
        top.addStretch()
        add_group(self.hide_btn, self.quit_btn)

        self.h_split = QSplitter(Qt.Orientation.Horizontal, self)
        self.h_split.setChildrenCollapsible(False)

        navbar = QFrame(self)
        navbar.setObjectName("navbar")
        navbar.setFixedHeight(34)
        nav = QHBoxLayout(navbar)
        nav.setContentsMargins(7, 0, 7, 0)
        nav.setSpacing(4)

        self.prev_btn = btn("◀", obj_name="nav", tip="Previous page  (Ctrl+Left, Ctrl+wheel up)")
        self.prev_btn.clicked.connect(self._prev_page)

        self.page_title = QLabel("")
        self.page_title.setObjectName("page-title")
        self.page_title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.next_btn = btn("▶", obj_name="nav", tip="Next page  (Ctrl+Right, Ctrl+wheel down)")
        self.next_btn.clicked.connect(self._next_page)

        self.del_btn = btn("✕", obj_name="del", tip="Delete page  (Ctrl+W)")
        self.del_btn.clicked.connect(self._delete_page)

        grip = ResizeGrip(self)

        nav.addWidget(self.prev_btn)
        nav.addWidget(self.page_label)
        nav.addWidget(self.page_title, 1)
        nav.addWidget(self.next_btn)
        nav.addWidget(self.del_btn)
        nav.addWidget(grip)

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
        self.page_title.setText(page_title_from_html(self.notes["pages"][idx]))
        self._sync_command_states()

    def _set_button_object_name(self, button, name):
        if button.objectName() == name:
            return
        button.setObjectName(name)
        button.style().unpolish(button)
        button.style().polish(button)

    def _sync_command_states(self):
        if hasattr(self, "edit_btn"):
            self._set_button_object_name(
                self.edit_btn,
                "mode-on" if self._active_pane()._edit_mode else "command",
            )
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
            pane.load_page(pane.page_index - 1)
            self._flush_save()
            self._update_nav()

    def _next_page(self):
        pane = self._active_pane()
        if pane.page_index < len(self.notes["pages"]) - 1:
            pane.load_page(pane.page_index + 1)
            self._flush_save()
            self._update_nav()

    def _new_page(self):
        self._flush_save()
        idx = self._active_pane().page_index
        self.notes["pages"].insert(idx + 1, "")
        self._active_pane().load_page(idx + 1)
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
        self.pinned = not self.pinned
        if self.pinned:
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint |
                Qt.WindowType.WindowStaysOnTopHint |
                Qt.WindowType.Tool)
            self.pin_btn.setObjectName("pin-on")
        else:
            # Drop Tool too — on KDE, Tool windows stay above others even
            # without WindowStaysOnTopHint.
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
            self.pin_btn.setObjectName("pin-off")
        self.pin_btn.style().unpolish(self.pin_btn)
        self.pin_btn.style().polish(self.pin_btn)
        self.show()
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

        self._new_page()
        response_idx = self._active_pane().page_index
        self.notes["pages"][response_idx] = ""
        self._active_pane().load_page(response_idx)
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

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background:#16213e; color:#e6edf3; border:1px solid #2d2d4e; }"
            "QMenu::item { padding:6px 24px; }"
            "QMenu::item:selected { background:#2d2d4e; }"
        )

        def add_action(label, shortcut=None):
            action = menu.addAction(label)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            return action

        edit_act = add_action("Edit preview", "Ctrl+E")
        new_act = add_action("New page", "Ctrl+N")
        prev_act = add_action("Previous page", "Ctrl+Left")
        next_act = add_action("Next page", "Ctrl+Right")
        delete_act = add_action("Delete page", "Ctrl+W")
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
        config_act = add_action("Configure...", "Ctrl+,")
        menu.addSeparator()
        color_act = add_action("Background color...", "Ctrl+B")
        export_act = add_action("Export page...", "Ctrl+S")
        pin_act = add_action("Toggle pin", "Ctrl+P")
        menu.addSeparator()
        hide_act = add_action("Hide to tray", "Ctrl+H")
        quit_act = add_action("Quit", "Ctrl+Q")

        action = menu.exec(event.globalPos())
        if action == edit_act:
            self._toggle_edit_mode()
        elif action == new_act:
            self._new_page()
        elif action == prev_act:
            self._prev_page()
        elif action == next_act:
            self._next_page()
        elif action == delete_act:
            self._delete_page()
        elif action == ask_act:
            self._ask_ollama()
        elif action == term_act:
            self._toggle_terminal()
        elif action == split_act:
            self._toggle_split_panes()
        elif action == config_act:
            self._open_config_panel()
        elif action == color_act:
            self._pick_bg_color()
        elif action == export_act:
            self._export_page()
        elif action == pin_act:
            self._toggle_pin()
        elif action == hide_act:
            self.hide()
        elif action == quit_act:
            self._quit()

    def _apply_bg_color(self, pane):
        if self._bg_color and pane._editor_ready:
            pane.view.page().runJavaScript(
                f"setBgColor({json.dumps(self._bg_color)})")

    def _pick_bg_color(self):
        initial = QColor(self._bg_color) if self._bg_color else QColor("#f8f6f1")
        color = QColorDialog.getColor(initial, self, "Background color")
        if color.isValid():
            self._set_bg_color(color.name())

    def _set_bg_color(self, hex_color):
        self._bg_color = hex_color
        for pane in self._panes:
            if pane._editor_ready:
                pane.view.page().runJavaScript(
                    f"setBgColor({json.dumps(hex_color)})")
        self.schedule_save()

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

    def _open_share_menu(self):
        menu = self._build_share_menu()
        anchor = getattr(self, "share_btn", self)
        menu.exec(anchor.mapToGlobal(QPoint(0, anchor.height())))

    def _build_share_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background:#16213e; color:#e6edf3; border:1px solid #2d2d4e; }"
            "QMenu::item { padding:6px 24px; }"
            "QMenu::item:selected { background:#2d2d4e; }"
        )
        copy_act = menu.addAction("Copy share text")
        copy_act.setShortcut(QKeySequence("Ctrl+Shift+C"))
        copy_act.triggered.connect(lambda: self._share_current("clipboard", {}))
        ai_clipboard_act = menu.addAction("Copy for AI")
        ai_clipboard_act.triggered.connect(lambda: self._share_current("ai_clipboard", {}))

        telegram_menu = menu.addMenu("Telegram")
        telegram = self.config.get("telegram", {})
        default_chat = telegram.get("default_chat_id", "")
        default_act = telegram_menu.addAction("Default chat" if default_chat else "Configure Telegram...")
        default_act.triggered.connect(
            (lambda: self._share_current("telegram", {"chat_id": default_chat}))
            if default_chat
            else self._open_config_panel
        )
        for chat in telegram.get("recent_chats", []):
            label = chat.get("title") or chat.get("username") or str(chat.get("id", ""))
            chat_id = str(chat.get("id", ""))
            if not chat_id:
                continue
            action = telegram_menu.addAction(label)
            action.triggered.connect(
                lambda checked=False, cid=chat_id: self._share_current("telegram", {"chat_id": cid})
            )
        telegram_menu.addSeparator()
        telegram_menu.addAction("Configure Telegram...").triggered.connect(self._open_config_panel)

        targets = self.config.get("share_targets", [])
        if targets:
            menu.addSeparator()
            for target in targets:
                if not isinstance(target, dict):
                    continue
                name = target.get("name") or target.get("kind") or "Share target"
                action = menu.addAction(str(name))
                action.triggered.connect(
                    lambda checked=False, t=target: self._share_current(str(t.get("kind", "")), t)
                )

        menu.addSeparator()
        menu.addAction("Configure sharing...").triggered.connect(self._open_config_panel)
        return menu

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
        self._flush_save()
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
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
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
        self._flush_save()
        super().hide()

    def moveEvent(self, event):
        self.schedule_save()
        super().moveEvent(event)

    def resizeEvent(self, event):
        self._position_resize_handles()
        self.schedule_save()
        super().resizeEvent(event)

    def closeEvent(self, event):
        self._flush_save()
        self.global_pty.stop()
        event.ignore()
        self.hide()

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
