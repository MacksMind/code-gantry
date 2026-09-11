"""Importing a ledger file from before one sequence per ledger.

The old file kept a sequence per origin and replayed rows by `(at, origin,
seq)`; ids were `<origin>-<seq>`. The import keeps the replay order, gives
each row the new ledger's sequence, and rewrites every id a body names so
a finding answered under its old id is still the finding that was opened.
"""

import json
import sqlite3

import pytest

from code_gantry.ledger import FINDING_OPENED, LedgerError, import_old_file, open_ledger

OLD_SCHEMA = """
CREATE TABLE events (
    origin TEXT NOT NULL, seq INTEGER NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL,
    key TEXT, stage_id TEXT, run_id TEXT, sha TEXT, body TEXT NOT NULL,
    PRIMARY KEY (origin, seq)
)
"""


def old_file(tmp_path, rows):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(OLD_SCHEMA)
    for origin, seq, at, kind, body in rows:
        conn.execute(
            "INSERT INTO events (origin, seq, at, kind, key, body) VALUES (?, ?, ?, ?, ?, ?)",
            (origin, seq, at, kind, "p.001", json.dumps(body)),
        )
    conn.commit()
    conn.close()
    return path


class TestImport:
    def test_rows_keep_their_replay_order_and_take_one_sequence(self, tmp_path):
        path = old_file(tmp_path, [
            ("spark", 1, "2026-01-01T00:00:01+00:00", "a", {}),
            ("spark", 2, "2026-01-01T00:00:03+00:00", "c", {}),
            ("mac", 1, "2026-01-01T00:00:02+00:00", "b", {}),
        ])
        led = open_ledger(tmp_path / "new.db", origin="mac")
        assert import_old_file(led, path) == 3
        assert [(e.origin, e.seq, e.kind) for e in led.events()] == [
            ("spark", 1, "a"), ("mac", 2, "b"), ("spark", 3, "c"),
        ]

    def test_ids_a_body_names_follow_the_row_they_named(self, tmp_path):
        path = old_file(tmp_path, [
            ("mac", 1, "2026-01-01T00:00:01+00:00", "x", {}),
            ("spark", 1, "2026-01-01T00:00:02+00:00", FINDING_OPENED, {"keys": ["p.001"], "by": "planner", "claim": "c"}),
            ("spark", 2, "2026-01-01T00:00:03+00:00", "finding.answered", {"finding_id": "f-spark-1", "text": "ok"}),
            ("spark", 3, "2026-01-01T00:00:04+00:00", "stage.taken", {"derived_id": "d-spark-9"}),
        ])
        led = open_ledger(tmp_path / "new.db", origin="mac")
        import_old_file(led, path)
        opened, answered, taken = led.events()[1:]
        assert opened.finding_id == "f-spark-2"
        assert answered.body["finding_id"] == opened.finding_id, "the answer still names the finding"
        assert taken.body["derived_id"] == "d-spark-9", "an id naming no row is left alone"

    def test_an_old_file_cannot_simply_be_opened(self, tmp_path):
        from code_gantry.ledgerstore import StoreError

        path = old_file(tmp_path, [("spark", 1, "2026-01-01T00:00:01+00:00", "a", {})])
        with pytest.raises(StoreError, match="ledger import"):
            open_ledger(path, origin="mac")

    def test_a_ledger_that_holds_events_refuses_an_import(self, tmp_path):
        path = old_file(tmp_path, [("spark", 1, "2026-01-01T00:00:01+00:00", "a", {})])
        led = open_ledger(tmp_path / "new.db", origin="mac")
        led.append("already")
        with pytest.raises(LedgerError, match="already holds"):
            import_old_file(led, path)
        assert len(led.events()) == 1
