# hugr facts store

A dedicated **`pgvector/pgvector:pg17`** container holding the keyed `facts` table
— the deterministic Fact tier that stops the model confabulating stable user
facts. It is **isolated** from the shared home-prod Postgres (`postdb1`, pg14.6)
per [ADR 0005](../../docs/adr/0005-dedicated-pgvector-instance.md), and its schema
follows [ADR 0002](../../docs/adr/0002-keyed-facts-table.md).

Release **0.5** exercises only `facts`. The `vector` extension is installed so the
schema is ready for the episodic `memories` tier later, but there are no vector
columns or indexes yet.

## Run it

From the repo root:

```bash
cp deploy/facts/.env.example deploy/facts/.env        # then set POSTGRES_PASSWORD
docker compose -f deploy/facts/docker-compose.yml up -d --wait
```

The store binds to **loopback only** (`127.0.0.1:5432`, override with
`HUGR_FACTS_PORT`). Never publish it to the LAN. `POSTGRES_PASSWORD` is required —
the stack refuses to start without it, so no default password ships.

```bash
docker compose -f deploy/facts/docker-compose.yml down       # stop (keep data)
docker compose -f deploy/facts/docker-compose.yml down -v     # stop + destroy data
```

Data persists to `deploy/facts/data/postgres/` (gitignored). `init.sql` runs only
on first boot of an empty data directory.

## Connection string

For host-side clients (e.g. the hugr CLI in PV-2):

```
postgresql://hugr:<POSTGRES_PASSWORD>@localhost:5432/hugr
```

## Acceptance (PV-1)

A fresh psql can insert and read the canonical global fact. Over the container's
local socket (trusted — no password needed):

```bash
docker exec hugr-facts-db psql -U hugr -d hugr -c \
  "INSERT INTO facts (scope, key, value, asserted_by, confirmed_at)
   VALUES ('*', 'location', 'Portland, OR metro', 'user', NOW());"

docker exec hugr-facts-db psql -U hugr -d hugr -tAc \
  "SELECT value FROM facts WHERE scope = '*' AND key = 'location';"
# -> Portland, OR metro
```

The same acceptance path is covered by `tests/test_facts_schema.py`, which skips
when this container is not running:

```bash
pytest tests/test_facts_schema.py -v
```

## Schema

See [`init.sql`](./init.sql). The `facts` table:

| column         | notes                                                              |
| -------------- | ------------------------------------------------------------------ |
| `scope`        | `'*'` = global; else a project key. Part of the PK.                |
| `key`          | Fact key. Part of the PK.                                          |
| `value`        | Fact value, injected verbatim.                                     |
| `asserted_by`  | Provenance: `'user'` or `'model'` (CHECK-constrained).             |
| `confirmed_at` | `NULL` = Pending; `NOT NULL` = Confirmed (the sole injection gate).|
| `created_at`   | Defaults to `NOW()`.                                               |
| `updated_at`   | Defaults to `NOW()`; kept current by a trigger.                    |

Primary key is the composite `(scope, key)` — one value per scope+key. No
embedding column (Facts are not fuzzy-searched) and no `project` column (Fact
Scope subsumes it).
