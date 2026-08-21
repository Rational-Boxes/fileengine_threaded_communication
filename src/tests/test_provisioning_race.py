# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Tenant provisioning must happen once, under a lock.

The bug these pin produced ``psycopg.errors.DeadlockDetected`` from ordinary
dashboard reads, intermittently: a page load fans out several requests, FastAPI
runs sync handlers on a worker-thread pool, and every one of them re-ran the
tenant DDL in its own transaction. Idempotent DDL is not concurrency-safe DDL —
concurrent ``CREATE``/``ALTER`` on the same tables take locks in interleaved
order and Postgres kills one of them.

No database here on purpose. The property is about *how many times* the DDL runs
and *under what mutual exclusion*, which a fake connection shows precisely and a
live one only shows on unlucky timing.
"""
import threading
import time

import pytest

from discussion import db
from discussion.schema import ensure_tenant_schema


class _Cursor:
    def __init__(self, log):
        self._log = log

    def execute(self, sql, params=None):
        self._log.append((str(sql), params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    """Just enough psycopg to walk connect_for_tenant."""

    def __init__(self):
        self.log: list = []
        self.commits = 0

    def cursor(self):
        return _Cursor(self.log)

    def commit(self):
        self.commits += 1

    def close(self):
        pass


class _Config:
    pg_dsn = "postgresql://unused"
    pg_replica_enabled = False
    embedding_dimension = 1024
    db_statement_timeout_ms = 0


@pytest.fixture(autouse=True)
def _clean_memo():
    """A cold process, per test. Reaching into the module's own memo on purpose:
    it is process-global state, and a test that inherited another's would pass
    for the wrong reason."""
    db._provisioned.clear()
    yield
    db._provisioned.clear()


def _patch(monkeypatch, on_ddl):
    monkeypatch.setattr(db, "connect", lambda config, readonly=False: _Conn())
    monkeypatch.setattr(db, "ensure_tenant_schema", on_ddl)


def test_concurrent_first_touch_provisions_exactly_once(monkeypatch):
    """The reported failure. Twelve threads arrive together on a cold process."""
    runs = []

    def ddl(conn, tenant, dimension=1024):
        runs.append(tenant)
        # Hold the section open. Without mutual exclusion the other threads
        # sail through the membership check while this one is still working,
        # which is exactly the window that reaches Postgres as a deadlock.
        time.sleep(0.05)
        return f"tenant_{tenant}"

    _patch(monkeypatch, ddl)

    def worker():
        db.connect_for_tenant(_Config(), "acme", provision=True)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not any(t.is_alive() for t in threads), "provisioning deadlocked or hung"
    assert runs == ["acme"], f"DDL ran {len(runs)} times concurrently, not once"


def test_provision_flag_does_not_re_run_the_ddl(monkeypatch):
    """What turned a once-per-process race into a once-per-REQUEST one.

    Six stores pass provision=True, and it used to bypass the memo entirely, so
    the DDL ran under every concurrent request for the life of the process."""
    runs = []
    _patch(monkeypatch, lambda conn, tenant, dimension=1024: runs.append(tenant))

    for _ in range(5):
        db.connect_for_tenant(_Config(), "acme", provision=True)

    assert len(runs) == 1, "provision=True re-provisioned an already-provisioned tenant"


def test_each_tenant_is_provisioned_on_its_own(monkeypatch):
    """The memo is per tenant — one tenant's arrival must not mark another's."""
    runs = []
    _patch(monkeypatch, lambda conn, tenant, dimension=1024: runs.append(tenant))

    db.connect_for_tenant(_Config(), "acme")
    db.connect_for_tenant(_Config(), "someco")
    db.connect_for_tenant(_Config(), "acme")

    assert runs == ["acme", "someco"]


def test_replica_reads_still_skip_the_ddl(monkeypatch):
    """A standby cannot run DDL. Unchanged behaviour, pinned so the rework of
    the condition above did not quietly drop it."""
    runs = []
    _patch(monkeypatch, lambda conn, tenant, dimension=1024: runs.append(tenant))

    class Replicated(_Config):
        pg_replica_enabled = True

    db.connect_for_tenant(Replicated(), "acme", provision=True, readonly=True)
    assert runs == []


def test_search_path_is_set_even_when_provisioning_is_skipped(monkeypatch):
    """The schema name is needed on every connection, not only the one that
    provisions — an early return from the memo must not leave search_path unset."""
    conn = _Conn()
    monkeypatch.setattr(db, "connect", lambda config, readonly=False: conn)
    monkeypatch.setattr(db, "ensure_tenant_schema", lambda *a, **k: None)

    db.connect_for_tenant(_Config(), "acme")   # provisions
    conn.log.clear()
    db.connect_for_tenant(_Config(), "acme")   # memoised

    assert any("search_path" in sql and "tenant_acme" in sql for sql, _ in conn.log)


def test_ddl_takes_the_advisory_lock_first():
    """Cross-process serialisation: the workers, the consumer and the digest job
    have separate memos, so the in-process lock cannot cover them.

    Order matters — a lock taken after the DDL guards nothing."""
    conn = _Conn()
    ensure_tenant_schema(conn, "acme", 1024)

    statements = [sql for sql, _ in conn.log]
    assert any("pg_advisory_xact_lock" in s for s in statements), "no cross-process lock"
    lock_at = next(i for i, s in enumerate(statements) if "pg_advisory_xact_lock" in s)
    ddl_at = next(i for i, s in enumerate(statements) if "CREATE TABLE" in s)
    assert lock_at < ddl_at, "the lock was taken after the DDL it is meant to guard"

    # Transaction-scoped, so the commit is what releases it. If this connection
    # were autocommit the lock would be gone before the DDL ran.
    assert conn.commits == 1
