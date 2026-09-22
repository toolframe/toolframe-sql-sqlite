# Toolframe SQLite provider

`storage-sqlite` owns `data/plugins/<plugin-id>/data.sqlite`, separately from core's
management database. Plugins use `RuntimeContext.storage()` and parameterized SQL;
the host supplies application, owner, generation, root and deadline identity.
No file path or raw connection is returned. This is lifecycle/data ownership
isolation for trusted local plugins, not an operating-system sandbox.

Normal SQL supports queries, mutations, indexes declared in migrations, and
same-store transactions. Runtime DDL, transaction control, attachment, PRAGMAs
and provider metadata access are denied by SQLite's authorizer. Transactions are
bound to one root/generation and roll back on expiry, root loss or disposal. A
statement failure poisons a transaction. Mutation IDs and same-database receipts
allow callers to resolve an unknown acknowledgement without repeating a write.

Empty stores initialize automatically. Existing schema changes require a core
migration plan and explicit apply. Apply checks the plan/history, uses SQLite's
backup API, executes all pending migrations and history in one transaction, and
rolls back failures. Nontransactional statements are rejected. Committed schema
changes are never automatically reverted; backups require an explicit operator
repair workflow. Disabling/uninstalling a plugin retains its database.

The `toolframe.storage.admin` service is host-only. Business plugins declare a
requirement on `toolframe.storage.sql` version 1. Cross-plugin data access uses the
owning plugin's business service. There are no cross-database transactions.

Development: `uv sync --group dev`, `uv run pytest -q`, `uv build --no-sources`.
The sibling SDK UV source is a development convenience; wheels depend only on the
published SDK contract.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
