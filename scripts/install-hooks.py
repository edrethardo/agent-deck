#!/usr/bin/env python3
"""Registers agent-monitor hooks in ~/.claude/settings.json (with backup)."""
import json
import shutil
from pathlib import Path

EVENTS = [
    "SessionStart", "UserPromptSubmit", "Notification", "Stop", "SessionEnd",
    # Permission-dialog lifecycle — the reliable "red" path (the VS Code
    # extension never fires Notification for its inline permission prompts).
    # PostToolUse only clears a waiting state after an approval. Unknown event
    # names are ignored by older Claude Code versions, so this degrades safely.
    "PermissionRequest", "PermissionDenied", "PostToolUse",
]
SETTINGS = Path.home() / ".claude" / "settings.json"
COMMAND = str(Path.home() / ".local" / "bin" / "agent-monitor") + " hook"


def main() -> None:
    cfg = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    if SETTINGS.exists():
        shutil.copy(SETTINGS, SETTINGS.with_suffix(".json.bak"))
    hooks = cfg.setdefault("hooks", {})
    for event in EVENTS:
        entries = hooks.setdefault(event, [])
        already = any(
            h.get("command", "").endswith("agent-monitor hook")
            for e in entries
            for h in e.get("hooks", [])
        )
        if not already:
            entries.append({"hooks": [{"type": "command", "command": COMMAND, "timeout": 5}]})
    SETTINGS.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"Hooks registered in {SETTINGS} (backup: {SETTINGS}.bak)")


if __name__ == "__main__":
    main()
