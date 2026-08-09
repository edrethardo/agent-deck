# [DRAFT — not filed] Expose remote-control state in session metadata (and a small session IPC)

**Repo:** anthropics/claude-code
**Title:** Feature request: `remoteControl` field in `~/.claude/sessions/<pid>.json` (+ optional command IPC)

## Description

`~/.claude/sessions/<pid>.json` is a great integration point: PID, sessionId, cwd, version, entrypoint. Two additions would let external tools cooperate with sessions without fragile workarounds:

### 1. `remoteControl: boolean`

There is currently **no external trace** of whether a session has Remote Control enabled. We verified (2026-08-09, v2.1.226):

- `/remote-control on|off` changes nothing in config files, session metadata, or transcripts;
- the relay connection cannot be distinguished from ordinary API traffic by address — it terminates at the same edge IPs as `api.anthropic.com` and uses either address family;
- after `/remote-control off` the connection lingers, so even socket inspection cannot see the off-toggle.

A `remoteControl` field in the session file (updated on toggle) would make the state observable. Our use case: [agent-deck](https://github.com/edrethardo/agent-deck) colors each session's key blue when it can be picked up from the phone — today that relies on a connection heuristic plus manual pinning.

### 2. (Optional) documented session IPC for slash commands

The session file already references a `messagingSocketPath`. A documented way to ask a session to run a user-visible command — `/compact` being the prime candidate — would let external tools trigger it without X11 keystroke injection, which requires an unlocked desktop and breaks on Wayland. The phone app notably has no manual compact either; a local IPC would enable both hardware controllers and future remote UIs.

## Expected

- `~/.claude/sessions/<pid>.json` gains `remoteControl: true|false`, updated when the user toggles it.
- (Stretch) a minimal, documented IPC verb to enqueue a slash command such as `/compact`.
