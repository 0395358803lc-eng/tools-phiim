from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import sys
from typing import Final

SOCKS_VERSION: Final = 5


async def _read_address(reader: asyncio.StreamReader, atyp: int) -> tuple[bytes, str]:
    if atyp == 1:
        raw = await reader.readexactly(4)
        return raw, str(ipaddress.IPv4Address(raw))
    if atyp == 3:
        length = (await reader.readexactly(1))[0]
        raw_host = await reader.readexactly(length)
        return bytes([length]) + raw_host, raw_host.decode("idna")
    if atyp == 4:
        raw = await reader.readexactly(16)
        return raw, str(ipaddress.IPv6Address(raw))
    raise ValueError("Unsupported SOCKS address type")


async def _read_reply_tail(reader: asyncio.StreamReader, atyp: int) -> bytes:
    if atyp == 1:
        address = await reader.readexactly(4)
    elif atyp == 3:
        length = await reader.readexactly(1)
        address = length + await reader.readexactly(length[0])
    elif atyp == 4:
        address = await reader.readexactly(16)
    else:
        raise ValueError("Unsupported upstream SOCKS reply address type")
    port = await reader.readexactly(2)
    return address + port


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.write_eof()
        except (OSError, RuntimeError):
            pass


async def _handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    config: dict[str, object],
) -> None:
    upstream_writer: asyncio.StreamWriter | None = None
    try:
        header = await client_reader.readexactly(2)
        if header[0] != SOCKS_VERSION:
            raise ValueError("Unsupported SOCKS version")
        methods = await client_reader.readexactly(header[1])
        if 0 not in methods:
            client_writer.write(b"\x05\xff")
            await client_writer.drain()
            return
        client_writer.write(b"\x05\x00")
        await client_writer.drain()

        request = await client_reader.readexactly(4)
        if request[0] != SOCKS_VERSION or request[1] != 1:
            client_writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            await client_writer.drain()
            return
        atyp = request[3]
        encoded_address, _host = await _read_address(client_reader, atyp)
        encoded_port = await client_reader.readexactly(2)

        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(
                str(config["upstream_host"]),
                int(config["upstream_port"]),
            ),
            timeout=12,
        )

        username = str(config.get("username") or "")
        password = str(config.get("password") or "")
        if username:
            upstream_writer.write(b"\x05\x01\x02")
        else:
            upstream_writer.write(b"\x05\x01\x00")
        await upstream_writer.drain()
        method_reply = await upstream_reader.readexactly(2)
        expected_method = 2 if username else 0
        if method_reply != bytes([5, expected_method]):
            raise ConnectionError("Upstream SOCKS authentication method rejected")

        if username:
            user = username.encode("utf-8")
            secret = password.encode("utf-8")
            if len(user) > 255 or len(secret) > 255:
                raise ValueError("SOCKS credentials are too long")
            upstream_writer.write(
                b"\x01" + bytes([len(user)]) + user + bytes([len(secret)]) + secret
            )
            await upstream_writer.drain()
            auth_reply = await upstream_reader.readexactly(2)
            if auth_reply != b"\x01\x00":
                raise ConnectionError("Upstream SOCKS credentials rejected")

        upstream_writer.write(bytes([5, 1, 0, atyp]) + encoded_address + encoded_port)
        await upstream_writer.drain()
        reply = await upstream_reader.readexactly(4)
        tail = await _read_reply_tail(upstream_reader, reply[3])
        client_writer.write(reply + tail)
        await client_writer.drain()
        if reply[1] != 0:
            return

        left = asyncio.create_task(_pipe(client_reader, upstream_writer))
        right = asyncio.create_task(_pipe(upstream_reader, client_writer))
        done, pending = await asyncio.wait(
            {left, right},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
    except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
        try:
            client_writer.write(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
            await client_writer.drain()
        except (ConnectionError, OSError, RuntimeError):
            pass
    finally:
        if upstream_writer is not None:
            upstream_writer.close()
            try:
                await upstream_writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        client_writer.close()
        try:
            await client_writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, required=True)
    args = parser.parse_args()

    raw = sys.stdin.readline()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise SystemExit(2)

    server = await asyncio.start_server(
        lambda reader, writer: _handle_client(reader, writer, payload),
        args.listen_host,
        args.listen_port,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_main())
