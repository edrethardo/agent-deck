from __future__ import annotations

import logging
from collections.abc import Callable

from .model import Session, Status, status_for_event

_LOGGER = logging.getLogger(__name__)
MAX_SLOTS = 16


GREEN_HOLD_S = 600.0  # how long a finished session stays green


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        # Which key each project last held — lets a restarted session reclaim
        # its key even when lower-numbered keys are free.
        self._last_slot_by_cwd: dict[str, int] = {}
        # When the board is full, let a working/blocked session take the key of
        # the session idle longest. Owned by the pad; see docs/superpowers/specs.
        self.forward_mode = False

    def sessions(self) -> list[Session]:
        """Sorted by slot, overflow (None) last."""
        return sorted(
            self._sessions.values(),
            key=lambda s: (s.slot is None, s.slot if s.slot is not None else 0, s.since),
        )

    def by_id(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def apply_event(
        self,
        event: str,
        session_id: str,
        cwd: str,
        pid: int,
        message: str | None,
        now: float,
    ) -> bool:
        """Apply an event. True if the display state changed."""
        if event == "SessionEnd":
            removed = self._sessions.pop(session_id, None)
            if removed is None:
                return False
            self._remember_slot(removed)
            self._promote_overflow()
            return True

        sess = self._sessions.get(session_id)
        if sess is None:
            status = status_for_event(event, message, None) or Status.AVAILABLE
            twin = self._sessions.pop(f"proc-{pid}", None) if pid else None
            slot = twin.slot if twin is not None and twin.slot is not None else None
            source = "twin" if slot is not None else "fresh"
            if slot is None:
                preferred = self._last_slot_by_cwd.get(cwd)
                if preferred is not None and self._slot_is_free(preferred):
                    slot = preferred
                    source = "memory"
                    self._last_slot_by_cwd.pop(cwd, None)
            if slot is None:
                # Forward mode may only evict on behalf of a session that is
                # itself active — a brand-new AVAILABLE session must wait its
                # turn in overflow like anyone else.
                slot = self._claim_slot_for_real(
                    active=status in (Status.BUSY, Status.WAITING))
            _LOGGER.info("session %s created pid=%s slot=%s (%s)",
                         session_id[:8], pid, slot, source)
            self._sessions[session_id] = Session(
                session_id=session_id,
                cwd=cwd,
                pid=pid,
                status=status,
                slot=slot,
                since=now,
                finished=event == "Stop",
            )
            return True

        if not sess.cwd:
            # Frozen at creation otherwise: a `cd` inside the session must not
            # rename the key or shift its per-cwd sticky-slot identity.
            sess.cwd = cwd
        sess.pid = pid if pid > 1 else sess.pid
        # A hook event IS activity, so a hidden session earns its key back —
        # even for events that change nothing else (e.g. a second Stop).
        woke = self.unhide(sess) if sess.hidden_at else False
        new = status_for_event(event, message, sess.status)
        if new is None:
            return woke
        finished = event == "Stop"
        if new == sess.status and finished == sess.finished:
            return woke
        sess.status = new
        sess.finished = finished
        sess.since = now
        self._claim_if_active(sess)
        return True

    def decay_finished(self, now: float, hold: float = GREEN_HOLD_S) -> bool:
        """Fade finished sessions back to plain idle after `hold` seconds."""
        changed = False
        for sess in self._sessions.values():
            if sess.finished and sess.status is Status.AVAILABLE and now - sess.since >= hold:
                sess.finished = False
                changed = True
        return changed

    def _remember_slot(self, sess: Session) -> None:
        if sess.slot is not None and sess.cwd:
            self._last_slot_by_cwd[self._slot_key(sess.host, sess.cwd)] = sess.slot

    @staticmethod
    def _slot_key(host: str, cwd: str) -> str:
        """Sticky-key identity: same project name on two machines is two keys."""
        return f"{host}:{cwd}" if host else cwd

    def _slot_is_free(self, slot: int) -> bool:
        return 0 <= slot < MAX_SLOTS and all(s.slot != slot for s in self._sessions.values())

    def add_scanned(self, pid: int, cwd: str, now: float) -> bool:
        """Register a discovered claude process unless its PID is already tracked."""
        if any(s.pid == pid for s in self._sessions.values()):
            return False
        preferred = self._last_slot_by_cwd.get(cwd)
        if preferred is not None and self._slot_is_free(preferred):
            slot = preferred
            self._last_slot_by_cwd.pop(cwd, None)
        else:
            slot = self._free_slot()
        _LOGGER.info("scanned session pid=%s slot=%s", pid, slot)
        self._sessions[f"proc-{pid}"] = Session(
            session_id=f"proc-{pid}",
            cwd=cwd,
            pid=pid,
            status=Status.UNKNOWN,
            slot=slot,
            since=now,
        )
        return True

    def swap_slots(self, a: int, b: int) -> bool:
        """Swap whatever is shown on keys a and b (either may be empty)."""
        if a == b:
            return False
        sess_a = next((s for s in self._sessions.values() if s.slot == a), None)
        sess_b = next((s for s in self._sessions.values() if s.slot == b), None)
        if sess_a is None and sess_b is None:
            return False
        if sess_a is not None:
            sess_a.slot = b
        if sess_b is not None:
            sess_b.slot = a
        return True

    def hide_slot(self, slot: int, now: float) -> bool:
        """Take the session on `slot` off the deck without touching it.

        The session keeps running and stays tracked; it simply loses its key
        until its next sign of activity (see `unhide`)."""
        for sess in self._sessions.values():
            if sess.slot == slot:
                self._remember_slot(sess)   # prefer this key when it returns
                sess.slot = None
                sess.hidden_at = now
                _LOGGER.info("hid session %s (was slot %s)", sess.session_id[:8], slot)
                self._promote_overflow()   # a waiting session may take the freed key
                return True
        return False

    def unhide(self, sess: Session) -> bool:
        """Put a hidden session back on the board. True if anything changed."""
        if not sess.hidden_at:
            return False
        sess.hidden_at = 0.0
        key = self._slot_key(sess.host, sess.cwd)
        preferred = self._last_slot_by_cwd.get(key)
        if preferred is not None and self._slot_is_free(preferred):
            sess.slot = preferred
            self._last_slot_by_cwd.pop(key, None)
        else:
            # Miss: the remembered entry deliberately stays, matching the
            # pop-only-on-hit convention used elsewhere in this file. It now
            # serves a FUTURE session of this project, not this one.
            # No displacement here: waking up is not the same as needing a key.
            # If the event that woke it also makes it BUSY/WAITING, apply_event's
            # tail claims one properly, displacing if forward mode allows.
            sess.slot = self._claim_slot_for_real()
        _LOGGER.info("unhid session %s -> slot %s", sess.session_id[:8], sess.slot)
        return True

    def update_remote_flags(self, has_rc: Callable[[int], bool]) -> bool:
        """Refresh each session's remote-control flag. True if any changed.

        Sessions with a manual (pad-toggled) flag are skipped: the relay
        connection lingers after an in-process off-toggle, so detection
        would wrongly overwrite the known state."""
        changed = False
        for sess in self._sessions.values():
            if sess.pid > 1 and not sess.rc_manual:
                flag = has_rc(sess.pid)
                if flag != sess.remote:
                    sess.remote = flag
                    changed = True
        return changed

    ACTIVITY_WINDOW_S = 30.0  # only a RECENT post-Stop write proves live work

    def update_context(self, infos: dict[int, "ContextInfo | None"], now: float | None = None) -> bool:
        """Adopt per-PID context info. True if anything changed.

        A None/missing entry keeps the last known values — a transcript
        that is momentarily unreadable must not blank the display."""
        changed = False
        for sess in self._sessions.values():
            info = infos.get(sess.pid)
            if info is None:
                continue
            if sess.hidden_at and info.activity > sess.hidden_at:
                self.unhide(sess)
                changed = True
            new = (info.percent, info.model, info.effort, info.question, info.blocked)
            if (sess.context_pct, sess.model, sess.effort, sess.question, sess.blocked) != new:
                (sess.context_pct, sess.model, sess.effort,
                 sess.question, sess.blocked) = new
                changed = True
            changed |= self._claim_if_active(sess)
            if info.interrupted:
                # Esc ends the turn without a Stop hook. The session is idle
                # and the user is right there — green, like a finished task.
                # Claimed before the activity check: the interrupt itself is
                # the fresh transcript write it would otherwise react to.
                if sess.status is not Status.AVAILABLE or not sess.finished:
                    sess.status = Status.AVAILABLE
                    sess.finished = True
                    sess.since = info.activity or (now or sess.since)
                    changed = True
            elif (sess.status is Status.AVAILABLE
                    and now is not None
                    and info.activity > sess.since + 2.0
                    and now - info.activity < self.ACTIVITY_WINDOW_S):
                # Written AFTER the Stop that made it green: an autonomous
                # re-invocation or a background subagent is working — no
                # UserPromptSubmit ever fires for those. The next real Stop
                # turns it back green.
                sess.status = Status.BUSY
                sess.since = info.activity
                sess.finished = False
                changed = True
                self._claim_if_active(sess)
        return self._update_peer_waits(infos) or changed

    def _update_peer_waits(self, infos: dict[int, "ContextInfo | None"]) -> bool:
        """Mark sessions whose dispatched peer task is still in flight.

        Work handed to another session lives in THAT session — the sender sits
        idle with a Stop as its last event and would otherwise read as green
        ("finished, come look") while the answer is still being written."""
        by_pid = {s.pid: s for s in self._sessions.values()}
        changed = False
        for sess in self._sessions.values():
            info = infos.get(sess.pid)
            waiting = ""
            if info is not None and info.peer_pid and not info.interrupted:
                peer = by_pid.get(info.peer_pid)
                if peer is not None and peer is not sess and (
                        peer.status in (Status.BUSY, Status.WAITING)
                        or peer.question or peer.blocked):
                    waiting = info.peer_name
            if sess.waiting_for != waiting:
                sess.waiting_for = waiting
                changed = True
        return changed

    def set_remote_manual(self, slot: int, value: bool) -> bool:
        """Pin a session's remote flag after a pad-initiated toggle."""
        for sess in self._sessions.values():
            if sess.slot == slot:
                sess.remote = value
                sess.rc_manual = True
                return True
        return False

    def prune(self, pid_alive: Callable[[int], bool]) -> bool:
        """Remove sessions whose process is dead. True if anything changed.

        Remote sessions are exempt: their pid belongs to another machine and
        would always read as dead here. The probe's own result removes them."""
        dead = [sid for sid, s in self._sessions.items()
                if not s.host and not pid_alive(s.pid)]
        for sid in dead:
            sess = self._sessions[sid]
            _LOGGER.info("pruned session %s pid=%s slot=%s (process dead)",
                         sid[:8], sess.pid, sess.slot)
            self._remember_slot(sess)
            del self._sessions[sid]
        promoted = self._promote_overflow()
        return bool(dead) or promoted

    REMOTE_BUSY_S = 60.0  # a remote transcript written this recently = working

    def sync_remote(self, host: str, entries: list[dict], now: float) -> bool:
        """Adopt one host's probe result: upsert its sessions, drop vanished ones.

        Remote sessions have no hooks — their status comes entirely from what
        the probe read out of the transcript (see `context.read_context`)."""
        changed = False
        seen = set()
        for entry in entries:
            sid = f"{host}:{entry.get('session_id') or entry.get('pid')}"
            seen.add(sid)
            cwd = str(entry.get("cwd") or "")
            age = float(entry.get("age", 1e9))
            if entry.get("blocked") or entry.get("question"):
                status, finished = Status.WAITING, False
            elif entry.get("interrupted"):
                status, finished = Status.AVAILABLE, True
            elif age < self.REMOTE_BUSY_S:
                status, finished = Status.BUSY, False
            else:
                status, finished = Status.AVAILABLE, age < GREEN_HOLD_S
            sess = self._sessions.get(sid)
            if sess is None:
                key = self._slot_key(host, cwd)
                preferred = self._last_slot_by_cwd.get(key)
                if preferred is not None and self._slot_is_free(preferred):
                    slot = preferred
                    self._last_slot_by_cwd.pop(key, None)
                else:
                    slot = self._claim_slot_for_real(
                        active=status in (Status.BUSY, Status.WAITING))
                _LOGGER.info("remote session %s on %s slot=%s", sid[:24], host, slot)
                sess = Session(session_id=sid, cwd=cwd, pid=0, status=status,
                               slot=slot, since=now, host=host)
                self._sessions[sid] = sess
                changed = True
            new = (status, finished, str(entry.get("model") or ""),
                   str(entry.get("effort") or ""), entry.get("context_pct"),
                   bool(entry.get("question")), bool(entry.get("blocked")))
            old = (sess.status, sess.finished, sess.model, sess.effort,
                   sess.context_pct, sess.question, sess.blocked)
            if new != old:
                if sess.status is not status:
                    sess.since = now
                (sess.status, sess.finished, sess.model, sess.effort,
                 sess.context_pct, sess.question, sess.blocked) = new
                changed = True
                changed |= self._claim_if_active(sess)
            sess.cwd = sess.cwd or cwd
            if sess.hidden_at and (now - age) > sess.hidden_at:
                self.unhide(sess)
                changed = True
            if not sess.rc_manual:
                # the probe read this off the remote's own sockets; a pad
                # toggle still wins, exactly as for local sessions
                flag = bool(entry.get("remote"))
                if sess.remote != flag:
                    sess.remote = flag
                    changed = True
        for sid in [s for s, sess in self._sessions.items()
                    if sess.host == host and s not in seen]:
            self._remember_slot(self._sessions[sid])
            del self._sessions[sid]
            changed = True
        return self._promote_overflow() or changed

    def _claim_slot_for_real(self, active: bool = False) -> int | None:
        """Free slot, or evict the newest scanned (UNKNOWN) session to overflow.

        With forward mode on, a full board additionally yields the key of the
        session idle longest — see `_displace_longest_idle`. `active` gates
        only that last resort and defaults to False (fail safe): a caller
        must opt in explicitly, and only on behalf of a session that is
        itself BUSY/WAITING — waking up or merely being created must never
        jump the overflow queue."""
        slot = self._free_slot()
        if slot is not None:
            return slot
        unknowns = [s for s in self._sessions.values()
                    if s.status is Status.UNKNOWN and s.slot is not None]
        if unknowns:
            victim = max(unknowns, key=lambda s: s.since)
            slot, victim.slot = victim.slot, None
            return slot
        return self._displace_longest_idle() if active else None

    def set_forward_mode(self, value: bool) -> bool:
        """Toggle forward mode. True if the display changed.

        Switching it ON re-evaluates the board immediately: sessions already
        waiting in overflow are what the user is trying to rescue."""
        if self.forward_mode == value:
            return False
        self.forward_mode = value
        changed = True
        if value:
            # oldest first, so the longest-waiting session is served first
            for sess in sorted(self._sessions.values(), key=lambda s: s.since):
                self._claim_if_active(sess)
        return changed

    def _claim_if_active(self, sess: Session) -> bool:
        """Give an overflowed session a key once it genuinely needs one.

        `question` and `blocked` count as needing one even when `status` does
        not say so: in VS Code no hook fires for a permission prompt, and both
        render red — exactly the states forward mode exists to surface."""
        if sess.slot is not None or sess.hidden_at:
            return False
        if not (sess.status in (Status.BUSY, Status.WAITING) or sess.question or sess.blocked):
            return False
        slot = self._claim_slot_for_real(active=True)
        if slot is None:
            return False
        sess.slot = slot
        return True

    def _displace_longest_idle(self) -> int | None:
        """Free the key of the longest-idle session, or None if there is none.

        Only plain idle sessions are eligible: a BUSY or WAITING one is doing
        or needing something, and a hidden one holds no key anyway. A green
        (recently finished) session is protected in practice, because `since`
        is recent by construction — it is chosen only when every keyed session
        is green and somebody has to yield."""
        if not self.forward_mode:
            return None
        idle = [s for s in self._sessions.values()
                if s.slot is not None and s.status is Status.AVAILABLE]
        if not idle:
            return None
        # oldest `since` wins; the higher slot breaks an exact tie so the
        # choice never depends on dict iteration order
        victim = min(idle, key=lambda s: (s.since, -s.slot))
        self._remember_slot(victim)          # it comes back to this key
        slot, victim.slot = victim.slot, None
        _LOGGER.info("forward mode: session %s yields slot %s (idle longest)",
                     victim.session_id[:8], slot)
        return slot

    def _free_slot(self) -> int | None:
        used = {s.slot for s in self._sessions.values() if s.slot is not None}
        for i in range(MAX_SLOTS):
            if i not in used:
                return i
        return None

    def _promote_overflow(self) -> bool:
        changed = False
        for sess in sorted(self._sessions.values(), key=lambda s: s.since):
            if sess.slot is None and not sess.hidden_at:  # hidden ones stay off
                slot = self._free_slot()
                if slot is None:
                    break
                sess.slot = slot
                changed = True
        return changed

    def to_dict(self) -> dict:
        return {"sessions": [s.to_dict() for s in self.sessions()],
                "forward_mode": self.forward_mode}

    @classmethod
    def from_dict(cls, data: dict) -> "SessionRegistry":
        """Load a snapshot, repairing any corrupt slot data it contains."""
        reg = cls()
        for entry in data.get("sessions", []):
            try:
                sess = Session.from_dict(entry)
            except (KeyError, ValueError, TypeError):
                continue
            reg._sessions[sess.session_id] = sess
        reg.forward_mode = bool(data.get("forward_mode", False))
        reg._normalize_slots()
        return reg

    def _normalize_slots(self) -> None:
        """Clear invalid or duplicate slots (newest keeps a contested slot),
        then promote overflow sessions into any free slots."""
        seen: set[int] = set()
        for sess in sorted(self._sessions.values(), key=lambda s: s.since, reverse=True):
            if sess.hidden_at:
                sess.slot = None      # hidden sessions never hold a key
                continue
            slot = sess.slot
            if slot is None:
                continue
            valid = isinstance(slot, int) and not isinstance(slot, bool) and 0 <= slot < MAX_SLOTS
            if not valid or slot in seen:
                sess.slot = None
            else:
                seen.add(slot)
        self._promote_overflow()
