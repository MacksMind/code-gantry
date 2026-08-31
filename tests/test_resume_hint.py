"""Every message that tells an operator what to run next names a config.

The CLI took a slug, then a run id, and now takes a **config path** on every
command. Four messages were left behind saying `code-gantry resume <run_id>`,
and one went further and told the operator to "approve the config" — a command
deleted with `approval.py` when the config's git blob sha replaced it.

None of it was catchable. They are console strings, so nothing fails when they
go stale; the test that touched one asserted only that the substring
`code-gantry resume` appeared, which stayed true through every change to what
follows it. What an operator got was a usage error at whatever hour their run
stopped, or a command that no longer exists.

So the hint is built in one place and this pins the callers to it. The rule
these are an instance of is already written down — a prompt sentence outlives
the fact it was written about, and the prompts are where a deleted thing goes
on living — and the sweep it asks for is what found three of these four.
"""

import subprocess

import pytest

from test_config import as_test_tools

from code_gantry.config import parse_config


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def cfg(tmp_path):
    repo = tmp_path / "target"
    (repo / "docs").mkdir(parents=True)
    git_dir = ["init", "-q", "-b", "main"]
    subprocess.run(["git", "-C", str(repo), *git_dir], check=True, capture_output=True)
    source = repo / "docs" / "code_gantry.yaml"
    source.write_text("x: 1\n")
    return parse_config(
        as_test_tools({
            "target_repo": str(repo), "base_ref": "main", "project_branch": "p",
            "plan_root": "docs/PLAN.md", "full_test_command": "true",
            "executor": {"model": "m"}, "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.6-sol"},
        }),
        source=source,
    )


class TestTheHintItself:
    def test_it_names_the_config_the_run_was_read_from(self, cfg):
        assert cfg.resume_command().endswith("docs/code_gantry.yaml")
        assert cfg.resume_command().startswith("code-gantry resume ")

    def test_the_run_id_is_optional_because_the_cli_defaults_it(self, cfg):
        # It resolves to the newest run in the work dir, which is nearly always
        # the one that just stopped.
        assert " r-1" in cfg.resume_command("r-1")
        assert cfg.resume_command().count(" ") == 2

    def test_flags_come_last(self, cfg):
        assert cfg.resume_command(flags="--reset-progress-budget").endswith(
            "code_gantry.yaml --reset-progress-budget"
        )

    def test_a_config_from_no_file_says_so_rather_than_inventing_a_path(self):
        # Every test in this suite builds one of these, and so does anyone
        # constructing a config by hand. A placeholder is honest; a wrong
        # absolute path is worse than no path.
        cfg = parse_config(as_test_tools({
            "target_repo": "/tmp", "base_ref": "main", "project_branch": "p",
            "plan_root": "PLAN.md", "full_test_command": "true",
            "executor": {"model": "m"}, "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.6-sol"},
        }))
        assert cfg.resume_command() == "code-gantry resume <config>"


class TestNoCallerSpellsItItself:
    def test_no_source_file_hardcodes_a_run_id_after_resume(self):
        """The sweep, as a test.

        A fifth message is one feature away, and it will be written by copying
        one of these four. Catching the *shape* is what makes that fail here
        rather than in front of an operator.
        """
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "code_gantry"
        offenders = []
        for path in src.rglob("*.py"):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if "code-gantry resume" not in line:
                    continue
                after = line.split("code-gantry resume", 1)[1].lstrip()
                # The stale shape exactly: a run id interpolated where the
                # config path now goes. Written as the shape rather than as
                # the words, so the CLI's usage banner — which documents
                # `[run_id]` as the optional second argument, correctly — and
                # `resume_command` itself, which assembles the parts, are not
                # swept up by a test that cannot tell right from wrong.
                if after.startswith("{") and "run_id" in after.split("}")[0]:
                    offenders.append(f"{path.name}:{n}")
        assert offenders == [], offenders

    def test_nothing_still_tells_an_operator_to_approve_a_config(self):
        """The instruction, not the word.

        `preflight` names `code-gantry approve` in a docstring, to say what
        the config-sha check replaced, and that is the good kind: a maintainer
        reading `_approval_check` needs to know why it is called that. What
        must not survive is an operator being told to *run* it — which is what
        the progress-budget escalation did, months after the command was
        deleted.
        """
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "code_gantry"
        hits = [
            f"{p.name}:{n}"
            for p in src.rglob("*.py")
            for n, line in enumerate(p.read_text().splitlines(), 1)
            if "approve the config" in line
        ]
        assert hits == [], hits
