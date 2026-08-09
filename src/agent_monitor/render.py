from __future__ import annotations

import os

from .context import UsageLimit
from .model import Session, Status

NUM_KEY_LEDS = 16
# Physical LED order on the strip. If the hardware test (Task 12) reveals a
# different wiring (e.g. serpentine), remap here.
KEY_LEDS = list(range(NUM_KEY_LEDS))
BRIGHTNESS = 0.4
MAX_OLED_LINES = 8
CTX_WARN_PCT = 85  # a session this close to auto-compact gets a visible marker

# Active states keep fixed colors; the idle color encodes reachability:
# blue = idle and remote-controllable (grab it from your phone),
# white = idle but only reachable at the PC (incl. pre-hook sessions).
COLORS: dict[Status, tuple[int, int, int]] = {
    Status.BUSY: (255, 160, 0),
    Status.WAITING: (255, 0, 0),
}
IDLE_REMOTE_COLOR = (0, 0, 255)
IDLE_LOCAL_COLOR = (255, 255, 255)
FINISHED_COLOR = (0, 255, 0)  # green: recently finished a task (decays to idle)


def _color_for(sess: Session) -> tuple[int, int, int]:
    if sess.question:
        return COLORS[Status.WAITING]  # a human is being asked — red wins
    fixed = COLORS.get(sess.status)
    if fixed is not None:
        return fixed
    if sess.status is Status.AVAILABLE and sess.finished:
        return FINISHED_COLOR
    return IDLE_REMOTE_COLOR if sess.remote else IDLE_LOCAL_COLOR
def led_colors(sessions: list[Session]) -> list[int]:
    out = [0] * (NUM_KEY_LEDS * 3)
    for sess in sessions:
        if sess.slot is None or not 0 <= sess.slot < NUM_KEY_LEDS:
            continue
        led = KEY_LEDS[sess.slot]
        r, g, b = _color_for(sess)
        out[led * 3 : led * 3 + 3] = [
            int(r * BRIGHTNESS),
            int(g * BRIGHTNESS),
            int(b * BRIGHTNESS),
        ]
    return out


def project_name(cwd: str) -> str:
    return os.path.basename(cwd.rstrip("/")) or cwd or "?"


def usage_lines(limits: list[UsageLimit]) -> list[str]:
    """Idle-screen text per account limit (rides the `lines` protocol slot).

    A stale limit (reset time passed, cache not yet refreshed) shows "--%"
    instead of a number that is knowably wrong."""
    out = []
    for lim in limits:
        if lim.stale:
            out.append(f"{lim.label}  --%")
        elif lim.resets_at:
            out.append(f"{lim.label} {lim.percent:>3}% -> {lim.resets_at}")
        else:
            out.append(f"{lim.label} {lim.percent:>3}%")
    return out[:MAX_OLED_LINES]


def usage_percents(limits: list[UsageLimit]) -> list[int]:
    """Bar fill per usage line; -1 = draw no bar (stale)."""
    return [-1 if lim.stale else max(0, min(100, lim.percent)) for lim in limits]


def overlay_info(sessions: list[Session], notes: dict[int, str] | None = None) -> list[str]:
    """Extra overlay lines per key slot ('\\n'-separated): model+effort, context %.

    `notes` prepends a short-lived line for a slot — how a pad action that did nothing
    visible says so, since the key press otherwise leaves no trace on the OLED.
    """
    out = [""] * NUM_KEY_LEDS
    for sess in sessions:
        if sess.slot is None or not 0 <= sess.slot < NUM_KEY_LEDS:
            continue
        parts = []
        if sess.model:
            parts.append(f"{sess.model} {sess.effort}".strip())
        if sess.context_pct is not None:
            warn = " !" if sess.context_pct >= CTX_WARN_PCT else ""
            parts.append(f"ctx {sess.context_pct}%{warn}")
        out[sess.slot] = "\n".join(parts)
    for slot, text in (notes or {}).items():
        if text and 0 <= slot < NUM_KEY_LEDS:
            out[slot] = f"{text}\n{out[slot]}".rstrip("\n")
    return out


MAX_NAME_LEN = 25  # OLED fits ~25 chars of the size-8 mono font per line


def key_names(sessions: list[Session]) -> list[str]:
    """Full project name per key slot (16 entries, "" = key free)."""
    out = [""] * NUM_KEY_LEDS
    for sess in sessions:
        if sess.slot is None or not 0 <= sess.slot < NUM_KEY_LEDS:
            continue
        name = project_name(sess.cwd)[:MAX_NAME_LEN]
        if sess.remote:
            name = name[: MAX_NAME_LEN - 4] + " [R]"
        out[sess.slot] = name
    return out


def flash_flags(sessions: list[Session]) -> list[int]:
    """All zeros — pulsing was tried and disliked; the protocol slot remains
    so the firmware's flash capability can be re-enabled without a reflash."""
    return [0] * NUM_KEY_LEDS
