"""`search` must search the repository, never its own stdin.

ripgrep given no path argument and a stdin that is not a terminal reads *stdin*.
Interactively that never happens, so the tool worked at a shell and returned
nothing from `subprocess.run` with an inherited pipe:

    inherited stdin  rc=1  files=0
    stdin=DEVNULL    rc=0  files=399
    positional path  rc=0  files=399

Measured against a real repository with one literal. Nothing warns, nothing
errors — every search simply comes back empty, which reads as "not in this
repository". That is the same failure the pathspec bug produced, arriving by a
completely different route, and it would have hit whichever way a run happens to
be launched: a cron job, CI, or a shell with stdin redirected.

The explicit path is the fix that cannot be argued with — given a path, ripgrep
does not consult stdin at all. `stdin=DEVNULL` is kept as well because it costs
nothing and removes any dependence on how ripgrep classifies the stream.
"""

import subprocess

import pytest

from code_gantry.gitops import Git
from code_gantry.repotools import ReadBudget, RepoReader


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "target"
    (r / "app").mkdir(parents=True)
    (r / "app" / "order.rb").write_text("class Order\n  NEEDLE = 1\nend\n")
    for args in (
        ["init", "-q", "-b", "main"], ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"], ["config", "commit.gpgsign", "false"],
        ["add", "-A"], ["commit", "-qm", "init"],
    ):
        subprocess.run(["git", *args], cwd=r, check=True, capture_output=True)
    return r


class TestStdinCannotSubstituteForTheRepository:
    def test_a_path_argument_is_always_passed(self, repo, monkeypatch):
        # The property, asserted on the argv rather than the result, because a
        # result can be right by luck depending on what stdin happens to be.
        import code_gantry.repotools as rt

        seen = {}
        real = rt.subprocess.run

        def spy(argv, **kw):
            # `search` also shells out to `git check-ignore`, which is fed on
            # stdin by design. Recording the last call would assert this
            # property of the wrong command.
            if argv and argv[0] == "rg":
                seen["argv"] = argv
                seen["stdin"] = kw.get("stdin")
            return real(argv, **kw)

        monkeypatch.setattr(rt.subprocess, "run", spy)
        RepoReader(Git(repo), repo, ReadBudget()).search("NEEDLE")

        argv = seen["argv"]
        assert argv[-1] != "NEEDLE", "the pattern must not be the final argument"
        assert seen["stdin"] is subprocess.DEVNULL

    def test_it_finds_matches_with_a_pipe_on_stdin(self, repo):
        # The regression itself. Under a pipe, the old code searched the pipe.
        script = (
            "import sys, json;"
            "sys.path.insert(0, 'src');"
            "from code_gantry.gitops import Git;"
            "from code_gantry.repotools import ReadBudget, RepoReader;"
            f"print(len(RepoReader(Git({str(repo)!r}), {str(repo)!r}, ReadBudget())"
            ".search('NEEDLE')))"
        )
        out = subprocess.run(
            ["python3", "-c", script],
            input="unrelated text on stdin\n",
            capture_output=True, text=True,
            cwd=".",
        )
        assert out.stdout.strip() == "1", out.stderr[-600:]

    def test_paths_are_still_relative(self, repo):
        # A positional path can prefix every hit with `./`, which would break
        # the `path:line:text` contract every caller parses.
        hits = RepoReader(Git(repo), repo, ReadBudget()).search("NEEDLE")
        assert hits == ["app/order.rb:2:  NEEDLE = 1"]
