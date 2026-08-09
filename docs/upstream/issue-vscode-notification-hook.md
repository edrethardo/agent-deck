# [DRAFT — not filed] VS Code extension: permission prompts do not fire the `Notification` hook

**Repo:** anthropics/claude-code
**Title:** VS Code extension: inline permission prompts never fire the `Notification` hook

## Description

The `Notification` hook fires reliably for permission prompts in the terminal CLI, but the VS Code extension's inline permission dialog never triggers it — the hook command is simply not invoked.

## Environment

- Claude Code 2.1.226 (VS Code extension, `entrypoint: claude-vscode`), Linux (X11, Ubuntu 22.04)
- Hook registered globally in `~/.claude/settings.json` under `Notification` (same registration works for `SessionStart` / `UserPromptSubmit` / `Stop` / `SessionEnd` in both clients)

## Reproduction

1. Register a `Notification` hook that appends to a log file.
2. Terminal CLI: run a command that needs approval → hook fires with `permission` message. ✓
3. VS Code extension: trigger the same permission prompt (auto-approval off) → the inline dialog appears, but the hook never fires. Verified with the window focused, unfocused, and with the screen locked (2026-08-09).

## Why it matters

External tooling that surfaces session state (in our case [agent-deck](https://github.com/edrethardo/agent-deck), a hardware traffic-light for Claude Code sessions — red means "blocked waiting for your approval") cannot detect a blocked session in VS Code. Users who mostly run the extension silently miss pending prompts — exactly the situation the `Notification` hook exists for.

## Expected

The extension fires the `Notification` hook for its inline permission dialogs (and questions/plan approvals), matching CLI behavior.
