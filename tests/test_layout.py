"""The repository layout block given to the planner.

The planner authors `edit_files` globs. Without knowing what the repository
actually contains it guesses, and a wrong guess costs a scope violation and a
planner intervention on every stage — which is exactly what the first live run
did, guessing `src/calculator.py` at a repo containing `src/calc.py`.

Dumping `git ls-files` is not an option at real scale: the target Rails app has
4,289 tracked files and 217KB of paths, over a thousand of them vendored assets
no stage will ever touch. So this summarises, and the tests below are mostly
about summarising *honestly* — never implying a file exists, and never implying
coverage it does not have.
"""

from orchestrator.layout import summarize_layout


def test_lists_files_in_a_small_directory():
    out = summarize_layout(["src/calc.py", "src/util.py", "tests/test_calc.py"])
    assert "src/calc.py" in out
    assert "tests/test_calc.py" in out


def test_names_the_real_file_not_a_plausible_one():
    # The failure this exists to prevent.
    out = summarize_layout(["src/calc.py"])
    assert "calc.py" in out
    assert "calculator.py" not in out


def test_root_level_files_are_shown():
    out = summarize_layout(["README.md", "pyproject.toml", "src/a.py"])
    assert "README.md" in out
    assert "pyproject.toml" in out


def test_a_large_directory_is_summarised_not_listed():
    paths = [f"vendor/assets/f{i}.js" for i in range(500)]
    out = summarize_layout(paths, per_dir_files=25)
    assert "vendor/assets" in out
    assert "500" in out, "the count must be visible so the planner can glob it"
    assert out.count("f4") < 25, "a 500-file directory must not be listed in full"


def test_a_large_directory_still_shows_examples():
    # Naming conventions matter even where the full list does not fit.
    paths = [f"app/views/thing_{i}.html.erb" for i in range(300)]
    out = summarize_layout(paths, per_dir_files=5)
    assert ".html.erb" in out


def test_respects_the_line_budget():
    paths = [f"dir{i}/file{j}.py" for i in range(200) for j in range(10)]
    out = summarize_layout(paths, max_lines=50)
    assert len(out.splitlines()) <= 50


def test_says_when_it_truncated():
    paths = [f"dir{i}/file{j}.py" for i in range(200) for j in range(10)]
    out = summarize_layout(paths, max_lines=30)
    assert "truncat" in out.lower() or "omitted" in out.lower()

    # And it must never imply the omitted directories do not exist.
    assert "2,000" in out or "2000" in out


def test_reports_the_total_count():
    out = summarize_layout([f"a/f{i}.py" for i in range(42)])
    assert "42" in out


def test_empty_repository_is_stated_plainly():
    out = summarize_layout([])
    assert out.strip()
    assert "no tracked files" in out.lower()


def test_areas_are_ordered_alphabetically_not_by_size():
    # Size ordering puts vendored assets above application code, which is
    # backwards; the count is printed either way. `app` is both smaller and
    # alphabetically first here, so only alphabetical ordering passes.
    paths = ["app/model.rb"] + [f"vendor/assets/f{i}.js" for i in range(50)]
    out = summarize_layout(paths, per_dir_files=2)
    assert out.index("app/") < out.index("vendor/assets")


def test_a_vendored_tree_cannot_crowd_out_application_code():
    # The failure mode of the first design: one huge area consumed the budget
    # and small, relevant directories were truncated away.
    paths = [f"vendor/assets/f{i}.js" for i in range(4000)] + [
        "app/controllers/orders_controller.rb",
        "spec/models/order_spec.rb",
    ]
    out = summarize_layout(paths)
    assert "app/controllers/orders_controller.rb" in out
    assert "spec/models/order_spec.rb" in out


def test_output_is_deterministic():
    paths = ["b/2.py", "a/1.py", "b/1.py"]
    assert summarize_layout(paths) == summarize_layout(list(reversed(paths)))


class TestReachesThePlanner:
    """Wiring, not formatting. The block is worthless if it never arrives."""

    def test_the_layout_is_in_the_planner_prompt(self):
        from orchestrator.plandoc import PlanDocument, PlanTree
        from orchestrator.prompts import build_planner_messages

        plan = PlanTree(
            root=PlanDocument(path="p.md", content="do the thing"),
            children=[],
            problems=[],
            skipped=[],
        )
        messages = build_planner_messages(
            cfg=None, plan=plan, completed=[], layout="- `src/` (1)\n  src/calc.py"
        )
        assert "src/calc.py" in messages[0]["content"]

    def test_it_leads_so_it_stays_cacheable(self):
        # It is read once at the base sha and never changes, so it belongs in
        # the stable prefix with the plan — not beside the per-call material.
        from orchestrator.plandoc import PlanDocument, PlanTree
        from orchestrator.prompts import build_planner_messages

        plan = PlanTree(
            root=PlanDocument(path="p.md", content="do the thing"),
            children=[],
            problems=[],
            skipped=[],
        )
        messages = build_planner_messages(
            cfg=None,
            plan=plan,
            completed=[],
            layout="LAYOUT_MARKER",
            status_tail="TAIL_MARKER",
        )
        assert "LAYOUT_MARKER" in messages[0]["content"]
        assert "TAIL_MARKER" not in messages[0]["content"]

    def test_a_repo_with_no_layout_still_builds_a_prompt(self):
        from orchestrator.plandoc import PlanDocument, PlanTree
        from orchestrator.prompts import build_planner_messages

        plan = PlanTree(
            root=PlanDocument(path="p.md", content="do the thing"),
            children=[],
            problems=[],
            skipped=[],
        )
        messages = build_planner_messages(cfg=None, plan=plan, completed=[], layout="")
        assert messages[0]["content"].strip()
