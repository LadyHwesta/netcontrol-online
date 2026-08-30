"""
Tests for GET /admin/db-stats -- native, lightweight Postgres visibility for
the Admin page (connection counts, table sizes, slow queries via
pg_stat_statements if installed). Added instead of integrating pghero (a
separate Ruby/Rack tool) to avoid running a second language runtime for a
single-process, club-scale deployment -- see TECH_DEBT.md.

The test suite always runs against SQLite (see conftest.py), so the
Postgres-specific SQL paths (pg_stat_activity/pg_stat_user_tables/
pg_stat_statements) aren't exercised here -- only the SQLite no-op
short-circuit and the access-control gate are. Those paths were verified by
hand against a real Postgres instance during development.
"""


def test_requires_auth(client):
    resp = client.get("/admin/db-stats")
    assert resp.status_code == 401


def test_requires_admin(client, user_headers):
    resp = client.get("/admin/db-stats", headers=user_headers)
    assert resp.status_code == 403


def test_sqlite_short_circuits_to_dialect_only(client, admin_headers):
    """The test DB is SQLite -- every Postgres-only field should come back
    empty/None rather than the endpoint attempting (and failing) to run
    Postgres-only SQL against it."""
    resp = client.get("/admin/db-stats", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["dialect"] == "sqlite"
    assert data["database_size"] is None
    assert data["connections"] is None
    assert data["tables"] == []
    assert data["pg_stat_statements_available"] is False
    assert data["slow_queries"] == []
    assert data["slow_queries_note"] is None
