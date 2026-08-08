"""`plan` fills the queue, and the orthogonality check decides what goes in it.

The last connection: `additional_stages` arrives, is validated, and until now
was discarded. This is where a batch becomes real work, and where the check
runs against the repository rather than against a fixture.

The first stage is never dropped. It is the one being started, and the check
exists to protect the stages behind it.
"""

import subprocess

import pytest


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "target"
    (r / "app").mkdir(parents=True)
    (r / "spec").mkdir()
    for name in ("app/a.rb", "app/b.rb", "app/c.rb", "spec/a_spec.rb"):
        (r / name).write_text("x\n")
    for args in (
        ["init", "-q", "-b", "main"], ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"], ["config", "commit.gpgsign", "false"],
        ["add", "-A"], ["commit", "-qm", "init"],
    ):
        subprocess.run(["git", *args], cwd=r, check=True, capture_output=True)
    return r


def _spec(sid, edit, read=()):
    return {
        "id": sid, "instruction": "do it",
        "edit_files": list(edit), "read_files": list(read),
    }


class TestTheQueueIsBuiltFromTheBatch:
    def _queued(self, repo, first, extras):
        from orchestrator.config import parse_config
        from orchestrator.nodes import _queue_from_batch

        cfg = parse_config({
            "target_repo": str(repo), "base_ref": "main", "project_branch": "p",
            "plan_root": "PLAN.md", "test_command": "true",
            "executor": {"model": "m"},
            # Opted in, as a project using this feature must be. The default is
            # 1 and turns batching off.
            "planner": {"model": "claude-opus-5", "max_batch_stages": 5},
            "reviewer": {"model": "gpt-5.6-sol"},
        })
        from orchestrator.gitops import Git

        return _queue_from_batch(
            cfg, Git(repo), cfg.stage_from_planner(first), extras
        )

    def test_no_extras_means_no_queue(self, repo):
        queue, dropped = self._queued(repo, _spec("one", ["app/a.rb"]), [])
        assert queue == [] and dropped == []

    def test_orthogonal_extras_are_all_queued(self, repo):
        queue, dropped = self._queued(
            repo, _spec("one", ["app/a.rb"]),
            [_spec("two", ["app/b.rb"]), _spec("three", ["app/c.rb"])],
        )
        assert [s["id"] for s in queue] == ["two", "three"]
        assert dropped == []

    def test_one_colliding_with_the_first_is_dropped(self, repo):
        queue, dropped = self._queued(
            repo, _spec("one", ["app/a.rb"]),
            [_spec("two", ["app/a.rb"]), _spec("three", ["app/c.rb"])],
        )
        assert [s["id"] for s in queue] == ["three"], "the rest still runs"
        assert len(dropped) == 1 and "two" in dropped[0]

    def test_the_started_stage_is_never_in_the_queue(self, repo):
        # It is the one being run now; the queue is what comes after it.
        queue, _ = self._queued(
            repo, _spec("one", ["app/a.rb"]), [_spec("two", ["app/b.rb"])]
        )
        assert "one" not in [s["id"] for s in queue]

    def test_an_extra_is_filtered_through_the_allowlist(self, repo):
        # It reaches the queue as a Stage built by `stage_from_planner`, so an
        # executable field cannot ride in on a batched stage.
        queue, _ = self._queued(
            repo, _spec("one", ["app/a.rb"]),
            [dict(_spec("two", ["app/b.rb"]), command="rm -rf /")],
        )
        assert queue[0].get("command") in (None, "")

    def test_a_batched_stage_reading_what_the_first_writes_is_dropped(self, repo):
        queue, dropped = self._queued(
            repo, _spec("one", ["app/a.rb"]),
            [_spec("two", ["app/b.rb"], read=["app/a.rb"])],
        )
        assert queue == []
        assert "app/a.rb" in dropped[0]


class TestARevisionRechecksTheQueue:
    """Rework is not batched, but the queue behind it need not be thrown away.

    A failed stage routes to the planner with its branch intact, so the planner
    revises that one stage. What can change is its scope: a revision widening
    `edit_files` to fix a scope violation may now name a file a queued stage
    was drawn against. Re-check and drop only what the revision collides with —
    the invariant needs re-checking, not forgetting.
    """

    def _rechecked(self, repo, revised, queue):
        from orchestrator.config import parse_config
        from orchestrator.gitops import Git
        from orchestrator.nodes import _requeue_after_revision

        cfg = parse_config({
            "target_repo": str(repo), "base_ref": "main", "project_branch": "p",
            "plan_root": "PLAN.md", "test_command": "true",
            "executor": {"model": "m"},
            "planner": {"model": "claude-opus-5", "max_batch_stages": 5},
            "reviewer": {"model": "gpt-5.6-sol"},
        })
        return _requeue_after_revision(
            cfg, Git(repo), cfg.stage_from_planner(revised), queue
        )

    def test_a_revision_in_scope_keeps_the_queue(self, repo):
        kept, dropped = self._rechecked(
            repo, _spec("one", ["app/a.rb"]),
            [_spec("two", ["app/b.rb"]), _spec("three", ["app/c.rb"])],
        )
        assert [s["id"] for s in kept] == ["two", "three"]
        assert dropped == []

    def test_a_widened_revision_drops_only_what_it_now_touches(self, repo):
        kept, dropped = self._rechecked(
            repo, _spec("one", ["app/a.rb", "app/c.rb"]),
            [_spec("two", ["app/b.rb"]), _spec("three", ["app/c.rb"])],
        )
        assert [s["id"] for s in kept] == ["two"]
        assert len(dropped) == 1 and "three" in dropped[0]

    def test_an_empty_queue_stays_empty(self, repo):
        assert self._rechecked(repo, _spec("one", ["app/a.rb"]), []) == ([], [])


class TestTheBatchIsCapped:
    """Off by default, and bounded when on.

    A schema field with no ceiling is an invitation, and the measured risk of
    this feature is not a bad stage but a slower derivation — a planner asked
    for several may survey as though it needs several. So a project opts in,
    and the cap is the operator's rather than the model's.
    """

    def _queued(self, repo, first, extras, cap):
        from orchestrator.config import parse_config
        from orchestrator.gitops import Git
        from orchestrator.nodes import _queue_from_batch

        cfg = parse_config({
            "target_repo": str(repo), "base_ref": "main", "project_branch": "p",
            "plan_root": "PLAN.md", "test_command": "true",
            "executor": {"model": "m"},
            "planner": {"model": "claude-opus-5", "max_batch_stages": cap},
            "reviewer": {"model": "gpt-5.6-sol"},
        })
        return _queue_from_batch(cfg, Git(repo), cfg.stage_from_planner(first), extras)

    def test_the_default_is_one_stage(self):
        from orchestrator.config import parse_config

        cfg = parse_config({
            "target_repo": ".", "base_ref": "main", "project_branch": "p",
            "plan_root": "PLAN.md", "test_command": "true",
            "executor": {"model": "m"}, "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.6-sol"},
        })
        assert cfg.planner.max_batch_stages == 1

    def test_a_cap_of_one_queues_nothing(self, repo):
        queue, dropped = self._queued(
            repo, _spec("one", ["app/a.rb"]), [_spec("two", ["app/b.rb"])], cap=1
        )
        assert queue == []
        assert dropped and "cap" in dropped[0].lower()

    def test_the_cap_counts_the_stage_being_started(self, repo):
        # A cap of two means one now and one queued, not one now and two.
        queue, _ = self._queued(
            repo, _spec("one", ["app/a.rb"]),
            [_spec("two", ["app/b.rb"]), _spec("three", ["app/c.rb"])], cap=2,
        )
        assert [s["id"] for s in queue] == ["two"]

    def test_under_the_cap_is_untouched(self, repo):
        queue, dropped = self._queued(
            repo, _spec("one", ["app/a.rb"]), [_spec("two", ["app/b.rb"])], cap=5
        )
        assert len(queue) == 1 and dropped == []
