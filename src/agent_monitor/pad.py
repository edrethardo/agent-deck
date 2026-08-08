from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable

from aioesphomeapi import APIClient

from .config import PadConfig

_LOGGER = logging.getLogger(__name__)
SERVICE_NAME = "set_state"


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
        self._last: tuple[list[int], list[str]] | None = None

    async def run(self) -> None:
        while True:
            stopped = asyncio.Event()

            async def _on_stop(expected_disconnect: bool) -> None:
                stopped.set()

            client = self._factory()
            try:
                await client.connect(login=True, on_stop=_on_stop)
                _, services = await client.list_entities_services()
                self._service = next(
                    (s for s in services if s.name == SERVICE_NAME), None
                )
                if self._service is None:
                    raise RuntimeError(f"service {SERVICE_NAME!r} missing on the pad")
                self._client = client
                self._connected.set()
                await self._push_last()
                await stopped.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.warning("pad connection failed: %s", exc)
            finally:
                self._connected.clear()
                self._client = None
                try:
                    await client.disconnect()
                except Exception:
                    pass
            await asyncio.sleep(self._cfg.reconnect_delay)

    async def wait_connected(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def show(self, colors: list[int], lines: list[str]) -> None:
        self._last = (list(colors), list(lines))
        await self._push_last()

    async def _push_last(self) -> None:
        if self._client is None or self._service is None or self._last is None:
            return
        try:
            result = self._client.execute_service(
                self._service, {"colors": self._last[0], "lines": self._last[1]}
            )
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            _LOGGER.warning("pad push failed: %s", exc)
