"""The prompts travel with the package.

`prompts/` is the repository's top-level directory so a person edits it in
place, and the wheel carries a copy inside the package so an install that
has no checkout can still render every prompt. `prompts_dir` prefers the
packaged copy when one exists; a checkout has none, so it reads the top
level as before. The proof is the built wheel installed somewhere with no
checkout in sight, not an assertion about paths.
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

from code_gantry import promptfiles

REPO = Path(__file__).resolve().parents[1]


def test_a_checkout_reads_the_top_level_prompts(monkeypatch, tmp_path):
    monkeypatch.delenv(promptfiles.PROMPTS_ENV, raising=False)
    monkeypatch.setattr(promptfiles, "PACKAGED", tmp_path / "absent")
    assert promptfiles.prompts_dir() == REPO / "prompts"


def test_a_packaged_copy_is_preferred_to_the_top_level(monkeypatch, tmp_path):
    monkeypatch.delenv(promptfiles.PROMPTS_ENV, raising=False)
    packaged = tmp_path / "prompts"
    packaged.mkdir()
    monkeypatch.setattr(promptfiles, "PACKAGED", packaged)
    assert promptfiles.prompts_dir() == packaged


def test_the_override_beats_both(monkeypatch, tmp_path):
    packaged = tmp_path / "prompts"
    packaged.mkdir()
    monkeypatch.setattr(promptfiles, "PACKAGED", packaged)
    monkeypatch.setenv(promptfiles.PROMPTS_ENV, str(tmp_path / "elsewhere"))
    assert promptfiles.prompts_dir() == tmp_path / "elsewhere"


def test_the_wheel_renders_every_prompt_without_a_checkout(tmp_path):
    out = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--project", str(REPO), "--out-dir", str(out)],
        check=True, capture_output=True, text=True,
    )
    (wheel,) = out.glob("*.whl")
    inside = {n for n in zipfile.ZipFile(wheel).namelist() if n.startswith("code_gantry/prompts/")}
    expected = {f"code_gantry/prompts/{n}.md" for n in promptfiles.names()}
    assert inside == expected

    venv = tmp_path / "venv"
    subprocess.run(["uv", "venv", "--quiet", "--python", sys.executable, str(venv)], check=True, capture_output=True)
    subprocess.run(
        ["uv", "pip", "install", "--quiet", "--python", str(venv / "bin" / "python"), "--no-deps", str(wheel)],
        check=True, capture_output=True, text=True,
    )
    probe = (
        "import json, code_gantry.promptfiles as p;"
        "print(json.dumps({'dir': str(p.prompts_dir()), 'names': p.names(),"
        " 'rendered': [bool(p.raw(n)) for n in p.names()]}))"
    )
    # A cwd outside the checkout, and no environment override, so the only
    # prompts the installed package can find are the ones it carries.
    result = subprocess.run(
        [str(venv / "bin" / "python"), "-c", probe], cwd=tmp_path, check=True,
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
    )
    got = json.loads(result.stdout)
    assert Path(got["dir"]).is_relative_to(venv)
    assert got["names"] == promptfiles.names()
    assert all(got["rendered"])
