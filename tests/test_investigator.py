"""The investigator: a model with a shell, run on one thing waiting on a
person, that writes a card through `ledger recommend` and nothing else.
Driven through the real entry point against a fake investigator command
that records the prompt it was given and, when told to, writes the card the
way a real one would — through the verb the prompt names."""

import json
import sys

import pytest

from test_ledger_cli import imported, project, run  # noqa: F401  (fixtures)

from code_gantry.config import parse_config
from code_gantry.investigator import MEANINGS
from code_gantry.ledger import RECOMMENDATIONS, open_ledger


@pytest.fixture
def investigable(project, tmp_path):
    repo, config, paths, sha = project
    imported(project)
    run("plan", "edit", "p.006", "--owner", "human")
    writer = open_ledger(paths.ledger, origin="run-host", actor="run:1")
    f = writer.open_finding(keys=["p.004"], by="planner", claim="Two callers remain, both admin-only.", needs="human")
    writer.close()
    fake = tmp_path / "fake-investigator.py"
    fake.write_text(f"""
import json, subprocess, sys
from pathlib import Path
root = Path({str(tmp_path)!r})
prompt = sys.stdin.read()
(root / "prompt.txt").write_text(prompt)
if (root / "recommend").exists():
    verb = next(line.strip() for line in prompt.splitlines() if line.strip().startswith("code-gantry ledger recommend "))
    args = verb.split()[1:]  # after `code-gantry`, up to and including --config <path>
    args = args[:args.index("--card")]
    card = {{"says": "two callers", "anchors": ["app/thing.rb:1"], "checked": "grep -rn caller app",
            "recommend": {{"disposition": "discard", "text": "duplicate"}}, "would_write": None}}
    subprocess.run([sys.executable, "-c", "from code_gantry.cli import main; main()", *args, "--card", json.dumps(card)], check=True)
print("done")
""")
    config.write_text(config.read_text() + f"investigator:\n  command: [{sys.executable!r}, {str(fake)!r}]\n  timeout_minutes: 1\n")
    return repo, config, paths, f.finding_id, tmp_path


class TestTheInvestigator:
    def test_a_card_arrives_through_the_verb_the_prompt_names(self, investigable):
        repo, config, paths, finding_id, root = investigable
        (root / "recommend").write_text("")
        result = run("ledger", "investigate", finding_id)
        assert result.exit_code == 0, result.output
        assert "card written" in result.output
        prompt = (root / "prompt.txt").read_text()
        assert "Two callers remain, both admin-only." in prompt
        assert f"code-gantry ledger recommend {finding_id} --config {config.resolve()}" in prompt
        # The others waiting, for consolidation on identity; not the thing itself.
        assert "- p.006 (item):" in prompt and f"- {finding_id} (finding)" not in prompt
        for word in RECOMMENDATIONS:
            assert f"**{word}**" in prompt
        rows = json.loads(run("ledger", "waiting", "--json").output)
        card = next(r for r in rows if r["id"] == finding_id)["recommendation"]
        assert card["recommend"]["disposition"] == "discard"
        transcripts = list((paths.work_dir / "investigations").glob(f"{finding_id}-*.md"))
        assert len(transcripts) == 1 and "card written: yes" in transcripts[0].read_text()

    def test_no_card_is_a_failure_with_its_transcript(self, investigable):
        repo, config, paths, finding_id, root = investigable
        result = run("ledger", "investigate", finding_id)
        assert result.exit_code == 1
        assert "no card written" in result.output
        transcripts = list((paths.work_dir / "investigations").glob(f"{finding_id}-*.md"))
        assert len(transcripts) == 1 and "card written: no" in transcripts[0].read_text()
        assert "done" in transcripts[0].read_text()

    def test_only_what_is_waiting_can_be_investigated(self, investigable):
        result = run("ledger", "investigate", "p.003")
        assert result.exit_code != 0 and "not waiting" in result.output

    def test_json_face(self, investigable):
        repo, config, paths, finding_id, root = investigable
        (root / "recommend").write_text("")
        row = json.loads(run("ledger", "investigate", finding_id, "--json").output)
        assert row["about"] == finding_id and row["recommended"] is True and row["transcript"].endswith(".md")


class TestTheVocabularyIsOneList:
    def test_every_recommendation_has_a_meaning_and_no_meaning_is_stray(self):
        assert set(MEANINGS) == set(RECOMMENDATIONS)

    def test_the_investigator_command_is_swept_by_the_denylist(self, tmp_path):
        from test_config import as_test_tools

        cfg = parse_config(as_test_tools({
            "target_repo": str(tmp_path), "base_ref": "main", "project_branch": "work",
            "plan_root": "PLAN.md", "full_test_command": "true",
            "executor": {"model": "m"}, "planner": {"model": "claude-opus-5"}, "reviewer": {"model": "gpt-5.6-sol"},
            "investigator": {"command": ["claude", "-p"]},
        }))
        assert ("investigator.command", "claude -p") in cfg.all_commands()
