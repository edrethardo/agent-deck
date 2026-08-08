from __future__ import annotations

from collections.abc import Callable

from .model import Session, Status, status_for_event

MAX_SLOTS = 16


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

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
            if self._sessions.pop(session_id, None) is None:
                return False
            self._promote_overflow()
            return True

        sess = self._sessions.get(session_id)
        if sess is None:
            status = status_for_event(event, message, None) or Status.AVAILABLE
            twin = self._sessions.pop(f"proc-{pid}", None) if pid else None
            self._sessions[session_id] = Session(
                session_id=session_id,
                cwd=cwd,
                pid=pid,
                status=status,
                slot=twin.slot if twin is not None and twin.slot is not None
                else self._claim_slot_for_real(),
                since=now,
            )
            return True

        sess.cwd = cwd or sess.cwd
        sess.pid = pid if pid > 1 else sess.pid
        new = status_for_event(event, message, sess.status)
        if new is None or new == sess.status:
            return False
        sess.status = new
        sess.since = now
        return True

    def add_scanned(self, pid: int, cwd: str, now: float) -> bool:
        """Register a discovered claude process unless its PID is already tracked."""
        if any(s.pid == pid for s in self._sessions.values()):
            return False
        self._sessions[f"proc-{pid}"] = Session(
            session_id=f"proc-{pid}",
            cwd=cwd,
            pid=pid,
            status=Status.UNKNOWN,
            slot=self._free_slot(),
            since=now,
        )
        return True

    def prune(self, pid_alive: Callable[[int], bool]) -> bool:
        """Remove sessions whose process is dead. True if anything changed."""
        dead = [sid for sid, s in self._sessions.items() if not pid_alive(s.pid)]
        for sid in dead:
            del self._sessions[sid]
        promoted = self._promote_overflow()
        return bool(dead) or promoted

    def _claim_slot_for_real(self) -> int | None:
        """Free slot, or evict the newest scanned (UNKNOWN) session to overflow."""
        slot = self._free_slot()
        if slot is not None:
            return slot
        unknowns = [s for s in self._sessions.values()
                    if s.status is Status.UNKNOWN and s.slot is not None]
        if not unknowns:
            return None
        victim = max(unknowns, key=lambda s: s.since)
        slot, victim.slot = victim.slot, None
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
            if sess.slot is None:
                slot = self._free_slot()
                if slot is None:
                    break
                sess.slot = slot
                changed = True
        return changed

    def to_dict(self) -> dict:
        return {"sessions": [s.to_dict() for s in self.sessions()]}

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
        reg._normalize_slots()
        return reg

    def _normalize_slots(self) -> None:
        """Clear invalid or duplicate slots (newest keeps a contested slot),
        then promote overflow sessions into any free slots."""
        seen: set[int] = set()
        for sess in sorted(self._sessions.values(), key=lambda s: s.since, reverse=True):
            slot = sess.slot
            if slot is None:
                continue
            valid = isinstance(slot, int) and not isinstance(slot, bool) and 0 <= slot < MAX_SLOTS
            if not valid or slot in seen:
                sess.slot = None
            else:
                seen.add(slot)
        self._promote_overflow()
