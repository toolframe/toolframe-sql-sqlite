# SQLite storage and transactions

Normal SQL supports queries, mutations, indexes declared in migrations, and
same-store transactions. Runtime DDL, transaction control, attachment, PRAGMAs
and provider metadata access are denied by SQLite's authorizer. Transactions are
bound to one root/generation and roll back on expiry, root loss or disposal. A
statement failure poisons a transaction. Mutation IDs and same-database receipts
allow callers to resolve an unknown acknowledgement without repeating a write.

Each application/owner store lazily pools one writer connection and one separate
reader connection. A transaction retains the writer until commit, rollback,
expiry or root cleanup; standalone mutations use the same writer. Other writes
wait asynchronously instead of opening competing write connections, leaving the
holder free to finish its next RPC. Committed-state reads and other owners can
proceed while a transaction is open. Return all writes within a transaction through
its transaction client/token; a second unscoped writer from the same root/store
is rejected as `nested_transaction` instead of waiting on itself.
A queued transaction also reserves its root/store: await an earlier write before
beginning a transaction, and await `begin` before issuing its token-bound writes.

Each connection slot permits at most 128 waiting calls. Admission obeys the
host-issued root deadline and cancellation; a full queue returns `storage_busy`
before that call runs SQL. Connections are reset before reuse and closed on
provider disposal. This coordinates one provider process, not arbitrary external
SQLite writers. SQL execution is still synchronous and external lock contention
retains SQLite's 50ms busy timeout. Pooling does not provide business idempotency
or make long-running SQL nonblocking.

Empty stores initialize automatically. Existing schema changes require a core
migration plan and explicit apply. Apply checks the plan/history, uses SQLite's
backup API, executes all pending migrations and history in one transaction, and
rolls back failures. Nontransactional statements are rejected. Committed schema
changes are never automatically reverted; backups require an explicit operator
repair workflow. Disabling/uninstalling a plugin retains its database.

The `toolframe.sql.admin` service is host-only. Business plugins declare a
requirement on `toolframe.sql` version 1. Cross-plugin data access uses the
owning plugin's business service. There are no cross-database transactions.

Version 0.3 adds `execute_returning` for mutations with a `RETURNING` clause. It
returns `rows` and `rowcount`, including within transactions and durable mutation
receipts. Existing SQLite SQL, migration checksums and `execute` results are
preserved; PostgreSQL variants in migration declarations do not change SQLite
history.
