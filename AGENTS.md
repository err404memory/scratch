# Repository Guidelines

## Project Structure & Module Organization

This repository contains the Scratch sticky-note app. `scratch.py` is the PyQt6/WebEngine application entrypoint and UI shell. `scratch_core.py` holds pure-Python helpers for note loading, rendering, shortcuts, LiveCodes config, Ollama payloads, and resize math. Browser-side assets live in `assets/`, including Quill, xterm.js, editor HTML, terminal HTML, and CSS. Tests live in `tests/`; `test_scratch_core.py` covers pure helpers and `test_ui_behavior.py` covers UI-adjacent behavior with display-dependent skips. `workflow/` contains local Agent Orchestrator workspace helpers. `telegram-notifier/` is a separate TypeScript AO notifier package.

## Build, Test, and Development Commands

- `/usr/bin/pytest tests/ -q`: run the reliable Scratch test suite from the repo root.
- `python3.13 scratch.py`: launch the desktop sticky-note app when PyQt6 WebEngine is available.
- `cd telegram-notifier && npm run typecheck`: validate the notifier TypeScript without emitting files.
- `cd telegram-notifier && npm run build`: compile notifier output into `telegram-notifier/dist/`.
- `bash workflow/ao-workspace.sh`: start the local AO/zellij workflow if those tools are installed.

## Coding Style & Naming Conventions

Use 4-space indentation for Python and keep UI wiring in `scratch.py` separate from reusable logic in `scratch_core.py`. Prefer typed helper functions, small dataclasses, and explicit names such as `normalize_notes` or `resize_rect`. Keep Qt slot and handler names consistent with the existing underscore style, for example `_toggle_terminal`. TypeScript uses ESM, `strict` mode, ES2022, and `src/` to `dist/` compilation.

## Testing Guidelines

Add pure logic tests to `tests/test_scratch_core.py` when possible; it is faster and avoids display dependencies. Add UI behavior tests to `tests/test_ui_behavior.py` only when the behavior needs Qt objects or WebEngine checks. Tests should be named `test_<behavior>`. UI tests may skip when `DISPLAY` or PyQt6 WebEngine is unavailable.

## Commit & Pull Request Guidelines

The current history uses short, lower-case commit summaries such as `initial commit pt. 2`. Keep future commits concise and imperative, for example `fix preview title migration`. Pull requests should describe the visible behavior changed, list test commands run, and include screenshots or short recordings for UI changes.

## Agent-Specific Instructions

Keep Scratch preview-first: the note surface should remain readable by default, with Quill editing exposed only through the edit flow. Do not commit local notes data from `~/.scratch-notes/`. Never add bot tokens, chat IDs, or other secrets to the repo; configure notifier credentials through environment variables.
