"""A reusable connection slot with cancellable, deadline-bound admission."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from collections.abc import Callable

from toolframe_sdk import PluginError


class ConnectionPool:
    """Keep one connection leased until the caller has finished its transaction.

    Each store has separate reader and writer pools. SQLite statements still run
    synchronously; waiting for a writer slot must yield so its holder can commit.
    """

    def __init__(self, connect: Callable[[], sqlite3.Connection]):
        self.connect = connect
        self.available: asyncio.Queue[sqlite3.Connection | None] = asyncio.Queue(1)
        self.available.put_nowait(None)
        self.waiters = 0
        self.closed = False

    async def acquire(self, deadline: float) -> sqlite3.Connection:
        if self.closed:
            raise PluginError("storage_stopped", "Storage connections are closed")
        if deadline <= time.time():
            raise PluginError("deadline_exceeded", "Storage root has expired")
        if self.available.empty() and self.waiters >= 128:
            raise PluginError(
                "storage_busy", "Storage connection queue is full; no statement was run"
            )
        self.waiters += 1
        try:
            async with asyncio.timeout(deadline - time.time()):
                connection = await self.available.get()
        except TimeoutError as error:
            raise PluginError(
                "deadline_exceeded",
                "Deadline exceeded waiting for a storage connection",
            ) from error
        except asyncio.QueueShutDown as error:
            raise PluginError(
                "storage_stopped", "Storage connections are closed"
            ) from error
        finally:
            self.waiters -= 1
        try:
            # Recheck after admission: the waiting request may now be stale.
            if deadline <= time.time():
                raise PluginError("deadline_exceeded", "Storage root has expired")
            return connection if connection is not None else self.connect()
        except BaseException:
            if connection is not None:
                self.release(connection)
            elif not self.closed:
                self.available.put_nowait(None)
            raise

    def release(self, connection: sqlite3.Connection) -> None:
        try:
            connection.set_authorizer(None)
            connection.set_progress_handler(None, 0)
            if connection.in_transaction:
                connection.rollback()
        except sqlite3.Error:
            # A failed reset must never leak transaction/guard state to a borrower.
            with contextlib.suppress(sqlite3.Error):
                connection.close()
            connection = None
        if self.closed:
            if connection is not None:
                connection.close()
        else:
            self.available.put_nowait(connection)

    def close(self) -> None:
        self.closed = True
        while not self.available.empty():
            connection = self.available.get_nowait()
            if connection is not None:
                connection.close()
        self.available.shutdown(immediate=True)
