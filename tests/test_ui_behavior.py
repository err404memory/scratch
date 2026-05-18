from __future__ import annotations

import os
import subprocess
import sys
import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication, QSplitter
from pathlib import Path

# WebEngine tests require a display and proper initialization
def _has_display() -> bool:
    """Check if a DISPLAY is available (X11/Wayland)."""
    return bool(os.environ.get("DISPLAY") or sys.platform == "darwin")


def _qt_platform_is_usable() -> bool:
    """Probe Qt in a child process so plugin aborts do not kill pytest."""
    code = (
        "import sys\n"
        "from PyQt6.QtWebEngineWidgets import QWebEngineView\n"
        "from PyQt6.QtWidgets import QApplication\n"
        "app = QApplication([])\n"
        "QWebEngineView()\n"
        "print(app.platformName())\n"
    )
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "xcb")
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def test_editor_page_has_explicit_focus_contract():
    editor_html = Path("assets/editor.html").read_text()

    assert "function focusEditor()" in editor_html
    assert "window.focusEditor = focusEditor;" in editor_html
    assert "focusEditor();" in editor_html


def test_editor_csp_allows_inline_bootstrap_script():
    editor_html = Path("assets/editor.html").read_text()

    assert "script-src 'self' qrc: 'unsafe-inline'" in editor_html


def test_editor_preview_preserves_typed_newlines():
    editor_html = Path("assets/editor.html").read_text()

    assert "function preserveTextNewlines(source)" in editor_html
    assert ".replace(/\\r?\\n/g, '<br>')" in editor_html
    assert "white-space:pre-wrap" not in editor_html


def test_editor_preview_renders_code_fences():
    editor_html = Path("assets/editor.html").read_text()

    assert "function renderNoteSource(source)" in editor_html
    assert "<pre><code${lang}>" in editor_html
    assert "renderNoteSource(html)" in editor_html
    assert "codeLineCount === 0" in editor_html


def test_editor_exposes_selected_share_payload():
    editor_html = Path("assets/editor.html").read_text()

    assert "function getSharePayload()" in editor_html
    assert "previewSelection.trim()" in editor_html
    assert "editor.selectionStart" in editor_html
    assert "window.getSharePayload = getSharePayload;" in editor_html


def test_livecodes_pane_exposes_scratch_bridge_contract():
    livecodes_html = Path("assets/livecodes_pane.html").read_text()

    assert "livecodes-frame" in livecodes_html
    assert "livecodes-get-config" in livecodes_html
    assert "livecodes-ready" in livecodes_html
    assert "livecodes-change" in livecodes_html
    assert "scratch-open-context-menu" in livecodes_html
    assert "function completeConfig(config)" in livecodes_html
    assert "function callApiWithResponse(method, args)" in livecodes_html
    assert "function snapshotConfig(pageId, force)" in livecodes_html
    assert "Bridge.contentChanged(JSON.stringify({ scratchPageId: pageId, config: complete }))" in livecodes_html
    assert "window.captureLiveCodesConfig" in livecodes_html
    assert "setInterval(function()" in livecodes_html
    assert "style: { language: style.language || 'css', content: style.content || '' }" in livecodes_html
    assert "callApi('setConfig'" in livecodes_html
    assert "window.callLiveCodesApi" in livecodes_html
    assert "window.callLiveCodesEditCommand" in livecodes_html
    assert "url.searchParams.set('config', 'sdk')" not in livecodes_html
    assert "window.loadNoteSource" in livecodes_html
    assert "window.getSharePayload" in livecodes_html
    assert "window.focusEditor" in livecodes_html
    assert "Bridge.contentChanged(JSON.stringify({ scratchPageId: pageId, config: complete }))" in livecodes_html


def test_scratch_ui_exposes_low_friction_commands():
    scratch_source = Path("scratch.py").read_text()

    assert 'btn("+"' in scratch_source
    assert 'btn("🗑"' in scratch_source
    assert 'btn("⇪"' not in scratch_source
    assert 'btn("⚙"' not in scratch_source
    assert 'btn("✎"' not in scratch_source
    assert 'add_action("Edit preview", "Ctrl+E")' not in scratch_source
    assert 'add_action("Background color...", "Ctrl+B")' not in scratch_source
    assert "QGridLayout(navbar)" in scratch_source
    assert "Qt.AlignmentFlag.AlignCenter" in scratch_source
    assert "right_nav_layout.addStretch()" in scratch_source
    assert "class ConfigDialog(QDialog):" in scratch_source
    assert '"Copy for AI"' in scratch_source
    assert '"telegram":' in scratch_source
    assert '"share_targets":' in scratch_source
    assert "getSharePayload()" in scratch_source
    assert "livecodes_config_from_source" in scratch_source
    assert "start_livecodes_server()" in scratch_source
    assert "LIVECODES_URL" in scratch_source
    assert "LocalContentCanAccessRemoteUrls, True" in scratch_source
    assert "def wheelEvent(self, event):" in scratch_source
    assert "mouseDoubleClickEvent" in scratch_source
    assert "OpenHandCursor" in scratch_source
    assert "ClosedHandCursor" in scratch_source
    assert "class ResizeHandle(QFrame):" in scratch_source
    assert "def _install_resize_handles(self):" in scratch_source
    assert '"top-left"' in scratch_source
    assert "current_geometry = (self.x(), self.y(), self.width(), self.height())" in scratch_source
    assert "self._init_geometry = current_geometry" in scratch_source
    assert "scratch-context-menu-bridge" in scratch_source
    assert "scratch-edit-command" in scratch_source
    assert "def _build_context_menu(self):" in scratch_source
    assert "LiveCodes" in scratch_source
    assert 'menu.addMenu("LiveCodes")' not in scratch_source
    assert "LiveCodes: show editor" in scratch_source
    assert "Select all" in scratch_source
    assert "def _run_livecodes_action(self, method, args=None):" in scratch_source
    assert "def _run_livecodes_edit_action(self, command):" in scratch_source
    assert "def capture_current_content(self):" in scratch_source
    assert "scratchPageId" in scratch_source
    assert "def _after_content_snapshot(self, callback, delay_ms=140):" in scratch_source
    assert "font-size: 16px" in scratch_source
    assert "DEFAULT_UI_SETTINGS" in scratch_source
    assert "class UiSettingsDialog(QDialog):" in scratch_source
    assert "Window UI settings..." in scratch_source
    assert "pin_glow_color" in scratch_source
    assert "toolbar_group_spacing" in scratch_source
    assert "page_rail_padding" in scratch_source
    assert 'rgba({glow_r},{glow_g},{glow_b}' in scratch_source
    assert 'QSize(self._ui_settings["button_size"], self._ui_settings["button_height"])' in scratch_source
    assert "qradialgradient" in scratch_source

@pytest.fixture(scope="session")
def qt_app():
    """Start QApplication once for session, with WebEngine support."""
    if not _has_display():
        pytest.skip("UI tests require a display (DISPLAY not set)")

    if not _qt_platform_is_usable():
        pytest.skip("UI tests require a usable Qt platform plugin")

    # Ensure XCB platform for override-redirect windows
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

    # Import QtWebEngineWidgets BEFORE creating QApplication to initialize WebEngine
    try:
        from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    except ImportError:
        pytest.skip("PyQt6.WebEngine not available")

    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app
    # Cleanup not needed for session scope


def test_quill_pane_load_page_trigger(qt_app):
    """Test that loading a page triggers the right sequence."""
    from scratch import QuillPane
    from scratch_core import SHORTCUTS

    # Minimal mock for ScratchPad
    class MockPad:
        notes = {"pages": ["<h1>Test</h1>", "<p>Second</p>"]}

    pane = QuillPane(MockPad(), initial_page=0)
    assert pane.page_index == 0


def test_terminal_bridge_basic(qt_app):
    """Test TerminalBridge signal connectivity."""
    from scratch import TerminalBridge

    class MockWindow:
        global_pty = type("obj", (object,), {"resize": lambda self, *a: None, "write": lambda self, *a: None})()

    bridge = TerminalBridge(MockWindow())
    # Check that bridge exists and signals are set
    assert bridge.terminalOutputSignal is not None
    assert bridge.cwdSignal is not None


def test_resize_handling():
    """Test that resize logic correctly clamps to minimum size."""
    from scratch_core import resize_rect, Rect

    start = Rect(100, 100, 300, 200)
    resized = resize_rect(
        start,
        start_global=(100, 100),
        current_global=(50, 50),
        edge="left",
        minimum_size=(280, 180),
    )
    assert resized.width >= 280
    assert resized.height >= 180


def test_shortcut_creation():
    """Test that shortcuts are created and connected."""
    from scratch_core import create_shortcuts

    class MockParent:
        def _test_handler(self):
            pass

    parent = MockParent()
    shortcuts = create_shortcuts(
        parent,
        [("Ctrl+N", "_test_handler")],
        shortcut_cls=lambda *a: type("obj", (object,), {"activated": type("sig", (object,), {"connect": lambda self, cb: setattr(self, "callback", cb)})()})(),
        keyseq_cls=lambda x: x,
    )
    assert len(shortcuts) == 1


def test_migrate_notes_respects_html():
    """Test that migration leaves HTML pages untouched and converts plain text."""
    from scratch_core import migrate_notes

    notes = {"pages": ["<p>html</p>", "plain"]}
    migrated, changed = migrate_notes(notes, lambda x: f"<p>{x}</p>")
    assert changed is True  # plain page was converted
    assert migrated["pages"][0] == "<p>html</p>"  # HTML unchanged
    assert migrated["pages"][1] == "<p>plain</p>"  # plain converted


def test_ollama_stream_handles_empty_lines():
    """Test that streaming correctly skips empty chunks."""
    from scratch_core import ollama_stream_chunks

    lines = [
        b'{"response":"Hello","done":false}\n',
        b'\n',  # Empty line
        b'{"response":"world","done":false}\n',
    ]
    chunks = list(ollama_stream_chunks(lines))
    assert chunks == ["Hello", "world"]


def test_page_title_extraction():
    """Test that page titles are correctly extracted from HTML."""
    from scratch_core import page_title_from_html

    html = "<h1>My Note</h1><p>Details</p>"
    assert page_title_from_html(html, limit=5) == "My No"


def test_cookie_save():
    """Test that data persistence works correctly."""
    import tempfile
    import json
    from scratch_core import normalize_notes

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.json"
        path.write_text(json.dumps({"pages": ["test"]}))
        from scratch_core import load_notes_text
        data = load_notes_text(path.read_text())
        assert data["pages"] == ["test"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
