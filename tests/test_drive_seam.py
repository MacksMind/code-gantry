"""The lines between building a runtime and driving it.

`_drive` is where a run's collaborators are bound and the ledger is
released, synced and scoped before the first node runs. Nothing else calls
those lines, and a name error there stops every run at start — which is
how one shipped: the suite was green and the first resume crashed.
"""

from types import SimpleNamespace

from test_config import as_test_tools, minimal

from code_gantry import cli
from code_gantry.config import parse_config
from code_gantry.runtime import ProjectPaths, RunPaths


def test_a_run_reaches_the_driver_with_every_start_step_done(tmp_path, monkeypatch):
    cfg = parse_config(as_test_tools({**minimal(), "target_repo": str(tmp_path)}))
    project = ProjectPaths(tmp_path / "work")
    project.ensure()
    paths = RunPaths(project, "run-1")
    paths.ensure()

    steps = []
    fake_rt = SimpleNamespace(
        ledger=SimpleNamespace(close=lambda: steps.append("closed")),
        key_scope={"p.001"}, runner=None,
    )
    monkeypatch.setattr(cli, "make_planner", lambda *a, **k: object())
    monkeypatch.setattr(cli, "make_reviewer", lambda *a, **k: object())
    monkeypatch.setattr(cli, "build_runtime", lambda *a, **k: fake_rt)
    monkeypatch.setattr(cli, "release_dead_holders", lambda *a, **k: steps.append("released") or 0)
    monkeypatch.setattr(cli, "drive", lambda *a, **k: steps.append("driven") or {"status": "complete"})

    code = cli._drive(cfg, project, paths, {"run_id": "run-1", "key_scope": ["p.001"]})
    assert code == cli.EXIT_OK
    assert steps[:3] == ["released", "driven", "closed"], steps
