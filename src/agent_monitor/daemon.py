from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from .render import led_colors, oled_lines
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
    ):
        self._registry = registry
        self._pad = pad
        self._state_path = state_path
        self._socket_path = socket_path
        self._time_fn = time_fn
        self._pid_alive = pid_alive
        self._prune_interval = prune_interval
        self.ready = asyncio.Event()

    async def run(self) -> None:
        self._load_state()
        self._registry.prune(self._pid_alive)
        await self._refresh()

        self._socket_path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path)
        )
        tasks = [asyncio.create_task(self._prune_loop())]
        if self._pad is not None:
            pad_task = asyncio.create_task(self._pad.run())
            pad_task.add_done_callback(_log_if_died)
            tasks.append(pad_task)
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
        # key across a daemon restart.
        try:
            data = json.loads(self._state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        self._registry = SessionRegistry.from_dict(data)

    async def _handle_client(self, reader, writer) -> None:
        try:
            while line := await reader.readline():
                await self.handle_line(line)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
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
        sessions = self._registry.sessions()
        payload = {
            "updated": self._time_fn(),
            "sessions": [s.to_dict() for s in sessions],
        }
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, self._state_path)
        if self._pad is not None:
            await self._pad.show(led_colors(sessions), oled_lines(sessions))

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(self._prune_interval)
            if self._registry.prune(self._pid_alive):
                await self._refresh()


def _log_if_died(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        _LOGGER.error("background task died: %r", task.exception())
