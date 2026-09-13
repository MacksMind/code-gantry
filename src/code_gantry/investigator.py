"""The investigator: pass two of a fold, one thing at a time, by a model with
a shell.

A thing waiting on a person — a finding that needs a human, or an item a
person owns — gets a card: what it says, what it anchors to, what was checked
and how, and a recommended disposition with the text it would write. The
card is a ledger event (`ledger recommend`), so the person answers it from
the dashboard, a phone or a terminal, and the investigation never writes
anything else: not the tree, not the plan, not the answer.

The model is a command that reads the prompt on stdin and runs its own tool
loop — `claude -p` by default — started in the target checkout, which is a
bay's. What it did is a transcript under the work directory, and whether it
wrote a card is read back from the ledger rather than inferred from its
output: the ledger is the record.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from code_gantry.config import ProjectConfig
from code_gantry.ledger import RECOMMENDATIONS, Ledger, Waiting
from code_gantry.promptfiles import render

# What each recommendation means, as the prompt lists them. Kept beside the
# vocabulary rather than in the prompt file so a word added to one is missed
# by a test rather than by a model.
MEANINGS = {
    "fold": "change something already in the plan — a status, a scope, a count, a constraint it got wrong; `target` names the key and `text` is the sentence it carries",
    "discard": "the ledger keeps it; the plan never sees it — a complete outcome, and the right one for most observations",
    "debt": "a defect this project's own work created: an item under `target`, a section of this project, with `text` as the entry",
    "raise": "you cannot settle it and it needs the person's call; `text` says what would settle it",
    "move": "belongs to another project — general debt is a project like any other; `to` is that project's config path, `target` a section there for an item",
    "landed": "an item already done in the tree: `sha` is the commit that did it, checked with `git cat-file -e <sha>^{commit}`",
    "struck": "an item with nothing to do — zero population, wrong premise; `text` says why",
    "pipeline": "an item a person owns that the fleet could draw after all",
}


@dataclasses.dataclass
class Investigation:
    about: str
    transcript: Path
    exit_code: int
    recommended: bool
    seconds: float


def render_prompt(thing: Waiting, others: list[Waiting], *, project_label: str, config_path: Path) -> str:
    """The whole of what the investigator is told: the thing, the others
    waiting beside it, how to investigate, and the one verb it may write."""
    return render(
        "investigator/task",
        project=project_label,
        thing=json.dumps(dataclasses.asdict(thing), indent=2),
        others="\n".join(one_line(w) for w in others) or "- nothing else",
        verb=f"code-gantry ledger recommend {thing.id} --config {config_path}",
        dispositions="\n".join(f"- **{word}** — {MEANINGS[word]}" for word in sorted(RECOMMENDATIONS)),
    )


def investigate(cfg: ProjectConfig, led: Ledger, about: str, *, work_dir: Path, project_label: str, config_path: Path) -> Investigation:
    views = led.views()
    waiting = {w.id: w for w in views.waiting()}
    if about not in waiting:
        raise LookupError(f"{about} is not waiting on a person in {project_label}")
    before = len(views.threads.get(about, []))
    thing = waiting[about]
    others = [w for w in views.waiting() if w.id != about]
    prompt = render_prompt(thing, others, project_label=project_label, config_path=config_path)
    started = datetime.now(timezone.utc)
    transcript_dir = work_dir / "investigations"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript = transcript_dir / f"{about}-{started.strftime('%Y%m%d-%H%M%S')}.md"
    try:
        proc = subprocess.run(
            cfg.investigator.command, input=prompt, capture_output=True, text=True,
            cwd=cfg.target_repo, timeout=cfg.investigator.timeout_minutes * 60,
        )
        output, exit_code = proc.stdout + proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        output = (e.stdout or "") + (e.stderr or "") + f"\n[timed out after {cfg.investigator.timeout_minutes} minutes]"
        exit_code = 124
    seconds = (datetime.now(timezone.utc) - started).total_seconds()
    after = led.views()
    recommended = len(after.threads.get(about, [])) > before and about in after.recommendations
    transcript.write_text(
        f"# Investigation of {about} in {project_label}\n\n"
        f"started {started.isoformat(timespec='seconds')}, {seconds:.0f}s, "
        f"command exited {exit_code}, card written: {'yes' if recommended else 'no'}\n\n"
        f"## Prompt\n\n{prompt}\n\n## Output\n\n{output}\n"
    )
    return Investigation(about=about, transcript=transcript, exit_code=exit_code, recommended=recommended, seconds=seconds)


def one_line(thing: Waiting) -> str:
    return f"- {thing.id} ({thing.kind}): {thing.title}"
