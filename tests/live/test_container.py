"""End-to-end check in a real HA container: uv run pytest -m live"""

import base64
import os
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from noise.connection import NoiseConnection

from scripts import ha_dev

pytestmark = pytest.mark.live
NAME, PORT = ha_dev.LIVE_TEST_NAME, 28134


@pytest.fixture
def ha(socket_enabled):
    ha_dev.down(NAME)  # leftovers of an aborted run
    try:
        yield ha_dev.up(NAME, PORT)
    finally:
        ha_dev.down(NAME)


def test_notify_reaches_a_companion_on_this_pc(ha):
    base, token = ha
    host = ha_dev.gateway()
    key = os.urandom(32)
    # The Companion's side: a TCP server the container can reach, which does
    # the handshake once per connection (the config flow's, then the entry's).
    with socket.create_server(("0.0.0.0", 0)) as companion:
        companion.settimeout(30)
        port = companion.getsockname()[1]

        def accept():
            link, _ = companion.accept()
            link.settimeout(10)
            wire = link.makefile("rb")
            noise = NoiseConnection.from_name(b"Noise_NNpsk0_25519_ChaChaPoly_SHA256")
            noise.set_as_responder()
            noise.set_psks(key)
            noise.set_prologue(b"homelab-companion/1")
            noise.start_handshake()
            noise.read_message(wire.read(struct.unpack(">H", wire.read(2))[0]))
            reply = noise.write_message(b"live")
            link.sendall(struct.pack(">H", len(reply)) + reply)
            return link, wire, noise

        # The flow blocks on its own handshake: serve it from a thread.
        with ThreadPoolExecutor(1) as pool:
            flow = pool.submit(accept)
            user_input = {"host": host, "port": port, "key": base64.b64encode(key).decode()}
            result = ha_dev.add_config_entry(base, token, "homelab_companion", user_input)
            flow.result()[0].close()
        assert (result["type"], result["title"]) == ("create_entry", "live")
        link, wire, noise = accept()

        sensor = f"{base}/api/states/binary_sensor.live_connectivity"
        deadline = time.monotonic() + 10
        while (state := ha_dev.request("GET", sensor, token=token)[1])["state"] != "on":
            assert time.monotonic() < deadline, state
            time.sleep(0.2)

        note = {"kind": "deal", "urgent": True, "seconds": 60, "title": "SSD NVMe 2 TB", "body": "R$ 599 na Kabum"}
        call = {"config_entry_id": result["result"]["entry_id"], **note}
        status, body = ha_dev.request("POST", f"{base}/api/services/homelab_companion/notify", json=call, token=token)
        assert status == 200, body
        message = wire.read(struct.unpack(">H", wire.read(2))[0])
        assert b"SSD" not in message, "plain text on the wire"
        assert noise.decrypt(message) == "deal|1|60|SSD NVMe 2 TB|R$ 599 na Kabum\n".encode()
        link.close()
