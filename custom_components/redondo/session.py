"""The encrypted connection to a Companion.

`Noise_NNpsk0_25519_ChaChaPoly_SHA256`, the pattern of ESPHome's native API:
Home Assistant is the initiator, the key typed in the config flow is the
pre-shared key, every Noise message travels after its length (2 bytes, big
endian). The Companion's handshake reply carries its name. The wire is
specified in the firmware repo: apps/companion/SPEC.md.
"""

import asyncio
import base64
import binascii
import socket
import struct

from noise.connection import NoiseConnection

PATTERN = b"Noise_NNpsk0_25519_ChaChaPoly_SHA256"
# Binds the handshake to this protocol and its version.
PROLOGUE = b"redondo/1"
# The most bytes of a message after its length; the Companion ends the
# connection on a longer one. 16 of them are the cipher's tag.
MESSAGE_MAX = 1024
TEXT_MAX = MESSAGE_MAX - 16
CONNECT_TIMEOUT = 10
# Kernel keepalive: a Companion that vanished without closing is noticed
# within idle + interval * probes = 30 s. It needs no heartbeat line.
# TCP_USER_TIMEOUT (in ms) covers what keepalive does not: a line sent and
# never acknowledged, which would otherwise be retried for some 15 minutes.
KEEPALIVE = {socket.TCP_KEEPIDLE: 10, socket.TCP_KEEPINTVL: 5, socket.TCP_KEEPCNT: 4, socket.TCP_USER_TIMEOUT: 30_000}


class Refused(Exception):
    """The other side ended the handshake: a wrong key, or not a Companion."""


def parse_key(text: str) -> bytes:
    """The 32 bytes of a key written in base64; ValueError if it is not one."""
    try:
        key = base64.b64decode(text.strip(), validate=True)
    except binascii.Error as err:
        raise ValueError("not base64") from err
    if len(key) != 32:
        raise ValueError("not 32 bytes")
    return key


class Session:
    """An open connection whose handshake is done."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, noise: NoiseConnection, name: str) -> None:
        self.reader = reader
        self.writer = writer
        self._noise = noise
        # What the Companion calls itself.
        self.name = name

    async def send(self, text: bytes) -> None:
        """Encrypts `text`, in as many messages as it takes, and sends it."""
        for at in range(0, len(text), TEXT_MAX):
            message = self._noise.encrypt(text[at : at + TEXT_MAX])
            self.writer.write(struct.pack(">H", len(message)) + message)
        await self.writer.drain()


async def connect(host: str, port: int, key: bytes) -> Session:
    """Connects and does the handshake, both within CONNECT_TIMEOUT.

    OSError (the timeout included) or ValueError (a host that cannot be a
    name): nothing answered. Refused: something did, without this key.
    """
    async with asyncio.timeout(CONNECT_TIMEOUT):
        reader, writer = await asyncio.open_connection(host, port)
        try:
            sock = writer.get_extra_info("socket")
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for option, value in KEEPALIVE.items():
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
            noise = NoiseConnection.from_name(PATTERN)
            noise.set_as_initiator()
            noise.set_psks(key)
            noise.set_prologue(PROLOGUE)
            noise.start_handshake()
            first = noise.write_message()
            writer.write(struct.pack(">H", len(first)) + first)
            await writer.drain()
            try:
                (length,) = struct.unpack(">H", await reader.readexactly(2))
                if length > MESSAGE_MAX:
                    raise Refused
                name = bytes(noise.read_message(await reader.readexactly(length))).decode()
            # A Companion with another key closes without a word (EOF or a
            # reset); anything else answers something the cipher refuses.
            except Exception as err:
                raise Refused from err
            return Session(reader, writer, noise, name)
        except BaseException:
            writer.close()
            raise
