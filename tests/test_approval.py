"""Operator approval by config hash.

The point is that approval cannot be faked by anything that can write YAML.
There is no `approved: true` field, so the tests here are mostly about the hash
tracking the exact bytes a human read.
"""

from orchestrator.approval import (
    approval_problem,
    config_hash,
    read_approval,
    record_approval,
)


def a_config(tmp_path, body="test_command: pytest\n"):
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return path


class TestHashing:
    def test_hashes_the_file(self, tmp_path):
        assert len(config_hash(a_config(tmp_path))) == 64

    def test_same_bytes_same_hash(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        first = a_config(tmp_path / "a", "x: 1\n")
        second = a_config(tmp_path / "b", "x: 1\n")
        assert config_hash(first) == config_hash(second)

    def test_a_comment_change_changes_the_hash(self, tmp_path):
        # Deliberate: a comment change is a change the operator should re-read,
        # and normalising through YAML would let a reworded file inherit an old
        # approval.
        path = a_config(tmp_path, "test_command: pytest\n")
        before = config_hash(path)
        path.write_text("# now with a comment\ntest_command: pytest\n")
        assert config_hash(path) != before


class TestRecording:
    def test_records_and_reads_back(self, tmp_path):
        project = tmp_path / "project"
        config = a_config(tmp_path)
        recorded = record_approval(project, config, now="2026-07-30T00:00:00Z")
        read = read_approval(project)
        assert read.config_sha256 == recorded.config_sha256
        assert read.approved_at == "2026-07-30T00:00:00Z"

    def test_creates_the_project_directory(self, tmp_path):
        record_approval(tmp_path / "new" / "project", a_config(tmp_path), now="t")
        assert (tmp_path / "new" / "project" / "approval.json").is_file()

    def test_no_approval_file_reads_as_none(self, tmp_path):
        assert read_approval(tmp_path) is None

    def test_a_corrupt_approval_file_reads_as_unapproved(self, tmp_path):
        # Not a crash: a damaged approval is simply not an approval.
        (tmp_path / "approval.json").write_text("{ not json")
        assert read_approval(tmp_path) is None

    def test_an_approval_without_a_hash_reads_as_unapproved(self, tmp_path):
        (tmp_path / "approval.json").write_text('{"approved_at": "t"}')
        assert read_approval(tmp_path) is None


class TestApprovalProblem:
    def test_never_approved(self, tmp_path):
        problem = approval_problem(tmp_path, a_config(tmp_path))
        assert problem and "never been approved" in problem

    def test_approved_and_unchanged(self, tmp_path):
        project = tmp_path / "project"
        config = a_config(tmp_path)
        record_approval(project, config, now="t")
        assert approval_problem(project, config) is None

    def test_edited_after_approval(self, tmp_path):
        # This is the whole mechanism: editing the config invalidates approval.
        project = tmp_path / "project"
        config = a_config(tmp_path)
        record_approval(project, config, now="t")
        config.write_text("test_command: rm -rf /\n")
        problem = approval_problem(project, config)
        assert problem and "changed since it was approved" in problem

    def test_problem_shows_both_hashes(self, tmp_path):
        project = tmp_path / "project"
        config = a_config(tmp_path)
        record_approval(project, config, now="t")
        config.write_text("changed\n")
        problem = approval_problem(project, config)
        assert "approved:" in problem and "current:" in problem

    def test_reapproving_clears_the_problem(self, tmp_path):
        project = tmp_path / "project"
        config = a_config(tmp_path)
        record_approval(project, config, now="t1")
        config.write_text("test_command: pytest -q\n")
        assert approval_problem(project, config) is not None
        record_approval(project, config, now="t2")
        assert approval_problem(project, config) is None
