from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable

from aioesphomeapi import APIClient

from .config import PadConfig

_LOGGER = logging.getLogger(__name__)
SERVICE_NAME = "set_state"
SERVICE_ARGS = {"colors", "lines", "names", "flash"}
MAX_RECONNECT_DELAY = 60.0


class DeepDeckPad:
    """Maintains the connection to the DeepDeck and pushes the display state.

    `show()` always remembers the last state; after every (re)connect the
    complete state is pushed again — never just deltas.
    """

    def __init__(
        self,
        config: PadConfig,
        client_factory: Callable | None = None,
        *,
        on_focus: Callable[[int], None] | None = None,
        on_move: Callable[[int, int], None] | None = None,
        on_action: Callable[[int, int], None] | None = None,
    ):
        self._cfg = config
        self._factory = client_factory or (
            lambda: APIClient(
                config.host,
                config.port,
                password=None,
                noise_psk=config.api_key or None,
            )
        )
        self._client = None
        self._service = None
        self._connected = asyncio.Event()
        self._last: tuple[list[int], list[str], list[str], list[int]] | None = None
        self._on_focus = on_focus
        self._on_move = on_move
        self._on_action = on_action
        self._last_payloads: dict[int, str] = {}

    def _make_on_stop(self, client, stopped: asyncio.Event):
        async def _on_stop(expected_disconnect: bool) -> None:
            if self._client is client:
                # This callback belongs to the live connection: stop pushing
                # to it immediately; run() observes `stopped` and reconnects.
                self._connected.clear()
                self._client = None
                self._service = None
            # A callback from an already-abandoned connection only sets its
            # own (stale) event — it must never touch the live connection.
            stopped.set()
        return _on_stop

    async def run(self) -> None:
        delay = self._cfg.reconnect_delay
        failures = 0
        while True:
            stopped = asyncio.Event()

            client = None
            try:
                client = self._factory()
                await client.connect(
                    login=True,
                    on_stop=self._make_on_stop(client, stopped),
                    log_errors=False,
                )
                entities, services = await client.list_entities_services()
                service = next((s for s in services if s.name == SERVICE_NAME), None)
                if service is None or {a.name for a in getattr(service, "args", [])} != SERVICE_ARGS:
                    raise RuntimeError(
                        f"pad firmware mismatch: service {SERVICE_NAME!r} with args "
                        f"{sorted(SERVICE_ARGS)} not found"
                    )
                self._service = service
                self._client = client
                self._connected.set()
                if self._on_focus is not None or self._on_move is not None or self._on_action is not None:
                    keys = {}
                    for e in entities:
                        oid = getattr(e, "object_id", "")
                        if oid == "focus_request":
                            keys[e.key] = "focus"
                        elif oid == "move_request":
                            keys[e.key] = "move"
                        elif oid == "action_request":
                            keys[e.key] = "action"
                    if keys:
                        subscribed_at = self._loop_time()
                        client.subscribe_states(
                            lambda state: self._handle_state(state, keys, subscribed_at)
                        )
                delay = self._cfg.reconnect_delay
                failures = 0
                await self._push_last()
                await stopped.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                log = _LOGGER.warning if failures == 1 else _LOGGER.debug
                log("pad connection failed: %s", exc)
            finally:
                self._connected.clear()
                self._client = None
                self._service = None
                if client is not None:
                    try:
                        await client.disconnect(force=True)
                    except Exception:
                        pass
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY)

    @staticmethod
    def _loop_time() -> float:
        return asyncio.get_event_loop().time()

    def _handle_state(self, state, keys: dict, subscribed_at: float) -> None:
        kind = keys.get(getattr(state, "key", None))
        if kind is None:
            return
        payload = getattr(state, "state", "")
        if not payload or payload == self._last_payloads.get(state.key):
            return
        self._last_payloads[state.key] = payload
        # subscribe_states replays current states on every (re)connect; a
        # replayed press must never fire again minutes later.
        if self._loop_time() - subscribed_at < 1.0:
            return
        parts = payload.split(":")
        try:
            if kind == "focus" and self._on_focus is not None:
                slot = int(parts[0])
                if 0 <= slot < 16:
                    self._on_focus(slot)
            elif kind == "move" and self._on_move is not None:
                src, dst = int(parts[0]), int(parts[1])
                if 0 <= src < 16 and 0 <= dst < 16:
                    self._on_move(src, dst)
            elif kind == "action" and self._on_action is not None:
                slot, option = int(parts[0]), int(parts[1])
                if 0 <= slot < 16 and 0 <= option < 8:
                    self._on_action(slot, option)
        except (ValueError, IndexError):
            return

    async def wait_connected(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def show(
        self, colors: list[int], lines: list[str], names: list[str], flash: list[int]
    ) -> None:
        self._last = (list(colors), list(lines), list(names), list(flash))
        await self._push_last()

    async def _push_last(self) -> None:
        if self._client is None or self._service is None or self._last is None:
            return
        try:
            result = self._client.execute_service(
                self._service,
                {
                    "colors": self._last[0],
                    "lines": self._last[1],
                    "names": self._last[2],
                    "flash": self._last[3],
                },
            )
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            _LOGGER.warning("pad push failed: %s", exc)
