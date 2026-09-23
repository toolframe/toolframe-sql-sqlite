from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from toolframe_sdk import (
    AuthorizationSpec,
    MethodSpec,
    Plugin,
    PluginDefinition,
    PluginError,
    ServiceSpec,
    StoreSchema,
    digest,
)

SQL_SERVICE = "toolframe.storage.sql"
ADMIN_SERVICE = "toolframe.storage.admin"


def encoded(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


def guard(*, read_only=False):
    def authorize(action, first, second, database, origin):
        if action in (
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_PRAGMA,
            sqlite3.SQLITE_TRANSACTION,
            sqlite3.SQLITE_SAVEPOINT,
        ):
            return sqlite3.SQLITE_DENY
        if any(
            str(value or "").lower().startswith("_toolframe_")
            for value in (first, second)
        ):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and str(second).lower() in (
            "load_extension",
            "readfile",
            "writefile",
        ):
            return sqlite3.SQLITE_DENY
        if read_only and action not in (
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_RECURSIVE,
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorize


DDL = {
    getattr(sqlite3, name)
    for name in (
        "SQLITE_CREATE_INDEX",
        "SQLITE_CREATE_TABLE",
        "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TEMP_VIEW",
        "SQLITE_CREATE_TRIGGER",
        "SQLITE_CREATE_VIEW",
        "SQLITE_DROP_INDEX",
        "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TABLE",
        "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TEMP_VIEW",
        "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW",
        "SQLITE_ALTER_TABLE",
        "SQLITE_CREATE_VTABLE",
        "SQLITE_DROP_VTABLE",
        "SQLITE_REINDEX",
        "SQLITE_ANALYZE",
    )
}


def business_guard(*, read_only=False):
    base = guard(read_only=read_only)
    return lambda action, *args: (
        sqlite3.SQLITE_DENY if action in DDL else base(action, *args)
    )


def statements(script):
    pending = ""
    for character in script:
        pending += character
        if character == ";" and sqlite3.complete_statement(pending):
            yield pending
            pending = ""
    if pending.strip():
        # SQLite accepts a final statement without a semicolon.
        yield pending


@dataclass
class Transaction:
    connection: sqlite3.Connection
    owner: str
    generation: str
    root_id: str
    deadline: float
    mutation_id: str
    poisoned: bool = False


class SqliteProvider(Plugin):
    def __init__(self, context):
        super().__init__(context)
        self.directory = None
        self.transactions: dict[str, Transaction] = {}
        self.sweeper = None

    async def activate(self):
        if not self.context.state_directory:
            raise PluginError(
                "storage_directory_missing",
                "Core must allocate the storage provider directory",
            )
        self.directory = Path(self.context.state_directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sweeper = asyncio.create_task(self._expire())

    async def _expire(self):
        while True:
            await asyncio.sleep(0.25)
            for token, transaction in list(self.transactions.items()):
                if transaction.deadline <= time.time():
                    self._rollback(token)

    async def dispose(self):
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        for token in list(self.transactions):
            self._rollback(token)
        await super().dispose()

    def identity(self, *, admin=False, owner=None):
        info = self.context.call_info
        if admin:
            if not info.get("management"):
                raise PluginError("forbidden", "Storage administration is host-only")
        else:
            owner = info.get("owner")
            if not info.get("owner_generation") or not info.get("root_id"):
                raise PluginError(
                    "identity_missing",
                    "Storage calls need a host-issued root and owner generation",
                )
            if info.get("deadline", 0) <= time.time():
                raise PluginError("deadline_exceeded", "Storage root has expired")
        if not owner or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", owner):
            raise PluginError(
                "invalid_owner", "Storage owner must be a stable plugin ID"
            )
        if not info.get("application_id"):
            raise PluginError("identity_missing", "Application identity is required")
        return owner, info

    def connect(self, owner, *, create=False):
        path = self.directory / owner / "data.sqlite"
        if not create and not path.is_file():
            raise PluginError(
                "store_missing", "Store must be initialized before business activation"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            path, isolation_level=None, timeout=0.05, cached_statements=0
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=50")
        if create:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS _toolframe_identity(owner TEXT PRIMARY KEY, application_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS _toolframe_migrations(position INTEGER PRIMARY KEY, revision TEXT UNIQUE NOT NULL, checksum TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS _toolframe_receipts(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL);
            """)
        identity = connection.execute("SELECT * FROM _toolframe_identity").fetchone()
        application_id = self.context.call_info["application_id"]
        if identity and (
            identity["owner"] != owner or identity["application_id"] != application_id
        ):
            connection.close()
            raise PluginError(
                "store_identity_mismatch",
                "Database belongs to a different application or plugin",
            )
        if identity is None:
            if not create:
                connection.close()
                raise PluginError(
                    "store_identity_missing", "Store has no initialized identity"
                )
            connection.execute(
                "INSERT INTO _toolframe_identity VALUES(?,?)", (owner, application_id)
            )
        return connection

    def inspect(self, owner):
        if not (self.directory / owner / "data.sqlite").exists():
            return {"exists": False, "history": [], "revision": None}
        connection = self.connect(owner)
        try:
            history = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM _toolframe_migrations ORDER BY position"
                )
            ]
            return {
                "exists": True,
                "history": history,
                "revision": history[-1]["revision"] if history else None,
            }
        finally:
            connection.close()

    def plan(self, owner, schema):
        schema = StoreSchema.model_validate(schema)
        history = self.inspect(owner)["history"]
        for index, row in enumerate(history):
            if (
                index >= len(schema.migrations)
                or row["revision"] != schema.migrations[index].revision
                or row["checksum"] != schema.migrations[index].checksum
            ):
                raise PluginError(
                    "schema_incompatible",
                    "Migration history was changed, removed or downgraded",
                )
        pending = [item.model_dump() for item in schema.migrations[len(history) :]]
        current = history[-1]["revision"] if history else None
        value = {
            "owner": owner,
            "current": current,
            "target": schema.revision,
            "pending": pending,
            "history": history,
            "requires_apply": bool(history and pending),
        }
        value["plan_id"] = digest(value)
        return value

    def migrate(self, owner, schema, *, expected=None, initialize=False):
        plan = self.plan(owner, schema)
        if expected is not None and plan["plan_id"] != expected:
            raise PluginError(
                "stale_plan", "Store schema changed after migration planning"
            )
        if initialize and plan["requires_apply"]:
            raise PluginError(
                "migration_required",
                "Existing data requires explicit migration apply",
                plan,
            )
        if not plan["pending"]:
            return {"revision": plan["target"], "committed": False}
        if any(
            transaction.owner == owner for transaction in self.transactions.values()
        ):
            raise PluginError(
                "store_busy", "Drain all owner transactions before migration"
            )
        connection = self.connect(owner, create=True)
        backup_path = None
        try:
            if plan["history"]:
                backups = self.directory / owner / "backups"
                backups.mkdir(exist_ok=True)
                backup_path = backups / f"{int(time.time())}-{uuid.uuid4().hex}.sqlite"
                backup = sqlite3.connect(backup_path)
                try:
                    connection.backup(backup)
                finally:
                    backup.close()
            connection.execute("BEGIN IMMEDIATE")
            position = len(plan["history"])
            for migration in StoreSchema.model_validate(schema).migrations[position:]:
                connection.set_authorizer(guard())
                connection.set_progress_handler(
                    lambda: (
                        time.time()
                        > self.context.call_info.get("deadline", float("inf"))
                    ),
                    1000,
                )
                for statement in statements(migration.sql):
                    connection.execute(statement)
                connection.set_authorizer(None)
                connection.execute(
                    "INSERT INTO _toolframe_migrations VALUES(?,?,?)",
                    (position, migration.revision, migration.checksum),
                )
                position += 1
            connection.commit()
            return {
                "revision": plan["target"],
                "committed": True,
                "backup": str(backup_path.relative_to(self.directory))
                if backup_path
                else None,
            }
        except sqlite3.Error as error:
            connection.set_authorizer(None)
            connection.rollback()
            raise PluginError(
                "migration_failed",
                str(error),
                {"backup": str(backup_path) if backup_path else None},
            ) from error
        finally:
            connection.close()

    def _rollback(self, token):
        transaction = self.transactions.pop(token, None)
        if transaction:
            transaction.connection.set_authorizer(None)
            transaction.connection.rollback()
            transaction.connection.close()

    def _transaction(self, token, owner, info):
        transaction = self.transactions.get(token)
        if not transaction or (
            transaction.owner,
            transaction.generation,
            transaction.root_id,
        ) != (owner, info["owner_generation"], info["root_id"]):
            raise PluginError(
                "transaction_invalid",
                "Transaction is not owned by this root and generation",
            )
        if transaction.deadline <= time.time():
            self._rollback(token)
            raise PluginError("transaction_expired", "Transaction deadline exceeded")
        return transaction

    def receipt(self, connection, identifier, fingerprint=None):
        row = connection.execute(
            "SELECT fingerprint, result FROM _toolframe_receipts WHERE id=?",
            (identifier,),
        ).fetchone()
        if row and fingerprint is not None and row["fingerprint"] != fingerprint:
            raise PluginError(
                "mutation_conflict",
                "Mutation ID already refers to different SQL/parameters",
            )
        return json.loads(row["result"]) if row else None

    def mutation_id(self, params):
        value = params.get("mutation_id")
        if not isinstance(value, str) or not re.fullmatch(
            r"[a-zA-Z0-9_.:-]{1,128}", value
        ):
            raise PluginError(
                "mutation_id_invalid", "A bounded stable mutation ID is required"
            )
        return value

    async def invoke(self, service, method, params):
        if service == ADMIN_SERVICE:
            if method == "cleanup_root":
                if not self.context.call_info.get("management"):
                    raise PluginError("forbidden", "Root cleanup is host-only")
                for token, transaction in list(self.transactions.items()):
                    if transaction.root_id == params["root_id"]:
                        self._rollback(token)
                return {}
            owner, _ = self.identity(admin=True, owner=params.get("owner"))
            if method == "inspect":
                return self.inspect(owner)
            if method == "plan":
                return self.plan(owner, params["schema"])
            if method == "ensure_store":
                return self.migrate(owner, params["schema"], initialize=True)
            if method == "apply":
                return self.migrate(owner, params["schema"], expected=params["plan_id"])
            raise PluginError("method_not_found", method)
        if service != SQL_SERVICE:
            raise PluginError("method_not_found", service)
        owner, info = self.identity()
        token = params.get("transaction")
        transaction = self._transaction(token, owner, info) if token else None
        if transaction and transaction.poisoned and method != "rollback":
            raise PluginError(
                "transaction_poisoned",
                "A failed statement poisoned this transaction; roll it back",
            )
        connection = transaction.connection if transaction else self.connect(owner)
        keep = transaction is not None
        try:
            connection.set_authorizer(None)
            if method == "receipt":
                return {"receipt": self.receipt(connection, self.mutation_id(params))}
            if method == "begin":
                mutation_id = self.mutation_id(params)
                prior = self.receipt(connection, mutation_id, "transaction")
                if prior:
                    return prior
                if transaction:
                    raise PluginError(
                        "nested_transaction", "Nested transactions are unsupported"
                    )
                connection.execute("BEGIN IMMEDIATE")
                token = uuid.uuid4().hex
                self.transactions[token] = Transaction(
                    connection,
                    owner,
                    info["owner_generation"],
                    info["root_id"],
                    info["deadline"],
                    mutation_id,
                )
                keep = True
                return {"transaction": token, "mutation_id": mutation_id}
            if method == "rollback":
                if not transaction:
                    raise PluginError(
                        "transaction_required", "Rollback needs a transaction"
                    )
                self._rollback(token)
                return {"rolled_back": True}
            if method == "commit":
                if not transaction:
                    raise PluginError(
                        "transaction_required", "Commit needs a transaction"
                    )
                result = {"committed": True, "mutation_id": transaction.mutation_id}
                connection.execute(
                    "INSERT INTO _toolframe_receipts VALUES(?,?,?)",
                    (transaction.mutation_id, "transaction", encoded(result)),
                )
                connection.commit()
                self.transactions.pop(token)
                connection.close()
                return result
            if method not in ("query", "execute", "execute_returning"):
                raise PluginError("method_not_found", method)
            sql, parameters = params["sql"], params.get("parameters", [])
            fingerprint = digest({"sql": sql, "parameters": parameters})
            if method == "execute_returning":
                fingerprint = digest(
                    {"sql": sql, "parameters": parameters, "returning": True}
                )
            mutation_id = None
            if method in ("execute", "execute_returning") and not transaction:
                mutation_id = self.mutation_id(params)
                prior = self.receipt(connection, mutation_id, fingerprint)
                if prior is not None:
                    return prior
                connection.execute("BEGIN IMMEDIATE")
            base_guard = business_guard(read_only=method == "query")
            mutation_seen = False

            def authorize(action, *arguments):
                nonlocal mutation_seen
                if action in (
                    sqlite3.SQLITE_INSERT,
                    sqlite3.SQLITE_UPDATE,
                    sqlite3.SQLITE_DELETE,
                ):
                    mutation_seen = True
                return base_guard(action, *arguments)

            connection.set_authorizer(authorize)
            connection.set_progress_handler(
                lambda: time.time() > info["deadline"], 1000
            )
            cursor = connection.execute(sql, parameters)
            if method in ("query", "execute_returning"):
                if method == "execute_returning" and not mutation_seen:
                    raise PluginError(
                        "mutation_required", "Use query for read-only statements"
                    )
                if method == "execute_returning" and not cursor.description:
                    raise PluginError(
                        "result_required", "Mutation must include RETURNING"
                    )
                rows = cursor.fetchmany(10001)
                if len(rows) > 10000:
                    raise PluginError(
                        "result_too_large",
                        "Use LIMIT/pagination; query exceeds 10,000 rows",
                    )
                result = [dict(row) for row in rows]
                if method == "execute_returning":
                    result = {"rows": result, "rowcount": cursor.rowcount}
            else:
                if cursor.description:
                    raise PluginError(
                        "result_not_supported",
                        "Use a transaction with query after mutation; execute cannot return rows",
                    )
                result = {"rowcount": cursor.rowcount, "lastrowid": cursor.lastrowid}
            if len(encoded(result).encode()) > 900_000:
                raise PluginError(
                    "result_too_large",
                    "Use pagination; query exceeds the RPC result budget",
                )
            connection.set_authorizer(None)
            if mutation_id:
                connection.execute(
                    "INSERT INTO _toolframe_receipts VALUES(?,?,?)",
                    (mutation_id, fingerprint, encoded(result)),
                )
                connection.commit()
            return result
        except (sqlite3.Error, PluginError, ValueError, TypeError) as error:
            if transaction:
                transaction.poisoned = True
            else:
                connection.set_authorizer(None)
                connection.rollback()
            if isinstance(error, PluginError):
                raise
            raise PluginError("sql_failed", str(error)) from error
        finally:
            if not keep:
                connection.close()


plugin = PluginDefinition(
    id="storage-sqlite",
    kind="provider",
    provider_kind="storage",
    factory="toolframe_storage_sqlite.plugin:SqliteProvider",
    authorization=AuthorizationSpec(),
    services={
        SQL_SERVICE: ServiceSpec(
            methods={
                name: MethodSpec(
                    audience="internal", read_only=name in ("query", "receipt")
                )
                for name in (
                    "query",
                    "execute",
                    "execute_returning",
                    "begin",
                    "commit",
                    "rollback",
                    "receipt",
                )
            }
        ),
        ADMIN_SERVICE: ServiceSpec(
            methods={
                name: MethodSpec(audience="internal")
                for name in ("inspect", "ensure_store", "plan", "apply", "cleanup_root")
            }
        ),
    },
)
