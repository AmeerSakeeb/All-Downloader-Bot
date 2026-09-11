"""Loopback-only validating HTTP/HTTPS CONNECT proxy for subprocess traffic."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from urllib.parse import urlsplit

from security.ssrf import SSRFGuard

_MAX_HEADER = 64 * 1024


class ControlledOutboundProxy:
    """Pins each connection to a validated public IP, including redirects/reconnects."""

    def __init__(self, guard: type[SSRFGuard] = SSRFGuard):
        self.guard = guard
        self._server: asyncio.AbstractServer | None = None
        self.url: str | None = None

    async def start(self) -> str:
        if self._server is None:
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
            port = self._server.sockets[0].getsockname()[1]
            self.url = f"http://127.0.0.1:{port}"
        assert self.url is not None
        return self.url

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            self.url = None

    async def __aenter__(self) -> "ControlledOutboundProxy":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        remote_writer: asyncio.StreamWriter | None = None
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
            if len(header) > _MAX_HEADER:
                raise ValueError("request headers too large")
            first, *header_lines = header.split(b"\r\n")
            method_raw, target_raw, version = first.split(b" ", 2)
            method = method_raw.decode("ascii", errors="strict").upper()
            target = target_raw.decode("ascii", errors="strict")
            if method == "CONNECT":
                parsed = urlsplit(f"//{target}")
                hostname = parsed.hostname
                port = parsed.port or 443
                if not hostname or port not in {80, 443}:
                    raise ValueError("CONNECT destination is forbidden")
                ips = await asyncio.to_thread(
                    self.guard.validate_url, f"https://{hostname}:{port}/"
                )
                remote_reader, remote_writer = await self._connect_pinned(ips, port)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await self._relay_both(reader, writer, remote_reader, remote_writer)
                return

            parsed = urlsplit(target)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("proxy requires an absolute HTTP URL")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            ips = await asyncio.to_thread(self.guard.validate_url, target)
            remote_reader, connected_writer = await self._connect_pinned(ips, port)
            remote_writer = connected_writer
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
            outbound = b" ".join((method_raw, path.encode("ascii"), version)) + b"\r\n"
            filtered = [
                line for line in header_lines
                if not line.lower().startswith((b"proxy-authorization:", b"proxy-connection:"))
            ]
            connected_writer.write(outbound + b"\r\n".join(filtered))
            await connected_writer.drain()
            await self._relay_both(reader, writer, remote_reader, connected_writer)
        except (Exception, asyncio.CancelledError):
            if not writer.is_closing():
                with suppress(Exception):
                    writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                    await writer.drain()
        finally:
            if remote_writer:
                remote_writer.close()
                with suppress(Exception):
                    await remote_writer.wait_closed()
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    async def _connect_pinned(ips, port: int):
        last_error: Exception | None = None
        for address in ips:
            try:
                return await asyncio.open_connection(str(address), port)
            except OSError as error:
                last_error = error
        raise ConnectionError("No validated destination address was reachable") from last_error

    @staticmethod
    async def _relay_both(client_reader, client_writer, remote_reader, remote_writer) -> None:
        async def pump(source, destination):
            while data := await source.read(64 * 1024):
                destination.write(data)
                await destination.drain()
            with suppress(Exception):
                destination.write_eof()

        tasks = [
            asyncio.create_task(pump(client_reader, remote_writer)),
            asyncio.create_task(pump(remote_reader, client_writer)),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
