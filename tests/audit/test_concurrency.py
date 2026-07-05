"""Concurrent appends from multiple threads lose no records."""

from __future__ import annotations

import threading

from firm.audit import AuditLog


def test_concurrent_appends_lose_no_records(db_url: str) -> None:
    audit = AuditLog(database_url=db_url)
    try:
        n_threads = 8
        per_thread = 20
        errors: list[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                for i in range(per_thread):
                    audit.record(f"event.{thread_id}.{i}", actor=("Worker", str(thread_id)))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        rows = audit.history(limit=n_threads * per_thread)
        assert len(rows) == n_threads * per_thread
        assert len({(r["actor_id"], r["action"]) for r in rows}) == n_threads * per_thread
    finally:
        audit.close()
