"""`code-gantry plan …` and `code-gantry ledger …`: the operator's side of the ledger.

Every command here is a person's write or read. The pipeline's writes live in
`nodes.py`. Actor comes from `CODE_GANTRY_ACTOR`, falling back to the login
name; origin from `CODE_GANTRY_ORIGIN`, falling back to the hostname.
"""

from __future__ import annotations

import dataclasses
import getpass
import json
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
    Waiting,
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

    cfg, project = _project_for(_config_argument(config_path), quiet=True)
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


# The face another process reads. Every field of the record, as the type
# declares it, so a reader in another language sees what a reader here
# sees; the writer is the dataclass, never a hand-written list of keys.
json_option = click.option("--json", "as_json", is_flag=True, help="Machine-readable: every field of each record.")


def _emit_json(rows) -> None:
    click.echo(json.dumps(rows, indent=2, sort_keys=True))


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


@plan.command("sections")
@json_option
@config_option
def plan_sections(as_json, config_path) -> None:
    """Every document and section, in plan order with its depth: the places
    an item can be added or moved under."""
    _, _, led = _cfg_and_ledger(config_path, write=False)
    views = led.views()
    rows = []
    for node in views.walk():
        if node.kind in ("document", "section"):
            rows.append({"key": node.key, "kind": node.kind, "title": node.title, "parent": node.parent,
                         "depth": len(views.ancestors(node.key)), "owner": node.owner})
    if as_json:
        return _emit_json(rows)
    for row in rows:
        click.echo(f"{'  ' * row['depth']}{row['key']} {row['title']}")


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
@json_option
@config_option
def ledger_show(key, only, as_json, config_path) -> None:
    """Every item's state, or one key's."""
    _, _, led = _cfg_and_ledger(config_path, write=False)
    views = led.views()
    nodes = [_key(led, key)] if key else [n for n in views.walk() if n.kind == "item"]
    rows = [(node, views.state(node.key)) for node in nodes]
    rows = [(node, state) for node, state in rows if not only or state.state == only]
    if as_json:
        return _emit_json([{**dataclasses.asdict(node), "state": dataclasses.asdict(state)} for node, state in rows])
    for node, state in rows:
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
@json_option
@config_option
def ledger_findings(for_human, everything, as_json, config_path) -> None:
    """Open findings, oldest first."""
    _, _, led = _cfg_and_ledger(config_path, write=False)
    views = led.views()
    findings = [
        f for f in sorted(views.findings.values(), key=lambda f: f.opened_at)
        if (everything or f.status == "open") and (not for_human or f.needs == "human")
    ]
    if as_json:
        return _emit_json([dataclasses.asdict(f) for f in findings])
    for f in findings:
        keys = ", ".join(f.keys) or "-"
        click.echo(f"{f.id} [{f.status}] on {keys} by {f.by} (needs {f.needs}): {f.claim}")
        if f.total:
            click.echo(f"    total: {f.total}")
        if f.answer_text:
            click.echo(f"    answer: {f.disposition} — {f.answer_text}")


# -- what is waiting on a person ----------------------------------------------


def _waiting_row(w: Waiting) -> dict:
    return dataclasses.asdict(w)


def _thing_json(led: Ledger, about: str) -> dict:
    """The one thing, as `waiting` would list it, or its bare thread once it
    is no longer waiting."""
    views = led.views()
    for w in views.waiting():
        if w.id == about:
            return _waiting_row(w)
    return {
        "id": about, "recommendation": views.recommendations.get(about),
        "thread": list(views.threads.get(about, [])),
    }


@ledger.command("waiting")
@json_option
@config_option
def ledger_waiting(as_json, config_path) -> None:
    """Everything waiting on a person: open human-owned items in plan
    order, then findings that need a human, oldest first, each with the
    card an investigation attached and the thread since."""
    _, _, led = _cfg_and_ledger(config_path, write=False)
    rows = led.views().waiting()
    if as_json:
        return _emit_json([_waiting_row(w) for w in rows])
    for w in rows:
        where = ", ".join(w.keys) if w.kind == "finding" else ""
        click.echo(f"{w.id} [{w.kind}]{' on ' + where if where else ''}: {w.title}")
        if w.recommendation:
            rec = w.recommendation.get("recommend") or {}
            click.echo(f"    recommend: {rec.get('disposition')}" + (f" — {rec['text']}" if rec.get("text") else ""))
        for entry in w.thread:
            if entry["kind"] == "asked":
                click.echo(f"    asked by {entry.get('by')}: {entry.get('text')}")


@ledger.command("recommend")
@click.argument("about")
@click.option("--file", "card_file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help="The card, as JSON.")
@click.option("--card", "card_text", default=None, help="The card, as JSON, inline.")
@json_option
@config_option
def ledger_recommend(about, card_file, card_text, as_json, config_path) -> None:
    """Attach a card to a finding or an item: `says`, `anchors` (a list),
    `checked`, `recommend` and `would_write`. `recommend` is {disposition,
    text, target, to, sha, landings}: disposition one of amend, discard,
    debt, raise, move, landed, struck, pipeline; `target` a key (amend,
    debt, or the section for a moved item); `to` a project's config path
    (move); `sha` the commit (landed, an item); `landings` a list of
    {key, sha} (landed, a finding that says several items are done). The
    latest card is the recommendation; the thread keeps them all."""
    if (card_file is None) == (card_text is None):
        raise click.ClickException("give the card once: --file or --card")
    try:
        card = json.loads(card_file.read_text() if card_file else card_text)
    except json.JSONDecodeError as e:
        raise click.ClickException(f"the card is not JSON: {e}")
    _, _, led = _cfg_and_ledger(config_path, write=True)
    try:
        led.recommend(about, card=card)
    except LedgerError as e:
        raise click.ClickException(str(e))
    if as_json:
        return _emit_json(_thing_json(led, about))
    click.echo(f"{about}: recommended {card['recommend']['disposition']}")


@ledger.command("ask")
@click.argument("about")
@click.option("--text", required=True, help="The question, for the next investigation to read.")
@json_option
@config_option
def ledger_ask(about, text, as_json, config_path) -> None:
    """A person's question on a finding's or an item's thread."""
    _, _, led = _cfg_and_ledger(config_path, write=True)
    try:
        led.ask(about, text=text)
    except LedgerError as e:
        raise click.ClickException(str(e))
    if as_json:
        return _emit_json(_thing_json(led, about))
    click.echo(f"{about}: asked")


@ledger.command("move")
@click.argument("about")
@click.option("--to", "to_config", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path), help="The other project's config.")
@click.option("--under", default=None, help="For an item: the section key in the other project it goes under.")
@json_option
@config_option
def ledger_move(about, to_config, under, as_json, config_path) -> None:
    """Move a finding or an item to another project: opened there with a
    pointer back, closed here naming where it went. General debt is a
    project like any other, so this is how a thing becomes general debt."""
    from_cfg, from_project, led = _cfg_and_ledger(config_path, write=True)
    to_cfg, to_project, to_led = _cfg_and_ledger(to_config, write=True)
    label = lambda cfg, project: cfg.ledger.name or str(project.ledger)  # noqa: E731
    try:
        opened = led.move(
            about, to=to_led, to_label=label(to_cfg, to_project), from_label=label(from_cfg, from_project), under=under,
        )
    except LedgerError as e:
        raise click.ClickException(str(e))
    if as_json:
        return _emit_json({"from": about, "to": label(to_cfg, to_project), "opened_as": opened})
    click.echo(f"{about}: moved to {label(to_cfg, to_project)} as {opened}")


@ledger.command("investigate")
@click.argument("about")
@json_option
@config_option
def ledger_investigate(about, as_json, config_path) -> None:
    """Have the investigator attach a card to one thing waiting on a
    person: a model with a shell, run in the checkout, that writes
    `ledger recommend` and nothing else. Exits 0 when a card was written,
    1 when not; the transcript is under the work directory either way."""
    from code_gantry.investigator import investigate

    cfg, project, led = _cfg_and_ledger(config_path, write=False)
    label = cfg.ledger.name or str(project.ledger)
    try:
        result = investigate(cfg, led, about, work_dir=project.work_dir, project_label=label, config_path=Path(config_path or os.environ.get("CODE_GANTRY_CONFIG")).resolve())
    except LookupError as e:
        raise click.ClickException(str(e))
    if as_json:
        _emit_json({**dataclasses.asdict(result), "transcript": str(result.transcript)})
    else:
        click.echo(
            f"{about}: {'card written' if result.recommended else 'no card written'} "
            f"({result.seconds:.0f}s, exit {result.exit_code}); transcript {result.transcript}"
        )
    if not result.recommended:
        raise SystemExit(1)


@ledger.command("accept")
@click.argument("about")
@json_option
@config_option
def ledger_accept(about, as_json, config_path) -> None:
    """Apply what the card on a finding or an item recommends, as the events
    the answer would have been: a finding's disposition; `landed` as a
    landing per key it names and the finding closed; an item's landing,
    strike or hand-over to the fleet; a move to the project it names.
    One card, one transaction."""
    from code_gantry.ledger import DISPOSITIONS

    from_cfg, from_project, led = _cfg_and_ledger(config_path, write=True)
    views = led.views()
    card = views.recommendations.get(about)
    if not card:
        raise click.ClickException(f"{about} has no card to accept")
    kind = "finding" if about in views.findings else "item"
    rec = card.get("recommend") or {}
    disposition = rec.get("disposition")
    text = rec.get("text")
    applied: list[str] = []
    try:
        with led.transaction():
            if disposition in DISPOSITIONS and kind == "finding":
                led.answer_finding(about, disposition=disposition, text=text, target_key=rec.get("target"))
                applied.append(f"{disposition} {about}")
            elif disposition == "landed":
                landings = rec.get("landings") or ([{"key": rec.get("target") or about, "sha": rec["sha"]}] if rec.get("sha") else [])
                if not landings:
                    raise click.ClickException("the card recommends `landed` but names no sha")
                for landing in landings:
                    _key(led, landing["key"])
                    led.append(LANDED, key=landing["key"], sha=landing["sha"], evidence=text)
                    applied.append(f"landed {landing['key']} {landing['sha']}")
                if kind == "finding":
                    led.answer_finding(about, disposition="discard", text=", ".join(applied))
                    applied.append(f"discard {about}")
            elif disposition == "struck" and kind == "item":
                led.append(STRUCK, key=about, reason=text or "struck as recommended")
                applied.append(f"struck {about}")
            elif disposition == "pipeline" and kind == "item":
                node = _key(led, about)
                led.upsert_node(
                    about, parent=node.parent, position=node.position, kind=node.kind, title=node.title,
                    body=node.body, owner="pipeline", blocking=node.blocking, path=node.path, base_version=node.version,
                )
                applied.append(f"pipeline {about}")
            elif disposition == "move" and rec.get("to"):
                to_cfg, to_project, to_led = _cfg_and_ledger(Path(rec["to"]), write=True)
                label = lambda cfg, project: cfg.ledger.name or str(project.ledger)  # noqa: E731
                opened = led.move(about, to=to_led, to_label=label(to_cfg, to_project), from_label=label(from_cfg, from_project), under=rec.get("target"))
                applied.append(f"moved {about} to {label(to_cfg, to_project)} as {opened}")
            else:
                raise click.ClickException(f"the card recommends {disposition!r}, which cannot be applied to a {kind} from here")
    except LedgerError as e:
        raise click.ClickException(str(e))
    if as_json:
        return _emit_json({"about": about, "disposition": disposition, "applied": applied, "to": rec.get("to")})
    click.echo(f"{about}: accepted {disposition}; " + "; ".join(applied))


@ledger.command("answer")
@click.argument("finding_id")
@click.argument("disposition", type=click.Choice(["amend", "discard", "debt", "raise"]))
@click.option("--text", default=None, help="For `amend`, the sentence the item carries; for `debt`, the entry; for `raise`, why a person must decide.")
@click.option("--target", default=None, help="For `amend`, the key the text is written under (default: the finding's first key); for `debt`, the section the entry goes under.")
@json_option
@config_option
def ledger_answer(finding_id, disposition, text, target, as_json, config_path) -> None:
    """A disposition of one finding, in any order: `amend` writes the text
    on the item at the next fold; `discard` closes it; `debt` makes it an
    item under the target section and closes it; `raise` keeps it open and
    hands it to a person."""
    _, _, led = _cfg_and_ledger(config_path, write=True)
    if disposition == "amend" and not text:
        raise click.ClickException("`amend` needs --text: the sentence the plan should carry")
    if target:
        _key(led, target)
    try:
        led.answer_finding(finding_id, disposition=disposition, text=text, target_key=target)
    except LedgerError as e:
        raise click.ClickException(str(e))
    if as_json:
        return _emit_json(dataclasses.asdict(led.views().findings[finding_id]))
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


@ledger.command("candidates")
@json_option
@config_option
def ledger_candidates(as_json, config_path) -> None:
    """Candidates pushed and not yet composed, and rejected ones waiting for rework."""
    _, _, led = _cfg_and_ledger(config_path, write=False)
    views = led.views()
    pending = views.pending_candidates()
    rejected = views.rework_waiting()
    if as_json:
        return _emit_json({
            "pending": [dataclasses.asdict(c) for c in pending],
            "rejected": [{**dataclasses.asdict(r.candidate), "against": r.against, "reason": r.reason, "rejected_at": r.at} for r in rejected],
        })
    for c in pending:
        click.echo(f"pending  {c.branch} {c.sha[:12]} ({c.stage_id}) on {', '.join(c.landing.get('keys') or [])}")
    for r in rejected:
        click.echo(f"rejected {r.candidate.branch} {r.candidate.sha[:12]} ({r.candidate.stage_id}): {r.reason[:100]}")
    if not pending and not rejected:
        click.echo("no candidates")


@ledger.command("dismiss")
@click.argument("branch")
@click.option("--reason", required=True, help="Why it is not landed and not reworked.")
@click.option("--delete-branch", is_flag=True, help="Also take the branch off origin.")
@json_option
@config_option
def ledger_dismiss(branch, reason, delete_branch, as_json, config_path) -> None:
    """Put a pending or rejected candidate aside: nobody composes or
    reworks it. For a second candidate on work that is already on the
    branch, which a rework would only redo."""
    from code_gantry.ledger import CANDIDATE_DISMISSED

    cfg, _, led = _cfg_and_ledger(config_path, write=True)
    views = led.views()
    if branch not in views.candidates and branch not in views.rejected:
        raise click.ClickException(f"{branch} is neither pending nor rejected")
    led.append(CANDIDATE_DISMISSED, branch=branch, reason=reason)
    deleted = False
    if delete_branch:
        try:
            Git(cfg.target_repo).delete_remote_branch(branch)
            deleted = True
        except GitError as e:
            click.echo(f"dismissed, but the branch stays at origin: {e}", err=True)
    if as_json:
        return _emit_json({"branch": branch, "dismissed": True, "branch_deleted": deleted})
    click.echo(f"{branch}: dismissed" + (", branch deleted at origin" if deleted else ""))


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
