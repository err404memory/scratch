from __future__ import annotations

import os
import sys
import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication, QSplitter
from pathlib import Path

# WebEngine tests require a display and proper initialization
def _has_display() -> bool:
    """Check if a DISPLAY is available (X11/Wayland)."""
    return bool(os.environ.get("DISPLAY") or sys.platform == "darwin")

@pytest.fixture(scope="session")
def qt_app():
    """Start QApplication once for session, with WebEngine support."""
    if not _has_display():
        pytest.skip("UI tests require a display (DISPLAY not set)")

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
