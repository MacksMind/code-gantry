"""`code-gantry plan …` and `code-gantry ledger …`, driven through the real entry point."""

import subprocess

import pytest
from click.testing import CliRunner

from code_gantry import cli
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
