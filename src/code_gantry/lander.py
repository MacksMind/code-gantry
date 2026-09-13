"""Composing candidates onto the project branch.

Every bay that finishes a stage pushes a candidate and moves nothing. The
project branch is moved by whichever bay holds the landing semaphore: it
replays every pending candidate onto the fetched tip, proves the whole
composition with one suite, and fast-forwards. One suite per composition
rather than one per stage, which is what stops four bays serialising behind
a branch only one of them can move at a time.

**The semaphore is attempted, never waited for.** A bay that queued for its
turn would spend a suite's worth of time on a job another bay is already
doing, and the stage it could have worked instead is what that job is
waiting for. With no daemon there is no semaphore, and no bay composes: a
host cannot decide alone that it is the only one landing.

Two stages each green on their own tree were never green together, and this
is where that is established. When the composition is red, the offenders are
removed until what is left is green — the green part lands, and each
offender goes back as a rejection carrying its branch, which still holds the
work on a base that has since moved.
"""

from __future__ import annotations

from dataclasses import dataclass

from code_gantry import mesh
from code_gantry.flake import adjudicate
from code_gantry.gitops import GitError
from code_gantry.ledger import (
    CANDIDATE_LANDED,
    CANDIDATE_REJECTED,
    FINDING_RELEASED,
    RELEASED,
    Candidate,
)


@dataclass
class Composition:
    """What one turn of the lander did."""

    landed: list[Candidate]
    rejected: list[tuple[Candidate, str]]
    sha: str | None
    suites: int
    escalation: dict | None = None


def landing_lock(rt) -> str:
    """One composing bay per project branch, anywhere. Named for the branch
    rather than the ledger: it is the branch that only one push can move,
    and two projects landing on two branches never contend."""
    import hashlib

    identity = f"{rt.cfg.ledger.name or rt.project.ledger.resolve()}::{rt.cfg.project_branch}"
    return f"landing-{hashlib.sha1(identity.encode()).hexdigest()[:12]}"


def compose(rt) -> Composition | None:
    """Take the landing semaphore if it is free and compose what is pending.

    None when another bay holds it, or when there is nothing to compose.
    """
    if not rt.cfg.compose_landings or rt.ledger is None:
        return None
    if not rt.git.remote_exists():
        return None

    with mesh.attempt(landing_lock(rt), f"{_bay(rt)} {rt.paths.run_id}", rt.log) as holding:
        if not holding:
            return None
        return _compose_holding(rt)


def _bay(rt) -> str:
    from code_gantry.nodes import bay_id

    return bay_id(rt)


def _compose_holding(rt) -> Composition | None:
    """Land what is pending, and keep landing until nothing is.

    A bay that finishes a stage while this one is holding the semaphore
    pushes its candidate, offers to land it, is refused, and goes on — and
    this one has already read the list it is landing. Nobody looks again,
    and that candidate waits for the next bay to finish a stage anywhere in
    the fleet. When the planner has run out of work, no bay finishes
    another stage, so it waits forever: measured, with one candidate pushed
    at 18:32 into a landing that started at 18:30, and four bays stopped
    within nine minutes because the key it was holding was the one they
    needed.

    So the holder asks again before it lets go. This is the landing step
    finishing its own job rather than a second place that lands, and it
    costs a read of the ledger in the ordinary case where nothing arrived.
    """
    landed, rejected, suites = [], [], 0
    while True:
        turn = _compose_once(rt, budget=rt.cfg.limits.max_compose_suites - suites)
        if turn is None:
            break
        landed.extend(turn.landed)
        rejected.extend(turn.rejected)
        suites += turn.suites
        if turn.escalation or suites >= rt.cfg.limits.max_compose_suites:
            return Composition(landed, rejected, turn.sha, suites, turn.escalation)
    if not landed and not rejected:
        return None
    return Composition(landed, rejected, _tip(rt), suites)


def _tip(rt) -> str | None:
    try:
        return rt.git.rev_parse(f"origin/{rt.cfg.project_branch}")
    except GitError:  # pragma: no cover - defensive
        return None


def _compose_once(rt, *, budget: int) -> Composition | None:
    git = rt.git
    branch = rt.cfg.project_branch
    git.fetch()
    # Read after the fetch and after the semaphore: a bay that had the
    # candidate list from before either would compose a set that has moved.
    pending = rt.views().pending_candidates()
    if not pending:
        return None

    rt.log(f"[land] composing {len(pending)} candidate(s) onto origin/{branch}")
    try:
        git.checkout(branch)
        git.reset_hard(f"origin/{branch}")
    except GitError as e:
        return Composition([], [], None, 0, _escalate(rt, f"could not take origin's {branch!r}: {e}"))

    remaining = list(pending)
    rejected: list[tuple[Candidate, str]] = []
    suites = 0

    while True:
        applied, refused = _replay(rt, remaining)
        rejected.extend(refused)
        remaining = applied
        if not remaining:
            break

        if suites >= budget:
            return Composition(
                [], rejected, None, suites,
                _escalate(rt, f"{budget} suite(s) on this composition and it is still red"),
            )
        suites += 1
        red = _suite_is_red(rt)
        if red is None:
            break

        # Which one of them it is. Everything before the guilty candidate
        # composed green on the way to finding it, so that part is proven
        # and lands; the rest go back into the pool for the next turn.
        guilty = _bisect(rt, remaining)
        suites += guilty.suites

        if guilty.index == 0:
            # The first candidate alone is red, which is what a red tip
            # looks like from here too — and the difference matters more
            # than the suite it costs to tell them apart. Rejecting a good
            # stage because the branch it landed on was already broken
            # sends a person to read the wrong diff, and then does it again
            # to the next stage, and the next.
            rt.git.reset_hard(f"origin/{branch}")
            suites += 1
            broken = _suite_is_red(rt)
            if broken is not None:
                return Composition(
                    [], [], None, suites,
                    _escalate(rt, f"origin/{branch} is red before anything was composed onto it:\n{broken}"),
                )

        culprit = remaining.pop(guilty.index)
        rejected.append((culprit, f"turned the composition red on {git.head_sha()[:12]}:\n{red}"))
        rt.log(f"[land] {culprit.stage_id} turned the composition red; removing it and composing the rest")
        git.reset_hard(f"origin/{branch}")

    if not remaining:
        git.reset_hard(f"origin/{branch}")
        _record_rejections(rt, rejected)
        return Composition([], rejected, None, suites)

    try:
        git.push(branch)
    except GitError as e:
        # Somebody moved it between the fetch and the push. Nothing is
        # recorded: the candidates are still pending and the next turn of
        # the lander composes them onto whatever is there now.
        rt.log(f"[land] origin refused the push, composing again next time: {e}")
        git.reset_hard(f"origin/{branch}")
        return Composition([], rejected, None, suites)

    sha = git.head_sha()
    _record_landings(rt, remaining, sha)
    _record_rejections(rt, rejected)
    if rt.cfg.full_test_command and suites:
        rt.ledger.record_green(sha, rt.cfg.full_test_command, run_id=rt.paths.run_id)
    _delete_branches(rt, [c.branch for c in remaining])
    rt.log(f"[land] pushed {sha[:12]} carrying {len(remaining)} stage(s) to origin/{branch}")
    return Composition(remaining, rejected, sha, suites)


@dataclass
class Guilty:
    """Which candidate turned the composition red, and what finding out cost."""

    index: int
    suites: int


def _replay(rt, candidates: list[Candidate]) -> tuple[list[Candidate], list[tuple[Candidate, str]]]:
    """Cherry-pick each candidate onto the tree as it stands, in the order
    they were finished. One that will not apply is refused rather than
    resolved: a conflict is two stages disagreeing about the same lines, and
    what to do about that is the executor's job, not a merge strategy's."""
    applied, refused = [], []
    for candidate in candidates:
        try:
            rt.git.cherry_pick(candidate.sha)
        except GitError as e:
            rt.git.cherry_pick_abort()
            rt.log(f"[land] {candidate.stage_id} will not replay onto the tip: {e}")
            refused.append((candidate, f"would not replay onto {rt.git.head_sha()[:12]}: {e}"))
            continue
        applied.append(candidate)
    return applied, refused


def _bisect(rt, candidates: list[Candidate]) -> Guilty:
    """The first candidate whose arrival turns the composition red, by
    halving rather than by replaying one at a time: the composition of all
    of them is already known red and the tip they sit on is known green, so
    what is being searched for is the boundary between the two.

    Leaves the tree holding the largest green prefix it proved, which is
    what lands once the offender is out.
    """
    branch = rt.cfg.project_branch
    low, high, suites = 0, len(candidates), 0  # low is green, high is red
    while high - low > 1:
        middle = (low + high) // 2
        rt.git.reset_hard(f"origin/{branch}")
        applied, refused = _replay(rt, candidates[:middle])
        if refused:
            # It stopped applying at all, which is an answer of its own.
            return Guilty(candidates.index(refused[0][0]), suites)
        suites += 1
        if _suite_is_red(rt) is None:
            low = middle
        else:
            high = middle
    rt.git.reset_hard(f"origin/{branch}")
    _replay(rt, candidates[:low])
    # `low` composed green and `low + 1` did not, so the one that arrives
    # between them is the answer. Never None: the search starts from a tip
    # taken to be green, and the caller is what establishes that separately
    # when the answer comes back as the very first candidate.
    return Guilty(low, suites)


def _suite_is_red(rt) -> str | None:
    """The full suite on the composed tree: None when green or flaked, else
    what failed. The same adjudication a stage's own gate gets — a flake is
    no more the composition's fault than it is a stage's."""
    command = rt.cfg.full_test_command
    if not command:
        return None
    result = rt.runner.run(command)
    if result.ok:
        return None
    verdict = adjudicate(output=result.output, command=command, cfg=rt.cfg, runner=rt.runner)
    if verdict.flaked:
        rt.log(f"[land] the composed suite flaked — {verdict.summary}")
        return None
    return f"{result.summary()}\n{result.output[-2000:]}"


def _record_landings(rt, landed: list[Candidate], sha: str) -> None:
    from code_gantry.nodes import record_landing

    for candidate in landed:
        record_landing(rt, candidate.landing, sha, run_id=candidate.run_id or rt.paths.run_id)
        rt.ledger.append(
            CANDIDATE_LANDED, stage_id=candidate.stage_id, run_id=rt.paths.run_id,
            sha=sha, branch=candidate.branch,
        )


def _record_rejections(rt, rejected: list[tuple[Candidate, str]]) -> None:
    """Record each rejection, and give back what its stage was holding.

    The keys and findings are still claimed by the run that made the
    candidate — they are held from the moment a stage is drawn until its
    work lands, and this work has not. That run has long since moved on to
    another stage, so the claim describes nobody: leaving it would mean the
    rework could be taken by no bay but the one that is not doing it, and a
    claim never takes what another run holds.
    """
    for candidate, reason in rejected:
        rt.ledger.append(
            CANDIDATE_REJECTED, stage_id=candidate.stage_id, run_id=rt.paths.run_id,
            sha=rt.git.head_sha(), branch=candidate.branch, reason=reason,
        )
        _release_references(rt, candidate)


def _release_references(rt, candidate: Candidate) -> None:
    views = rt.views()
    facts = candidate.landing or {}
    for key in facts.get("keys") or []:
        state = views.state(key)
        if state.state == "claimed":
            rt.ledger.append(
                RELEASED, key=key, run_id=state.run_id, stage_id=state.stage_id,
                reason="its candidate was rejected",
            )
    for finding_id in facts.get("held") or []:
        finding = views.findings.get(finding_id)
        if finding is not None and finding.claimed_run:
            rt.ledger.append(
                FINDING_RELEASED, finding_id=finding_id, run_id=finding.claimed_run,
                stage_id=candidate.stage_id, reason="its candidate was rejected",
            )


def _delete_branches(rt, branches: list[str]) -> None:
    """A candidate that has landed is on the project branch, so its branch is
    a second copy of something already there. Left behind they accumulate one
    per stage forever, and the next person to look at the remote cannot tell
    which of them are waiting."""
    for branch in branches:
        try:
            rt.git.delete_remote_branch(branch)
        except GitError as e:
            rt.log(f"[land] could not delete origin/{branch}: {e}")


def _escalate(rt, why: str) -> dict:
    from code_gantry.nodes import _escalate as escalate

    rt.log(f"[land] {why}")
    return escalate("remote_landing", why)
