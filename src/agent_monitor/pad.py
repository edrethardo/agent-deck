from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable

from aioesphomeapi import APIClient

from .config import PadConfig

_LOGGER = logging.getLogger(__name__)
SERVICE_NAME = "set_state"
SERVICE_ARGS = {"colors", "lines", "names"}
MAX_RECONNECT_DELAY = 60.0


class DeepDeckPad:
    """Maintains the connection to the DeepDeck and pushes the display state.

    `show()` always remembers the last state; after every (re)connect the
    complete state is pushed again — never just deltas.
    """

    def __init__(self, config: PadConfig, client_factory: Callable | None = None):
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
        self._last: tuple[list[int], list[str], list[str]] | None = None

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
                _, services = await client.list_entities_services()
                service = next((s for s in services if s.name == SERVICE_NAME), None)
                if service is None or {a.name for a in getattr(service, "args", [])} != SERVICE_ARGS:
                    raise RuntimeError(
                        f"pad firmware mismatch: service {SERVICE_NAME!r} with args "
                        f"{sorted(SERVICE_ARGS)} not found"
                    )
                self._service = service
                self._client = client
                self._connected.set()
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

    async def wait_connected(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def show(self, colors: list[int], lines: list[str], names: list[str]) -> None:
        self._last = (list(colors), list(lines), list(names))
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
                },
            )
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            _LOGGER.warning("pad push failed: %s", exc)
