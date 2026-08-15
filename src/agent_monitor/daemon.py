from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import socket
import time
from collections.abc import Callable
from pathlib import Path

from .context import ContextInfo, UsageLimit
from .render import flash_flags, key_names, led_colors, overlay_info, usage_lines, usage_percents
from .state import SessionRegistry

_LOGGER = logging.getLogger(__name__)


def default_pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Daemon:
    def __init__(
        self,
        registry: SessionRegistry,
        pad,  # DeepDeckPad | None
        state_path: Path,
        socket_path: Path,
        *,
        time_fn: Callable[[], float] = time.time,
        pid_alive: Callable[[int], bool] = default_pid_alive,
        prune_interval: float = 15.0,
        scan_fn: Callable[[], dict[int, str]] | None = None,
        scan_interval: float = 20.0,
        rc_fn: Callable[[int], bool] | None = None,
        rc_interval: float = 5.0,
        ctx_fn: Callable[[int], ContextInfo | None] | None = None,
        usage_fn: Callable[[], list[UsageLimit]] | None = None,
        ctx_interval: float = 10.0,
        remote_fn: Callable[[], dict[str, list[dict]]] | None = None,
        remote_interval: float = 30.0,
        # The firmware keeps a key's overlay up for 3 s after a press, which is the
        # whole window a note has to be read in.
        note_seconds: float = 3.0,
    ):
        self._registry = registry
        self._pad = pad
        self._state_path = state_path
        self._socket_path = socket_path
        self._time_fn = time_fn
        self._pid_alive = pid_alive
        self._prune_interval = prune_interval
        self._scan_fn = scan_fn
        self._scan_interval = scan_interval
        self._rc_fn = rc_fn
        self._rc_interval = rc_interval
        self._ctx_fn = ctx_fn
        self._usage_fn = usage_fn
        self._ctx_interval = ctx_interval
        self._remote_fn = remote_fn
        self._remote_interval = remote_interval
        self._usage: list[UsageLimit] = []
        self._note_seconds = note_seconds
        self._notes: dict[int, str] = {}
        self._refresh_lock = asyncio.Lock()
        self.ready = asyncio.Event()

    def focus_slot(self, slot: int) -> None:
        """Fire-and-forget: focus the window of the session on `slot`."""
        for sess in self._registry.sessions():
            if sess.slot == slot:
                asyncio.get_event_loop().create_task(self._focus(sess.cwd, sess.slot))
                return

    def move_slot(self, src: int, dst: int) -> None:
        """Swap the sessions shown on keys src and dst (from a pad request)."""
        if self._registry.swap_slots(src, dst):
            asyncio.get_event_loop().create_task(self._refresh())

    def action_slot(self, slot: int, option: int) -> None:
        """Run a pad menu action for the session on `slot`."""
        for sess in self._registry.sessions():
            if sess.slot == slot:
                asyncio.get_event_loop().create_task(self._run_action(sess, option))
                return

    async def _focus(self, cwd: str, slot: int | None = None) -> None:
        from .focus import focus_window
        ok = await asyncio.to_thread(focus_window, cwd)
        _LOGGER.info("focus request for %s -> %s", cwd, "ok" if ok else "no match")
        if not ok:
            # Otherwise a double-press on a session with no window looks like a dead pad.
            await self._note(slot, "no window")

    async def _note(self, slot: int | None, text: str) -> None:
        """Put a line on the key's OLED overlay, then take it away again."""
        if slot is None:
            return
        self._notes[slot] = text
        await self._refresh()

        async def _expire() -> None:
            await asyncio.sleep(self._note_seconds)
            if self._notes.get(slot) == text:  # a newer note wins
                del self._notes[slot]
                await self._refresh()

        asyncio.get_event_loop().create_task(_expire())

    async def _run_action(self, sess, option: int) -> None:
        from . import actions
        if option == 3:                       # hide: display-only, never blocked
            slot = sess.slot
            if self._registry.hide_slot(slot, self._time_fn()):
                await self._note(slot, "hidden")   # its refresh pushes the freed key too
            return
        if option == 4:                       # end: stop the process itself
            if sess.host:
                _LOGGER.info("refusing to end remote session on %s", sess.host)
                await self._note(sess.slot, "local only")
                return
            ok = await asyncio.to_thread(actions.end_session, sess.pid)
            await self._note(sess.slot, "ending…" if ok else "failed")
            return
        fn = {0: actions.restart_session,
              1: actions.toggle_remote_control,
              2: actions.compact_session}.get(option)
        if fn is None:
            return
        if await asyncio.to_thread(actions.display_locked):
            # Refuse before touching the desktop AND say so on the pad — the
            # firmware's confirmation toast alone would claim success.
            _LOGGER.warning("pad action refused: desktop is locked")
            await self._note(sess.slot, "locked")
            return
        ok = await asyncio.to_thread(fn, sess.cwd)
        _LOGGER.info("pad action %s for %s -> %s", fn.__name__, sess.cwd, "ok" if ok else "failed")
        if ok and option == 1 and sess.slot is not None:
            # We initiated this toggle, so we KNOW the new state — detection
            # cannot see in-process off-toggles (the relay socket lingers).
            self._registry.set_remote_manual(sess.slot, not sess.remote)
        if ok:
            note = {0: "reloading", 2: "compacting"}.get(
                option, f"remote {'on' if sess.remote else 'off'}")
        else:
            note = "failed"
        await self._note(sess.slot, note)

    async def run(self) -> None:
        if self._socket_path.exists():
            if _socket_in_use(self._socket_path):
                raise RuntimeError(
                    f"another daemon is already listening on {self._socket_path}"
                )
            self._socket_path.unlink()

        self._load_state()
        self._registry.prune(self._pid_alive)
        await self._refresh()

        server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path)
        )
        tasks = [asyncio.create_task(self._prune_loop())]
        if self._pad is not None:
            tasks.append(asyncio.create_task(self._pad.run()))
        if self._scan_fn is not None:
            tasks.append(asyncio.create_task(self._scan_loop()))
        if self._rc_fn is not None:
            tasks.append(asyncio.create_task(self._rc_loop()))
        if self._ctx_fn is not None or self._usage_fn is not None:
            tasks.append(asyncio.create_task(self._ctx_loop()))
        if self._remote_fn is not None:
            tasks.append(asyncio.create_task(self._remote_loop()))
        for task in tasks:
            task.add_done_callback(_log_if_died)
        self.ready.set()
        try:
            async with server:
                await server.serve_forever()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _load_state(self) -> None:
        # Adopt the complete snapshot including slots — sessions keep their
        # key across a daemon restart. The snapshot is disposable: no parse
        # failure may ever be fatal.
        try:
            data = json.loads(self._state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            _LOGGER.warning("ignoring malformed state snapshot")
            return
        self._registry = SessionRegistry.from_dict(data)

    async def _handle_client(self, reader, writer) -> None:
        try:
            while line := await reader.readline():
                try:
                    await self.handle_line(line)
                except Exception:
                    _LOGGER.exception("failed to process hook event")
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass  # ValueError: line exceeded the stream limit — drop the connection
        finally:
            writer.close()

    async def handle_line(self, line: bytes) -> None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return
        event = data.get("event")
        session_id = data.get("session_id")
        if not event or not session_id:
            return
        try:
            pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if event == "Notification":
            # Notification texts drive the red state and vary by client —
            # keep them observable for diagnosing filter misses.
            _LOGGER.info("notification for %s: %r", session_id[:8], data.get("message"))
        elif event in ("PermissionRequest", "PermissionDenied"):
            # The red path that works in VS Code — keep arrivals observable.
            _LOGGER.info("%s for %s", event, session_id[:8])
        if event == "SessionStart":
            # A reloaded session announces itself before the periodic prune
            # notices its predecessor died — sweep first so the newcomer
            # inherits the freed key instead of drifting to a new one.
            self._registry.prune(self._pid_alive)
        changed = self._registry.apply_event(
            event=event,
            session_id=session_id,
            cwd=data.get("cwd") or "",
            pid=pid,
            message=data.get("message"),
            now=self._time_fn(),
        )
        if changed:
            await self._refresh()

    async def _refresh(self) -> None:
        # The state write is atomic (tmp + rename) and, with aioesphomeapi 45,
        # pad.show() never suspends. The lock keeps refreshes serialized even
        # if a future library version introduces an await point.
        async with self._refresh_lock:
            sessions = self._registry.sessions()
            payload = {
                "updated": self._time_fn(),
                "sessions": [s.to_dict() for s in sessions],
                "usage": [dataclasses.asdict(lim) for lim in self._usage],
            }
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, self._state_path)
            if self._pad is not None:
                await self._pad.show(
                    led_colors(sessions),
                    usage_lines(self._usage),
                    key_names(sessions),
                    flash_flags(sessions),
                    overlay_info(sessions, self._notes),
                    usage_percents(self._usage),
                )

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(self._prune_interval)
            try:
                changed = self._registry.prune(self._pid_alive)
                changed |= self._registry.decay_finished(self._time_fn())
                if changed:
                    await self._refresh()
            except Exception:
                _LOGGER.exception("prune tick failed")

    async def _scan_loop(self) -> None:
        while True:
            try:
                changed = False
                for pid, cwd in self._scan_fn().items():
                    changed |= self._registry.add_scanned(pid, cwd, self._time_fn())
                if changed:
                    await self._refresh()
            except Exception:
                _LOGGER.exception("scan tick failed")
            await asyncio.sleep(self._scan_interval)

    async def _ctx_loop(self) -> None:
        # Context %, model and account usage all come from files Claude Code
        # rewrites continuously — cheap reads, so a fast-ish cadence is fine.
        while True:
            try:
                changed = False
                if self._ctx_fn is not None:
                    infos = {
                        s.pid: await asyncio.to_thread(self._ctx_fn, s.pid)
                        for s in self._registry.sessions()
                        if s.pid > 1
                    }
                    changed |= self._registry.update_context(infos, self._time_fn())
                if self._usage_fn is not None:
                    usage = await asyncio.to_thread(self._usage_fn)
                    if usage != self._usage:
                        self._usage = usage
                        changed = True
                if changed:
                    await self._refresh()
            except Exception:
                _LOGGER.exception("context tick failed")
            await asyncio.sleep(self._ctx_interval)

    async def _remote_loop(self) -> None:
        # Sessions on other machines (VS Code Remote-SSH). Only hosts that
        # answered are synced: an unreachable host keeps its last known keys
        # instead of having them blink out.
        while True:
            try:
                results = await asyncio.to_thread(self._remote_fn)
                changed = False
                for host, entries in (results or {}).items():
                    changed |= self._registry.sync_remote(host, entries, self._time_fn())
                if changed:
                    await self._refresh()
            except Exception:
                _LOGGER.exception("remote probe tick failed")
            await asyncio.sleep(self._remote_interval)

    async def _rc_loop(self) -> None:
        # Own fast cadence: remote-control toggles should show within seconds,
        # not within the (slower) process-scan tick.
        while True:
            try:
                if self._registry.update_remote_flags(self._rc_fn):
                    await self._refresh()
            except Exception:
                _LOGGER.exception("rc tick failed")
            await asyncio.sleep(self._rc_interval)


def _log_if_died(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        _LOGGER.error("background task died: %r", task.exception())


def _socket_in_use(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()
