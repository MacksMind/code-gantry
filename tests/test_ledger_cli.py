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
        assert run("ledger", "answer", f.finding_id, "amend").exit_code != 0  # amend needs text
        result = run("ledger", "answer", f.finding_id, "amend", "--text", "Both callers are admin-only.")
        assert result.exit_code == 0
        assert run("ledger", "findings").output.strip() == ""
        assert "amend — Both callers" in run("ledger", "findings", "--all").output
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


class TestWhatIsWaitingOnAPerson:
    """The verbs behind the cards: one queue, a card attached, a question
    back, and a move to another project. Every one has a `--json` face,
    since the dashboard, `claude -p` and a person's own session all call
    these and nothing else."""

    def _second_project(self, project, tmp_path):
        repo, config, paths, sha = project
        (repo / "docs" / "general.md").write_text("# General debt\n\n## Inherited\n\n- [ ] **An old one.**\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "general"], cwd=repo, check=True)
        other = tmp_path / "general.yaml"
        other.write_text(
            config.read_text()
            .replace("plan_root: docs/plan.md", "plan_root: docs/general.md")
            .replace("ledger:\n  key_prefix: p\n", "ledger:\n  key_prefix: g\n  path: general/ledger.db\n")
        )
        result = run("plan", "import", "docs/general.md", "--config", str(other))
        assert result.exit_code == 0, result.output
        return other

    def test_waiting_lists_human_items_and_findings_with_their_threads(self, project, tmp_path):
        import json

        repo, config, paths, sha = project
        imported(project)
        run("plan", "edit", "p.006", "--owner", "human")
        writer = open_ledger(paths.ledger, origin="run-host", actor="run:1")
        f = writer.open_finding(keys=["p.004"], by="planner", claim="Two callers remain.", needs="human")
        writer.close()
        card = tmp_path / "card.json"
        card.write_text(json.dumps({
            "says": "two callers", "anchors": ["app/thing.rb:1"], "checked": "both admin-only",
            "recommend": {"disposition": "discard", "text": "duplicate"}, "would_write": None,
        }))
        result = run("ledger", "recommend", f.finding_id, "--file", str(card), "--json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["recommendation"]["recommend"]["disposition"] == "discard"
        result = run("ledger", "ask", f.finding_id, "--text", "which caller?", "--json")
        assert result.exit_code == 0, result.output
        assert [e["kind"] for e in json.loads(result.output)["thread"]] == ["recommended", "asked"]

        rows = json.loads(run("ledger", "waiting", "--json").output)
        assert [(r["kind"], r["id"]) for r in rows] == [("item", "p.006"), ("finding", f.finding_id)]
        assert rows[1]["recommendation"]["recommend"]["text"] == "duplicate"
        assert rows[1]["thread"][1]["text"] == "which caller?"
        text = run("ledger", "waiting").output
        assert "p.006" in text and f.finding_id in text and "discard" in text

    def test_move_takes_a_finding_or_an_item_to_another_project(self, project, tmp_path):
        import json

        repo, config, paths, sha = project
        imported(project)
        other = self._second_project(project, tmp_path)
        run("plan", "edit", "p.006", "--owner", "human")
        writer = open_ledger(paths.ledger, origin="run-host", actor="run:1")
        f = writer.open_finding(keys=["p.004"], by="planner", claim="General, not ours.", needs="human")
        writer.close()

        result = run("ledger", "move", f.finding_id, "--to", str(other), "--json")
        assert result.exit_code == 0, result.output
        moved = json.loads(result.output)
        assert moved["from"] == f.finding_id and moved["opened_as"].startswith("f-")
        assert json.loads(run("ledger", "waiting", "--json", "--config", str(other)).output)[-1]["id"] == moved["opened_as"]
        assert f.finding_id not in run("ledger", "waiting").output

        # An item needs a section there; the document's key will do.
        assert run("ledger", "move", "p.006", "--to", str(other)).exit_code != 0
        result = run("ledger", "move", "p.006", "--to", str(other), "--under", "g.002", "--json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["opened_as"] == "g.004"
        assert "g.004" in run("ledger", "show", "--open", "--config", str(other)).output
        assert "p.006 struck" in run("ledger", "show", "p.006").output


class TestDrawable:
    def test_drawable_is_open_pipeline_owned_items(self, project):
        import json
        imported(project)
        run("plan", "edit", "p.006", "--owner", "human")
        run("ledger", "claim", "p.008")
        rows = json.loads(run("ledger", "show", "--drawable", "--json").output)
        # p.003 landed, p.006 a person's, p.008 claimed: one left.
        assert [r["key"] for r in rows] == ["p.004"]
        assert run("ledger", "show", "--drawable").output.startswith("p.004 open")


class TestSections:
    def test_sections_are_the_documents_and_sections_in_plan_order_with_depth(self, project):
        import json
        imported(project)
        rows = json.loads(run("plan", "sections", "--json").output)
        assert [(r["key"], r["kind"], r["depth"]) for r in rows] == [
            ("p.001", "document", 0), ("p.002", "section", 1), ("p.005", "section", 1), ("p.007", "document", 0),
        ]
        assert rows[1]["title"] == "Routes" and rows[1]["parent"] == "p.001"
        text = run("plan", "sections").output
        assert "p.001 Demo plan\n  p.002 Routes\n" in text


class TestAccept:
    """`accept` applies what a card recommends as the events the answer
    would have been, so a click on the dashboard is the same answer a
    person would have typed. One card, one transaction."""

    def _card(self, recommend, **rest):
        import json
        return json.dumps({"says": "s", "anchors": [], "checked": "c", "recommend": recommend, "would_write": None, **rest})

    def test_a_finding_takes_its_disposition(self, project):
        import json
        repo, config, paths, sha = project
        imported(project)
        writer = open_ledger(paths.ledger, origin="run-host", actor="run:1")
        f = writer.open_finding(keys=["p.004"], by="planner", claim="dup", needs="human")
        writer.close()
        run("ledger", "recommend", f.finding_id, "--card", self._card({"disposition": "discard", "text": "duplicate of p.002"}))
        result = run("ledger", "accept", f.finding_id, "--json")
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["about"] == f.finding_id and out["disposition"] == "discard"
        assert f.finding_id not in run("ledger", "waiting").output
        assert "discard — duplicate of p.002" in run("ledger", "findings", "--all").output

    def test_a_finding_recommending_landings_lands_each_key_and_closes(self, project):
        repo, config, paths, sha = project
        imported(project)
        writer = open_ledger(paths.ledger, origin="run-host", actor="run:1")
        f = writer.open_finding(keys=[], by="planner", claim="already fixed", needs="human")
        writer.close()
        card = self._card({"disposition": "landed", "landings": [{"key": "p.004", "sha": "abc1234"}, {"key": "p.006", "sha": "def5678"}],
                           "text": "two unrecorded landings"})
        run("ledger", "recommend", f.finding_id, "--card", card)
        result = run("ledger", "accept", f.finding_id)
        assert result.exit_code == 0, result.output
        shown = run("ledger", "show", "--landed").output
        assert "p.004 landed" in shown and "abc1234" in shown and "p.006 landed" in shown
        assert f.finding_id not in run("ledger", "waiting").output
        assert "landed p.004 abc1234, landed p.006 def5678" in run("ledger", "findings", "--all").output

    def test_an_item_is_landed_struck_or_handed_to_the_fleet(self, project):
        repo, config, paths, sha = project
        imported(project)
        for key in ("p.004", "p.006", "p.008"):
            run("plan", "edit", key, "--owner", "human")
        run("ledger", "recommend", "p.004", "--card", self._card({"disposition": "landed", "sha": "abc1234"}))
        run("ledger", "recommend", "p.006", "--card", self._card({"disposition": "struck", "text": "zero population"}))
        run("ledger", "recommend", "p.008", "--card", self._card({"disposition": "pipeline"}))
        for key in ("p.004", "p.006", "p.008"):
            assert run("ledger", "accept", key).exit_code == 0
        shown = run("ledger", "show").output
        assert "p.004 landed" in shown and "p.006 struck" in shown
        assert "p.008 open" in shown and "(human)" not in [line for line in shown.splitlines() if line.startswith("p.008")][0]
        assert run("ledger", "waiting").output.strip() == ""

    def test_a_move_goes_to_the_named_project(self, project, tmp_path):
        repo, config, paths, sha = project
        imported(project)
        other = TestWhatIsWaitingOnAPerson()._second_project(project, tmp_path)
        writer = open_ledger(paths.ledger, origin="run-host", actor="run:1")
        f = writer.open_finding(keys=[], by="planner", claim="general", needs="human")
        writer.close()
        run("ledger", "recommend", f.finding_id, "--card", self._card({"disposition": "move", "to": str(other)}))
        result = run("ledger", "accept", f.finding_id)
        assert result.exit_code == 0, result.output
        assert f.finding_id not in run("ledger", "waiting").output
        assert "general" in run("ledger", "waiting", "--config", str(other)).output

    def test_no_card_or_a_card_that_cannot_apply_is_refused(self, project):
        repo, config, paths, sha = project
        imported(project)
        run("plan", "edit", "p.006", "--owner", "human")
        assert run("ledger", "accept", "p.006").exit_code != 0
        run("ledger", "recommend", "p.006", "--card", self._card({"disposition": "amend", "text": "x"}))
        result = run("ledger", "accept", "p.006")
        assert result.exit_code != 0 and "amend" in result.output


class TestCandidates:
    def test_dismiss_takes_a_rejected_candidate_off_the_rework_list(self, project):
        import json
        from code_gantry.ledger import CANDIDATE_PUSHED, CANDIDATE_REJECTED

        repo, config, paths, sha = project
        imported(project)
        writer = open_ledger(paths.ledger, origin="run-host", actor="run:1")
        writer.append(CANDIDATE_PUSHED, sha="a" * 40, stage_id="s", branch="work-stage/001-x", base="b" * 40, landing={"keys": ["p.004"]}, fields={})
        writer.append(CANDIDATE_REJECTED, sha="c" * 40, branch="work-stage/001-x", reason="would not replay")
        writer.close()
        assert "rejected work-stage/001-x" in run("ledger", "candidates").output
        assert run("ledger", "dismiss", "nope", "--reason", "x").exit_code != 0
        result = run("ledger", "dismiss", "work-stage/001-x", "--reason", "already on the branch", "--json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["dismissed"] is True
        assert run("ledger", "candidates").output.strip() == "no candidates"


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
