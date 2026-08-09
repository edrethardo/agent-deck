from __future__ import annotations

import os

from .model import Session, Status

NUM_KEY_LEDS = 16
# Physical LED order on the strip. If the hardware test (Task 12) reveals a
# different wiring (e.g. serpentine), remap here.
KEY_LEDS = list(range(NUM_KEY_LEDS))
BRIGHTNESS = 0.4
MAX_OLED_LINES = 8

COLORS: dict[Status, tuple[int, int, int]] = {
    Status.AVAILABLE: (0, 255, 0),
    Status.BUSY: (255, 160, 0),
    Status.WAITING: (255, 0, 0),
    Status.UNKNOWN: (0, 0, 255),  # blue — orange variants were tried and reverted  # Claude orange #D97757
}
STATUS_CHAR: dict[Status, str] = {
    Status.AVAILABLE: "+",
    Status.BUSY: "~",
    Status.WAITING: "!",
    Status.UNKNOWN: "?",
}


def led_colors(sessions: list[Session]) -> list[int]:
    out = [0] * (NUM_KEY_LEDS * 3)
    for sess in sessions:
        if sess.slot is None or not 0 <= sess.slot < NUM_KEY_LEDS:
            continue
        led = KEY_LEDS[sess.slot]
        r, g, b = COLORS[sess.status]
        out[led * 3 : led * 3 + 3] = [
            int(r * BRIGHTNESS),
            int(g * BRIGHTNESS),
            int(b * BRIGHTNESS),
        ]
    return out


def project_name(cwd: str) -> str:
    return os.path.basename(cwd.rstrip("/")) or cwd or "?"


def oled_lines(sessions: list[Session]) -> list[str]:
    lines = []
    for sess in sessions:
        if sess.slot is None or not 0 <= sess.slot < NUM_KEY_LEDS:
            continue
        name = project_name(sess.cwd)[:12]
        lines.append(f"{sess.slot + 1:>2} {name:<12} {STATUS_CHAR[sess.status]}")
    return lines[:MAX_OLED_LINES]


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
