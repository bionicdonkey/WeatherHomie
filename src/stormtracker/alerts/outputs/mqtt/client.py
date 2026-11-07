"""A minimal MQTT 3.1.1 publisher: connect, publish at QoS 1, wait for the broker's acknowledgement, disconnect.

Publishing is all this needs, so it is implemented directly on asyncio streams instead of adding a client library.
"""
from __future__ import annotations

import asyncio
import ssl
import struct

CONNACK_ERRORS = {1: "unacceptable protocol version", 2: "client id rejected", 3: "server unavailable",
                  4: "bad username or password", 5: "not authorised"}


class MqttError(RuntimeError):
    pass


def _remaining(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _s(text: str | bytes) -> bytes:
    b = text.encode() if isinstance(text, str) else text
    return struct.pack("!H", len(b)) + b


def connect_packet(client_id: str, username: str | None, password: str | None, keepalive: int = 30) -> bytes:
    flags = 0x02                                                       # clean session
    payload = _s(client_id)
    if username is not None:
        flags |= 0x80
        payload += _s(username)
        if password is not None:
            flags |= 0x40
            payload += _s(password)
    body = _s("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive) + payload
    return b"\x10" + _remaining(len(body)) + body


def publish_packet(topic: str, payload: bytes, packet_id: int, retain: bool) -> bytes:
    body = _s(topic) + struct.pack("!H", packet_id) + payload                # QoS 1 carries a packet id
    return bytes([0x30 | (1 << 1) | (1 if retain else 0)]) + _remaining(len(body)) + body


async def read_packet(r: asyncio.StreamReader) -> tuple[int, bytes]:
    first = (await r.readexactly(1))[0]
    n, mult = 0, 1
    for _ in range(4):
        b = (await r.readexactly(1))[0]
        n += (b & 0x7F) * mult
        mult *= 128
        if not b & 0x80:
            break
    return first, await r.readexactly(n) if n else b""


async def publish(host: str, port: int, messages: list[tuple[str, bytes, bool]], *, client_id: str,
                  username: str | None = None, password: str | None = None, tls: bool = False, timeout: float = 10) -> None:
    """Publish ``(topic, payload, retain)`` messages at QoS 1; raises MqttError / OSError / TimeoutError on failure."""
    async def run() -> None:
        r, w = await asyncio.open_connection(host, port, ssl=ssl.create_default_context() if tls else None)
        try:
            w.write(connect_packet(client_id, username, password))
            await w.drain()
            first, body = await read_packet(r)
            if first >> 4 != 2 or len(body) != 2:
                raise MqttError("the server did not answer like an MQTT broker")
            if body[1] != 0:
                raise MqttError(f"broker refused the connection: {CONNACK_ERRORS.get(body[1], f'code {body[1]}')}")
            for i, (topic, payload, retain) in enumerate(messages, start=1):
                w.write(publish_packet(topic, payload, i, retain))
                await w.drain()
                first, body = await read_packet(r)
                if first >> 4 != 4 or struct.unpack("!H", body[:2])[0] != i:
                    raise MqttError("the broker did not acknowledge the message")
            w.write(b"\xe0\x00")                                           # DISCONNECT
            await w.drain()
        finally:
            w.close()
            try:
                await w.wait_closed()
            except Exception:  # noqa: BLE001 - closing a dead socket may raise; nothing to do about it
                pass
    await asyncio.wait_for(run(), timeout)
