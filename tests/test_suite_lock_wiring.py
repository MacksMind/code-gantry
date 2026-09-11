"""The full suite runs under the host lock wherever the pipeline runs it.

Both runner constructions are wired from config and neither has a unit test
on its own: build each the way production does, run the suite command
through it, and read the lock file it left.
"""

from test_config import as_test_tools, minimal

from code_gantry import preflight
from code_gantry.config import parse_config
from code_gantry.planner import Planner
from code_gantry.reviewer import Reviewer
from code_gantry.runtime import ProjectPaths, RunPaths, build_runtime


def a_config(repo):
    return parse_config(
        as_test_tools({**minimal(), "target_repo": str(repo), "full_test_command": "echo suite"})
    )


def lock_file(tmp_path, monkeypatch):
    locks = tmp_path / "host-locks"
    monkeypatch.setenv("CODE_GANTRY_LOCK_DIR", str(locks))
    return locks / "full-suite.lock"


def test_the_run_takes_the_lock_for_the_suite(repo, tmp_path, monkeypatch):
    path = lock_file(tmp_path, monkeypatch)
    cfg = a_config(repo)
    project = ProjectPaths(tmp_path / "projects" / "proj")
    rt = build_runtime(
        cfg, project, RunPaths(project, "run-1"),
        Planner(cfg.planner, client=object()),
        Reviewer(cfg.reviewer, client=object()),
    )
    try:
        assert rt.runner.exclusive == {"echo suite": "full-suite"}
        rt.runner.run("echo suite")
    finally:
        rt.ledger.close()
    assert path.read_text().endswith(": echo suite")
    assert not path.is_relative_to(repo), "the lock lives outside the checkout"


def test_preflight_takes_the_lock_for_the_suite(repo, tmp_path, monkeypatch):
    path = lock_file(tmp_path, monkeypatch)
    preflight.run_preflight(
        a_config(repo), run_tests=True, check_models=False,
        check_endpoint=False, check_approval=False,
    )
    assert path.exists() and path.read_text().endswith(": echo suite")
