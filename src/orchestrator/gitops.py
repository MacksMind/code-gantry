"""Git operations on the target repo.

Three decisions are enforced structurally rather than by convention.

**There is no push.** The safety requirements forbid it, and the way to
guarantee that is for no method to exist that could.

**Stage diffs are computed against the working tree**, not
`<stage_start_sha>..HEAD`. An executor that auto-commits makes `..HEAD` look
correct, but a script stage leaves its transform uncommitted and a human's fix
after an escalation does too. Either would produce an empty diff, and every
gate downstream — scope guard, forbidden patterns, reviewer — would pass on
nothing.

**A stage lands by squash merge.** Aider commits before it tests, so a child
branch contains red intermediate commits. Squashing is what makes "every commit
on the project branch is green" and "Aider commits before testing" both true.
A `--no-ff` merge would drag the red commits onto the project branch.
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
        return self._run("rev-parse", "--git-dir", check=False).returncode == 0

    def is_clean(self) -> bool:
        """Ignored files do not count. A target repo legitimately carries env
        files, caches, and test databases."""
        return self._out("status", "--porcelain") == ""

    def head_sha(self) -> str:
        return self._out("rev-parse", "HEAD")

    def rev_parse(self, ref: str) -> str:
        return self._out("rev-parse", "--verify", f"{ref}^{{commit}}")

    def current_branch(self) -> str:
        return self._out("rev-parse", "--abbrev-ref", "HEAD")

    def branch_exists(self, name: str) -> bool:
        return (
            self._run(
                "show-ref", "--verify", "--quiet", f"refs/heads/{name}", check=False
            ).returncode
            == 0
        )

    def is_ancestor(self, maybe_ancestor: str, descendant: str) -> bool:
        return (
            self._run(
                "merge-base", "--is-ancestor", maybe_ancestor, descendant, check=False
            ).returncode
            == 0
        )

    def branches_matching(self, prefix: str) -> list[str]:
        out = self._out("for-each-ref", "--format=%(refname:short)", "refs/heads")
        return [b for b in out.splitlines() if b.startswith(prefix)]

    def commit_subject(self, ref: str = "HEAD") -> str:
        return self._out("log", "-1", "--pretty=%s", ref)

    def show_file(self, sha: str, path: str) -> str:
        """Read a file as it stood at `sha`.

        Plan documents are read at the run's base sha, not at the branch tip,
        so a concurrent edit on `main` cannot change what a run thinks it was
        asked to do.
        """
        proc = self._run("show", f"{sha}:{path}", check=False)
        if proc.returncode != 0:
            raise GitError(
                f"{path!r} does not exist at {sha[:12]}: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc.stdout

    def file_exists_at(self, sha: str, path: str) -> bool:
        return self._run("cat-file", "-e", f"{sha}:{path}", check=False).returncode == 0

    # --- branches -------------------------------------------------------

    def create_branch(self, name: str, base: str) -> None:
        self._run("checkout", "-q", "-b", name, base)

    def checkout(self, name: str) -> None:
        self._run("checkout", "-q", name)

    def delete_branch(self, name: str, force: bool = True) -> None:
        """Child branches are deleted after they land. Their content survives in
        the squash commit, and the reflog is the recovery path for a discarded
        attempt — which is why `gc.auto` is turned off for a run."""
        self._run("branch", "-D" if force else "-d", "-q", name)

    def ensure_project_branch(self, name: str, base_ref: str) -> str:
        """Cut the project branch once, or check it out if it already exists.

        Returns the base_ref sha the branch is measured against.
        """
        base_sha = self.rev_parse(base_ref)
        if self.branch_exists(name):
            if self.current_branch() != name:
                self.checkout(name)
        else:
            self.create_branch(name, base_ref)
        return base_sha

    def cut_stage_branch(self, name: str, project_branch: str) -> str:
        """Start a stage from the project branch tip.

        If the branch already exists — a revision that means to extend existing
        work — it is checked out rather than recreated.
        """
        if self.branch_exists(name):
            self.checkout(name)
        else:
            self.create_branch(name, project_branch)
        return self.head_sha()

    # --- branch identity ------------------------------------------------

    def branch_identity_problems(
        self,
        expected_branch: str,
        project_branch: str,
        base_ref: str,
        base_sha: str,
    ) -> list[str]:
        """Has anything moved that should not have?

        `script` stages, `checks`, `preconditions`, and `setup_command` are all
        arbitrary operator-authored shell, any of which could contain a stray
        `git checkout`. Over a ten-hour unattended run that is the failure you
        would least like to discover afterward. Free and deterministic, so it
        runs after every stage.
        """
        problems: list[str] = []

        current = self.current_branch()
        if current != expected_branch:
            problems.append(
                f"HEAD is on {current!r}, expected the stage branch "
                f"{expected_branch!r} — something changed branches mid-stage"
            )
            # Everything below assumes we are where we think we are.
            return problems

        if not self.branch_exists(project_branch):
            problems.append(
                f"the project branch {project_branch!r} no longer exists"
            )
            return problems

        project_tip = self.rev_parse(project_branch)
        if not self.is_ancestor(project_tip, "HEAD"):
            problems.append(
                f"the stage branch has diverged from {project_branch!r}: its tip "
                f"{project_tip[:12]} is not an ancestor of HEAD. The project "
                "branch was moved or rewritten underneath this stage"
            )

        try:
            current_base = self.rev_parse(base_ref)
        except GitError:
            problems.append(f"base_ref {base_ref!r} no longer resolves")
            return problems

        if current_base != base_sha:
            problems.append(
                f"{base_ref!r} moved during the run: was {base_sha[:12]}, now "
                f"{current_base[:12]}. The run's baseline is no longer what the "
                "report will claim"
            )

        return problems

    # --- diffs ----------------------------------------------------------

    def _mark_intent_to_add(self) -> None:
        """Make untracked files visible to `git diff`.

        `git add -N` records intent-to-add: the path enters the index with no
        content, so `git diff` reports it as a new file. It stages nothing for
        real and creates no commit; .gitignore is still respected.
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

        Added lines only, by design: `forbidden_patterns` matches against this.
        A stage whose purpose is removing a construct would otherwise flag
        itself the moment it succeeded.
        """
        added: list[tuple[str, str]] = []
        current = "(unknown)"
        for line in self.diff(since_sha).splitlines():
            if line.startswith("+++ "):
                path = line[4:].strip()
                current = path[2:] if path.startswith(("a/", "b/")) else path
                continue
            if line.startswith("--- ") or line.startswith("@@"):
                continue
            if line.startswith("+"):
                added.append((current, line[1:]))
        return added

    # --- mutations ------------------------------------------------------

    def commit_all(self, message: str) -> str | None:
        """Commit everything outstanding. Returns the new sha, or None if there
        was nothing to commit."""
        self._run("add", "-A", ".")
        if self._out("diff", "--cached", "--name-only") == "":
            return None
        self._run("-c", "commit.gpgsign=false", "commit", "-q", "-m", message)
        return self.head_sha()

    def reset_hard(self, sha: str) -> None:
        """Return the tree to `sha`, discarding tracked changes and untracked
        files created since.

        `git clean` without `-x` preserves ignored files — env files, caches,
        and test databases must survive a rework.
        """
        self._run("reset", "-q", "--hard", sha)
        self._run("clean", "-qfd")

    def revert_paths(self, sha: str, paths: list[str]) -> None:
        """Restore only these paths to their state at `sha`.

        This is what makes the scope-quarantine rule cheap: when the planner
        declines to widen a stage's scope, only the out-of-scope paths are
        reverted. Hours of correct in-scope work are not thrown away because
        one unexpected file was touched.

        A path that did not exist at `sha` cannot be checked out, so it is
        deleted instead.
        """
        for path in paths:
            existed = (
                self._run("cat-file", "-e", f"{sha}:{path}", check=False).returncode == 0
            )
            if existed:
                self._run("checkout", sha, "--", path)
            else:
                self._run("rm", "-q", "-f", "--ignore-unmatch", "--", path, check=False)
                target = self.repo / path
                if target.exists():
                    target.unlink()

    def squash_merge(self, child_branch: str, project_branch: str, message: str) -> str | None:
        """Land a stage as exactly one commit on the project branch.

        Returns the new commit sha, or None if the child contained nothing that
        was not already on the project branch.
        """
        self.checkout(project_branch)
        merge = self._run("merge", "--squash", child_branch, check=False)
        if merge.returncode != 0:
            raise GitError(
                f"squash merge of {child_branch!r} into {project_branch!r} "
                f"failed: {merge.stderr.strip() or merge.stdout.strip()}"
            )
        if self._out("diff", "--cached", "--name-only") == "":
            return None
        self._run("-c", "commit.gpgsign=false", "commit", "-q", "-m", message)
        return self.head_sha()

    # --- run-scoped repo settings ---------------------------------------

    def get_config(self, key: str) -> str | None:
        proc = self._run("config", "--local", "--get", key, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def set_config(self, key: str, value: str) -> None:
        self._run("config", "--local", key, value)

    def unset_config(self, key: str) -> None:
        self._run("config", "--local", "--unset", key, check=False)

    def disable_gc(self) -> str | None:
        """Turn off automatic gc for the duration of a run.

        Rework discards child branches, and the reflog is the only recovery
        path for a rejected attempt the operator later wants to inspect.
        Returns the previous value so it can be restored.
        """
        previous = self.get_config("gc.auto")
        self.set_config("gc.auto", "0")
        return previous

    def restore_gc(self, previous: str | None) -> None:
        if previous is None:
            self.unset_config("gc.auto")
        else:
            self.set_config("gc.auto", previous)
