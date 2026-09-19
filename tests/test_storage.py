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
