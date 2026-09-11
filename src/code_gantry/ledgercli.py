"""`code-gantry plan …` and `code-gantry ledger …`: the operator's side of the ledger.

Every command here is a person's write or read. The pipeline's writes live in
`nodes.py`. Actor comes from `CODE_GANTRY_ACTOR`, falling back to the login
name; origin from `CODE_GANTRY_ORIGIN`, falling back to the hostname.
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path

import click

from code_gantry.gitops import Git, GitError
from code_gantry.ledger import (
    ANSWER,
    BLOCKED,
    CLAIMED,
    LANDED,
    NODE_RETIRED,
    RELEASED,
    STRUCK,
    Ledger,
    LedgerError,
    apply_fold,
    import_old_file,
    ledger_for,
    STAGE_DROPPED,
)
from code_gantry.plandoc import extract_links
from code_gantry.planmodel import export_markdown, import_documents, parse_markdown
from code_gantry.render import render_plan, render_projection

ACTOR_ENV = "CODE_GANTRY_ACTOR"
ORIGIN_ENV = "CODE_GANTRY_ORIGIN"


def actor() -> str:
    return os.environ.get(ACTOR_ENV) or getpass.getuser()


def origin() -> str | None:
    return os.environ.get(ORIGIN_ENV) or None


def _cfg_and_ledger(config_path, *, write: bool) -> tuple:
    from code_gantry.cli import _config_argument, _project_for

    cfg, project = _project_for(_config_argument(config_path))
    if cfg.ledger is None:
        raise click.ClickException(
            "no `ledger:` section in the config; set `ledger.key_prefix` first"
        )
    led = ledger_for(cfg, project, write=write, origin=origin(), actor=actor())
    return cfg, project, led


def _key(led: Ledger, key: str):
    node = led.views().nodes.get(key)
    if node is None or node.retired:
        raise click.ClickException(f"no such key {key}")
    return node


config_option = click.option(
    "--config", "config_path", type=click.Path(path_type=Path), default=None,
    help="The project's config; or set CODE_GANTRY_CONFIG.",
)


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


@click.group()
def plan() -> None:
    """The plan tree in the ledger: import, export, show, edit."""


@plan.command("import")
@click.argument("paths", nargs=-1, required=True, type=click.Path(path_type=Path))
@click.option("--owner", type=click.Choice(["pipeline", "human"]), default="pipeline")
@click.option("--blocking", is_flag=True, help="Every imported node blocks the cutover.")
@click.option("--follow-links", is_flag=True, help="Also import documents the root links, one level.")
@config_option
def plan_import(paths, owner, blocking, follow_links, config_path) -> None:
    """Read Markdown documents into the ledger, closed items as landings."""
    cfg, project, led = _cfg_and_ledger(config_path, write=True)
    repo = Path(cfg.target_repo)
    at_sha = _head(repo)
    docs = []
    seen: list[Path] = []
    queue = [Path(p) for p in paths]
    while queue:
        path = queue.pop(0)
        full = path if path.is_absolute() else repo / path
        if full in seen:
            continue
        if not full.is_file():
            raise click.ClickException(f"{path} is not a file")
        seen.append(full)
        rel = str(full.relative_to(repo)) if full.is_relative_to(repo) else str(full)
        text = full.read_text()
        docs.append(parse_markdown(text, path=rel))
        if follow_links:
            for link in extract_links(text):
                queue.append(full.parent / link)
    counts = import_documents(
        led, docs, prefix=cfg.ledger.key_prefix, owner=owner, blocking=blocking,
        at_sha=at_sha, actor=actor(),
    )
    for doc in docs:
        click.echo(f"{doc.path}: {doc.key}")
    click.echo(
        f"{counts['document']} document(s), {counts['section']} section(s), "
        f"{counts['item']} item(s); {counts['landed']} landed, {counts['struck']} struck"
    )


@plan.command("export")
@click.argument("key")
@click.option("--out", type=click.Path(path_type=Path), default=None)
@config_option
def plan_export(key, out, config_path) -> None:
    """A document as Markdown, state included, for editing and re-import."""
    _, _, led = _cfg_and_ledger(config_path, write=False)
    node = _key(led, key)
    if node.kind != "document":
        raise click.ClickException(f"{key} is a {node.kind}; export takes a document key")
    text = export_markdown(led.views(), key)
    if out:
        out.write_text(text)
        click.echo(f"wrote {out}")
    else:
        click.echo(text, nl=False)


@plan.command("show")
@click.argument("key")
@config_option
def plan_show(key, config_path) -> None:
    """One node: where it sits, what state it is in, what findings hang on it."""
    _, _, led = _cfg_and_ledger(config_path, write=False)
    views = led.views()
    node = _key(led, key)
    state = views.state(key)
    crumbs = " > ".join(a.title for a in reversed(views.ancestors(key)))
    click.echo(f"{key} [{node.kind}, {node.owner}{', blocking' if node.blocking else ''}] v{node.version}")
    if crumbs:
        click.echo(f"  under: {crumbs}")
    click.echo(f"  title: {node.title}")
    click.echo(f"  state: {state.state}" + (f" `{state.sha}`" if state.sha else "") + (f" by {state.actor}" if state.actor else ""))
    if state.question:
        click.echo(f"  question: {state.question}")
    if state.answer:
        click.echo(f"  answer: {state.answer}")
    if node.body:
        click.echo(f"  body: {node.body}")
    for mark in node.marks:
        click.echo(f"  mark: {mark}")
    children = views.children(key)
    if children:
        click.echo(f"  children: {len(children)}")
        for child in children:
            click.echo(f"    {child.key} [{views.state(child.key).state}] {child.title}")
    for finding in views.findings_on(key):
        click.echo(f"  finding {finding.id} [{finding.status}] by {finding.by}: {finding.claim}")


@plan.command("add")
@click.option("--under", "parent", required=True, help="Parent key.")
@click.option("--title", required=True)
@click.option("--body-file", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--kind", type=click.Choice(["section", "item"]), default="item")
@click.option("--owner", type=click.Choice(["pipeline", "human"]), default="pipeline")
@click.option("--blocking", is_flag=True)
@config_option
def plan_add(parent, title, body_file, kind, owner, blocking, config_path) -> None:
    """A new node under an existing one, taking the next key."""
    cfg, _, led = _cfg_and_ledger(config_path, write=True)
    views = led.views()
    _key(led, parent)
    key = views.next_key(cfg.ledger.key_prefix)
    position = len(views.children(parent))
    body = body_file.read_text().strip() if body_file else ""
    led.upsert_node(key, parent=parent, position=position, kind=kind, title=title,
                    body=body, owner=owner, blocking=blocking)
    click.echo(key)


@plan.command("edit")
@click.argument("key")
@click.option("--title", default=None)
@click.option("--body-file", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--owner", type=click.Choice(["pipeline", "human"]), default=None)
@click.option("--blocking/--not-blocking", default=None)
@config_option
def plan_edit(key, title, body_file, owner, blocking, config_path) -> None:
    """Change a node's title, body, owner or blocking flag. Flipping owner to
    `pipeline` is what makes a human-answered item drawable."""
    _, _, led = _cfg_and_ledger(config_path, write=True)
    node = _key(led, key)
    try:
        led.upsert_node(
            key, parent=node.parent, position=node.position, kind=node.kind,
            title=title if title is not None else node.title,
            body=body_file.read_text().strip() if body_file else node.body,
            owner=owner or node.owner,
            blocking=node.blocking if blocking is None else blocking,
            path=node.path, base_version=node.version,
        )
    except LedgerError as e:
        raise click.ClickException(str(e))
    click.echo(f"{key} v{node.version + 1}")


@plan.command("retire")
@click.argument("key")
@config_option
def plan_retire(key, config_path) -> None:
    """Take a node out of the tree. Its key is never reused."""
    _, _, led = _cfg_and_ledger(config_path, write=True)
    _key(led, key)
    led.append(NODE_RETIRED, key=key)
    click.echo(f"{key} retired")


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------


@click.group()
def ledger() -> None:
    """Key states and findings: what is landed, claimed, blocked, or open."""


@ledger.command("show")
@click.argument("key", required=False)
@click.option("--open", "only", flag_value="open")
@click.option("--claimed", "only", flag_value="claimed")
@click.option("--blocked", "only", flag_value="blocked")
@click.option("--landed", "only", flag_value="landed")
@config_option
def ledger_show(key, only, config_path) -> None:
    """Every item's state, or one key's."""
    _, _, led = _cfg_and_ledger(config_path, write=False)
    views = led.views()
    nodes = [_key(led, key)] if key else [n for n in views.walk() if n.kind == "item"]
    for node in nodes:
        state = views.state(node.key)
        if only and state.state != only:
            continue
        extra = f" `{state.sha}`" if state.sha else ""
        if state.state == "claimed":
            extra = f" by {state.actor or state.run_id}"
        if state.state == "blocked":
            extra = f": {state.question}"
        owner = "" if node.owner == "pipeline" else f" ({node.owner})"
        click.echo(f"{node.key} {state.state:8}{owner} {node.title}{extra}")


@ledger.command("findings")
@click.option("--for-human", is_flag=True, help="Only findings waiting on a person.")
@click.option("--all", "everything", is_flag=True, help="Closed ones too.")
@config_option
def ledger_findings(for_human, everything, config_path) -> None:
    """Open findings, oldest first."""
    _, _, led = _cfg_and_ledger(config_path, write=False)
    views = led.views()
    findings = sorted(views.findings.values(), key=lambda f: f.opened_at)
    for f in findings:
        if not everything and f.status != "open":
            continue
        if for_human and f.needs != "human":
            continue
        keys = ", ".join(f.keys) or "-"
        click.echo(f"{f.id} [{f.status}] on {keys} by {f.by} (needs {f.needs}): {f.claim}")
        if f.total:
            click.echo(f"    total: {f.total}")
        if f.answer_text:
            click.echo(f"    answer: {f.disposition} — {f.answer_text}")


@ledger.command("answer")
@click.argument("finding_id")
@click.argument("disposition", type=click.Choice(["fold", "discard", "debt", "raise"]))
@click.option("--text", default=None, help="For `fold`, the sentence the plan carries; for `debt`, the entry; for `raise`, why a person must decide.")
@click.option("--target", default=None, help="For `fold`, the key the text is written under (default: the finding's first key); for `debt`, the section the entry goes under.")
@config_option
def ledger_answer(finding_id, disposition, text, target, config_path) -> None:
    """A disposition of one finding, in any order: `fold` writes the text
    into the plan at the next fold; `discard` closes it; `debt` makes it an
    item under the target section and closes it; `raise` keeps it open and
    hands it to a person."""
    _, _, led = _cfg_and_ledger(config_path, write=True)
    if disposition == "fold" and not text:
        raise click.ClickException("`fold` needs --text: the sentence the plan should carry")
    if target:
        _key(led, target)
    try:
        led.answer_finding(finding_id, disposition=disposition, text=text, target_key=target)
    except LedgerError as e:
        raise click.ClickException(str(e))
    click.echo(f"{finding_id} {disposition}")


def _state_command(name, kind, help_text, *, needs_sha=False, text_option=None):
    @ledger.command(name, help=help_text)
    @click.argument("key")
    @click.argument("value", required=needs_sha or text_option is not None)
    @config_option
    def command(key, value, config_path):
        cfg, _, led = _cfg_and_ledger(config_path, write=True)
        _key(led, key)
        body = {}
        sha = None
        if needs_sha:
            sha = value
            try:
                sha = Git(cfg.target_repo).rev_parse(f"{sha}^{{commit}}")
            except GitError:
                raise click.ClickException(f"{sha} is not a commit in {cfg.target_repo}")
        elif text_option:
            body[text_option] = value
        led.append(kind, key=key, sha=sha, **body)
        click.echo(f"{key} {kind}")

    return command


_state_command("claim", CLAIMED, "Mark a key as being worked on, by you.")
_state_command("release", RELEASED, "Give a claimed key back.")
_state_command("land", LANDED, "Record that a key landed as a commit: KEY SHA.", needs_sha=True)
_state_command("strike", STRUCK, "Close a key without work: KEY REASON.", text_option="reason")
_state_command("block", BLOCKED, "Hold a key on a question for a person: KEY QUESTION.", text_option="question")
_state_command("unblock", ANSWER, "Answer a blocked key: KEY TEXT.", text_option="text")


@ledger.command("derived")
@click.option("--all", "everything", is_flag=True, help="Taken, done and dropped ones too.")
@config_option
def ledger_derived(everything, config_path) -> None:
    """Stages the planner has drawn: waiting, or with --all every one."""
    _, _, led = _cfg_and_ledger(config_path, write=False)
    views = led.views()
    rows = views.derived.values() if everything else views.derived_waiting()
    for d in sorted(rows, key=lambda d: (d.at, d.id)):
        who = f" by {d.taken_run}" if d.status == "taken" and d.taken_run else ""
        why = f": {d.reason}" if d.reason else ""
        refs = " ".join([*d.keys, *d.findings]) or "-"
        click.echo(f"{d.id} {d.status:8}{who}{why} {d.stage_id} on {refs} (drawn by {d.by_run or '?'})")


@ledger.command("drop")
@click.argument("derived_id")
@click.option("--reason", default="dropped by hand")
@config_option
def ledger_drop(derived_id, reason, config_path) -> None:
    """Withdraw a drawn stage so no run takes it."""
    _, _, led = _cfg_and_ledger(config_path, write=True)
    d = led.views().derived.get(derived_id)
    if d is None or d.status not in ("derived", "taken"):
        raise click.ClickException(f"no waiting or taken stage {derived_id}")
    led.append(STAGE_DROPPED, derived_id=derived_id, reason=reason)
    click.echo(f"{derived_id} dropped")


@ledger.command("import")
@click.argument("old_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@config_option
def ledger_import(old_file, config_path) -> None:
    """Copy a ledger file from before one sequence per ledger into the
    configured ledger, which must hold nothing yet."""
    _, _, led = _cfg_and_ledger(config_path, write=True)
    try:
        n = import_old_file(led, old_file)
    except LedgerError as e:
        raise click.ClickException(str(e))
    click.echo(f"{n} event(s) imported into {led.where}")


@ledger.command("fold")
@config_option
def ledger_fold(config_path) -> None:
    """Write landings and answered findings into the plan text now."""
    _, _, led = _cfg_and_ledger(config_path, write=True)
    click.echo(f"{apply_fold(led)} mark(s) written")


@ledger.command("render")
@click.option("--projection", is_flag=True, help="The churning half instead of the plan text.")
@config_option
def ledger_render(projection, config_path) -> None:
    """What the planner is sent, as text."""
    cfg, _, led = _cfg_and_ledger(config_path, write=False)
    views = led.views()
    if projection:
        click.echo(render_projection(views, note_chars=cfg.ledger.note_chars), nl=False)
    else:
        click.echo(render_plan(views), nl=False)


def _head(repo: Path) -> str | None:
    try:
        return Git(repo).head_sha()
    except GitError:
        return None
