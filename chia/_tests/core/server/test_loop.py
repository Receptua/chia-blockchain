from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import random
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Optional

import anyio
import pytest

from chia._tests.core.server import serve
from chia._tests.util.misc import create_logger
from chia.server import chia_policy
from chia.util.task_referencer import create_referenced_task
from chia.util.timing import adjusted_timeout

here = pathlib.Path(__file__).parent

# TODO: CAMPid 0945094189459712842390t591
IP = "127.0.0.1"
PORT = 8444
NUM_CLIENTS = 500


@contextlib.asynccontextmanager
async def serve_in_thread(
    out_path: pathlib.Path, ip: str, port: int, connection_limit: int
) -> AsyncIterator[ServeInThread]:
    server = ServeInThread(out_path=out_path, ip=ip, requested_port=port, connection_limit=connection_limit)
    server.start()
    # TODO: can we check when it has really started?  just make a connection?
    await asyncio.sleep(1)
    try:
        yield server
    finally:
        server.stop()


@dataclass
class Client:
    reader: Optional[asyncio.StreamReader]
    writer: Optional[asyncio.StreamWriter]

    @classmethod
    async def open(cls, ip: str, port: int) -> Client:
        try:
            with anyio.fail_after(delay=1):
                reader, writer = await asyncio.open_connection(ip, port)
                return cls(reader=reader, writer=writer)
        except (TimeoutError, ConnectionResetError, ConnectionRefusedError):
            return cls(reader=None, writer=None)

    @classmethod
    @contextlib.asynccontextmanager
    async def open_several(cls, count: int, ip: str, port: int) -> AsyncIterator[list[Client]]:
        clients: list[Client] = await asyncio.gather(*(cls.open(ip=ip, port=port) for _ in range(count)))
        try:
            yield [*clients]
        finally:
            await asyncio.gather(*(client.close() for client in clients))

    async def is_alive(self) -> bool:
        if self.reader is None or self.writer is None:
            return False
        separator = b"\xff"
        n = 8
        to_send = bytes(random.randrange(255) for _ in range(n))
        try:
            with anyio.fail_after(delay=1):
                self.writer.write(to_send + separator)
                received = await self.reader.readuntil(separator=separator)
                received = received[:-1]
        except TimeoutError:
            return False

        # print(f" ==== {received=} {to_send=}")
        return received == to_send

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()


@dataclass()
class ServeInThread:
    ip: str
    requested_port: int
    out_path: pathlib.Path
    connection_limit: int = 25
    original_connection_limit: Optional[int] = None
    loop: Optional[asyncio.AbstractEventLoop] = None
    server_task: Optional[asyncio.Task[None]] = None
    thread: Optional[threading.Thread] = None
    thread_end_event: threading.Event = field(default_factory=threading.Event)
    port_holder: list[int] = field(default_factory=list)

    def start(self) -> None:
        self.original_connection_limit = chia_policy.global_max_concurrent_connections
        # TODO: yuck yuck, messes with a single global
        chia_policy.global_max_concurrent_connections = self.connection_limit

        self.thread = threading.Thread(target=self._run)
        self.thread.start()

    def port(self) -> int:
        [port] = self.port_holder
        return port

    def _run(self) -> None:
        # TODO: yuck yuck, messes with a single global
        original_event_loop_policy = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(chia_policy.ChiaPolicy())
        try:
            asyncio.run(self.main())
        finally:
            asyncio.set_event_loop_policy(original_event_loop_policy)

    async def main(self) -> None:
        self.server_task = create_referenced_task(
            serve.async_main(
                out_path=self.out_path,
                ip=self.ip,
                port=self.requested_port,
                thread_end_event=self.thread_end_event,
                port_holder=self.port_holder,
            ),
        )
        try:
            await self.server_task
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        # print(f" ==== cancelling {self.server_task}")
        # self.server_task.cancel()
        # print(f" ==== requested cancel of {self.server_task}")
        self.thread_end_event.set()
        if self.thread is None:
            raise Exception("trying to stop without a running thread")
        self.thread.join()

        if self.original_connection_limit is not None:
            chia_policy.global_max_concurrent_connections = self.original_connection_limit


@pytest.mark.anyio
async def test_loop(tmp_path: pathlib.Path) -> None:
    logger = create_logger()

    allowed_over_connections = 0 if sys.platform == "win32" else 100

    serve_file = tmp_path.joinpath("serve")
    serve_file.touch()
    flood_file = tmp_path.joinpath("flood")
    flood_file.touch()

    logger.info(" ==== launching serve.py")
    # TODO: is there some reason not to use an async process here?
    with subprocess.Popen(  # noqa: ASYNC220
        [sys.executable, "-m", "chia._tests.core.server.serve", os.fspath(serve_file)],
    ):
        logger.info(" ====           serve.py running")

        await asyncio.sleep(adjusted_timeout(5))

        logger.info(" ==== launching flood.py")
        # TODO: is there some reason not to use an async process here?
        with subprocess.Popen(  # noqa: ASYNC220
            [sys.executable, "-m", "chia._tests.core.server.flood", os.fspath(flood_file)],
        ):
            logger.info(" ====           flood.py running")

            await asyncio.sleep(adjusted_timeout(10))

            logger.info(" ====   killing flood.py")
            flood_file.unlink()

        flood_output = flood_file.with_suffix(".out").read_text()
        logger.info(" ====           flood.py done")

        await asyncio.sleep(adjusted_timeout(5))

        writer = None
        post_connection_error: Optional[str] = None
        try:
            logger.info(" ==== attempting a single new connection")
            with anyio.fail_after(delay=adjusted_timeout(1)):
                _reader, writer = await asyncio.open_connection(IP, PORT)
            logger.info(" ==== connection succeeded")
            post_connection_succeeded = True
        except (TimeoutError, ConnectionRefusedError) as e:
            logger.info(" ==== connection failed")
            post_connection_succeeded = False
            post_connection_error = f"{type(e).__name__}: {e}"
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()

        logger.info(" ====   killing serve.py")

        serve_file.unlink()

    serve_output = serve_file.with_suffix(".out").read_text()

    logger.info(" ====           serve.py done")

    logger.info(f"\n\n ==== serve output:\n{serve_output}")
    logger.info(f"\n\n ==== flood output:\n{flood_output}")

    over = []
    connection_limit = 25
    accept_loop_count_over: list[int] = []
    server_output_lines = serve_output.splitlines()
    found_shutdown = False
    shutdown_lines: list[str] = []
    for line in server_output_lines:
        if not found_shutdown:
            if not line.casefold().endswith("shutting down"):
                continue

            found_shutdown = True
        shutdown_lines.append(line)

    assert len(shutdown_lines) > 0, "shutdown message is missing from log, unable to verify timing of connections"

    for line in server_output_lines:
        mark = "Total connections:"
        if mark in line:
            _, _, rest = line.partition(mark)
            count = int(rest)
            if count > connection_limit + allowed_over_connections:
                over.append(count)

    assert over == [], over
    assert accept_loop_count_over == [], accept_loop_count_over
    assert "Traceback" not in serve_output
    assert "paused accepting connections" in serve_output
    assert post_connection_succeeded, post_connection_error
    assert all("new connection" not in line.casefold() for line in shutdown_lines), (
        "new connection found during shut down"
    )

    logger.info(" ==== all checks passed")


@pytest.mark.parametrize(
    # repeating in case there are races or flakes to expose
    argnames="repetition",
    argvalues=[x + 1 for x in range(5)],
    ids=lambda repetition: f"#{repetition}",
)
@pytest.mark.parametrize(
    # make sure the server continues to work after exceeding limits repeatedly
    argnames="cycles",
    argvalues=[1, 3],
    ids=lambda cycles: f"{cycles} cycle{'s' if cycles != 1 else ''}",
)
@pytest.mark.anyio
async def test_limits_connections(repetition: int, cycles: int, tmp_path: pathlib.Path) -> None:
    ip = "127.0.0.1"
    connection_limit = 10
    connection_attempts = connection_limit + 10

    async with serve_in_thread(
        out_path=tmp_path.joinpath("serve.out"), ip=ip, port=0, connection_limit=connection_limit
    ) as server:
        for cycle in range(cycles):
            if cycle > 0:
                await asyncio.sleep(1)

            async with Client.open_several(count=connection_limit, ip=ip, port=server.port()) as good_clients:
                remaining_connections = connection_attempts - connection_limit

                await asyncio.sleep(1)
                async with Client.open_several(count=remaining_connections, ip=ip, port=server.port()) as bad_clients:
                    good_alive = await asyncio.gather(*(client.is_alive() for client in good_clients))
                    bad_alive = await asyncio.gather(*(client.is_alive() for client in bad_clients))

            actual = {
                "good": sum(1 if alive else 0 for alive in good_alive),
                "bad": sum(1 if not alive else 0 for alive in bad_alive),
            }
            expected = {"good": connection_limit, "bad": remaining_connections}

            assert actual == expected, f"cycle={cycle}"
