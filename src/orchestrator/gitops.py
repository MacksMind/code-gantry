"""Git operations on the target repo.

Three decisions are enforced structurally rather than by convention.

**There is no push.** The safety requirements forbid it, and the way to
guarantee that is for no method to exist that could.

**Stage diffs are computed against the working tree**, not
`<stage_start_sha>..HEAD`. An executor that auto-commits makes `..HEAD` look
correct, but a human's fix after an escalation is uncommitted, and so is
anything a check rewrote after the last commit. Either would produce an empty
diff, and every
gate downstream — scope guard, forbidden patterns, reviewer — would pass on
nothing.

**A stage lands by squash merge.** The executor commits before it tests, so a
child branch contains red intermediate commits. Squashing is what makes "every
commit on the project branch is green" and "the executor commits before it
tests" both true.
A `--no-ff` merge would drag the red commits onto the project branch.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")


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
            # A repository is not obliged to be UTF-8. Without this, one
            # Windows-1252 curly quote in one tracked file crashes the process
            # the moment any git command's output includes it — which happened
            # on a planner search, mid-run, with a traceback instead of a
            # report. `git grep -I` is no defence: it skips binary files, and a
            # file with no NUL byte is not binary however it is encoded.
            # `CommandRunner` has carried this same guard all along.
            errors="replace",
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

    def uncommitted(self) -> list[str]:
        """Porcelain lines for whatever is dirty, for an operator to read.

        `is_clean` answers whether to stop; this answers what to look at. A
        message that says the tree is dirty and not which files sends someone
        to run the command themselves.
        """
        out = self._out("status", "--porcelain")
        return [line for line in out.splitlines() if line.strip()]

    def head_sha(self) -> str:
        return self._out("rev-parse", "HEAD")

    def rev_parse(self, ref: str) -> str:
        return self._out("rev-parse", "--verify", f"{ref}^{{commit}}")

    def blob_at(self, ref: str, path: str) -> str:
        """The object id of a file's contents at a ref, or "" if it is not there.

        Contents rather than commit: the question it answers is whether these
        bytes moved, and a commit id changes when anything in the tree moves.
        Absence is a value rather than an error because a deleted file is a
        legitimate answer to "is this the same as it was" — and it is `no`.
        """
        proc = self._run("rev-parse", "--verify", "--quiet", f"{ref}:{path}",
                         check=False)
        return proc.stdout.strip() if proc.returncode == 0 else ""

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

    def is_symlink(self, sha: str, path: str) -> bool:
        """Whether `path` is a symlink at `sha`.

        Worth asking because `show_file` on one returns the *target path*, not
        the file it points at — a caller reading it as content gets a document
        whose entire body is a filename. `CLAUDE.md -> AGENTS.md` is a common
        enough shape to be worth the extra call.
        """
        out = self._run("ls-tree", sha, "--", path, check=False).stdout
        return out.startswith("120000")

    def tracked_paths(self, sha: str) -> list[str]:
        """Every tracked path at `sha`.

        Read at the run's base sha for the same reason plan documents are: the
        planner should be shown one stable picture of the repository for the
        whole run, not one that shifts under it as stages land.
        """
        out = self._out("ls-tree", "-r", "--name-only", sha)
        return [line for line in out.splitlines() if line.strip()]

    def tracked_paths_now(self) -> list[str]:
        """Every tracked path in the working tree, as it stands.

        Distinct from `tracked_paths(sha)`, which answers a question about a
        commit. The residue check needs the files as the attempt left them,
        including ones it added, and excluding scratch files it never staged —
        git's idea of the repository, at this instant.
        """
        out = self._out("ls-files", "-z")
        return [line for line in out.split("\0") if line.strip()]

    def commits_between(self, have: str, want: str) -> list[str]:
        """Subjects of commits on `want` that `have` does not contain.

        Used by `reconcile` to say how much work the plan is being checked
        against. Deliberately not used at preflight: an earlier version warned
        that the project branch was behind `base_ref`, which on any repository
        with more than one engineer fires every time and teaches the operator
        to skim the checks that matter.
        """
        out = self._out("log", "--oneline", "--no-decorate", f"{have}..{want}")
        return [line for line in out.splitlines() if line.strip()]

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

    def cut_stage_branch(
        self, name: str, project_branch: str, fresh: bool = False
    ) -> str:
        """Start a stage from the project branch tip.

        `fresh` distinguishes the planner's two revision modes, which are
        otherwise indistinguishable in git. Stage branch names are deterministic
        — index plus stage id — so a redrawn stage recomputes the same name, and
        without this the existing branch was simply checked out. A restart then
        inherited the very attempt it was restarting from, and the reviewer saw
        only the delta against work it had never been shown.

        `fresh=False` is the extend case: scope was too narrow, the approach was
        sound, and the work survives.
        """
        if self.branch_exists(name):
            if fresh:
                self.checkout(project_branch)
                self._run("branch", "-D", name)
                self.create_branch(name, project_branch)
            else:
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

    def diff(self, since_sha: str, *, ignore_line_endings: bool = False) -> str:
        """The stage's diff.

        `ignore_line_endings` hides hunks whose only difference is a carriage
        return at end of line. That churn is the editor's, not the model's:
        `edittools.normalise` rewrites line endings on every file it writes, so
        on a repository with mixed endings — most have some — a one-line
        semantic change arrives as a whole-file rewrite.

        Written for a subprocess editor that did the same thing, and kept
        because the replacement does it too. Worth knowing what hiding it costs:
        the reviewer never sees the conversion, so it accrues unobserved. On one
        target with 542 CRLF files and no `.gitattributes`, ten had been
        silently converted after 77 stages, leaving the repository mixed where
        it had been uniform. The exemption is right and its effect still wants
        counting — see CLAUDE.md, "Hiding a tool's own churn from the gate".

        Symmetric on purpose. On Linux and macOS the platform default converts
        CRLF to LF; on Windows it converts LF to CRLF. A fix that assumed
        either direction was "correct" would be wrong for the other half of the
        operators, so the direction is not judged at all.

        Observed: a stage converting one Prototype call in a 156-line CRLF
        template was rejected with "the semantic conversion matches the stage,
        but the whole-file line-ending churn is out of scope and must be
        removed". The executor cannot comply — nothing in the model chose the
        rewrite — so it reproduced the identical diff until the progress guard
        stopped it.

        Used for the reviewer's copy only. `diff_names`, the scope guard and
        `added_lines` are unaffected, and the churn still lands in the commit:
        this hides it from a judgement it would only distort, not from the
        record.
        """
        self._mark_intent_to_add()
        args = ["diff"]
        if ignore_line_endings:
            args.append("--ignore-cr-at-eol")
        args.append(since_sha)
        return self._run(*args).stdout

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

    def _added_line_numbers(self, since_sha: str) -> dict[str, set[int]]:
        """Line numbers, in the working tree, of every line the stage added."""
        self._mark_intent_to_add()
        out = self._run("diff", "-U0", "--no-color", since_sha).stdout
        added: dict[str, set[int]] = {}
        path: str | None = None
        lineno = 0
        for line in out.splitlines():
            if line.startswith("+++"):
                raw = line[3:].strip()
                if raw == "/dev/null":
                    path = None
                else:
                    path = raw[2:] if raw.startswith(("a/", "b/")) else raw
                continue
            if line.startswith("---"):
                continue
            match = _HUNK.match(line)
            if match:
                lineno = int(match.group(1))
                continue
            if path and line.startswith("+"):
                added.setdefault(path, set()).add(lineno)
                lineno += 1
        return added

    # --- mutations ------------------------------------------------------

    def strip_added_trailing_whitespace(self, since_sha: str) -> list[str]:
        """Remove trailing blanks from the lines this stage added.

        What the orchestrator commits must survive a pre-commit hook, and
        `git diff --cached --check` — the usual form of one — rejects trailing
        whitespace on added lines. Nothing upstream reliably prevents it.
        The operator's `checks` run inside the executor's loop and can correct
        it, but only for the file types the declared linter understands — a
        Ruby formatter does nothing for ERB templates, YAML, or most non-source
        files, and those are exactly where a stray trailing space survives.

        The line need not be the executor's. One that already carried trailing
        whitespace becomes an *added* line the moment the edit rewrites enough
        of its surroundings. Observed: a single inherited trailing space in an
        ERB partial failed the same stage four times, and a fresh branch would
        not have helped — the executor had nothing to do differently.

        Added lines only. Rewriting whole files would put churn on lines no
        stage touched in front of the reviewer, which is the mistake the
        line-ending exemption in `diff` exists to undo. Carriage returns are
        left alone for the same reason: a CRLF file is not a file with trailing
        whitespace, and stripping the `\\r` would rewrite every line in it.

        Returns the paths it rewrote, for the log.
        """
        changed: list[str] = []
        for rel, numbers in sorted(self._added_line_numbers(since_sha).items()):
            target = self.repo / rel
            if not target.is_file():
                continue
            try:
                raw = target.read_bytes()
            except OSError:
                continue
            if b"\0" in raw:
                continue
            # Split on "\n" rather than `splitlines`, which also breaks on form
            # feeds and \x1c-\x1e. Git counts lines by "\n", and a numbering
            # that disagreed with git's would strip the wrong line.
            lines = raw.split(b"\n")
            touched = False
            for number in numbers:
                if not 1 <= number <= len(lines):
                    continue
                body = lines[number - 1]
                cr = b"\r" if body.endswith(b"\r") else b""
                core = body[:-1] if cr else body
                stripped = core.rstrip(b" \t")
                if stripped != core:
                    lines[number - 1] = stripped + cr
                    touched = True
            if touched:
                target.write_bytes(b"\n".join(lines))
                changed.append(rel)
        return changed

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
        # `merge --squash` stages and `commit` is a second step, so between
        # them the project branch carries a staged merge and a modified
        # worktree with no commit. Anything that fails in that window — a
        # pre-commit hook rejecting whitespace is the usual one, seen three
        # times — used to end the run there and leave the next resume opening
        # on a project branch dirty in a way nothing in the pipeline produced.
        #
        # So the window is closed by rolling back to where the branch was. This
        # is safe because the child branch still holds every commit: the
        # landing can simply be retried, and nothing that was not already
        # recoverable is lost.
        before = self.head_sha()
        try:
            merge = self._run("merge", "--squash", child_branch, check=False)
            if merge.returncode != 0:
                raise GitError(
                    f"squash merge of {child_branch!r} into {project_branch!r} "
                    f"failed: {merge.stderr.strip() or merge.stdout.strip()}"
                )
            if self._out("diff", "--cached", "--name-only") == "":
                return None
            self._run("-c", "commit.gpgsign=false", "commit", "-q", "-m", message)
        except Exception:
            # Best effort, and deliberately silent: the original failure is the
            # diagnosis and must be what reaches the caller. A rollback that
            # cannot run leaves exactly the state that existed before this
            # method tried to help.
            self._run("reset", "--hard", before, check=False)
            raise
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
