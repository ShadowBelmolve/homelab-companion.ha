import asyncio
import socket
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "redondo"
NOTE = {"kind": "deal", "urgent": True, "seconds": 60, "title": "SSD NVMe 2 TB", "body": "R$ 599 na Kabum"}


class FakeCompanion:
    """The firmware's side: a TCP server that takes Home Assistant's connection."""

    def __init__(self) -> None:
        self.links: asyncio.Queue = asyncio.Queue()
        self.port = 0

    async def accepted(self):
        """The (reader, writer) of the next connection Home Assistant opened."""
        return await asyncio.wait_for(self.links.get(), 2)


@pytest.fixture
async def companion(socket_enabled):
    fake = FakeCompanion()
    writers = []

    async def handle(reader, writer):
        writers.append(writer)
        fake.links.put_nowait((reader, writer))

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    fake.port = server.sockets[0].getsockname()[1]
    # No 5 s wait between tries in a test.
    with patch("custom_components.redondo.RETRY_SECONDS", 0.05):
        yield fake
    for writer in writers:
        writer.close()
    server.close()


async def setup(hass: HomeAssistant, port: int, host: str = "127.0.0.1") -> tuple[MockConfigEntry, str]:
    """A loaded entry for the fake at `port` and its sensor's entity id."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=host, title="redondo", data={"host": host, "port": port})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    sensor = er.async_get(hass).async_get_entity_id("binary_sensor", DOMAIN, entry.entry_id)
    return entry, sensor


async def until(hass: HomeAssistant, sensor: str, state: str) -> None:
    async with asyncio.timeout(2):
        while hass.states.get(sensor).state != state:
            await asyncio.sleep(0.01)


async def notify(hass: HomeAssistant, entry: MockConfigEntry, **changes) -> None:
    data = {"config_entry_id": entry.entry_id, **NOTE, **changes}
    await hass.services.async_call(DOMAIN, "notify", data, blocking=True)


async def test_user_step_creates_an_entry_with_the_default_port(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.redondo.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "redondo.local"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "redondo.local"
    assert result["data"] == {"host": "redondo.local", "port": 7010}


async def test_user_step_refuses_a_port_that_does_not_exist(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "redondo.local", "port": 99999})


async def test_same_host_twice_aborts(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="redondo.local").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "Redondo.local"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_sensor_follows_the_connection(hass: HomeAssistant, companion: FakeCompanion) -> None:
    entry, sensor = await setup(hass, companion.port)
    assert sensor == "binary_sensor.redondo_connectivity"
    _, writer = await companion.accepted()
    await until(hass, sensor, STATE_ON)
    # What turns it off within 30 s of a Companion vanishing.
    sock = entry.runtime_data.writer.get_extra_info("socket")
    assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
    options = (socket.TCP_KEEPIDLE, socket.TCP_KEEPINTVL, socket.TCP_KEEPCNT, socket.TCP_USER_TIMEOUT)
    assert [sock.getsockopt(socket.IPPROTO_TCP, o) for o in options] == [10, 5, 4, 30_000]

    writer.close()
    await until(hass, sensor, STATE_OFF)

    await companion.accepted()  # Home Assistant came back by itself
    await until(hass, sensor, STATE_ON)


async def test_sensor_is_off_while_nothing_listens(hass: HomeAssistant, socket_enabled, unused_tcp_port: int) -> None:
    with patch("custom_components.redondo.RETRY_SECONDS", 0.05):
        entry, sensor = await setup(hass, unused_tcp_port)
        await asyncio.sleep(0.2)  # a few refused tries
        assert hass.states.get(sensor).state == STATE_OFF
        with pytest.raises(HomeAssistantError, match="not connected"):
            await notify(hass, entry)


async def test_a_host_that_cannot_be_a_name_is_tried_again(hass: HomeAssistant) -> None:
    # What `open_connection` raises for `a..b`: a ValueError, not an OSError.
    # Patched because the test plugin turns DNS off.
    error = UnicodeError("label empty or too long")
    with (
        patch("custom_components.redondo.RETRY_SECONDS", 0.05),
        patch("custom_components.redondo.asyncio.open_connection", side_effect=error) as connect,
    ):
        _, sensor = await setup(hass, 7010, host="a..b")
        await asyncio.sleep(0.2)
    assert hass.states.get(sensor).state == STATE_OFF
    assert connect.call_count > 1, "the task died on the first try"


async def test_notify_writes_the_line(hass: HomeAssistant, companion: FakeCompanion) -> None:
    entry, sensor = await setup(hass, companion.port)
    reader, _ = await companion.accepted()
    await until(hass, sensor, STATE_ON)

    await notify(hass, entry)
    assert await reader.readline() == "deal|1|60|SSD NVMe 2 TB|R$ 599 na Kabum\n".encode()

    # The email screen: not urgent, no body; a number selector hands a float.
    await hass.services.async_call(
        DOMAIN, "notify", {"config_entry_id": entry.entry_id, "kind": "email", "urgent": False, "seconds": 15.0, "title": "3"}, blocking=True
    )
    assert await reader.readline() == b"email|0|15|3|\n"

    # A line break would be a second notification; the body may hold a '|'.
    await notify(hass, entry, kind="novo", urgent=False, body="a\r\nb|c")
    assert await reader.readline() == b"novo|0|60|SSD NVMe 2 TB|a  b|c\n"


async def test_notify_refuses_what_it_cannot_send(hass: HomeAssistant, companion: FakeCompanion) -> None:
    entry, sensor = await setup(hass, companion.port)
    reader, _ = await companion.accepted()
    await until(hass, sensor, STATE_ON)

    with pytest.raises(ServiceValidationError, match="title"):
        await notify(hass, entry, title="a|b")
    with pytest.raises(ServiceValidationError, match="kind"):
        await notify(hass, entry, kind="a|b")
    with pytest.raises(ServiceValidationError, match="not a Redondo"):
        await notify(hass, entry, config_entry_id="nope")
    for seconds in (0, 2**32):
        with pytest.raises(Exception, match="seconds"):
            await notify(hass, entry, seconds=seconds)

    # Nothing of the refused calls reached the Companion.
    await notify(hass, entry)
    assert (await reader.readline()).startswith(b"deal|1|60|")


async def test_unload_closes_the_connection(hass: HomeAssistant, companion: FakeCompanion) -> None:
    entry, sensor = await setup(hass, companion.port)
    reader, _ = await companion.accepted()
    await until(hass, sensor, STATE_ON)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get(sensor).state == STATE_UNAVAILABLE
    assert await asyncio.wait_for(reader.read(), 2) == b""  # EOF: the socket closed
    with pytest.raises(HomeAssistantError, match="not connected"):
        await notify(hass, entry)
