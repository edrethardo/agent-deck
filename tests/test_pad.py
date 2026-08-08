import asyncio
from types import SimpleNamespace

import pytest

from agent_monitor.config import PadConfig
from agent_monitor.pad import DeepDeckPad


class FakeClient:
    def __init__(self):
        self.calls = []
        self.on_stop = None
        self.disconnected = False

    async def connect(self, login=False, on_stop=None):
        self.on_stop = on_stop

    async def list_entities_services(self):
        return [], [SimpleNamespace(name="other"), SimpleNamespace(name="set_state")]

    def execute_service(self, service, data):
        assert service.name == "set_state"
        self.calls.append(data)

    async def disconnect(self):
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
    await pad.show([1, 2, 3], ["line"])
    assert created[0].calls[-1] == {"colors": [1, 2, 3], "lines": ["line"]}
    task.cancel()


async def test_state_before_connect_is_pushed_on_connect(fakes):
    created, factory = fakes
    pad = DeepDeckPad(_cfg(), client_factory=factory)
    await pad.show([9], ["a"])
    task = asyncio.create_task(pad.run())
    assert await pad.wait_connected(1)
    await asyncio.sleep(0.05)
    assert created[0].calls == [{"colors": [9], "lines": ["a"]}]
    task.cancel()


async def test_reconnect_pushes_full_state_again(fakes):
    created, factory = fakes
    pad = DeepDeckPad(_cfg(), client_factory=factory)
    task = asyncio.create_task(pad.run())
    assert await pad.wait_connected(1)
    await pad.show([5], ["x"])
    await created[0].on_stop(False)  # simulate connection loss
    await asyncio.sleep(0.1)
    assert len(created) >= 2
    assert created[1].calls == [{"colors": [5], "lines": ["x"]}]
    task.cancel()


async def test_show_without_connection_does_not_raise(fakes):
    _, factory = fakes
    pad = DeepDeckPad(_cfg(), client_factory=factory)
    await pad.show([1], [])  # run() not started — must not raise
