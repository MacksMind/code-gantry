"""Nothing tracked names the target repository, a host, or a person.

This repository will be published, and the target it is run against, the
machines it runs on and the person running it are not its business. The
names themselves cannot be written here, so they live in a file that is
never tracked: `.git/info/forbidden_names` in the clone, one token per
line, `#` for a comment, matched case-insensitively as a substring. The
pre-commit hook sweeps the staged blobs against the same file; this test
sweeps every tracked file and every untracked file git would offer to add,
so a name is caught by the suite before the hook has to.

A clone without the file skips: the list is the operator's, and a clone
elsewhere has nothing of theirs to leak.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def names_file() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--git-path", "info/forbidden_names"],
        cwd=REPO, check=True, capture_output=True, text=True,
    ).stdout.strip()
    return (REPO / out).resolve()


def forbidden(path: Path) -> list[str]:
    tokens = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            tokens.append(line.lower())
    return tokens


def candidate_files() -> list[Path]:
    """Every tracked file, and every untracked one git does not ignore."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO, check=True, capture_output=True,
    ).stdout
    return [REPO / p.decode() for p in out.split(b"\0") if p]


def hits(files, tokens) -> list[str]:
    found = []
    for path in files:
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="replace").lower()
        for token in tokens:
            if token in text:
                lines = [i + 1 for i, line in enumerate(text.splitlines()) if token in line]
                found.append(f"{path.relative_to(REPO)}:{lines[0]}: {token!r} ({len(lines)} line(s))")
    return found


def test_no_tracked_file_carries_a_forbidden_name():
    path = names_file()
    if not path.is_file():
        pytest.skip(f"no {path}; the operator's name list is not in this clone")
    tokens = forbidden(path)
    assert tokens, f"{path} names nothing"
    found = hits(candidate_files(), tokens)
    assert not found, "forbidden names in the repository:\n" + "\n".join(found)


def test_the_list_is_never_tracked():
    """The list is the one file that may hold the names, so it must be
    outside what git tracks: `.git/info/` is, by construction."""
    out = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(names_file())],
        cwd=REPO, capture_output=True, text=True,
    )
    assert out.returncode != 0


def test_the_sweep_finds_a_name_in_a_file(tmp_path, monkeypatch):
    """The sweep itself, on a tree of our own: case-insensitive, substring,
    line numbered."""
    (tmp_path / "a.md").write_text("fine\nThe MyClient thing\n")
    (tmp_path / "b.py").write_text("x = 1\n")
    monkeypatch.setattr("test_nothing_names_the_target.REPO", tmp_path)
    found = hits([tmp_path / "a.md", tmp_path / "b.py"], ["myclient"])
    assert found == ["a.md:2: 'myclient' (1 line(s))"]
