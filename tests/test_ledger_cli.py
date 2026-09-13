"""`code-gantry plan …` and `code-gantry ledger …`, driven through the real entry point."""

import subprocess

import pytest
from click.testing import CliRunner

from code_gantry import cli
from code_gantry.gitops import Git
from code_gantry.ledger import open_ledger, read_ledger
from code_gantry.runtime import ProjectPaths

PLAN = """# Demo plan

Intro prose. Details in [debt.md](debt.md).

## Routes

About routes.

- [x] ~~**Old route gone.**~~ — `{sha}`. Removed cleanly.
- [ ] **Add the new route.** Under `admin`.

## Later

- [ ] **Tidy the helper.**
"""

DEBT = """# Debt

- [ ] **A stray N+1.**
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    repo = tmp_path / "target"
    (repo / "docs").mkdir(parents=True)
    (repo / "app").mkdir()
    (repo / "app" / "thing.rb").write_text("x\n")
    (repo / ".gitignore").write_text(".code_gantry/\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "T"],
        ["config", "commit.gpgsign", "false"],
        ["add", "-A"],
        ["commit", "-q", "-m", "initial"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    sha = subprocess.run(["git", "rev-parse", "--short=9", "HEAD"], cwd=repo,
                         check=True, capture_output=True, text=True).stdout.strip()
    (repo / "docs" / "plan.md").write_text(PLAN.format(sha=sha))
    (repo / "docs" / "debt.md").write_text(DEBT)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "plan"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=repo, check=True)

    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
target_repo: {repo}
base_ref: main
project_branch: work
plan_root: docs/plan.md
full_test_command: "true"
executor:
  model: m
planner:
  model: claude-opus-5
reviewer:
  model: gpt-5.5
scoped_test_tool: scoped_suite
project_tools:
  - name: scoped_suite
    description: The suite, taking a selection.
    command: ['true', '{{paths}}']
    arguments:
      - {{name: paths, description: Files or examples., repeated: true}}
    roles: ['executor']
ledger:
  key_prefix: p
"""
    )
    monkeypatch.setenv(cli.CONFIG_ENV, str(config))
    monkeypatch.setenv("CODE_GANTRY_ACTOR", "mack")
    monkeypatch.setenv("CODE_GANTRY_ORIGIN", "test-host")
    paths = ProjectPaths(repo / "docs" / ".code_gantry")
    return repo, config, paths, sha


def run(*args):
    result = CliRunner().invoke(cli.main, list(args), catch_exceptions=False)
    return result


def imported(project):
    result = run("plan", "import", "docs/plan.md", "--follow-links")
    assert result.exit_code == 0, result.output
    return result


class TestPlanImport:
    def test_imports_the_root_and_its_links_and_reports_counts(self, project):
        result = imported(project)
        assert "docs/plan.md: p.001" in result.output
        assert "docs/debt.md:" in result.output
        assert "2 document(s), 2 section(s), 4 item(s); 1 landed, 0 struck" in result.output

    def test_records_the_closed_item_with_its_sha(self, project):
        repo, config, paths, sha = project
        imported(project)
        views = read_ledger(paths.ledger).views()
        landed = [k for k, s in views.key_states.items() if s.state == "landed"]
        assert len(landed) == 1
        assert views.state(landed[0]).sha == sha
        assert all(e.body.get("actor") == "mack" and e.origin == "test-host" for e in read_ledger(paths.ledger).events())

    def test_refuses_without_a_ledger_section(self, project, tmp_path):
        repo, config, paths, sha = project
        config.write_text(config.read_text().replace("ledger:\n  key_prefix: p\n", ""))
        result = run("plan", "import", "docs/plan.md")
        assert result.exit_code != 0 and "ledger" in result.output

    def test_a_missing_file_is_refused(self, project):
        result = run("plan", "import", "docs/nope.md")
        assert result.exit_code != 0 and "not a file" in result.output


class TestPlanCommands:
    def test_export_round_trips_and_show_describes_a_node(self, project):
        imported(project)
        result = run("plan", "export", "p.001")
        assert result.exit_code == 0
        assert "# Demo plan {#p.001}" in result.output
        assert "- [x] {#p.003} ~~**Old route gone.**~~ —" in result.output
        assert "- [ ] {#p.004} **Add the new route.** Under `admin`." in result.output
        result = run("plan", "show", "p.004")
        assert "p.004 [item, pipeline] v1" in result.output
        assert "under: Demo plan > Routes" in result.output
        assert "state: open" in result.output

    def test_export_takes_only_a_document(self, project):
        imported(project)
        assert run("plan", "export", "p.002").exit_code != 0

    def test_add_edit_and_retire(self, project):
        repo, config, paths, sha = project
        imported(project)
        result = run("plan", "add", "--under", "p.002", "--title", "One more", "--owner", "human")
        assert result.exit_code == 0 and result.output.strip() == "p.009"
        assert read_ledger(paths.ledger).views().nodes["p.009"].owner == "human"
        result = run("plan", "edit", "p.009", "--owner", "pipeline", "--title", "Now drawable")
        assert result.exit_code == 0 and "p.009 v2" in result.output
        node = read_ledger(paths.ledger).views().nodes["p.009"]
        assert (node.owner, node.title) == ("pipeline", "Now drawable")
        assert run("plan", "retire", "p.009").exit_code == 0
        assert read_ledger(paths.ledger).views().nodes["p.009"].retired

    def test_an_unknown_key_is_refused(self, project):
        imported(project)
        for args in (("plan", "show", "p.999"), ("plan", "retire", "p.999"), ("ledger", "claim", "p.999")):
            assert run(*args).exit_code != 0


class TestLedgerCommands:
    def test_show_lists_items_and_filters_by_state(self, project):
        imported(project)
        result = run("ledger", "show")
        assert result.exit_code == 0
        assert "p.003 landed" in result.output and "p.004 open" in result.output
        assert run("ledger", "show", "--open").output.count("\n") == 3
        assert "p.003" in run("ledger", "show", "--landed").output

    def test_claim_release_land_strike(self, project):
        repo, config, paths, sha = project
        imported(project)
        assert run("ledger", "claim", "p.004").exit_code == 0
        assert read_ledger(paths.ledger).views().state("p.004").state == "claimed"
        assert run("ledger", "release", "p.004").exit_code == 0
        assert read_ledger(paths.ledger).views().state("p.004").state == "open"
        assert run("ledger", "land", "p.004", sha).exit_code == 0
        state = read_ledger(paths.ledger).views().state("p.004")
        assert state.state == "landed" and state.sha.startswith(sha)
        assert run("ledger", "strike", "p.006", "zero population").exit_code == 0
        assert read_ledger(paths.ledger).views().state("p.006").reason == "zero population"

    def test_land_refuses_a_sha_that_is_not_a_commit(self, project):
        imported(project)
        result = run("ledger", "land", "p.004", "deadbeef1")
        assert result.exit_code != 0 and "not a commit" in result.output

    def test_block_and_unblock(self, project):
        repo, config, paths, sha = project
        imported(project)
        assert run("ledger", "block", "p.004", "which admin?").exit_code == 0
        assert "p.004 blocked" in run("ledger", "show", "--blocked").output
        assert run("ledger", "unblock", "p.004", "the new one").exit_code == 0
        state = read_ledger(paths.ledger).views().state("p.004")
        assert (state.state, state.question, state.answer) == ("open", "which admin?", "the new one")

    def test_findings_answer_and_fold(self, project):
        repo, config, paths, sha = project
        imported(project)
        writer = open_ledger(paths.ledger, origin="run-host", actor="run:1")
        f = writer.open_finding(keys=["p.004"], by="planner", claim="Two callers remain.", needs="human")
        writer.close()
        result = run("ledger", "findings", "--for-human")
        assert f.finding_id in result.output and "needs human" in result.output
        assert run("ledger", "answer", f.finding_id, "fold").exit_code != 0  # fold needs text
        result = run("ledger", "answer", f.finding_id, "fold", "--text", "Both callers are admin-only.")
        assert result.exit_code == 0
        assert run("ledger", "findings").output.strip() == ""
        assert "fold — Both callers" in run("ledger", "findings", "--all").output
        result = run("ledger", "fold")
        assert result.exit_code == 0 and "mark(s) written" in result.output
        views = read_ledger(paths.ledger).views()
        assert "Both callers are admin-only." in views.nodes["p.004"].marks
        assert views.findings[f.finding_id].status == "folded"

    def test_render_prints_both_halves(self, project):
        repo, config, paths, sha = project
        imported(project)
        run("ledger", "claim", "p.004")
        plan_text = run("ledger", "render").output
        projection = run("ledger", "render", "--projection").output
        assert "# Demo plan {#p.001}" in plan_text and "claimed" not in plan_text
        assert "### Claimed" in projection and "p.004" in projection


class TestTheJsonFace:
    """What another process reads: every field of the record, as the type
    declares it, so a reader in another language sees what a reader here sees.
    The writer is the dataclass, never a hand-written list of keys."""

    def test_findings_carry_every_field_of_the_type(self, project):
        import dataclasses
        import json
        from code_gantry.ledger import Finding

        repo, config, paths, sha = project
        imported(project)
        writer = open_ledger(paths.ledger, origin="run-host", actor="run:1")
        f = writer.open_finding(keys=["p.004"], by="planner", claim="Two callers remain.", needs="human")
        writer.open_finding(keys=["p.005"], by="planner", claim="For the pipeline.", needs="pipeline")
        writer.close()
        result = run("ledger", "findings", "--for-human", "--json")
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert [row["id"] for row in rows] == [f.finding_id]
        assert set(rows[0]) == {field.name for field in dataclasses.fields(Finding)}
        assert rows[0]["needs"] == "human" and rows[0]["keys"] == ["p.004"]

    def test_show_carries_the_node_and_its_state(self, project):
        import dataclasses
        import json
        from code_gantry.ledger import KeyState, Node

        repo, config, paths, sha = project
        imported(project)
        run("ledger", "claim", "p.004")
        rows = json.loads(run("ledger", "show", "--json").output)
        by_key = {row["key"]: row for row in rows}
        # The same rows the text face prints, one per item.
        printed = [line.split()[0] for line in run("ledger", "show").output.splitlines()]
        assert list(by_key) == printed and len(printed) == 4
        node_fields = {field.name for field in dataclasses.fields(Node)}
        assert set(by_key["p.004"]) == node_fields | {"state"}
        assert set(by_key["p.004"]["state"]) == {field.name for field in dataclasses.fields(KeyState)}
        assert by_key["p.004"]["state"]["state"] == "claimed"
        assert by_key["p.003"]["state"]["state"] == "landed"
        only_open = json.loads(run("ledger", "show", "--open", "--json").output)
        assert [row["key"] for row in only_open] == [
            line.split()[0] for line in run("ledger", "show", "--open").output.splitlines()
        ]
        assert 0 < len(only_open) < len(rows)

    def test_an_answer_returns_the_finding_it_changed(self, project):
        import json

        repo, config, paths, sha = project
        imported(project)
        writer = open_ledger(paths.ledger, origin="run-host", actor="run:1")
        f = writer.open_finding(keys=["p.004"], by="planner", claim="Two callers remain.", needs="human")
        writer.close()
        result = run("ledger", "answer", f.finding_id, "discard", "--json")
        assert result.exit_code == 0, result.output
        row = json.loads(result.output)
        assert row["id"] == f.finding_id
        assert row["status"] == "discarded" and row["disposition"] == "discard"


class TestValidateSeesTheLedger:
    def test_validate_reports_the_imported_plan(self, project):
        imported(project)
        result = CliRunner().invoke(cli.main, ["validate", "--skip-tests"])
        assert "Traceback" not in result.output
        assert "ledger holds a plan" in result.output
        assert "2 document(s), 4 item(s), 3 open and drawable" in result.output

    def test_validate_before_an_import_is_not_fatal_about_it(self, project):
        result = CliRunner().invoke(cli.main, ["validate", "--skip-tests"])
        assert "Traceback" not in result.output
        assert "no ledger at" in result.output


class TestAConfiguredLedgerPath:
    def _point_elsewhere(self, project, tmp_path):
        repo, config, paths, sha = project
        config.write_text(
            config.read_text().replace(
                "ledger:\n  key_prefix: p\n",
                "ledger:\n  key_prefix: p\n  path: shared/ledger.db\n",
            )
        )
        return tmp_path / "shared" / "ledger.db"

    def test_import_and_show_use_the_configured_file(self, project, tmp_path):
        repo, config, paths, sha = project
        shared = self._point_elsewhere(project, tmp_path)
        imported(project)
        assert shared.is_file()
        assert not paths.ledger.exists(), "nothing went under the work dir"
        result = run("ledger", "show", "--open")
        assert result.exit_code == 0 and "p.004 open" in result.output

    def test_two_checkouts_naming_one_file_see_one_plan(self, project, tmp_path):
        repo, config, paths, sha = project
        shared = self._point_elsewhere(project, tmp_path)
        imported(project)
        other = open_ledger(shared, origin="test-host", actor="bay-2")
        assert other.views().documents(), "the second bay reads the plan the first imported"


class TestAFreshHost:
    """A host that has never run the project holds no ledger. Under
    `remote_landing` the run fetches the other origins' events before
    preflight reads the plan, so the first run on a new host imports
    nothing by hand."""


    def test_without_remote_landing_a_missing_ledger_is_not_created(self, project, monkeypatch):
        repo, config, paths, sha = project
        monkeypatch.setattr(cli, "run_preflight", lambda *a, **k: [])
        result = run("run")
        assert result.exit_code != 0
        assert "holds no plan" in result.output
        assert not paths.ledger.exists()


class TestDrawnStagesOnTheCommandLine:
    def _drawn(self, paths):
        from code_gantry.ledger import STAGE_DERIVED

        writer = open_ledger(paths.ledger, origin="test-host", actor="run:1")
        return writer.append(
            STAGE_DERIVED, stage_id="fix-routes", run_id="run-1", fields={"id": "fix-routes"},
            keys=["p.004"], findings=[], batch=None, rank=0,
        ).derived_id

    def test_derived_lists_what_is_waiting(self, project):
        repo, config, paths, sha = project
        imported(project)
        did = self._drawn(paths)
        result = run("ledger", "derived")
        assert result.exit_code == 0, result.output
        assert f"{did} derived  fix-routes on p.004 (drawn by run-1)" in result.output

    def test_drop_withdraws_it(self, project):
        repo, config, paths, sha = project
        imported(project)
        did = self._drawn(paths)
        result = run("ledger", "drop", did, "--reason", "not wanted")
        assert result.exit_code == 0, result.output
        assert run("ledger", "derived").output.strip() == ""
        assert f"{did} dropped : not wanted" in run("ledger", "derived", "--all").output
        again = run("ledger", "drop", did)
        assert again.exit_code != 0


class TestImportOnTheCommandLine:
    def test_an_old_file_is_imported_into_an_empty_ledger(self, project, tmp_path):
        import json
        import sqlite3

        repo, config, paths, sha = project
        # The fixture's ledger holds the project's plan; an import wants an
        # empty one, so the file and SQLite's sidecars go first.
        for sidecar in ("", "-wal", "-shm"):
            candidate = paths.ledger.with_name(paths.ledger.name + sidecar)
            if candidate.exists():
                candidate.unlink()
        old = tmp_path / "old.db"
        conn = sqlite3.connect(old)
        conn.execute(
            "CREATE TABLE events (origin TEXT NOT NULL, seq INTEGER NOT NULL, at TEXT NOT NULL,"
            " kind TEXT NOT NULL, key TEXT, stage_id TEXT, run_id TEXT, sha TEXT, body TEXT NOT NULL,"
            " PRIMARY KEY (origin, seq))"
        )
        conn.execute(
            "INSERT INTO events (origin, seq, at, kind, key, body) VALUES (?, ?, ?, ?, ?, ?)",
            ("spark", 1, "2026-01-01T00:00:00+00:00", "node.upserted", "p.001",
             json.dumps({"parent": None, "position": 0, "node_kind": "document", "title": "Plan"})),
        )
        conn.commit()
        conn.close()
        result = run("ledger", "import", str(old))
        assert result.exit_code == 0, result.output
        assert "1 event(s) imported" in result.output
        assert "p.001" in run("ledger", "render").output
        again = run("ledger", "import", str(old))
        assert again.exit_code != 0 and "already holds" in again.output
