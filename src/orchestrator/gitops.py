"""Git operations on the target repo.

Two decisions from PLAN.md are enforced structurally rather than by
convention:

- **There is no push.** The safety requirements forbid it, and the way to
  guarantee that is for no method to exist that could.
- **Stage diffs are computed against the working tree**, not
  `<stage_start_sha>..HEAD`. An executor that auto-commits makes `..HEAD`
  look correct, but a script stage leaves its transform uncommitted and a
  greenfield stage leaves new files untracked. Either would produce an empty
  diff, and every gate downstream — scope guard, forbidden patterns,
  reviewer — would pass on nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    pass


class Git:
    def __init__(self, repo: Path | str):
        self.repo = Path(repo)

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed ({proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc

    def _out(self, *args: str) -> str:
        return self._run(*args).stdout.strip()

    # --- inspection -----------------------------------------------------

    def is_repo(self) -> bool:
        proc = self._run("rev-parse", "--git-dir", check=False)
        return proc.returncode == 0

    def is_clean(self) -> bool:
        """Ignored files do not count. A target repo legitimately carries
        env files, caches, and test databases."""
        return self._out("status", "--porcelain") == ""

    def head_sha(self) -> str:
        return self._out("rev-parse", "HEAD")

    def rev_parse(self, ref: str) -> str:
        return self._out("rev-parse", "--verify", f"{ref}^{{commit}}")

    def current_branch(self) -> str:
        return self._out("rev-parse", "--abbrev-ref", "HEAD")

    def branch_exists(self, name: str) -> bool:
        proc = self._run(
            "show-ref", "--verify", "--quiet", f"refs/heads/{name}", check=False
        )
        return proc.returncode == 0

    # --- branches -------------------------------------------------------

    def create_branch(self, name: str, base: str) -> None:
        self._run("checkout", "-q", "-b", name, base)

    def checkout(self, name: str) -> None:
        self._run("checkout", "-q", name)

    # --- diffs ----------------------------------------------------------

    def _mark_intent_to_add(self) -> None:
        """Make untracked files visible to `git diff`.

        `git add -N` records intent-to-add: the path enters the index with no
        content, so `git diff` reports it as a new file. Without this, a
        greenfield or script stage that creates files produces an empty diff.
        It stages nothing for real and creates no commit; .gitignore is still
        respected.
        """
        self._run("add", "-A", "-N", ".")

    def diff(self, since_sha: str) -> str:
        self._mark_intent_to_add()
        return self._run("diff", since_sha).stdout

    def diff_names(self, since_sha: str) -> list[str]:
        self._mark_intent_to_add()
        out = self._run("diff", "--name-only", since_sha).stdout
        return [line for line in out.splitlines() if line.strip()]

    def added_lines(self, since_sha: str) -> list[tuple[str, str]]:
        """Every added line, as (path, text).

        Added lines only, by design: `forbidden_patterns` matches against
        this. A stage whose purpose is removing a construct would otherwise
        flag itself the moment it succeeded.
        """
        added: list[tuple[str, str]] = []
        current = "(unknown)"
        for line in self.diff(since_sha).splitlines():
            if line.startswith("+++ "):
                path = line[4:].strip()
                # `+++ b/path`, or `+++ /dev/null` for a deletion.
                current = path[2:] if path.startswith(("a/", "b/")) else path
                continue
            if line.startswith("--- ") or line.startswith("@@"):
                continue
            if line.startswith("+"):
                added.append((current, line[1:]))
        return added

    # --- mutations ------------------------------------------------------

    def commit_all(self, message: str) -> str | None:
        """Commit everything outstanding. Returns the new sha, or None if
        there was nothing to commit.

        `advance` calls this unconditionally, and an agent stage whose
        executor already auto-committed leaves nothing to do.
        """
        self._run("add", "-A", ".")
        if self._out("diff", "--cached", "--name-only") == "":
            return None
        self._run("commit", "-q", "-m", message)
        return self.head_sha()

    def reset_hard(self, sha: str) -> None:
        """Return the tree to `sha`, discarding tracked changes and any
        untracked files created since.

        `git reset --hard` alone leaves untracked files behind, which would
        survive into the next attempt's diff and break the single-purpose
        diff guarantee `rework_reset` exists to provide. `git clean -fd`
        without `-x` removes them while preserving ignored files — env files,
        caches, and test databases must survive a rework.
        """
        self._run("reset", "-q", "--hard", sha)
        self._run("clean", "-qfd")
