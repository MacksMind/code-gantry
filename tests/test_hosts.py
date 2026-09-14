"""Every host's state and every daemon's events, in the table.

`_hosts` is a ledger each daemon appends its state to; the latest row per
origin is what a host is doing now, and a host that stopped writing shows
its last row with its age. `_events` is one line per thing a daemon did,
from every host, in one sequence. Both reuse the ledger store, so nothing
here is a second way to reach the table.
"""

from click.testing import CliRunner

from code_gantry import hosts
from code_gantry.ledgerstore import DynamoStore, MemoryTable


def store(table, name):
    return DynamoStore(table, name, holder="test")


class TestHosts:
    def test_the_latest_row_per_origin_is_the_state(self):
        table = MemoryTable()
        s = store(table, hosts.HOSTS)
        hosts.put(s, origin="host-b", node="d@host-b", code="aaaaaaaaaaaa", at="2026-01-01T00:00:00+00:00",
                  bays=[hosts.Bay("bay1", "repo", "technical_debt", "running", "r1", "2026-01-01T00:00:00+00:00")])
        hosts.put(s, origin="host-a", node="d@host-a", code="bbbbbbbbbbbb", at="2026-01-01T00:01:00+00:00", bays=[])
        hosts.put(s, origin="host-b", node="d@host-b", code="cccccccccccc", at="2026-01-01T00:02:00+00:00",
                  bays=[hosts.Bay("bay1", "repo", "technical_debt", "finished", "r1", "2026-01-01T00:02:00+00:00"),
                        hosts.Bay("bay2", "repo", "rails_6", "running", "r2", "2026-01-01T00:02:00+00:00")])
        latest = hosts.latest(s)
        assert [h.origin for h in latest] == ["host-a", "host-b"]
        second = next(h for h in latest if h.origin == "host-b")
        assert second.code == "cccccccccccc"
        assert [(b.name, b.project, b.state) for b in second.bays] == [("bay1", "technical_debt", "finished"), ("bay2", "rails_6", "running")]

    def test_rendering_names_every_host_repository_project_and_bay_with_age(self):
        table = MemoryTable()
        s = store(table, hosts.HOSTS)
        hosts.put(s, origin="host-b", node="d@host-b", code="aaaaaaaaaaaa", at="2026-01-01T00:00:00+00:00",
                  bays=[hosts.Bay("bay1", "repo", "technical_debt", "running", "r1", "2026-01-01T00:00:00+00:00")])
        text = hosts.render(hosts.latest(s), now="2026-01-01T00:05:30+00:00")
        assert "host-b  d@host-b  code aaaaaaaaaaaa  written 5m ago" in text
        assert "  repo/technical_debt  bay1  running  r1  since 2026-01-01T00:00:00+00:00" in text

    def test_a_host_that_stopped_writing_is_marked_stale(self):
        table = MemoryTable()
        s = store(table, hosts.HOSTS)
        hosts.put(s, origin="rv", node="d@rv", code="aaaaaaaaaaaa", at="2026-01-01T00:00:00+00:00", bays=[])
        text = hosts.render(hosts.latest(s), now="2026-01-02T03:00:00+00:00")
        assert "rv  d@rv  code aaaaaaaaaaaa  written 1d 3h ago (stale)" in text


class TestEvents:
    def test_events_from_every_host_are_one_sequence(self):
        table = MemoryTable()
        s = store(table, hosts.EVENTS)
        hosts.event(s, origin="host-b", at="2026-01-01T00:00:00+00:00", text="bay1: run r1 started")
        hosts.event(s, origin="host-a", at="2026-01-01T00:00:01+00:00", text="code: a -> b; daemon: 8 module(s) loaded")
        lines = hosts.events_after(s, 0)
        assert [(e.seq, e.origin, e.text) for e in lines] == [
            (1, "host-b", "bay1: run r1 started"), (2, "host-a", "code: a -> b; daemon: 8 module(s) loaded"),
        ]
        assert hosts.events_after(s, 1)[0].seq == 2
        assert hosts.render_event(lines[0]) == "2026-01-01T00:00:00+00:00 [host-b] bay1: run r1 started"


class TestTheCommands:
    """Driven through the real click entry point, with the table swapped for
    the in-memory one."""

    def run(self, table, *args):
        from code_gantry import cli

        return CliRunner().invoke(cli.main, list(args), catch_exceptions=False, obj=None), table

    def test_hosts_put_and_list(self, monkeypatch, tmp_path):
        from code_gantry import cli

        table = MemoryTable()
        monkeypatch.setattr(hosts, "table_for", lambda cfg: table)
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("")
        monkeypatch.setattr(hosts, "config_for", lambda path: None)
        r = CliRunner().invoke(cli.main, [
            "hosts", "put", "--config", str(cfg), "--origin", "host-b", "--node", "d@host-b", "--code", "aaaaaaaaaaaa",
            "--bay", "bay1|repo|technical_debt|running|r1|2026-01-01T00:00:00+00:00",
        ], catch_exceptions=False)
        assert r.exit_code == 0, r.output
        r = CliRunner().invoke(cli.main, ["hosts", "--config", str(cfg)], catch_exceptions=False)
        assert r.exit_code == 0, r.output
        assert "host-b  d@host-b  code aaaaaaaaaaaa" in r.output
        assert "repo/technical_debt  bay1  running  r1" in r.output

    def test_events_put_and_list(self, monkeypatch, tmp_path):
        from code_gantry import cli

        table = MemoryTable()
        monkeypatch.setattr(hosts, "table_for", lambda cfg: table)
        monkeypatch.setattr(hosts, "config_for", lambda path: None)
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("")
        r = CliRunner().invoke(cli.main, ["events", "put", "--config", str(cfg), "--origin", "host-a", "--text", "bay2: placed"], catch_exceptions=False)
        assert r.exit_code == 0, r.output
        r = CliRunner().invoke(cli.main, ["events", "--config", str(cfg), "--after", "0"], catch_exceptions=False)
        assert r.exit_code == 0, r.output
        assert r.output.strip().endswith("[host-a] bay2: placed")
