# Scratch

Scratch is a compact PyQt6 sticky-note app for quick capture, multi-page notes, and LiveCodes-backed rendering of Markdown, HTML, CSS, JavaScript, and fenced code blocks.

## Essential Functions

- **Quick capture:** type or paste notes into a lightweight always-available desktop window.
- **Multi-page notes:** add, delete, and move between note pages from the toolbar or page rail.
- **Live rendering:** preview Markdown, HTML, CSS, JavaScript, and fenced code through the embedded LiveCodes pane.
- **Session persistence:** save note content, active page, split sizes, terminal height, and window geometry.
- **Window control:** pin on top, hide to tray, resize from all edges, and restore the last position and size.
- **Sharing:** use context-menu sharing actions for selected content or the full note.
- **Local AI handoff:** use Ask Ollama separately from sharing.
- **Terminal pane:** open a small PTY-backed terminal inside the Scratch window.
- **Data protection:** write notes atomically and keep rolling backups before overwrites.

## Data Storage

Scratch stores user data outside the repository:

- Notes: `~/.scratch-notes/notes.json`
- Settings: `~/.scratch-notes/config.json`
- Backups: `~/.scratch-notes/backups/notes-*.json`

Do not commit files from `~/.scratch-notes/`.

## Save Behavior

LiveCodes editor changes are sent through Qt WebChannel into Python and then written to `notes.json`. Scratch also captures the active LiveCodes pane before page navigation, hiding, or quitting. Saves are atomic: Scratch writes `notes.json.tmp`, then replaces `notes.json`.

To avoid destructive autosave failures, Scratch rejects malformed LiveCodes payloads and blocks automatic empty updates from overwriting non-empty pages.

## Development Commands

Run from the repository root:

```bash
/usr/bin/pytest tests/ -q
python3.13 -m py_compile scratch.py scratch_core.py
python3.13 scratch.py
```

Notifier package checks:

```bash
cd telegram-notifier && npm run typecheck
cd telegram-notifier && npm run build
```

## Recovery Notes

If note content disappears, stop using the app immediately and inspect:

```bash
ls -lt ~/.scratch-notes/backups/
cp ~/.scratch-notes/backups/<backup>.json ~/.scratch-notes/notes.json
```

Use a unique word from the missing note to search backups and caches before restoring.

## Current Limitations

- Clearing an existing non-empty note by deleting all editor content may not persist automatically; use delete page for intentional destructive clears.
- Push access depends on a valid GitHub token in `gh`; an invalid token causes `git push` to fail with HTTP 403.
