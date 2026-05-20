from __future__ import annotations

from dataclasses import dataclass

import scratch_core


def test_load_notes_defaults_to_single_blank_page():
    assert scratch_core.load_notes_text(None) == {"pages": [""], "window": {}}


def test_load_notes_rejects_invalid_payload():
    assert scratch_core.normalize_notes({}) == {"pages": [""], "window": {}}
    assert scratch_core.normalize_notes({"pages": []}) == {"pages": [""], "window": {}}
    assert scratch_core.normalize_notes({"pages": ["ok", 1]}) == {"pages": ["ok", ""], "window": {}}


def test_page_title_from_html_strips_tags_and_truncates():
    html = "<h1>Scratch title</h1><p>with extra content</p>"
    assert scratch_core.page_title_from_html(html, limit=12) == "Scratch titl"


def test_render_note_source_supports_markdown_and_raw_html():
    source = """# Scratch

```python
print("hi")
```

<details>
<summary>More</summary>
<p>Body</p>
</details>
"""

    rendered = scratch_core.render_note_source(source)

    assert "<h1" in rendered
    assert "Scratch" in rendered
    assert "<pre" in rendered and "<code" in rendered
    assert "print(&quot;hi&quot;)" in rendered or 'print("hi")' in rendered
    assert "<details>" in rendered
    assert "<summary>More</summary>" in rendered


def test_render_note_source_closes_language_code_tag():
    rendered = scratch_core.render_note_source("```python\nprint('hi')\n```")

    assert '<pre><code class="language-python">' in rendered
    assert '<pre><code class="language-python"\n' not in rendered
    assert '<pre><code class="language-python">print' in rendered


def test_render_note_source_emits_valid_css_braces():
    rendered = scratch_core.render_note_source("# Scratch")

    assert "details{" in rendered
    assert "details{{" not in rendered
    assert "summary{" in rendered
    assert "summary{{" not in rendered


def test_render_note_source_preserves_typed_newlines():
    rendered = scratch_core.render_note_source("first\nsecond")

    assert "<p>first</p>" in rendered
    assert "<p>second</p>" in rendered
    assert "white-space:pre-wrap" not in rendered


def test_preserve_text_newlines_ignores_whitespace_between_tags():
    source = "<h3>heading</h3>\n<ol>\n<li>item</li>\n<li>item</li>\n</ol>"

    rendered = scratch_core.preserve_text_newlines(source)

    assert rendered == "<h3>heading</h3><ol><li>item</li><li>item</li></ol>"


def test_preserve_text_newlines_keeps_text_breaks():
    rendered = scratch_core.preserve_text_newlines("first\nsecond")

    assert rendered == "first<br>second"


def test_preformatted_html_escapes_generated_text():
    rendered = scratch_core.preformatted_html("a <b>tag</b> & value")

    assert rendered == "<pre>a &lt;b&gt;tag&lt;/b&gt; &amp; value</pre>"


def test_migrate_notes_converts_plain_text_pages_only():
    calls: list[str] = []

    def fake_converter(text: str) -> str:
        calls.append(text)
        return f"<p>{text}</p>"

    notes = {"pages": ["alpha", "<p>already html</p>", ""]}

    migrated, changed = scratch_core.migrate_notes(notes, fake_converter)

    assert changed is True
    assert migrated == {"pages": ["<p>alpha</p>", "<p>already html</p>", ""]}
    assert calls == ["alpha"]


@dataclass
class FakeSignal:
    connected: list = None

    def __post_init__(self):
        if self.connected is None:
            self.connected = []

    def connect(self, callback):
        self.connected.append(callback)


class FakeShortcut:
    def __init__(self, keyseq, parent):
        self.keyseq = keyseq
        self.parent = parent
        self.activated = FakeSignal()


class FakeKeySequence:
    def __init__(self, text):
        self.text = text


class ShortcutParent:
    def __init__(self):
        self.calls: list[str] = []

    def _new_page(self):
        self.calls.append("new")

    def _delete_page(self):
        self.calls.append("delete")


def test_create_shortcuts_retains_each_binding():
    parent = ShortcutParent()
    shortcuts = scratch_core.create_shortcuts(
        parent,
        [
            ("Ctrl+N", "_new_page"),
            ("Ctrl+W", "_delete_page"),
        ],
        shortcut_cls=FakeShortcut,
        keyseq_cls=FakeKeySequence,
    )

    assert [s.keyseq.text for s in shortcuts] == ["Ctrl+N", "Ctrl+W"]
    assert shortcuts[0].activated.connected[0].__self__ is parent
    assert shortcuts[0].activated.connected[0].__func__ is ShortcutParent._new_page
    assert shortcuts[1].activated.connected[0].__self__ is parent
    assert shortcuts[1].activated.connected[0].__func__ is ShortcutParent._delete_page

    shortcuts[0].activated.connected[0]()
    shortcuts[1].activated.connected[0]()

    assert parent.calls == ["new", "delete"]


def test_shortcuts_cover_visible_command_strip_actions():
    bindings = dict(scratch_core.SHORTCUTS)

    assert bindings["Ctrl+N"] == "_new_page"
    assert bindings["Ctrl+T"] == "_toggle_terminal"
    assert bindings["Ctrl+\\"] == "_split_pane"
    assert bindings["Ctrl+Shift+\\"] == "_close_extra_panes"
    assert bindings["Ctrl+P"] == "_toggle_pin"
    assert "Ctrl+E" not in bindings
    assert "Ctrl+B" not in bindings
    assert "Ctrl+S" not in bindings
    assert "Ctrl+Shift+S" not in bindings
    assert "Ctrl+," not in bindings


def test_hit_test_resize_edges_prefers_corners():
    rect = scratch_core.Rect(10, 20, 300, 200)

    assert scratch_core.hit_test_resize_edges((11, 21), rect, margin=8) == "top-left"
    assert scratch_core.hit_test_resize_edges((309, 219), rect, margin=8) == "bottom-right"
    assert scratch_core.hit_test_resize_edges((160, 120), rect, margin=8) is None


def test_resize_rect_clamps_to_minimum_size():
    start = scratch_core.Rect(100, 100, 300, 200)

    resized = scratch_core.resize_rect(
        start,
        start_global=(100, 100),
        current_global=(150, 100),
        edge="left",
        minimum_size=(280, 180),
    )

    assert resized.x == 120
    assert resized.width == 280
    assert resized.height == 200


def test_resize_rect_handles_all_edges_and_corners():
    start = scratch_core.Rect(100, 100, 300, 200)

    top = scratch_core.resize_rect(start, (100, 100), (100, 80), "top")
    right = scratch_core.resize_rect(start, (400, 100), (450, 100), "right")
    bottom_left = scratch_core.resize_rect(start, (100, 300), (80, 340), "bottom-left")

    assert top == scratch_core.Rect(100, 80, 300, 220)
    assert right == scratch_core.Rect(100, 100, 350, 200)
    assert bottom_left == scratch_core.Rect(80, 100, 320, 240)


# ── LiveCodes config helpers ─────────────────────────────────────────────────


def test_default_livecodes_config():
    cfg = scratch_core.default_livecodes_config("markdown", "# hello")
    assert cfg == {"markup": {"language": "markdown", "content": "# hello"}}


def test_livecodes_url_uses_loopback_alias_to_avoid_dev_sandbox():
    assert scratch_core.livecodes_url(4173) == "http://127.0.0.2:4173/"


def test_livecodes_config_detects_html_markdown_and_css():
    html_cfg = scratch_core.livecodes_config_from_source("<h1>Hello</h1>")
    md_cfg = scratch_core.livecodes_config_from_source("# Hello")
    css_cfg = scratch_core.livecodes_config_from_source("body { background: tomato; }")

    assert html_cfg["markup"]["language"] == "html"
    assert md_cfg["markup"]["language"] == "markdown"
    assert md_cfg["style"] == {"language": "css", "content": ""}
    assert md_cfg["script"] == {"language": "javascript", "content": ""}
    assert css_cfg["markup"]["language"] == "html"
    assert css_cfg["style"]["language"] == "css"
    assert "scratch-css-preview" in css_cfg["markup"]["content"]


def test_livecodes_config_clears_absent_tabs_between_notes():
    css_cfg = scratch_core.livecodes_config_from_source("body { background: tomato; }")
    markdown_cfg = scratch_core.livecodes_config_from_source("# Plain note")
    html_cfg = scratch_core.livecodes_config_from_source("<h1>Plain HTML</h1>")

    assert css_cfg["style"]["content"] == "body { background: tomato; }"
    assert markdown_cfg["style"]["content"] == ""
    assert markdown_cfg["script"]["content"] == ""
    assert html_cfg["style"]["content"] == ""
    assert html_cfg["script"]["content"] == ""


def test_livecodes_config_validation_requires_content_panel():
    assert scratch_core.is_livecodes_content_config({"markup": {"content": ""}})
    assert scratch_core.is_livecodes_content_config({"style": {"content": "body {}"}})
    assert not scratch_core.is_livecodes_content_config({})
    assert not scratch_core.is_livecodes_content_config({"markup": {"language": "markdown"}})
    assert not scratch_core.is_livecodes_content_config({"markup": {"content": None}})


def test_livecodes_config_splits_fenced_blocks():
    source = """```html
<h1>Hello</h1>
```

```css
h1 { color: red; }
```

```js
console.log("hi")
```"""

    cfg = scratch_core.livecodes_config_from_source(source)

    assert cfg["markup"]["language"] == "html"
    assert cfg["markup"]["content"] == "<h1>Hello</h1>"
    assert cfg["style"]["content"] == "h1 { color: red; }"
    assert cfg["script"]["language"] == "javascript"


def test_livecodes_source_round_trips_config_without_css_placeholder():
    cfg = {
        "markup": {"language": "html", "content": scratch_core.CSS_PREVIEW_MARKUP},
        "style": {"language": "css", "content": "body { color: red; }"},
    }

    source = scratch_core.livecodes_source_from_config(cfg)

    assert "scratch-css-preview" not in source
    assert source == "```css\nbody { color: red; }\n```"


def test_livecodes_source_keeps_simple_html_plain():
    cfg = {"markup": {"language": "html", "content": "<h1>Hello</h1>"}}

    assert scratch_core.livecodes_source_from_config(cfg) == "<h1>Hello</h1>"


def test_page_title_from_config_extracts_markup():
    cfg = {"markup": {"language": "markdown", "content": "# My Title\n\nBody text"}}
    assert scratch_core.page_title_from_config(cfg, limit=20) == "My Title"


def test_plain_text_from_config_strips_tags():
    cfg = {"markup": {"language": "html", "content": "<h1>Title</h1><p>Body</p>"}}
    assert scratch_core.plain_text_from_config(cfg) == "TitleBody"


# ── v1 → v2 migration ────────────────────────────────────────────────────────


def test_migrate_v1_html_pages_to_livecodes():
    v1 = {"pages": ["<p>hello</p>", "plain text"]}
    v2 = scratch_core.migrate_v1_to_v2(v1)
    assert v2["version"] == 2
    assert v2["pages"][0]["markup"]["language"] == "html"
    assert v2["pages"][0]["markup"]["content"] == "<p>hello</p>"
    assert v2["pages"][1]["markup"]["language"] == "markdown"
    assert v2["pages"][1]["markup"]["content"] == "plain text"


def test_migrate_v1_preserves_window():
    v1 = {"pages": [], "window": {"x": 10, "y": 20}}
    v2 = scratch_core.migrate_v1_to_v2(v1)
    assert v2["window"] == {"x": 10, "y": 20}


def test_migrate_v2_passthrough():
    v2 = {"version": 2, "pages": [{"markup": {"language": "html", "content": ""}}]}
    result = scratch_core.migrate_v1_to_v2(v2)
    assert result["version"] == 2
    assert result["pages"][0]["markup"]["language"] == "html"


# ── Ollama helpers ───────────────────────────────────────────────────────────


def test_ollama_generate_payload_structure():
    payload = scratch_core.ollama_generate_payload("hi", model="llama3.2")
    assert payload["model"] == "llama3.2"
    assert payload["prompt"] == "hi"
    assert payload["stream"] is True
    assert "system" not in payload


def test_ollama_generate_payload_with_system():
    payload = scratch_core.ollama_generate_payload("hi", system="Be helpful")
    assert payload["system"] == "Be helpful"


def test_ollama_stream_chunks_yields_text():
    lines = [
        b'{"response":"Hello","done":false}\n',
        b'{"response":" world","done":false}\n',
        b'{"response":"","done":true}\n',
    ]
    chunks = list(scratch_core.ollama_stream_chunks(lines))
    assert chunks == ["Hello", " world"]


def test_ollama_stream_chunks_skips_malformed():
    lines = [b'not json\n', b'{"response":"ok","done":true}\n']
    chunks = list(scratch_core.ollama_stream_chunks(lines))
    assert chunks == ["ok"]
