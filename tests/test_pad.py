import asyncio
from types import SimpleNamespace

import pytest

from agent_monitor.config import PadConfig
from agent_monitor.pad import DeepDeckPad


class FakeClient:
    def __init__(self, connect_error=None):
        self.calls = []
        self.on_stop = None
        self.disconnected = False
        self.connect_error = connect_error

    async def connect(self, login=False, on_stop=None, log_errors=True):
        if self.connect_error is not None:
            raise self.connect_error
        self.on_stop = on_stop

    async def list_entities_services(self):
        return [], [
            SimpleNamespace(name="other", args=[]),
            SimpleNamespace(
                name="set_state",
                args=[
                    SimpleNamespace(name="colors"),
                    SimpleNamespace(name="lines"),
                    SimpleNamespace(name="names"),
                ],
            ),
        ]

    async def execute_service(self, service, data):
        assert service.name == "set_state"
        self.calls.append(data)

    async def disconnect(self, force=False):
        self.disconnected = True


def _cfg():
    return PadConfig(host="test.local", reconnect_delay=0.01)


@pytest.fixture
def fakes():
    created = []

    def factory():
        client = FakeClient()
        created.append(client)
        return client

    return created, factory


async def test_show_after_connect_pushes_state(fakes):
    created, factory = fakes
    pad = DeepDeckPad(_cfg(), client_factory=factory)
    task = asyncio.create_task(pad.run())
    assert await pad.wait_connected(1)
    await pad.show([1, 2, 3], ["line"], ["n"])
    assert created[0].calls[-1] == {"colors": [1, 2, 3], "lines": ["line"], "names": ["n"]}
    task.cancel()


async def test_state_before_connect_is_pushed_on_connect(fakes):
    created, factory = fakes
    pad = DeepDeckPad(_cfg(), client_factory=factory)
    await pad.show([9], ["a"], [])
    task = asyncio.create_task(pad.run())
    assert await pad.wait_connected(1)
    await asyncio.sleep(0.05)
    assert created[0].calls == [{"colors": [9], "lines": ["a"], "names": []}]
    task.cancel()


async def test_reconnect_pushes_full_state_again(fakes):
    created, factory = fakes
    pad = DeepDeckPad(_cfg(), client_factory=factory)
    task = asyncio.create_task(pad.run())
    assert await pad.wait_connected(1)
    await pad.show([5], ["x"], [])
    await created[0].on_stop(False)  # simulate connection loss
    await asyncio.sleep(0.1)
    assert len(created) >= 2
    assert created[1].calls == [{"colors": [5], "lines": ["x"], "names": []}]
    task.cancel()


async def test_show_without_connection_does_not_raise(fakes):
    _, factory = fakes
    pad = DeepDeckPad(_cfg(), client_factory=factory)
    await pad.show([1], [], [])  # run() not started — must not raise


async def test_connect_failure_then_recovery(fakes):
    created, factory = fakes
    attempts = {"n": 0}

    def flaky_factory():
        attempts["n"] += 1
        if attempts["n"] == 1:
            return FakeClient(connect_error=ConnectionError("boom"))
        return factory()

    pad = DeepDeckPad(_cfg(), client_factory=flaky_factory)
    task = asyncio.create_task(pad.run())
    assert await pad.wait_connected(1)
    assert attempts["n"] >= 2
    task.cancel()


async def test_missing_service_disconnects_and_retries(fakes):
    created, factory = fakes

    class NoServiceClient(FakeClient):
        async def list_entities_services(self):
            return [], [SimpleNamespace(name="other", args=[])]

    bad = NoServiceClient()
    queue = [bad]

    def once_factory():
        return queue.pop(0) if queue else factory()

    pad = DeepDeckPad(_cfg(), client_factory=once_factory)
    task = asyncio.create_task(pad.run())
    assert await pad.wait_connected(1)
    assert bad.disconnected is True
    task.cancel()


async def test_show_never_raises_when_push_fails(fakes):
    class ExplodingClient(FakeClient):
        async def execute_service(self, service, data):
            raise ConnectionError("dead link")

    pad = DeepDeckPad(_cfg(), client_factory=ExplodingClient)
    task = asyncio.create_task(pad.run())
    assert await pad.wait_connected(1)
    await pad.show([1], ["x"], [])  # must not raise
    task.cancel()


async def test_show_during_outage_buffers_and_reconnect_pushes_new_state(fakes):
    created, factory = fakes
    pad = DeepDeckPad(_cfg(), client_factory=factory)
    task = asyncio.create_task(pad.run())
    assert await pad.wait_connected(1)
    await pad.show([1], ["old"], [])
    await created[0].on_stop(False)
    await pad.show([2], ["new"], [])  # pad is down — must buffer, not hit the dead client
    assert created[0].calls[-1] == {"colors": [1], "lines": ["old"], "names": []}
    await asyncio.sleep(0.1)
    assert created[1].calls[-1] == {"colors": [2], "lines": ["new"], "names": []}
    task.cancel()


async def test_cancel_stops_loop_and_disconnects(fakes):
    created, factory = fakes
    pad = DeepDeckPad(_cfg(), client_factory=factory)
    task = asyncio.create_task(pad.run())
    assert await pad.wait_connected(1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert created[0].disconnected is True


async def test_stale_on_stop_does_not_tear_down_new_connection(fakes):
    created, factory = fakes
    pad = DeepDeckPad(_cfg(), client_factory=factory)
    task = asyncio.create_task(pad.run())
    assert await pad.wait_connected(1)
    stale = created[0].on_stop
    await stale(False)        # connection 0 dies for real
    await asyncio.sleep(0.1)  # pad reconnects on client 1
    assert pad._client is created[1]
    await stale(True)         # late duplicate from the dead connection
    assert pad._client is created[1]
    assert pad._connected.is_set()
    task.cancel()
