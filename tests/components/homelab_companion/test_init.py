import asyncio
import base64
import json
import socket
import struct
from datetime import datetime
from ipaddress import ip_address
from unittest.mock import patch

import pytest
import pytest_socket
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from noise.connection import NoiseConnection
from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "homelab_companion"
NOTE = {"kind": "deal", "urgent": True, "seconds": 60, "title": "SSD NVMe 2 TB", "body": "R$ 599 na Kabum"}


KEY = "BwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwc="
OTHER_KEY = "CAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg="


class Peer:
    """The Companion's end of one connection whose handshake is done."""

    def __init__(self, reader, writer, noise) -> None:
        self.reader, self.writer, self.noise = reader, writer, noise
        self.text = b""

    async def readline(self) -> bytes:
        """The next line Home Assistant sent, decrypted."""
        async with asyncio.timeout(2):
            while b"\n" not in self.text:
                (length,) = struct.unpack(">H", await self.reader.readexactly(2))
                assert length <= 1024, "a message the Companion would refuse"
                self.text += self.noise.decrypt(await self.reader.readexactly(length))
        line, _, self.text = self.text.partition(b"\n")
        return line + b"\n"

    def send(self, *messages: dict | bytes) -> None:
        """Sends the Companion's lines (a dict as its JSON), all in one Noise message."""
        text = b"".join(m if isinstance(m, bytes) else json.dumps(m).encode() + b"\n" for m in messages)
        sealed = self.noise.encrypt(text)
        self.writer.write(struct.pack(">H", len(sealed)) + sealed)


class FakeCompanion:
    """The firmware's side: a TCP server that takes Home Assistant's connection."""

    def __init__(self) -> None:
        self.links: asyncio.Queue = asyncio.Queue()
        self.port = 0
        self.name = "mesa"
        # Connections ended as the firmware ends one with another key.
        self.refused = 0

    async def accepted(self) -> Peer:
        """The next connection Home Assistant opened and keyed."""
        return await asyncio.wait_for(self.links.get(), 2)


@pytest.fixture
async def companion(socket_enabled):
    fake = FakeCompanion()
    writers = []

    async def handle(reader, writer):
        writers.append(writer)
        noise = NoiseConnection.from_name(b"Noise_NNpsk0_25519_ChaChaPoly_SHA256")
        noise.set_as_responder()
        noise.set_psks(base64.b64decode(KEY))
        noise.set_prologue(b"homelab-companion/2")
        noise.start_handshake()
        try:
            (length,) = struct.unpack(">H", await reader.readexactly(2))
            noise.read_message(await reader.readexactly(length))
        except Exception:  # noqa: BLE001 - a wrong key, or the client left
            fake.refused += 1
            writer.close()
            return
        reply = noise.write_message(fake.name.encode())
        writer.write(struct.pack(">H", len(reply)) + reply)
        fake.links.put_nowait(Peer(reader, writer, noise))

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    fake.port = server.sockets[0].getsockname()[1]
    # No 5 s wait between tries in a test.
    with patch("custom_components.homelab_companion.RETRY_SECONDS", 0.05):
        yield fake
    for writer in writers:
        writer.close()
    server.close()


async def setup(hass: HomeAssistant, port: int, host: str = "127.0.0.1", key: str = KEY) -> tuple[MockConfigEntry, str]:
    """A loaded entry for the fake at `port` and its sensor's entity id."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="mesa", title="mesa", data={"host": host, "port": port, "key": key})
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


async def flow(hass: HomeAssistant, **user_input) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "127.0.0.1", "key": KEY, **user_input})


async def test_user_step_names_the_entry_after_the_companion(hass: HomeAssistant, companion: FakeCompanion) -> None:
    with patch("custom_components.homelab_companion.async_setup_entry", return_value=True):
        result = await flow(hass, port=companion.port)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (result["title"], result["result"].unique_id) == ("mesa", "mesa")
    assert result["data"] == {"host": "127.0.0.1", "port": companion.port, "key": KEY}


async def test_user_step_says_what_is_wrong(hass: HomeAssistant, companion: FakeCompanion, unused_tcp_port: int) -> None:
    for user_input, errors in [
        ({"port": companion.port, "key": "not a key"}, {"key": "invalid_key"}),
        ({"port": companion.port, "key": KEY[:-4]}, {"key": "invalid_key"}),
        ({"port": unused_tcp_port}, {"base": "cannot_connect"}),
        ({"port": companion.port, "key": OTHER_KEY}, {"base": "invalid_auth"}),
    ]:
        result = await flow(hass, **user_input)
        assert (result["type"], result["errors"]) == (FlowResultType.FORM, errors), user_input
    assert companion.refused == 1
    # The form is still good for the right answer.
    with patch("custom_components.homelab_companion.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "127.0.0.1", "port": companion.port, "key": KEY})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_step_does_not_hang_on_a_peer_that_is_not_a_companion(hass: HomeAssistant, socket_enabled) -> None:
    async def serve(answer: bytes | None) -> asyncio.Server:
        async def handle(reader, writer):
            if answer is not None:
                writer.write(answer)
            await reader.read()  # until Home Assistant gives up
            writer.close()

        return await asyncio.start_server(handle, "127.0.0.1", 0)

    with patch("custom_components.homelab_companion.session.CONNECT_TIMEOUT", 1):
        # Silent; a length above the limit, with the message never coming.
        for answer, error in [(None, "cannot_connect"), (b"\xff\xff", "invalid_auth")]:
            # A flow that waits for ever fails here instead of hanging the run.
            async with await serve(answer) as server, asyncio.timeout(5):
                result = await flow(hass, port=server.sockets[0].getsockname()[1])
            assert (result["type"], result["errors"]) == (FlowResultType.FORM, {"base": error}), answer


async def test_user_step_default_port_is_the_companions(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    defaults = {str(key): key.default() for key in result["data_schema"].schema if key.default is not vol.UNDEFINED}
    assert defaults == {"host": "redondo.local", "port": 7010}


async def test_user_step_refuses_a_port_that_does_not_exist(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "redondo.local", "port": 99999, "key": KEY})


async def test_the_same_companion_again_moves_its_entry(hass: HomeAssistant, companion: FakeCompanion) -> None:
    # Added when it was somewhere else, with another key.
    entry = MockConfigEntry(domain=DOMAIN, unique_id="mesa", data={"host": "192.0.2.1", "port": 7010, "key": OTHER_KEY})
    entry.add_to_hass(hass)
    result = await flow(hass, port=companion.port)
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "already_configured")
    assert entry.data == {"host": "127.0.0.1", "port": companion.port, "key": KEY}


def announced(port: int, name: str = "mesa._hl-companion._tcp.local.") -> ZeroconfServiceInfo:
    """What Home Assistant's zeroconf hands the flow for a board's announcement."""
    ip = ip_address("192.0.2.9")
    # The target and the address are the announcer's word: the flow uses neither.
    return ZeroconfServiceInfo(ip, [ip], port, "elsewhere.local.", "_hl-companion._tcp.local.", name, {})


@pytest.fixture
def mdns(companion):
    """`mesa.local` resolves to the fake Companion."""

    class Resolver:
        def __init__(self, name: str) -> None:
            assert name == "mesa.local."

        async def async_request(self, zc, timeout) -> bool:
            return True

        def ip_addresses_by_version(self, version) -> list[str]:
            return ["127.0.0.1"]

    with patch("custom_components.homelab_companion.AddressResolver", Resolver):
        yield


async def test_a_discovered_companion_asks_for_its_key_only(hass: HomeAssistant, companion: FakeCompanion, mdns) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=announced(companion.port))
    assert (result["type"], result["step_id"], result["description_placeholders"]) == (FlowResultType.FORM, "discovered", {"name": "mesa"})
    assert [str(key) for key in result["data_schema"].schema] == ["key"]

    for key, errors in [("not a key", {"key": "invalid_key"}), (OTHER_KEY, {"base": "invalid_auth"})]:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"key": key})
        assert (result["type"], result["errors"]) == (FlowResultType.FORM, errors)

    with patch("custom_components.homelab_companion.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"key": KEY})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (result["title"], result["result"].unique_id) == ("mesa", "mesa")
    assert result["data"] == {"host": "mesa.local", "port": companion.port, "key": KEY}


async def test_a_waiting_discovery_does_not_stop_adding_it_by_hand(hass: HomeAssistant, companion: FakeCompanion) -> None:
    # A card nobody answered, announced with a port where nothing listens.
    card = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=announced(1))
    assert card["step_id"] == "discovered"
    with patch("custom_components.homelab_companion.async_setup_entry", return_value=True):
        result = await flow(hass, port=companion.port)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert hass.config_entries.flow.async_progress() == [], "the card went with it"


async def test_a_discovered_name_that_another_companion_answers_is_refused(hass: HomeAssistant, companion: FakeCompanion, mdns) -> None:
    companion.name = "sala"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=announced(companion.port))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"key": KEY})
    assert (result["type"], result["errors"]) == (FlowResultType.FORM, {"base": "wrong_device"})


async def test_discovering_a_configured_companion_moves_it_to_its_name(hass: HomeAssistant) -> None:
    # Added by address, with its key.
    entry = MockConfigEntry(domain=DOMAIN, unique_id="mesa", data={"host": "192.0.2.1", "port": 7010, "key": KEY})
    entry.add_to_hass(hass)
    with patch("custom_components.homelab_companion.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=announced(7011))
        await hass.async_block_till_done()
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "already_configured")
    assert entry.data == {"host": "mesa.local", "port": 7011, "key": KEY}


async def test_an_announcement_that_names_no_companion_is_dropped(hass: HomeAssistant) -> None:
    names = ["Mesa", "a.b", "", "-mesa", "mesa-", "a" * 64]
    for name in [f"{name}._hl-companion._tcp.local." for name in names] + ["mesa._other._tcp.local."]:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=announced(7010, name))
        assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "invalid_discovery_info"), name


async def test_sensor_follows_the_connection(hass: HomeAssistant, companion: FakeCompanion) -> None:
    entry, sensor = await setup(hass, companion.port)
    assert sensor == "binary_sensor.mesa_connectivity"
    writer = (await companion.accepted()).writer
    await until(hass, sensor, STATE_ON)
    # What turns it off within 30 s of a Companion vanishing.
    sock = entry.runtime_data.session.writer.get_extra_info("socket")
    assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
    options = (socket.TCP_KEEPIDLE, socket.TCP_KEEPINTVL, socket.TCP_KEEPCNT, socket.TCP_USER_TIMEOUT)
    assert [sock.getsockopt(socket.IPPROTO_TCP, o) for o in options] == [10, 5, 4, 30_000]

    writer.close()
    await until(hass, sensor, STATE_OFF)

    await companion.accepted()  # Home Assistant came back by itself
    await until(hass, sensor, STATE_ON)


async def test_sensor_is_off_while_nothing_listens(hass: HomeAssistant, socket_enabled, unused_tcp_port: int) -> None:
    with patch("custom_components.homelab_companion.RETRY_SECONDS", 0.05):
        entry, sensor = await setup(hass, unused_tcp_port)
        await asyncio.sleep(0.2)  # a few refused tries
        assert hass.states.get(sensor).state == STATE_OFF
        with pytest.raises(HomeAssistantError, match="not connected"):
            await notify(hass, entry)


async def test_a_key_the_companion_refuses_keeps_the_sensor_off(hass: HomeAssistant, companion: FakeCompanion, caplog) -> None:
    entry, sensor = await setup(hass, companion.port, key=OTHER_KEY)
    await asyncio.sleep(0.3)
    assert hass.states.get(sensor).state == STATE_OFF
    assert companion.refused > 1, "it keeps trying"
    assert caplog.text.count("the key was refused") == 1, "said once"
    assert OTHER_KEY not in caplog.text and KEY not in caplog.text
    with pytest.raises(HomeAssistantError, match="not connected"):
        await notify(hass, entry)


async def test_another_companion_at_the_address_is_not_used(hass: HomeAssistant, companion: FakeCompanion, caplog) -> None:
    companion.name = "sala"
    _, sensor = await setup(hass, companion.port)
    await companion.accepted()
    await asyncio.sleep(0.2)
    assert hass.states.get(sensor).state == STATE_OFF
    assert "it is the Companion 'sala', not 'mesa'" in caplog.text


async def test_a_host_that_cannot_be_a_name_is_tried_again(hass: HomeAssistant) -> None:
    # What `open_connection` raises for `a..b`: a ValueError, not an OSError.
    # Patched because the test plugin turns DNS off.
    error = UnicodeError("label empty or too long")
    with (
        patch("custom_components.homelab_companion.RETRY_SECONDS", 0.05),
        patch("custom_components.homelab_companion.session.asyncio.open_connection", side_effect=error) as connect,
    ):
        entry, sensor = await setup(hass, 7010, host="a..b")
        await asyncio.sleep(0.2)
        assert hass.states.get(sensor).state == STATE_OFF
        assert connect.call_count > 1, "the task died on the first try"
        # Before the patches end: a later try would reach the real resolver.
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_local_name_is_resolved_by_mdns_at_every_try(hass: HomeAssistant, companion: FakeCompanion) -> None:
    # The Companion's address changes between tries: nobody listens on the
    # first one, the fake listens on the second.
    answers = iter(["127.0.0.2"])
    asked = []

    class Resolver:
        def __init__(self, name: str) -> None:
            asked.append(name)

        async def async_request(self, zc, timeout) -> bool:
            return True

        def ip_addresses_by_version(self, version) -> list[str]:
            return [next(answers, "127.0.0.1")]

    pytest_socket.socket_allow_hosts(["127.0.0.1", "127.0.0.2"])
    with patch("custom_components.homelab_companion.AddressResolver", Resolver):
        _, sensor = await setup(hass, companion.port, host="Redondo.local")
        await companion.accepted()
        await until(hass, sensor, STATE_ON)
    assert asked == ["redondo.local.", "redondo.local."]


async def test_a_local_name_mdns_does_not_know_goes_to_the_system_resolver(hass: HomeAssistant) -> None:
    class Resolver:
        def __init__(self, name: str) -> None:
            pass

        async def async_request(self, zc, timeout) -> bool:
            return False

    with (
        patch("custom_components.homelab_companion.RETRY_SECONDS", 0.05),
        patch("custom_components.homelab_companion.AddressResolver", Resolver),
        patch("custom_components.homelab_companion.session.asyncio.open_connection", side_effect=OSError) as connect,
    ):
        entry, _ = await setup(hass, 7010, host="redondo.local")
        await asyncio.sleep(0.1)
        # The task ends before the real resolver is back.
        await hass.config_entries.async_unload(entry.entry_id)
    assert connect.call_args.args == ("redondo.local", 7010)


async def test_notify_writes_the_line(hass: HomeAssistant, companion: FakeCompanion) -> None:
    entry, sensor = await setup(hass, companion.port)
    reader = await companion.accepted()
    await until(hass, sensor, STATE_ON)

    await notify(hass, entry, title="Atenção", body="R$ 599 na Kabum")
    line = await reader.readline()
    assert "Atenção".encode() in line, "UTF-8, not escaped to ASCII"
    want = {"t": "notify", "kind": "deal", "urgent": True, "ms": 60_000, "title": "Atenção", "body": "R$ 599 na Kabum"}
    assert json.loads(line) == want

    # The email screen: not urgent, no body; a number selector hands a float.
    await hass.services.async_call(
        DOMAIN, "notify", {"config_entry_id": entry.entry_id, "kind": "email", "urgent": False, "seconds": 15.0, "title": "3"}, blocking=True
    )
    assert json.loads(await reader.readline()) == {"t": "notify", "kind": "email", "urgent": False, "ms": 15_000, "title": "3", "body": ""}

    # Still one line each, whatever the text holds.
    await notify(hass, entry, kind="a|b", title='c|"d"', body="e\r\nf|g")
    got = json.loads(await reader.readline())
    assert (got["kind"], got["title"], got["body"]) == ("a|b", 'c|"d"', "e\r\nf|g")


async def test_notify_refuses_what_it_cannot_send(hass: HomeAssistant, companion: FakeCompanion) -> None:
    entry, sensor = await setup(hass, companion.port)
    reader = await companion.accepted()
    await until(hass, sensor, STATE_ON)

    # A line the Companion would drop: 4 bytes a character fills it.
    with pytest.raises(ServiceValidationError, match="1024"):
        await notify(hass, entry, title="😀" * 80, body="😀" * 200)
    with pytest.raises(ServiceValidationError, match="not a Home Lab Companion"):
        await notify(hass, entry, config_entry_id="nope")
    for seconds in (0, 2**32 // 1000 + 1):
        with pytest.raises(Exception, match="seconds"):
            await notify(hass, entry, seconds=seconds)

    # Nothing of the refused calls reached the Companion.
    await notify(hass, entry)
    assert json.loads(await reader.readline())["title"] == NOTE["title"]


async def test_unload_closes_the_connection(hass: HomeAssistant, companion: FakeCompanion) -> None:
    entry, sensor = await setup(hass, companion.port)
    reader = await companion.accepted()
    await until(hass, sensor, STATE_ON)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get(sensor).state == STATE_UNAVAILABLE
    assert await asyncio.wait_for(reader.reader.read(), 2) == b""  # EOF: the socket closed
    with pytest.raises(HomeAssistantError, match="not connected"):
        await notify(hass, entry)


STATE = {"t": "state", "rssi": -61, "normal": 2, "urgent": 1, "screen": "dim", "heap": 126408, "uptime_ms": 60_000, "version": "70b9728"}


def entity_id(hass: HomeAssistant, entry: MockConfigEntry, platform: str, key: str) -> str:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")


async def value(hass: HomeAssistant, entry: MockConfigEntry, key: str, want: str, platform: str = "sensor") -> None:
    """Waits for the entity of `key` to show `want`."""
    entity = entity_id(hass, entry, platform, key)
    try:
        async with asyncio.timeout(2):
            while hass.states.get(entity).state != want:
                await asyncio.sleep(0.01)
    except TimeoutError:
        pytest.fail(f"{entity} is {hass.states.get(entity).state!r}, not {want!r}")


async def test_sensors_show_the_state_the_companion_sends(hass: HomeAssistant, companion: FakeCompanion) -> None:
    entry, connected = await setup(hass, companion.port)
    peer = await companion.accepted()
    await until(hass, connected, STATE_ON)
    # Connected, but it said nothing yet.
    for key in ("rssi", "normal", "urgent", "screen", "heap", "last_boot"):
        assert hass.states.get(entity_id(hass, entry, "sensor", key)).state == STATE_UNAVAILABLE, key

    peer.send(STATE)
    for key, want in (("rssi", "-61"), ("normal", "2"), ("urgent", "1"), ("screen", "dim"), ("heap", "126408")):
        await value(hass, entry, key, want)
    rssi = hass.states.get(entity_id(hass, entry, "sensor", "rssi"))
    assert rssi.attributes["unit_of_measurement"] == "dBm"
    assert rssi.name == "mesa Wi-Fi signal"
    registry = er.async_get(hass)
    assert registry.async_get(rssi.entity_id).entity_category == "diagnostic"
    assert registry.async_get(entity_id(hass, entry, "sensor", "normal")).entity_category is None
    assert hass.states.get(connected).name == "mesa Connectivity", "the first entity keeps its name and id"

    # The whole state replaces the last one.
    peer.send({**STATE, "normal": 0, "urgent": 0, "screen": "off", "rssi": None})
    await value(hass, entry, "screen", "off")
    await value(hass, entry, "normal", "0")
    await value(hass, entry, "rssi", "unknown")

    # Gone: what it reported is not known any more; the connection sensor says why.
    peer.writer.close()
    await until(hass, connected, STATE_OFF)
    await value(hass, entry, "screen", STATE_UNAVAILABLE)
    # Back: unavailable until it speaks again.
    peer = await companion.accepted()
    await until(hass, connected, STATE_ON)
    assert hass.states.get(entity_id(hass, entry, "sensor", "screen")).state == STATE_UNAVAILABLE
    peer.send(STATE)
    await value(hass, entry, "screen", "dim")


async def test_a_state_of_another_firmware_does_not_break_the_reading(hass: HomeAssistant, companion: FakeCompanion) -> None:
    entry, connected = await setup(hass, companion.port)
    peer = await companion.accepted()
    await until(hass, connected, STATE_ON)
    # Older: fields missing. Newer: fields and values this version does not know.
    peer.send(
        b"not json\n",
        b"[1]\n",
        {"t": "dance", "steps": 3},
        {"t": "state", "normal": 4, "screen": "blinking", "rssi": "strong", "heap": True, "later": {"x": [1]}},
    )
    await value(hass, entry, "normal", "4")
    for key in ("rssi", "urgent", "screen", "heap", "last_boot"):
        assert hass.states.get(entity_id(hass, entry, "sensor", key)).state == "unknown", key
    # A state that cannot be shown (an uptime no clock holds) is dropped, not the connection.
    peer.send({**STATE, "normal": 5, "uptime_ms": 2**63})
    # Nor is a line with no end kept: past the limit it is forgotten, so what
    # follows it is a line of its own.
    peer.send(b"x" * 600)
    peer.send(b"x" * 600)
    peer.send({**STATE, "normal": 6})
    await value(hass, entry, "normal", "6")
    # Still reading, and several lines in one message or one line in two are the same.
    half = json.dumps(STATE).encode() + b"\n"
    peer.send(half[:20])
    peer.send(half[20:], {**STATE, "normal": 7})
    await value(hass, entry, "normal", "7")
    assert hass.states.get(connected).state == STATE_ON


async def test_the_last_boot_does_not_move_with_every_state(hass: HomeAssistant, companion: FakeCompanion) -> None:
    entry, connected = await setup(hass, companion.port)
    peer = await companion.accepted()
    await until(hass, connected, STATE_ON)

    def clock(text: str):
        # Only what the integration reads: a frozen clock would stop the event loop's own timeouts.
        return patch("custom_components.homelab_companion.dt_util.utcnow", return_value=datetime.fromisoformat(text))

    with clock("2026-11-14 12:00:00+00:00"):
        peer.send(STATE)
        await value(hass, entry, "last_boot", "2026-11-14T11:59:00+00:00")
    # Half a minute later the uptime says 2 s less than the clock: the same boot.
    with clock("2026-11-14 12:00:30+00:00"):
        peer.send({**STATE, "uptime_ms": 88_000, "normal": 5})
        await value(hass, entry, "normal", "5")
    assert hass.states.get(entity_id(hass, entry, "sensor", "last_boot")).state == "2026-11-14T11:59:00+00:00"
    # It restarted.
    with clock("2026-11-14 12:10:00+00:00"):
        peer.send({**STATE, "uptime_ms": 5_000})
        await value(hass, entry, "last_boot", "2026-11-14T12:09:55+00:00")


async def test_the_device_shows_the_firmware_version(hass: HomeAssistant, companion: FakeCompanion) -> None:
    entry, connected = await setup(hass, companion.port)
    peer = await companion.accepted()
    await until(hass, connected, STATE_ON)
    device = lambda: dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)[0]  # noqa: E731
    assert device().sw_version is None
    peer.send(STATE)
    await value(hass, entry, "normal", "2")
    assert (device().sw_version, device().name) == ("70b9728", "mesa")
    peer.send({**STATE, "version": "e295594", "normal": 3})
    await value(hass, entry, "normal", "3")
    assert device().sw_version == "e295594"
    assert len(dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)) == 1


async def test_the_event_entity_tells_how_a_notification_left(hass: HomeAssistant, companion: FakeCompanion) -> None:
    entry, connected = await setup(hass, companion.port)
    peer = await companion.accepted()
    await until(hass, connected, STATE_ON)
    entity = entity_id(hass, entry, "event", "notification")
    # Before any state: the Companion may send an event first.
    await value(hass, entry, "notification", "unknown", platform="event")

    async def fired(event_type: str, **attributes) -> None:
        async with asyncio.timeout(2):
            while (state := hass.states.get(entity)).attributes.get("event_type") != event_type:
                await asyncio.sleep(0.01)
        assert {key: state.attributes[key] for key in attributes} == attributes

    peer.send({"t": "event", "event": "dismissed", "kind": "deal", "title": "SSD NVMe 2 TB"})
    await fired("dismissed", kind="deal", title="SSD NVMe 2 TB")
    first = hass.states.get(entity).state
    # One this version does not know is not an event; the next known one is.
    peer.send({"t": "event", "event": "exploded", "kind": "deal", "title": "x"}, {"t": "event", "event": "expired", "kind": "email", "title": "3"})
    await fired("expired", kind="email", title="3")
    assert hass.states.get(entity).state != first, "each event is a new moment"
    assert hass.states.get(entity).attributes["event_types"] == ["dismissed", "expired"]
