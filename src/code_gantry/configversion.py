"""The config's identity, and the rule that it cannot change under a run.

This replaces an approval hash, and the reasoning is worth keeping because the
approval mechanism was right about the problem and had a weaker answer.

Approval was a sha256 of the exact bytes an operator confirmed they had read,
stored beside the config and invalidated by any edit. Its own module explained
why there was no `approved: true` field: "such a field could be set by
anything — including a model that had somehow been given write access to it."

Once the config lives inside the repository it describes, git already hashes
those bytes — and its hash carries what a bare sha256 cannot: an author, a
message, a parent, and whatever review the repository requires to land a
commit. A blob sha is the approval hash with provenance attached.

So the rule becomes a property rather than a ceremony:

**A run reads one config and reads it for its whole life.** The sha is recorded
at start, checked on every resume, and a mismatch refuses. Changing the config
does not invalidate a token to be re-issued; it means the run in front of you
is not the run that config describes, and the answer is a new run.

That is stricter than approval in the direction that matters. Approval would
let an edited-and-re-approved config take over a run already twenty stages
deep, so the commands that produced the first twenty and the commands
producing the next twenty could differ with nothing in the record saying
where. It is also less work: there is no command to remember, and forgetting
it is not a way to be stopped.

**Uncommitted means unrunnable.** A config the repository has never seen has
none of the provenance this rests on, so a run refuses to start on one. That
is the one piece of friction approval had and this keeps, moved to where it
buys something.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class ConfigVersionError(Exception):
    """The config is not in a state a run can be started or continued on."""


def blob_sha(config_path: Path | str) -> str:
    """What git calls these bytes.

    `hash-object` rather than hashing here, so the value is the one `git cat-
    file` will answer to. A sha computed our own way would be right and
    useless: the point of using git's is that an operator can look it up.
    """
    path = Path(config_path)
    try:
        out = subprocess.run(
            ["git", "hash-object", "--", str(path)],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as e:
        raise ConfigVersionError(f"cannot hash {path}: {e}") from e
    return out.stdout.strip()


def committed_sha(config_path: Path | str, repo: Path | str, ref: str) -> str | None:
    """The sha of this config as `ref` has it, or None if it is not there.

    Distinguished from "differs" by the caller, because the two want different
    sentences: a config the branch has never seen is a setup mistake, and one
    that differs is an edit somebody has not committed.
    """
    path = Path(config_path).resolve()
    repo = Path(repo).resolve()
    if not path.is_relative_to(repo):
        return None
    rel = path.relative_to(repo)
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{ref}:{rel}"],
        capture_output=True, text=True,
    )
    return out.stdout.strip() if out.returncode == 0 else None


def problem_starting(config_path: Path | str, repo: Path | str, ref: str) -> str:
    """Why this config cannot begin a run, or empty.

    The config must be committed on the branch the run will read the plan from,
    and the working copy must match it. Both halves matter: an uncommitted
    config has no provenance, and a working copy that differs from the commit
    means the bytes about to run are not the bytes anyone reviewed.
    """
    on_disk = blob_sha(config_path)
    committed = committed_sha(config_path, repo, ref)
    if committed is None:
        return (
            f"{config_path} is not committed on {ref}. A run's config is "
            "identified by its git sha, so it has to be in the branch's "
            "history before a run can cite it."
        )
    if committed != on_disk:
        return (
            f"{config_path} has uncommitted changes ({on_disk[:12]} on disk, "
            f"{committed[:12]} on {ref}). Commit them, then start a run — a "
            "run records the sha it read and is answered by that commit "
            "afterwards."
        )
    return ""


def problem_resuming(config_path: Path | str, recorded: str) -> str:
    """Why this run cannot continue, or empty.

    A run reads one config for its whole life. If the file has moved on, this
    is not the run that config describes: the stages already landed were
    produced by different commands, and nothing in the record would say where
    the change fell. Starting a fresh run is the answer, and it costs nothing
    that matters — every landed stage is squash-merged onto the project branch
    and the plan documents carry the history.
    """
    if not recorded:
        return ""
    current = blob_sha(config_path)
    if current == recorded:
        return ""
    return (
        f"the config changed since this run started ({recorded[:12]} then, "
        f"{current[:12]} now). A run reads one config for its whole life, so "
        "the stages it already landed were produced by different commands. "
        "Start a new run against the current config; everything that landed "
        "is on the project branch."
    )
