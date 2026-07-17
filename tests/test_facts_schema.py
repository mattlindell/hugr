"""
Integration test for the PV-1 dedicated pgvector facts store.

Exercises the acceptance criterion from PV-1 / ADR 0002 against a live
`hugr-facts-db` container: the pgvector extension is installed, the `facts`
table matches the ADR 0002 shape, and a fresh psql can insert and read the
canonical global fact ('*', 'location', 'Portland, OR metro').

This test drives psql *inside* the container via `docker exec`, so it needs
neither psycopg2 nor psql on the host (and it sidesteps the psycopg2 mock in
conftest.py). It SKIPS cleanly when Docker is unavailable or the store is not
running, keeping the default `pytest tests/` suite Docker-free and green.

To run it for real:

    POSTGRES_PASSWORD=... docker compose -f deploy/facts/docker-compose.yml up -d --wait
    pytest tests/test_facts_schema.py -v
"""
import shutil
import subprocess

import pytest

CONTAINER = "hugr-facts-db"
DB_USER = "hugr"
DB_NAME = "hugr"

# The canonical global fact from PV-1's acceptance criterion.
FACT_SCOPE = "*"
FACT_KEY = "location"
FACT_VALUE = "Portland, OR metro"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _container_running() -> bool:
    if not _docker_available():
        return False
    result = subprocess.run(
        ["docker", "ps", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    return CONTAINER in result.stdout.split()


pytestmark = pytest.mark.skipif(
    not _container_running(),
    reason=(
        f"{CONTAINER} container not running; start it with "
        "`docker compose -f deploy/facts/docker-compose.yml up -d --wait`"
    ),
)


def psql(sql: str) -> str:
    """Run a single SQL statement in the container and return trimmed stdout.

    Uses -tA for a bare, unaligned value with no header/footer. Connects over
    the container's local socket as the bootstrap superuser (trusted), so no
    password is needed.
    """
    result = subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME, "-tAc", sql],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"psql failed for {sql!r}:\n{result.stderr}"
    return result.stdout.strip()


def psql_expect_error(sql: str) -> str:
    """Run SQL expected to fail; return stderr. Fails the test if psql succeeds."""
    result = subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME,
         "-v", "ON_ERROR_STOP=1", "-tAc", sql],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, f"expected {sql!r} to fail, but it succeeded"
    return result.stderr


def test_pgvector_extension_installed():
    """The pgvector extension is installed so the schema is ready for 1.0 vectors."""
    assert psql("SELECT 1 FROM pg_extension WHERE extname = 'vector'") == "1"


def test_facts_table_shape_matches_adr_0002():
    """facts has exactly the ADR 0002 columns, no embedding and no project column."""
    columns = psql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'facts' ORDER BY column_name"
    ).splitlines()
    assert columns == [
        "asserted_by",
        "confirmed_at",
        "created_at",
        "key",
        "scope",
        "updated_at",
        "value",
    ]


def test_facts_primary_key_is_scope_key():
    """The PK is the composite (scope, key) — single-valued per scope+key."""
    pk_cols = psql(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'facts'::regclass AND i.indisprimary "
        "ORDER BY a.attname"
    ).splitlines()
    assert pk_cols == ["key", "scope"]


def test_confirmed_at_is_nullable():
    """confirmed_at NULL = Pending; it must be nullable so proposals can land Pending."""
    assert psql(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'facts' AND column_name = 'confirmed_at'"
    ) == "YES"


def test_asserted_by_rejects_unknown_provenance():
    """Provenance is constrained to the {user, model} domain from ADR 0002."""
    stderr = psql_expect_error(
        "INSERT INTO facts (scope, key, value, asserted_by) "
        "VALUES ('*', '__bad_provenance__', 'x', 'distiller')"
    )
    assert "constraint" in stderr.lower() or "check" in stderr.lower()


def test_insert_and_read_global_location_fact():
    """PV-1 acceptance: a fresh psql can insert and read the global location fact."""
    try:
        psql(
            "INSERT INTO facts (scope, key, value, asserted_by, confirmed_at) "
            f"VALUES ('{FACT_SCOPE}', '{FACT_KEY}', '{FACT_VALUE}', 'user', NOW()) "
            "ON CONFLICT (scope, key) DO UPDATE SET value = EXCLUDED.value"
        )
        value = psql(
            f"SELECT value FROM facts WHERE scope = '{FACT_SCOPE}' AND key = '{FACT_KEY}'"
        )
        assert value == FACT_VALUE
    finally:
        psql(f"DELETE FROM facts WHERE scope = '{FACT_SCOPE}' AND key = '{FACT_KEY}'")
