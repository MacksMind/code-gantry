"""The prompts are files, and the files are free to change.

Every standing sentence a model is sent lives under `prompts/`. What these
tests hold fixed is the seam, not the words: every file is carried by some
builder, every placeholder a file names is one the code fills, nothing a
builder assembles leaks a bare placeholder, and an edit to a file reaches
the model unchanged.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_config import as_test_tools, minimal

from code_gantry import planner, promptfiles, prompts
from code_gantry.config import Stage, parse_config

SRC = Path(__file__).resolve().parents[1] / "src" / "code_gantry"


def every_assembled_prompt() -> list[str]:
    """Each builder, driven with enough shape to reach every file."""
    data = as_test_tools({
        **minimal(),
        "stage_defaults": {"checks": ["bin/lint"]},
        "planner": {"model": "claude-opus-5", "max_batch_stages": 4, "guidance": "G"},
        "reviewer": {"model": "gpt-5.5", "repo_access": True, "history_stages": 1},
        "executor": {"model": "m", "no_direct_edit": [{"path_glob": "Gemfile.lock", "reason": "generated"}]},
        "ledger": {"key_prefix": "zz"},
    })
    data["project_tools"].append({
        "name": "lookup", "description": "Look a thing up.", "command": ["true", "{q}"],
        "arguments": [{"name": "q", "description": "What to look up.", "repeated": False}],
        "roles": ["planner"],
    })
    cfg = parse_config(data)
    stage = Stage(
        id="s", instruction="do", edit_files=["a.py", "missing/x.py"], read_files=["b.py"],
        constraints="C", acceptance="A", require_new_tests=True, forbidden_patterns=["foo"],
        plan_keys=["zz.001"],
    )
    failure = {"layer": "tests", "summary": "S", "detail": "D", "out_of_scope_paths": ["p"], "failing_paths": ["q"]}
    out = [
        planner._system_blocks(guidance="G", project_tools=cfg.project_tools)[0]["text"],
        planner._system_blocks()[0]["text"],
        prompts._executor_system_prompt(cfg),
        prompts._executor_system_prompt(None),
        prompts._review_system_prompt(cfg),
        prompts.build_executor_prompt(
            stage, cfg, context=[("cmd", "out")], feedback=["fix"], failure_layer="review",
            cumulative_diff="d", excerpts=[("a.py:1-2 — note", "1 x")],
        ),
        prompts.build_executor_prompt(stage, cfg, feedback=["fix"], failure_layer="tests"),
        prompts.build_executor_prompt(stage, cfg, excerpts=[("a.py:1-2", "1 x")]),
    ]
    for m in prompts.build_executor_messages(stage, cfg, "P", agent_context="conv", feedback=["f"]):
        out += [c["text"] for c in m["content"]]
    for m in prompts.build_executor_messages(stage, cfg, "P"):
        out += [c["text"] for c in m["content"]]
    for m in prompts.build_review_messages(
        stage, cfg, "d", plan_text="PLAN", completed=[{"index": 0, "id": "a", "merge_sha": "0" * 40}] * 3,
        projection="PJ", agent_context="conv", proposed=[("f-1", "claim")],
    ):
        out += [c["text"] for c in m["content"]]
    for kw in (
        dict(current_stage=None, failure={"layer": "validation", "summary": "bad"}),
        dict(current_stage=stage, failure=failure),
        dict(current_stage=stage, failure=failure, opening_failure={"layer": "residue", "summary": "first"},
             gate_history=[{"revision": 0, "layer": "residue"}], stage_diff="D",
             stage_queue=[{"id": "q"}], batch_notes=["n"]),
    ):
        for m in prompts.build_planner_messages(
            cfg=cfg, plan_text="PLAN", completed=[], projection="PJ", layout="L", agent_context="conv",
            stage_costs=[{"merge_sha": "abcdef123456", "stage_id": "s", "context_tokens": 1, "files": 2}],
            test_warnings="W", interventions_used=1, interventions_max=12, revision=1, **kw,
        ):
            out += [c["text"] for c in m["content"]]
    landed = [{"index": i, "id": f"s{i}", "merge_sha": "0" * 40, "withheld_reads": ["x"]} for i in range(3)]
    out.append(prompts._history_block(landed, limit=1))
    out.append(prompts._history_block(landed))
    out.append(prompts._batch_block(SimpleNamespace(planner=SimpleNamespace(max_batch_stages=1))))
    out.append(prompts._conventions_block("conv", role="executor", project_tools=()))
    from code_gantry.investigator import render_prompt
    from code_gantry.ledger import Waiting

    thing = Waiting(id="f-x-1", kind="finding", title="t", text="claim", keys=["k.001"], since="2026-01-01T00:00:00+00:00")
    out.append(render_prompt(thing, [Waiting(id="k.002", kind="item", title="other", text="", keys=["k.002"], since=None)],
                             project_label="repo/p", config_path=Path("/cfg.yaml")))
    return out


class TestTheSeam:
    def test_every_file_is_carried_by_some_builder(self):
        code = "\n".join(p.read_text() for p in SRC.glob("*.py"))
        unused = [name for name in promptfiles.names() if f'"{name}"' not in code]
        assert unused == [], f"prompt files nothing renders: {unused}"

    def test_every_placeholder_is_filled_and_none_leaks(self):
        for assembled in every_assembled_prompt():
            leaked = re.findall(r"(?<!\$)\$\{?[A-Za-z_]\w*\}?", assembled)
            assert leaked == [], leaked
            assert "%%" not in assembled

    def test_every_file_reaches_a_model(self):
        """Each file's text, with its placeholders masked, appears in at least
        one assembled prompt."""
        assembled = "\n".join(every_assembled_prompt())
        for name in promptfiles.names():
            fragment = re.sub(r"\$\{?[A-Za-z_]\w*\}?", "\n", promptfiles.raw(name))
            longest = max((piece.strip() for piece in fragment.split("\n")), key=len)
            assert longest in assembled, f"{name}: {longest[:60]!r} reaches no prompt"

    def test_a_placeholder_the_code_does_not_supply_names_the_file(self, tmp_path, monkeypatch):
        alt = tmp_path / "prompts"
        shutil.copytree(promptfiles.prompts_dir(), alt)
        (alt / "planner" / "derive.md").write_text("Draw the next stage, $nobody.\n")
        monkeypatch.setenv(promptfiles.PROMPTS_ENV, str(alt))
        promptfiles.forget()
        try:
            with pytest.raises(KeyError, match=r"planner/derive\.md names \$nobody"):
                prompts.build_planner_messages(cfg=None, plan_text="P", completed=[])
        finally:
            promptfiles.forget()


class TestAHandEditReachesTheModel:
    def test_an_edited_sentence_is_what_is_sent(self, tmp_path, monkeypatch):
        alt = tmp_path / "prompts"
        shutil.copytree(promptfiles.prompts_dir(), alt)
        (alt / "planner" / "derive.md").write_text("## Now\n\nDraw the next stage, plainly.\n")
        monkeypatch.setenv(promptfiles.PROMPTS_ENV, str(alt))
        promptfiles.forget()
        try:
            messages = prompts.build_planner_messages(cfg=None, plan_text="P", completed=[])
            text = "\n".join(c["text"] for c in messages[0]["content"])
            assert "Draw the next stage, plainly." in text
        finally:
            promptfiles.forget()

    def test_the_directory_is_the_repository_top_level(self):
        assert promptfiles.prompts_dir() == Path(__file__).resolve().parents[1] / "prompts"
        assert promptfiles.prompts_dir().is_dir()
