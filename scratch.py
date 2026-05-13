#!/usr/bin/env python3
"""scratch — always-on-top sticky pad: Quill editor, split panes, global terminal, Ollama."""

import fcntl
import json
import logging
import os
import pty
import socket as _socket
import struct
import subprocess
import sys
import termios
import threading
import urllib.request
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, QPoint, QSize, QTimer, QUrl, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QIcon, QKeySequence, QShortcut
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (
    QApplication, QColorDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QMenu, QPushButton, QSplitter, QSystemTrayIcon,
    QVBoxLayout, QWidget, QInputDialog,
)

from scratch_core import (
    SHORTCUTS,
    Rect,
    create_shortcuts,
    ollama_generate_payload,
    ollama_stream_chunks,
    page_title_from_html,
    plain_text_from_html,
    resize_rect,
)

logger = logging.getLogger(__name__)

DATA_FILE    = Path.home() / ".scratch-notes" / "notes.json"
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
    font-size: 14px; padding: 4px 8px; border-radius: 4px;
}
QPushButton:hover { background: #2d2d4e; color: #e6edf3; }
QPushButton#pin-on  { color: #ffd700; }
QPushButton#pin-off { color: #4a4a6a; }
QPushButton#add     { color: #7ec8a4; font-size: 16px; font-weight: bold; }
QPushButton#del     { color: #e06c75; }
QPushButton#nav     { color: #6272a4; font-size: 16px; }
QPushButton#nav:hover    { color: #e6edf3; }
QPushButton#nav:disabled { color: #2d2d4e; }
QLabel#page-label { color: #4a4a6a; font-size: 11px; }
QLabel#page-title { color: #6272a4; font-size: 10px; font-style: italic; }
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
        # Called after JS editor.focus() — only update Qt's focus pointer.
        # Do NOT call activateWindow()/raise_() here: they send a second X11
        # FocusIn that arrives after editor.focus() and resets Chromium's
        # internal focus away from the textarea.
        self._pane._edit_mode = True
        self._pane.view.setFocus(Qt.FocusReason.OtherFocusReason)


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


class ResizeGrip(QFrame):
    """Bottom-right resize handle widget."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_pos = None
        self._start_geom = None
        self.setFixedSize(QSize(18, 18))
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setToolTip("Resize window")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint()
            self._start_geom = self.window().geometry()

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
            "bottom-right",
            minimum_size=(280, 220),
        )
        self.window().setGeometry(geom.x, geom.y, geom.width, geom.height)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None
        self._start_geom = None


# ── main window ──────────────────────────────────────────────────────────────

class ScratchPad(QWidget):
    """Main application window containing panes, terminal, and controls."""
    _ollama_chunk = pyqtSignal(int, str)
    _ollama_done  = pyqtSignal(int, str)

    def __init__(self):
        super().__init__()
        self.notes = self._load()
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

    def schedule_save(self):
        self._save_timer.start(400)

    def _flush_save(self):
        self.notes["window"] = {
            "x": self.x(),
            "y": self.y(),
            "width": self.width(),
            "height": self.height(),
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
        self.setMinimumSize(280, 220)
        self.setStyleSheet(STYLE)

        w = ws.get("width", 320)
        h = ws.get("height", 440)

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
        topbar.setFixedHeight(36)
        top = QHBoxLayout(topbar)
        top.setContentsMargins(6, 0, 6, 0)
        top.setSpacing(2)

        def btn(label, size=28, obj_name=None, tip=None):
            b = QPushButton(label)
            b.setFixedSize(QSize(size, 28))
            if obj_name: b.setObjectName(obj_name)
            if tip:      b.setToolTip(tip)
            return b

        self.pin_btn = btn("📌", obj_name="pin-on", tip="Toggle always-on-top")
        self.pin_btn.clicked.connect(self._toggle_pin)

        self.page_label = QLabel("1 / 1")
        self.page_label.setObjectName("page-label")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        ask_btn         = btn("🧠", tip="Ask Ollama  (Ctrl+Shift+A)")
        term_btn        = btn("⌨",  tip="Toggle terminal  (Ctrl+T)")
        edit_btn        = btn("✎",  tip="Toggle edit mode  (Ctrl+E)")
        split_btn       = btn("⊟",  tip="Split pane  (Ctrl+\\)")
        close_split_btn = btn("⊞",  tip="Close split pane")
        add_btn         = btn("+",  obj_name="add", tip="New page  (Ctrl+N)")
        del_btn         = btn("✕",  obj_name="del", tip="Delete page  (Ctrl+W)")
        hide_btn        = btn("–",  obj_name="del", tip="Hide to tray  (Ctrl+H)")
        quit_btn        = btn("✕",  obj_name="del", tip="Quit  (Ctrl+Q)")

        ask_btn.clicked.connect(self._ask_ollama)
        term_btn.clicked.connect(self._toggle_terminal)
        edit_btn.clicked.connect(self._toggle_edit_mode)
        split_btn.clicked.connect(self._split_pane)
        close_split_btn.clicked.connect(self._close_extra_panes)
        add_btn.clicked.connect(self._new_page)
        del_btn.clicked.connect(self._delete_page)
        hide_btn.clicked.connect(self.hide)
        quit_btn.clicked.connect(self._quit)

        for w in (self.pin_btn, self.page_label):
            top.addWidget(w)
        top.addStretch()
        for w in (ask_btn, term_btn, edit_btn, split_btn, close_split_btn):
            top.addWidget(w)
        top.addSpacing(4)
        for w in (add_btn, del_btn):
            top.addWidget(w)
        top.addSpacing(8)
        top.addWidget(hide_btn)
        top.addWidget(quit_btn)

        self.h_split = QSplitter(Qt.Orientation.Horizontal, self)
        self.h_split.setChildrenCollapsible(False)

        navbar = QFrame(self)
        navbar.setObjectName("navbar")
        navbar.setFixedHeight(32)
        nav = QHBoxLayout(navbar)
        nav.setContentsMargins(6, 0, 6, 0)

        self.prev_btn = btn("◀", obj_name="nav")
        self.prev_btn.setFixedSize(QSize(28, 24))
        self.prev_btn.clicked.connect(self._prev_page)

        self.page_title = QLabel("")
        self.page_title.setObjectName("page-title")
        self.page_title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.next_btn = btn("▶", obj_name="nav")
        self.next_btn.setFixedSize(QSize(28, 24))
        self.next_btn.clicked.connect(self._next_page)

        grip = ResizeGrip(self)

        nav.addWidget(self.prev_btn)
        nav.addWidget(self.page_title, 1)
        nav.addWidget(self.next_btn)
        nav.addWidget(grip)

        root.addWidget(topbar)
        root.addWidget(self.h_split, 1)
        root.addWidget(navbar)

        self._shortcuts = create_shortcuts(
            self,
            SHORTCUTS,
            shortcut_cls=QShortcut,
            keyseq_cls=QKeySequence,
        )

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

    def _on_global_pty_exit(self):
        self.global_pty.stop()
        self.global_term.hide()

    def _toggle_edit_mode(self):
        self._active_pane().toggle_edit_mode()

    def _toggle_pin(self):
        self.pinned = not self.pinned
        if self.pinned:
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint |
                Qt.WindowType.WindowStaysOnTopHint |
                Qt.WindowType.Tool)
            self.pin_btn.setStyleSheet("color: #ffd700;")
        else:
            # Drop Tool too — on KDE, Tool windows stay above others even
            # without WindowStaysOnTopHint.
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
            self.pin_btn.setStyleSheet("color: #4a4a6a;")
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

        full_prompt = f"Context:\n{text_content}\n\nUser request:\n{prompt}"
        payload = json.dumps(ollama_generate_payload(full_prompt)).encode()

        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
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
                    f"setPreviewHtml({json.dumps('<pre>' + text + '</pre>')})")
        self._update_nav()

    def _on_ollama_done(self, page_index, full_text):
        html = f"<pre>{full_text}</pre>"
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
            "QMenu::item { padding:4px 18px; }"
            "QMenu::item:selected { background:#2d2d4e; }"
        )
        export_act = menu.addAction("Export page…")
        color_act  = menu.addAction("Background color…")
        action = menu.exec(event.globalPos())
        if action == export_act:
            self._export_page()
        elif action == color_act:
            self._pick_bg_color()

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
        _, _, w, h = getattr(self, '_init_geometry', (0, 0, 320, 440))
        return QSize(w, h)

    def showEvent(self, event):
        super().showEvent(event)
        x, y, w, h = self._init_geometry
        self.setGeometry(x, y, w, h)
        QTimer.singleShot(50, self.activateWindow)

    def hide(self):
        self._flush_save()
        super().hide()

    def moveEvent(self, event):
        self.schedule_save()
        super().moveEvent(event)

    def resizeEvent(self, event):
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
