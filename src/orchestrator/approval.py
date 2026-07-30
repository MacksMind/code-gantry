"""Operator approval, recorded as a hash.

There is no `approved: true` field in the config, because such a field could be
set by anything — including a model that had somehow been given write access to
it. Approval is a hash of the exact bytes the operator reviewed, stored beside
the config.

Editing the config invalidates approval and takes one command to restore. The
friction is small, and it lands exactly where friction belongs: on a file full
of shell commands about to run unattended for hours.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

APPROVAL_FILENAME = "approval.json"


class ApprovalError(Exception):
    pass


@dataclass
class Approval:
    config_sha256: str
    approved_at: str
    config_path: str


def config_hash(config_path: Path | str) -> str:
    """Hash the file's exact bytes.

    Not the parsed config: a comment change is a change the operator should
    re-read, and normalising through YAML would let a semantically identical but
    differently-worded file inherit an old approval.
    """
    path = Path(config_path)
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as e:
        raise ApprovalError(f"cannot read {path}: {e}") from e


def approval_path(project_dir: Path | str) -> Path:
    return Path(project_dir) / APPROVAL_FILENAME


def record_approval(project_dir: Path | str, config_path: Path | str, now: str) -> Approval:
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    approval = Approval(
        config_sha256=config_hash(config_path),
        approved_at=now,
        config_path=str(Path(config_path).resolve()),
    )
    approval_path(project_dir).write_text(
        json.dumps(
            {
                "config_sha256": approval.config_sha256,
                "approved_at": approval.approved_at,
                "config_path": approval.config_path,
            },
            indent=2,
        )
        + "\n"
    )
    return approval


def read_approval(project_dir: Path | str) -> Approval | None:
    path = approval_path(project_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        # A corrupt approval file is an unapproved config, not a crash.
        return None
    if not isinstance(data, dict) or not data.get("config_sha256"):
        return None
    return Approval(
        config_sha256=data["config_sha256"],
        approved_at=data.get("approved_at", "(unknown)"),
        config_path=data.get("config_path", ""),
    )


def approval_problem(project_dir: Path | str, config_path: Path | str) -> str | None:
    """None if the config is approved as it currently stands, else why not."""
    approval = read_approval(project_dir)
    if approval is None:
        return (
            "this config has never been approved. Review it — every command in "
            "it is about to run unattended — then run `orchestrator approve`."
        )

    current = config_hash(config_path)
    if current != approval.config_sha256:
        return (
            f"the config has changed since it was approved at "
            f"{approval.approved_at}. Re-read the diff and run "
            "`orchestrator approve` again.\n"
            f"  approved: {approval.config_sha256[:16]}\n"
            f"  current:  {current[:16]}"
        )
    return None
