from __future__ import annotations

import html
import http.server
import json
import logging
import os
import re
import socket as _socket
import socketserver
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)


DEFAULT_NOTES: dict[str, list[str]] = {"pages": [""]}
LIVECODES_BUILD_DIR = Path.home() / "storage/workshop/apps/livecodes/build"
LIVECODES_PORT = 4173
SHORTCUTS: list[tuple[str, str]] = [
    ("Ctrl+N", "_new_page"),
    ("Ctrl+Left", "_prev_page"),
    ("Ctrl+Right", "_next_page"),
    ("Ctrl+W", "_delete_page"),
    ("Ctrl+T", "_toggle_terminal"),
    ("Ctrl+E", "_toggle_edit_mode"),
    ("Ctrl+\\", "_split_pane"),
    ("Ctrl+Shift+\\", "_close_extra_panes"),
    ("Ctrl+S", "_export_page"),
    ("Ctrl+B", "_pick_bg_color"),
    ("Ctrl+P", "_toggle_pin"),
    ("Ctrl+Shift+S", "_open_share_menu"),
    ("Ctrl+,", "_open_config_panel"),
    ("Ctrl+Shift+A", "_ask_ollama"),
    ("Ctrl+H", "hide"),
    ("Ctrl+Q", "_quit"),
]


@dataclass(frozen=True, slots=True)
class Rect:
    """Immutable rectangle with integer coordinates and dimensions."""
    x: int
    y: int
    width: int
    height: int


def normalize_notes(data: Any) -> dict[str, Any]:
    """Ensure notes are in the current format: {'pages': [str, ...], 'window': {...}}."""
    if not isinstance(data, dict):
        return {"pages": [""], "window": {}}

    raw_pages = data.get("pages")
    if not isinstance(raw_pages, list):
        raw_pages = [""]

    pages: list[str] = []
    for page in raw_pages:
        if isinstance(page, dict):
            # Migrate from LiveCodes v2 format (dict) to v3 (HTML string)
            markup = page.get("markup", {})
            content = markup.get("content", "") if isinstance(markup, dict) else ""
            pages.append(content)
        elif isinstance(page, str):
            pages.append(page)
        else:
            pages.append("")

    return {
        "pages": pages or [""],
        "window": data.get("window", {}) if isinstance(data.get("window"), dict) else {},
    }


def load_notes_text(raw_text: str | None) -> dict[str, Any]:
    """
    Parse raw JSON text of notes into a normalized dict.

    Args:
        raw_text: JSON string containing notes data, or None/empty for defaults.

    Returns:
        Normalized notes dict with 'pages' (list of HTML strings) and
        'window' (dict of window state). Invalid JSON falls back to defaults.
    """
    if not raw_text:
        return {"pages": [""], "window": {}}
    try:
        data = json.loads(raw_text)
    except Exception as e:
        logger.warning("Failed to parse notes JSON: %s", e)
        return {"pages": [""], "window": {}}
    return normalize_notes(data)


def migrate_notes(
    notes: dict[str, list[str]],
    converter: Callable[[str], str],
) -> tuple[dict[str, list[str]], bool]:
    migrated = {"pages": list(notes.get("pages", [""]))}
    changed = False

    for index, page in enumerate(migrated["pages"]):
        if page and not page.lstrip().startswith("<"):
            converted = converter(page)
            if converted != page:
                migrated["pages"][index] = converted
                changed = True

    return migrated, changed


def page_title_from_html(html: str, limit: int = 40) -> str:
    """
    Extract plain text title from HTML by stripping tags and truncating.

    Args:
        html: HTML content string.
        limit: Maximum number of characters to return. Default 40.

    Returns:
        Plain text title, truncated to limit characters.
    """
    title = re.sub(r"<[^>]+>", "", html).strip()
    return title[:limit]


def plain_text_from_html(html: str) -> str:
    """Strip all HTML tags from an HTML string, returning plain text."""
    return re.sub(r"<[^>]+>", "", html)


def preformatted_html(text: str) -> str:
    """Wrap generated plain text in a pre block without treating it as markup."""
    return f"<pre>{html.escape(text)}</pre>"


def preserve_text_newlines(source: str) -> str:
    """Preserve text newlines without making whitespace between HTML tags visible."""
    parts = re.split(r"(<[^>]+>)", source)
    rendered: list[str] = []
    for part in parts:
        if re.fullmatch(r"<[^>]+>", part or ""):
            rendered.append(part)
        elif "\n" not in part and "\r" not in part:
            rendered.append(part)
        elif part.strip():
            rendered.append(part.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>"))
    return "".join(rendered)


def wrap_note_body(body_html: str) -> str:
    """Wrap preview body HTML with the same styling used by the live editor."""
    bg = "linear-gradient(160deg,#f8f6f1 0%,#edeae0 100%)"
    return (
        '<!DOCTYPE html><html><head><meta charset="UTF-8"><style>'
        f'body{{background:{bg};color:#1c2430;'
        'font-family:Inter,system-ui,sans-serif;font-size:13px;line-height:1.6;'
        'margin:0;padding:18px 20px;min-height:100vh;}'
        'h1,h2,h3{color:#16324f;margin:.2em 0 .5em;line-height:1.2;}'
        'h1{font-size:1.45rem;}h2{font-size:1.2rem;}h3{font-size:1rem;}'
        'p,ul,ol,pre,blockquote,details{margin:.7em 0;}'
        'a{color:#5c7cfa;}'
        'code{background:rgba(22,50,79,.09);color:#0f3b63;padding:1px 5px;border-radius:4px;'
        'font-family:ui-monospace,monospace;}'
        'pre{background:#15202b;color:#d7e0ea;padding:14px;border-radius:10px;overflow:auto;}'
        'pre code{background:transparent;color:inherit;padding:0;}'
        'blockquote{border-left:4px solid #9aacb8;padding-left:12px;color:#4e5968;}'
        'table{border-collapse:collapse;}td,th{border:1px solid rgba(35,44,58,.15);padding:4px 8px;}'
        'img{max-width:100%;}'
        'details{border:1px solid rgba(35,44,58,.15);border-radius:8px;padding:8px 12px;}'
        'summary{cursor:pointer;font-weight:600;color:#16324f;}'
        '::-webkit-scrollbar{width:0;height:0;}'
        '</style></head><body>'
        f'{body_html}'
        '</body></html>'
    )


def render_note_source(source: str) -> str:
    """
    Convert raw note content (markdown or HTML) to a complete HTML document.

    If the source already appears to be a full HTML document (starts with
    <!DOCTYPE or <html), it is returned as-is. Otherwise, the source is
    treated as markdown and converted to HTML with sensible defaults, then
    wrapped in a styled HTML document.

    Args:
        source: Raw note content — either plain text/markdown or HTML.

    Returns:
        A complete HTML document ready for rendering in an iframe.
    """
    source = source.strip()
    if not source:
        return '<p style="color:#9aa0ab;font-style:italic;">Start writing…</p>'

    # Check if already a full HTML document
    if source.lower().startswith(("<!doctype html", "<!doctype html", "<html")):
        return source

    if source.lstrip().startswith("<"):
        return wrap_note_body(preserve_text_newlines(source))

    # Basic markdown → HTML conversion (covers test cases)
    lines = source.splitlines()
    in_code_block = False
    code_lang = ""
    code_line_count = 0
    rendered_lines: list[str] = []

    for line in lines:
        # Code fence handling (``` or ~~~)
        if line.strip().startswith("```") or line.strip().startswith("~~~"):
            if in_code_block:
                rendered_lines.append("</code></pre>")
                in_code_block = False
                code_lang = ""
                code_line_count = 0
            else:
                fence = line.strip()[:3]
                lang = line.strip()[3:].strip()
                code_lang = lang or ""
                rendered_lines.append(
                    f'<pre><code class="language-{html.escape(code_lang, quote=True)}">'
                    if code_lang
                    else "<pre><code>"
                )
                in_code_block = True
                code_line_count = 0
            continue

        if in_code_block:
            # Attach the first code line to the opening tag so <pre> does not
            # render an artificial leading blank line.
            if code_line_count == 0:
                rendered_lines[-1] += html.escape(line)
            else:
                rendered_lines.append(html.escape(line))
            code_line_count += 1
            continue

        # ATX headings (1-6 #)
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            level = len(heading_match.group(1))
            text = heading_match.group(2).strip()
            rendered_lines.append(f"<h{level}>{text}</h{level}>")
            continue

        # Plain line — treat as paragraph if non-empty
        if line.strip():
            # Escape HTML special chars but preserve existing HTML tags (e.g., <details>)
            # We'll preserve any line that contains an HTML tag; escape others
            if re.search(r"<[^>]+>", line):
                rendered_lines.append(line)
            else:
                rendered_lines.append(f"<p>{html.escape(line)}</p>")
        else:
            rendered_lines.append("")  # blank line

    if in_code_block:
        rendered_lines.append("</code></pre>")

    # Join and wrap with default styling (mirrors editor.html buildWrapper)
    body_html = "\n".join(rendered_lines)
    return wrap_note_body(body_html)


def page_title_from_config(config: dict[str, Any], limit: int = 40) -> str:
    """Extract a readable title from a LiveCodes config object."""
    markup = config.get("markup", {}) if isinstance(config, dict) else {}
    content = markup.get("content", "") if isinstance(markup, dict) else ""
    if not content:
        return ""
    # Strip HTML tags
    title = re.sub(r"<[^>]+>", "", content)
    # Take first non-empty line
    for line in title.splitlines():
        line = line.strip()
        if line:
            title = line
            break
    else:
        title = ""
    # Strip markdown heading syntax
    title = re.sub(r"^#{1,6}\s*", "", title)
    return title[:limit]


def plain_text_from_config(config: dict[str, Any]) -> str:
    """
    Extract plain text content from a LiveCodes configuration object.

    Args:
        config: LiveCodes config dict with 'markup' containing 'content'.

    Returns:
        Plain text (HTML tags stripped) from the content field.
    """
    markup = config.get("markup", {}) if isinstance(config, dict) else {}
    content = markup.get("content", "") if isinstance(markup, dict) else ""
    return re.sub(r"<[^>]+>", "", content)


def default_livecodes_config(language: str = "markdown", content: str = "") -> dict[str, Any]:
    """
    Create a default LiveCodes configuration for a given language and content.

    Args:
        language: Markup language identifier (e.g., "markdown", "html").
        content: Initial content string.

    Returns:
        LiveCodes config dict in v2 format.
    """
    return {
        "markup": {
            "language": language,
            "content": content,
        }
    }


def migrate_v1_to_v2(notes: dict[str, Any]) -> dict[str, Any]:
    """Migrate notes.json from v1 (list of HTML strings) to v2 (list of LiveCodes configs)."""
    pages = notes.get("pages", [""])
    migrated_pages: list[dict[str, Any]] = []
    for page in pages:
        if isinstance(page, dict):
            migrated_pages.append(page)
        elif isinstance(page, str):
            if page.lstrip().startswith("<"):
                migrated_pages.append(default_livecodes_config("html", page))
            else:
                migrated_pages.append(default_livecodes_config("markdown", page))
        else:
            migrated_pages.append(default_livecodes_config("markdown", ""))
    return {
        "version": 2,
        "pages": migrated_pages,
        "window": notes.get("window", {}),
    }




def create_shortcuts(
    parent: Any,
    bindings: Iterable[tuple[str, str]] = SHORTCUTS,
    *,
    shortcut_cls: Callable[[Any, Any], Any],
    keyseq_cls: Callable[[str], Any],
) -> list[Any]:
    """
    Create Qt shortcut objects from keybinding tuples.

    Args:
        parent: QObject parent to own the shortcuts.
        bindings: Iterable of (key_sequence, handler_method_name) pairs.
        shortcut_cls: Class to instantiate for shortcuts (e.g., QShortcut).
        keyseq_cls: Class to parse key sequences (e.g., QKeySequence).

    Returns:
        List of created shortcut objects.
    """
    shortcuts: list[Any] = []
    for key_sequence, handler_name in bindings:
        shortcut = shortcut_cls(keyseq_cls(key_sequence), parent)
        shortcut.activated.connect(getattr(parent, handler_name))
        shortcuts.append(shortcut)
    return shortcuts


def hit_test_resize_edges(
    point: tuple[int, int],
    rect: Rect,
    *,
    margin: int = 8,
) -> str | None:
    """
    Determine which edge or corner of a rectangle a point is near.

    Args:
        point: (x, y) screen coordinates to test.
        rect: Target rectangle.
        margin: Pixel distance from edge to count as a "hit". Default 8.

    Returns:
        Edge identifier string: "top", "bottom", "left", "right", or
        corner variants ("top-left", "top-right", "bottom-left", "bottom-right"),
        or None if point is not near any edge.
    """
    x, y = point
    left = x <= rect.x + margin
    right = x >= rect.x + rect.width - margin
    top = y <= rect.y + margin
    bottom = y >= rect.y + rect.height - margin

    if top and left:
        return "top-left"
    if top and right:
        return "top-right"
    if bottom and left:
        return "bottom-left"
    if bottom and right:
        return "bottom-right"
    if left:
        return "left"
    if right:
        return "right"
    if top:
        return "top"
    if bottom:
        return "bottom"
    return None


def resize_rect(
    start: Rect,
    start_global: tuple[int, int],
    current_global: tuple[int, int],
    edge: str,
    *,
    minimum_size: tuple[int, int] = (280, 220),
) -> Rect:
    """
    Compute a new rectangle by dragging one of its edges or corners.

    Args:
        start: Initial rectangle.
        start_global: (x, y) where the drag started (screen coordinates).
        current_global: Current drag position (screen coordinates).
        edge: Which edge or corner is being dragged (e.g., "left", "bottom-right").
        minimum_size: (min_width, min_height) tuple to enforce.

    Returns:
        New Rect after applying resize constraints, clamped to minimum size.
    """
    dx = current_global[0] - start_global[0]
    dy = current_global[1] - start_global[1]
    min_width, min_height = minimum_size

    x = start.x
    y = start.y
    width = start.width
    height = start.height

    if "left" in edge:
        x = start.x + dx
        width = start.width - dx
        if width < min_width:
            x = start.x + (start.width - min_width)
            width = min_width
    elif "right" in edge:
        width = start.width + dx
        if width < min_width:
            width = min_width

    if "top" in edge:
        y = start.y + dy
        height = start.height - dy
        if height < min_height:
            y = start.y + (start.height - min_height)
            height = min_height
    elif "bottom" in edge:
        height = start.height + dy
        if height < min_height:
            height = min_height

    return Rect(x, y, width, height)


# ── LiveCodes local server ───────────────────────────────────────────────────

class _CORSRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Static file handler that adds CORS headers for local app usage."""

    def __init__(self, *args, directory=None, **kwargs):
        self._serve_dir = directory
        super().__init__(*args, directory=directory, **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()


def start_livecodes_server(build_dir: Path = LIVECODES_BUILD_DIR, port: int = LIVECODES_PORT) -> tuple[threading.Thread, int]:
    """Start a background thread serving the LiveCodes build directory.
    Returns (thread, actual_port). Tries the requested port first, then
    increments until an available one is found.
    """
    if not build_dir.exists():
        raise FileNotFoundError(f"LiveCodes build directory not found: {build_dir}")

    actual_port = port
    while True:
        try:
            test_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            test_sock.bind(("", actual_port))
            test_sock.close()
            break
        except OSError:
            actual_port += 1
            if actual_port > port + 100:
                raise RuntimeError(f"Could not find an available port for LiveCodes server (tried {port}-{actual_port})")

    def _serve():
        with socketserver.TCPServer(("", actual_port), lambda *a, **k: _CORSRequestHandler(*a, directory=str(build_dir), **k)) as httpd:
            httpd.serve_forever()

    thread = threading.Thread(target=_serve, daemon=True, name="livecodes-server")
    thread.start()
    return thread, actual_port


def livecodes_url(port: int = LIVECODES_PORT, path: str = "") -> str:
    """Build a URL to the local LiveCodes development server."""
    return f"http://localhost:{port}/{path}"


# ── Ollama helpers ───────────────────────────────────────────────────────────

def ollama_generate_payload(prompt: str, model: str = "llama3.2", system: str | None = None) -> dict[str, Any]:
    """
    Build an Ollama /api/generate request payload.

    Args:
        prompt: User prompt text.
        model: Model name to use (default "llama3.2").
        system: Optional system message to set context.

    Returns:
        Dict payload ready for JSON encoding and POST.
    """
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "stream": True}
    if system:
        payload["system"] = system
    return payload


def ollama_stream_chunks(response_iter: Iterable[bytes]) -> Iterable[str]:
    """Yield text chunks from an Ollama streaming response iterator."""
    for line in response_iter:
        line = line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        chunk = data.get("response", "")
        if chunk:
            yield chunk
        if data.get("done"):
            break
