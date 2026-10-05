import asyncio
import sqlite3
import time
from contextlib import contextmanager

import pytest
from toolframe_sdk import (
    Migration,
    PluginError,
    RuntimeContext,
    StoreSchema,
    call_context,
)

from toolframe_storage_sqlite.plugin import ADMIN_SERVICE, SQL_SERVICE, SqliteProvider


@contextmanager
def caller(
    owner="alpha",
    generation="generation-1",
    root="root-1",
    *,
    admin=False,
    deadline=None,
):
    token = call_context.set(
        {
            "application_id": "application-1",
            "owner": owner,
            "owner_generation": generation,
            "root_id": root,
            "management": admin,
            "deadline": deadline or time.time() + 30,
        }
    )
    try:
        yield
    finally:
        call_context.reset(token)


def schema(*extra):
    initial = Migration(
        revision="v1",
        sql="CREATE TABLE entries(id INTEGER PRIMARY KEY, label TEXT NOT NULL);",
    )
    migrations = (initial, *extra)
    return StoreSchema(
        revision=migrations[-1].revision, migrations=migrations
    ).model_dump()


@pytest.fixture
async def storage(tmp_path):
    provider = SqliteProvider(
        RuntimeContext(
            None,
            plugin_id="storage-sqlite",
            generation="storage-generation",
            state_directory=tmp_path,
        )
    )
    await provider.activate()
    with caller(admin=True):
        for owner in ("alpha", "beta"):
            await provider.invoke(
                ADMIN_SERVICE, "ensure_store", {"owner": owner, "schema": schema()}
            )
    try:
        yield provider
    finally:
        await provider.dispose()


async def invoke(storage, method, **params):
    return await storage.invoke(SQL_SERVICE, method, params)


async def test_owner_isolation_and_sql_authorizer(storage):
    with caller("alpha"):
        await invoke(
            storage,
            "execute",
            sql="INSERT INTO entries(label) VALUES (?)",
            parameters=["only-alpha"],
            mutation_id="insert-1",
        )
        assert await invoke(storage, "query", sql="SELECT label FROM entries") == [
            {"label": "only-alpha"}
        ]
        forbidden = [
            "ATTACH DATABASE ':memory:' AS other",
            "PRAGMA database_list",
            "DELETE FROM _toolframe_receipts",
            "CREATE TABLE illicit(x)",
            "SELECT * FROM _toolframe_identity",
            "BEGIN",
            "SELECT load_extension('x')",
        ]
        for index, statement in enumerate(forbidden):
            with pytest.raises(PluginError):
                await invoke(
                    storage, "execute", sql=statement, mutation_id=f"forbidden-{index}"
                )
        with pytest.raises(PluginError):
            await invoke(storage, "query", sql="DELETE FROM entries")
        with pytest.raises(PluginError, match="host-only"):
            await storage.invoke(
                ADMIN_SERVICE, "plan", {"owner": "beta", "schema": schema()}
            )
    with caller("beta"):
        assert await invoke(storage, "query", sql="SELECT * FROM entries") == []


async def test_transaction_fencing_commit_receipt_and_poisoning(storage):
    with caller():
        result = await invoke(storage, "begin", mutation_id="transaction-1")
        tx = result["transaction"]
        await invoke(
            storage,
            "execute",
            sql="INSERT INTO entries(label) VALUES ('committed')",
            transaction=tx,
        )
    for owner, generation, root in (
        ("beta", "generation-1", "root-1"),
        ("alpha", "generation-2", "root-1"),
        ("alpha", "generation-1", "root-2"),
    ):
        with (
            caller(owner, generation, root),
            pytest.raises(PluginError, match="not owned"),
        ):
            await invoke(storage, "query", sql="SELECT * FROM entries", transaction=tx)
    with caller():
        assert (await invoke(storage, "commit", transaction=tx))["committed"]
        assert (await invoke(storage, "receipt", mutation_id="transaction-1"))[
            "receipt"
        ]["committed"]
        assert (await invoke(storage, "begin", mutation_id="transaction-1"))[
            "committed"
        ]
        tx = (await invoke(storage, "begin", mutation_id="transaction-2"))[
            "transaction"
        ]
        await invoke(
            storage,
            "execute",
            sql="INSERT INTO entries(label) VALUES ('rollback')",
            transaction=tx,
        )
        with pytest.raises(PluginError):
            await invoke(
                storage, "execute", sql="INSERT INTO missing VALUES (1)", transaction=tx
            )
        with pytest.raises(PluginError, match="poisoned"):
            await invoke(storage, "commit", transaction=tx)
        await invoke(storage, "rollback", transaction=tx)
        assert await invoke(storage, "query", sql="SELECT label FROM entries") == [
            {"label": "committed"}
        ]


async def test_replayed_mutation_and_root_loss(storage):
    with caller():
        params = {
            "sql": "INSERT INTO entries(label) VALUES (?)",
            "parameters": ["once"],
            "mutation_id": "once",
        }
        first = await invoke(storage, "execute", **params)
        assert await invoke(storage, "execute", **params) == first
        with pytest.raises(PluginError, match="different SQL"):
            await invoke(storage, "execute", **{**params, "parameters": ["twice"]})
        tx = (await invoke(storage, "begin", mutation_id="lost-root"))["transaction"]
        await invoke(
            storage,
            "execute",
            sql="INSERT INTO entries(label) VALUES ('never')",
            transaction=tx,
        )
    with caller(admin=True):
        await storage.invoke(ADMIN_SERVICE, "cleanup_root", {"root_id": "root-1"})
    with caller(root="root-2"):
        assert await invoke(
            storage, "query", sql="SELECT count(*) AS count FROM entries"
        ) == [{"count": 1}]


async def test_returning_mutations_preserve_receipts_and_transactions(storage):
    with caller():
        params = {
            "sql": "INSERT INTO entries(label) VALUES (?) RETURNING id",
            "parameters": ["returned"],
            "mutation_id": "returned",
        }
        result = await invoke(storage, "execute_returning", **params)
        assert result == {"rows": [{"id": 1}], "rowcount": 1}
        assert await invoke(storage, "execute_returning", **params) == result
        assert (await invoke(storage, "receipt", mutation_id="returned"))[
            "receipt"
        ] == result
        transaction = (await invoke(storage, "begin", mutation_id="rolled-returning"))[
            "transaction"
        ]
        await invoke(
            storage,
            "execute_returning",
            sql="INSERT INTO entries(label) VALUES ('lost') RETURNING id",
            transaction=transaction,
        )
        await invoke(storage, "rollback", transaction=transaction)
        assert await invoke(
            storage, "query", sql="SELECT count(*) AS n FROM entries"
        ) == [{"n": 1}]


async def test_expired_transaction_rolls_back(storage):
    with caller(deadline=time.time() + 0.08):
        tx = (await invoke(storage, "begin", mutation_id="expires"))["transaction"]
        await invoke(
            storage,
            "execute",
            sql="INSERT INTO entries(label) VALUES ('never')",
            transaction=tx,
        )
    await asyncio.sleep(0.3)
    with caller(root="fresh"):
        assert await invoke(storage, "query", sql="SELECT * FROM entries") == []


async def test_existing_schema_requires_explicit_apply_and_backups_are_consistent(
    storage,
):
    with caller():
        await invoke(
            storage,
            "execute",
            sql="INSERT INTO entries(label) VALUES ('retained')",
            mutation_id="insert",
        )
    upgrade = schema(
        Migration(
            revision="v2",
            parent="v1",
            sql="ALTER TABLE entries ADD COLUMN version INTEGER NOT NULL DEFAULT 1;",
        )
    )
    with caller(admin=True):
        with pytest.raises(PluginError) as required:
            await storage.invoke(
                ADMIN_SERVICE, "ensure_store", {"owner": "alpha", "schema": upgrade}
            )
        assert required.value.code == "migration_required"
        plan = await storage.invoke(
            ADMIN_SERVICE, "plan", {"owner": "alpha", "schema": upgrade}
        )
        with pytest.raises(PluginError, match="changed"):
            await storage.invoke(
                ADMIN_SERVICE,
                "apply",
                {"owner": "alpha", "schema": upgrade, "plan_id": "stale"},
            )
        applied = await storage.invoke(
            ADMIN_SERVICE,
            "apply",
            {"owner": "alpha", "schema": upgrade, "plan_id": plan["plan_id"]},
        )
        assert applied["committed"]
        with sqlite3.connect(storage.directory / applied["backup"]) as backup:
            assert (
                backup.execute("SELECT label FROM entries").fetchone()[0] == "retained"
            )
            assert [row[1] for row in backup.execute("PRAGMA table_info(entries)")] == [
                "id",
                "label",
            ]
        with pytest.raises(PluginError, match="downgraded"):
            await storage.invoke(
                ADMIN_SERVICE, "plan", {"owner": "alpha", "schema": schema()}
            )
    with caller():
        assert await invoke(
            storage, "query", sql="SELECT label,version FROM entries"
        ) == [{"label": "retained", "version": 1}]


async def test_failed_and_nontransactional_migrations_roll_back_schema_and_history(
    storage,
):
    for script in (
        "ALTER TABLE entries ADD COLUMN failed TEXT; INSERT INTO absent VALUES (1);",
        "VACUUM;",
        "COMMIT;",
        "PRAGMA journal_mode=DELETE;",
    ):
        upgrade = schema(Migration(revision="v2", parent="v1", sql=script))
        with caller(admin=True):
            plan = await storage.invoke(
                ADMIN_SERVICE, "plan", {"owner": "alpha", "schema": upgrade}
            )
            with pytest.raises(PluginError):
                await storage.invoke(
                    ADMIN_SERVICE,
                    "apply",
                    {"owner": "alpha", "schema": upgrade, "plan_id": plan["plan_id"]},
                )
            original = await storage.invoke(
                ADMIN_SERVICE, "plan", {"owner": "alpha", "schema": schema()}
            )
            assert original["current"] == "v1" and original["pending"] == []
    with caller(), pytest.raises(PluginError):
        await invoke(storage, "query", sql="SELECT failed FROM entries")


async def test_schema_inspection_is_read_only_and_can_reconcile_lost_ack(storage):
    with caller(admin=True):
        absent = await storage.invoke(ADMIN_SERVICE, "inspect", {"owner": "new-owner"})
        assert absent == {"exists": False, "history": [], "revision": None}
        await storage.invoke(
            ADMIN_SERVICE, "plan", {"owner": "new-owner", "schema": schema()}
        )
        assert not (storage.directory / "new-owner").exists()
        before = await storage.invoke(ADMIN_SERVICE, "inspect", {"owner": "alpha"})
        upgrade = schema(
            Migration(
                revision="v2",
                parent="v1",
                sql="ALTER TABLE entries ADD COLUMN version INTEGER NOT NULL DEFAULT 1;",
            )
        )
        plan = await storage.invoke(
            ADMIN_SERVICE, "plan", {"owner": "alpha", "schema": upgrade}
        )
        await storage.invoke(
            ADMIN_SERVICE,
            "apply",
            {"owner": "alpha", "schema": upgrade, "plan_id": plan["plan_id"]},
        )
        after = await storage.invoke(ADMIN_SERVICE, "inspect", {"owner": "alpha"})
        assert before["revision"] == "v1" and after["revision"] == "v2"
        assert after["history"][: len(before["history"])] == before["history"]


async def test_returning_requires_a_mutation(storage):
    with caller(), pytest.raises(PluginError, match="Use query"):
        await invoke(
            storage,
            "execute_returning",
            sql="SELECT 1",
            mutation_id="invalid-returning",
        )


async def test_concurrent_transactions_queue_without_business_locks(storage):
    with caller():
        for index in range(10):
            await invoke(
                storage,
                "execute",
                sql="INSERT INTO entries(label) VALUES ('available')",
                mutation_id=f"stock-{index}",
            )

    async def reserve(index):
        with caller(root=f"reservation-{index}"):
            transaction = None
            try:
                transaction = (
                    await invoke(storage, "begin", mutation_id=f"reservation-{index}")
                )["transaction"]
                # A real business transaction spans multiple RPC requests.
                await asyncio.sleep(0)
                result = await invoke(
                    storage,
                    "execute",
                    sql="DELETE FROM entries WHERE id=(SELECT id FROM entries LIMIT 1)",
                    transaction=transaction,
                )
                await invoke(storage, "commit", transaction=transaction)
                return "reserved" if result["rowcount"] else "insufficient_stock"
            except PluginError as error:
                if transaction:
                    await invoke(storage, "rollback", transaction=transaction)
                return error.code

    results = await asyncio.gather(*(reserve(index) for index in range(20)))
    assert sorted(results) == ["insufficient_stock"] * 10 + ["reserved"] * 10


async def test_queued_standalone_write_does_not_block_readers_or_other_owners(storage):
    with caller(root="holder"):
        transaction = (await invoke(storage, "begin", mutation_id="holder"))[
            "transaction"
        ]
        await invoke(
            storage,
            "execute",
            sql="INSERT INTO entries(label) VALUES ('uncommitted')",
            transaction=transaction,
        )

    async def write():
        with caller(root="queued"):
            return await invoke(
                storage,
                "execute",
                sql="INSERT INTO entries(label) VALUES ('queued')",
                mutation_id="queued",
            )

    pending = asyncio.create_task(write())
    try:
        await asyncio.sleep(0)
        assert not pending.done()
        with caller(root="reader"):
            assert await invoke(storage, "query", sql="SELECT * FROM entries") == []
        with caller(owner="beta", root="independent"):
            assert (
                await invoke(
                    storage,
                    "execute",
                    sql="INSERT INTO entries(label) VALUES ('independent')",
                    mutation_id="independent",
                )
            )["rowcount"] == 1
        with caller(root="holder"):
            await invoke(storage, "commit", transaction=transaction)
        assert (await asyncio.wait_for(pending, 1))["rowcount"] == 1
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_idle_connections_are_reused_and_do_not_retain_guards(
    storage, monkeypatch
):
    opened = []
    connect = sqlite3.connect

    def track(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", track)
    for index in range(5):
        with caller(root=f"pooled-{index}"):
            await invoke(
                storage,
                "execute",
                sql="INSERT INTO entries(label) VALUES (?)",
                parameters=[str(index)],
                mutation_id=f"pooled-{index}",
            )
            assert await invoke(
                storage, "query", sql="SELECT count(*) AS n FROM entries"
            ) == [{"n": index + 1}]
            assert (await invoke(storage, "receipt", mutation_id=f"pooled-{index}"))[
                "receipt"
            ]["rowcount"] == 1
    assert len(opened) == 2


@pytest.mark.parametrize("finish", ["timeout", "cancel", "root_cleanup"])
@pytest.mark.parametrize("method", ["execute", "begin"])
async def test_waiting_writer_can_end_without_leaking_a_slot_or_writing(
    storage, finish, method
):
    with caller(root="holder"):
        transaction = (await invoke(storage, "begin", mutation_id="holder"))[
            "transaction"
        ]

    async def wait():
        deadline = time.time() + (0.05 if finish == "timeout" else 10)
        with caller(root="waiter", deadline=deadline):
            return await invoke(
                storage,
                method,
                sql="INSERT INTO entries(label) VALUES ('cancelled')",
                mutation_id="cancelled",
            )

    pending = asyncio.create_task(wait())
    await asyncio.sleep(0)
    if finish == "timeout":
        with pytest.raises(PluginError) as failure:
            await asyncio.wait_for(pending, 1)
        assert failure.value.code == "deadline_exceeded"
    else:
        if finish == "cancel":
            pending.cancel()
        else:
            with caller(admin=True):
                await storage.invoke(
                    ADMIN_SERVICE, "cleanup_root", {"root_id": "waiter"}
                )
        with pytest.raises(asyncio.CancelledError):
            await pending
    with caller(root="holder"):
        await invoke(storage, "rollback", transaction=transaction)
    with caller(root="waiter"):
        retry = (await invoke(storage, "begin", mutation_id="retry"))["transaction"]
        await invoke(storage, "rollback", transaction=retry)
    with caller(root="fresh"):
        await asyncio.wait_for(
            invoke(
                storage,
                "execute",
                sql="INSERT INTO entries(label) VALUES ('fresh')",
                mutation_id="fresh",
            ),
            1,
        )
        assert await invoke(storage, "query", sql="SELECT label FROM entries") == [
            {"label": "fresh"}
        ]
        assert (await invoke(storage, "receipt", mutation_id="cancelled"))[
            "receipt"
        ] is None


async def test_root_loss_releases_writer_to_a_waiting_transaction(storage):
    with caller(root="lost"):
        transaction = (await invoke(storage, "begin", mutation_id="lost"))[
            "transaction"
        ]
        await invoke(
            storage,
            "execute",
            sql="INSERT INTO entries(label) VALUES ('lost')",
            transaction=transaction,
        )

    async def begin():
        with caller(root="next"):
            return await invoke(storage, "begin", mutation_id="next")

    pending = asyncio.create_task(begin())
    await asyncio.sleep(0)
    assert not pending.done()
    with caller(admin=True):
        await storage.invoke(ADMIN_SERVICE, "cleanup_root", {"root_id": "lost"})
    next_transaction = (await asyncio.wait_for(pending, 1))["transaction"]
    with caller(root="next"):
        assert (
            await invoke(
                storage,
                "query",
                sql="SELECT * FROM entries",
                transaction=next_transaction,
            )
            == []
        )
        await invoke(storage, "commit", transaction=next_transaction)


async def test_dispose_closes_pools_and_rejects_waiters(storage):
    with caller(root="holder"):
        transaction = (await invoke(storage, "begin", mutation_id="holder"))[
            "transaction"
        ]
        await invoke(
            storage,
            "execute",
            sql="INSERT INTO entries(label) VALUES ('uncommitted')",
            transaction=transaction,
        )
    with caller(root="reader"):
        assert await invoke(storage, "query", sql="SELECT * FROM entries") == []

    async def begin():
        with caller(root="waiter"):
            return await invoke(storage, "begin", mutation_id="waiter")

    pending = asyncio.create_task(begin())
    await asyncio.sleep(0)
    await storage.dispose()
    with pytest.raises(PluginError) as failure:
        await asyncio.wait_for(pending, 1)
    assert failure.value.code == "storage_stopped"
    with sqlite3.connect(storage.directory / "alpha" / "data.sqlite") as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert connection.execute("SELECT * FROM entries").fetchall() == []


async def test_concurrent_replayed_transaction_only_runs_once(storage):
    async def write(index):
        with caller(root=f"replay-{index}"):
            result = await invoke(storage, "begin", mutation_id="same-write")
            if result.get("committed"):
                return result
            transaction = result["transaction"]
            await asyncio.sleep(0)
            await invoke(
                storage,
                "execute",
                sql="INSERT INTO entries(label) VALUES ('once')",
                transaction=transaction,
            )
            return await invoke(storage, "commit", transaction=transaction)

    results = await asyncio.gather(*(write(index) for index in range(20)))
    assert results == [{"committed": True, "mutation_id": "same-write"}] * 20
    with caller(root="reader"):
        assert await invoke(storage, "query", sql="SELECT label FROM entries") == [
            {"label": "once"}
        ]


async def test_second_unscoped_write_from_transaction_root_is_rejected(storage):
    with caller():
        transaction = (await invoke(storage, "begin", mutation_id="outer"))[
            "transaction"
        ]
        with pytest.raises(PluginError) as failure:
            await asyncio.wait_for(invoke(storage, "begin", mutation_id="nested"), 1)
        assert failure.value.code == "nested_transaction"
        await invoke(storage, "rollback", transaction=transaction)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("begin", "begin"),
        ("begin", "execute"),
        ("begin", "execute_returning"),
        ("execute", "begin"),
        ("execute_returning", "begin"),
    ],
)
async def test_same_root_cannot_queue_a_transaction_with_another_writer(
    storage, first, second
):
    with caller(root="holder"):
        held = (await invoke(storage, "begin", mutation_id="holder"))["transaction"]

    async def write(method, mutation):
        with caller(root="queued"):
            params = {"mutation_id": mutation}
            if method != "begin":
                params["sql"] = "INSERT INTO entries(label) VALUES ('queued')"
                if method == "execute_returning":
                    params["sql"] += " RETURNING id"
            return await invoke(storage, method, **params)

    pending = asyncio.create_task(write(first, "first"))
    try:
        await asyncio.sleep(0)
        assert not pending.done()
        with pytest.raises(PluginError) as failure:
            await asyncio.wait_for(write(second, "second"), 0.2)
        assert failure.value.code == "nested_transaction"
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        with caller(root="holder"):
            await invoke(storage, "rollback", transaction=held)
    # Cancelled admission must not poison the next transaction from this root.
    with caller(root="queued"):
        retry = (await invoke(storage, "begin", mutation_id="retry"))["transaction"]
        await invoke(storage, "rollback", transaction=retry)


@pytest.mark.parametrize("method", ["query", "begin"])
async def test_failed_connection_open_does_not_exhaust_pool(
    storage, monkeypatch, method
):
    connect = storage.connect
    attempts = 0

    def fail_once(owner, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PluginError("store_missing", "Temporarily unavailable")
        return connect(owner, **kwargs)

    monkeypatch.setattr(storage, "connect", fail_once)
    with caller():
        with pytest.raises(PluginError, match="Temporarily"):
            await invoke(
                storage, method, sql="SELECT * FROM entries", mutation_id="failed-open"
            )
        transaction = (await invoke(storage, "begin", mutation_id="retry"))[
            "transaction"
        ]
        await invoke(storage, "rollback", transaction=transaction)
        assert (
            await asyncio.wait_for(
                invoke(storage, "query", sql="SELECT * FROM entries"), 1
            )
            == []
        )


async def test_pooled_connections_cannot_cross_application_identity(storage):
    with caller():
        await invoke(storage, "query", sql="SELECT * FROM entries")
        with pytest.raises(PluginError) as failure:
            token = call_context.set(
                {**call_context.get(), "application_id": "other-app"}
            )
            try:
                await invoke(storage, "query", sql="SELECT * FROM entries")
            finally:
                call_context.reset(token)
        assert failure.value.code == "store_identity_mismatch"


async def test_writer_queue_has_bounded_backpressure(storage):
    with caller(root="holder"):
        transaction = (await invoke(storage, "begin", mutation_id="holder"))[
            "transaction"
        ]

    async def begin(index):
        with caller(root=f"waiter-{index}"):
            return await invoke(storage, "begin", mutation_id=f"waiter-{index}")

    pending = [asyncio.create_task(begin(index)) for index in range(129)]
    try:
        await asyncio.sleep(0)
        assert all(not task.done() for task in pending[:-1])
        with pytest.raises(PluginError) as failure:
            await pending[-1]
        assert failure.value.code == "storage_busy"
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    with caller(root="holder"):
        await invoke(storage, "rollback", transaction=transaction)
