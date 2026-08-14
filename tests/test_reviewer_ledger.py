"""What the reviewer looked at, recorded from the ledger rather than the request.

`looked_at` was built at the call site, from the arguments, before dispatch ran
— so `review.json` recorded that a call was *made* and nothing about what came
back. Two things were invisible as a result, and the second is why this
matters:

- how much a read returned, which the planner's artifact has carried since the
  ledger began; and
- **whether the call was refused at all.** A reviewer cut off by its read
  budget produced an artifact identical to one that stopped because it was
  satisfied. Asked how often the 25-call cap was binding across 331 reviews,
  the honest answer was that the artifact could not say — four reviews reached
  25 answered calls and whether any hit the ceiling was unknowable.

That is the same defect the `ToolCall.refusal` field was added for one layer
down, and its docstring already says why: "a step that stopped because it had
finished and a step that stopped because it had been cut off produced the same
artifact. A cap whose binding cannot be observed cannot be tuned."

Rendered by `_render_call`, the same function the planner's log and artifact
use, because two renderings of one ledger is how the two drift — and they had.
"""

import pytest

from code_gantry.repotools import ToolCall


class Reader:
    def __init__(self):
        self.calls = []


def _reviewer(log=None):
    from code_gantry.config import ReviewerConfig
    from code_gantry.reviewer import OpenAIReviewer

    r = OpenAIReviewer.__new__(OpenAIReviewer)
    r.cfg = ReviewerConfig(model="gpt-5.6-sol")
    r.log = log
    r.reader = Reader()
    r.semantic = None
    r._client = None
    return r


class TestTheArtifactCarriesTheOutcome:
    def test_a_read_records_what_came_back(self):
        from code_gantry.reviewer import OpenAIReviewer

        r = _reviewer()
        r.reader.calls.append(ToolCall(tool="read_file", detail="a.rb:1-40", lines=40))
        assert OpenAIReviewer._looked_at(r) == ["read_file(a.rb:1-40) -> 40 line(s)"]

    def test_a_refusal_is_visible_as_one(self):
        # The whole point. Without this a reviewer that ran out of budget and
        # one that was satisfied leave the same record.
        from code_gantry.reviewer import OpenAIReviewer

        r = _reviewer()
        r.reader.calls.append(
            ToolCall(
                tool="read_file", detail="b.rb", lines=0,
                refusal="too many tool calls in one step (limit 25)",
            )
        )
        assert OpenAIReviewer._looked_at(r) == [
            "read_file(b.rb) -> refused: too many tool calls in one step (limit 25)"
        ]

    def test_a_reviewer_with_no_reader_records_nothing(self):
        from code_gantry.reviewer import OpenAIReviewer

        r = _reviewer()
        r.reader = None
        assert OpenAIReviewer._looked_at(r) == []


class TestOneRendererForBothRoles:
    """The planner and the reviewer describe the same ledger the same way."""

    def test_the_two_roles_render_a_call_identically(self):
        from code_gantry.planner import AnthropicPlanner, _render_call
        from code_gantry.reviewer import OpenAIReviewer

        call = ToolCall(tool="search", detail="X in .", lines=7)

        r = _reviewer()
        r.reader.calls.append(call)

        p = AnthropicPlanner.__new__(AnthropicPlanner)
        p.reader = Reader()
        p.reader.calls.append(call)

        assert OpenAIReviewer._looked_at(r) == AnthropicPlanner._tool_log(p)
        assert OpenAIReviewer._looked_at(r) == [_render_call(call)]


class TestTheReviewerReportsAsItReads:
    """The third role to get a live line, and the last blind one.

    Short beside the planner's — reviews took 29 to 66 seconds on the run this
    was written during, against derivations of 3 to 20 minutes — but the same
    argument and now nearly free, since `_looked_at` already renders the
    ledger. A gate that reads seven files before deciding is a different
    artefact from one that reads none, and until the verdict returns there was
    no way to tell which was happening.
    """

    def test_only_the_new_calls_are_reported(self):
        from code_gantry.reviewer import OpenAIReviewer

        lines = []
        r = _reviewer(log=lines.append)
        r.reader.calls.append(ToolCall(tool="search", detail="X", lines=2))
        seen = OpenAIReviewer._log_new_calls(r, 0)
        assert lines == ["[review] search(X) -> 2 line(s)"]

        r.reader.calls.append(ToolCall(tool="read_file", detail="a.rb:1-9", lines=9))
        seen = OpenAIReviewer._log_new_calls(r, seen)
        assert len(lines) == 2 and seen == 2

    def test_it_is_the_same_text_as_the_artifact(self):
        from code_gantry.reviewer import OpenAIReviewer

        lines = []
        r = _reviewer(log=lines.append)
        r.reader.calls.append(ToolCall(tool="read_file", detail="a.rb", lines=0, refusal="no"))
        OpenAIReviewer._log_new_calls(r, 0)
        assert [x.removeprefix("[review] ") for x in lines] == OpenAIReviewer._looked_at(r)

    def test_no_log_is_not_an_error(self):
        from code_gantry.reviewer import OpenAIReviewer

        r = _reviewer(log=None)
        r.reader.calls.append(ToolCall(tool="search", detail="X", lines=1))
        assert OpenAIReviewer._log_new_calls(r, 0) == 1


class TestTheCountsComeFromTheLedger:
    """Counted from `call.tool`, never from the rendered line.

    The run log prints counts because a large review's rendered calls are
    thousands of characters on one line. Deriving those counts by taking the
    name off the front of `_render_call`'s output would pass today and is the
    move this codebase has been burned by twice: a value read out of rendered
    text stops being derivable the moment the rendering changes, and nothing
    fails when it does. `tool_calls` remains the record and still goes to
    `review.json` in full.
    """

    def test_calls_are_counted_by_tool(self):
        from code_gantry.reviewer import OpenAIReviewer

        r = _reviewer()
        for i in range(3):
            r.reader.calls.append(
                ToolCall(tool="read_file", detail=f"a{i}.rb:1-40", lines=40)
            )
        r.reader.calls.append(ToolCall(tool="search", detail="x in app", lines=2))
        assert OpenAIReviewer._tool_counts(r) == {"read_file": 3, "search": 1}

    def test_a_reviewer_that_read_nothing_counts_nothing(self):
        from code_gantry.reviewer import OpenAIReviewer

        assert OpenAIReviewer._tool_counts(_reviewer()) == {}

    def test_a_refused_call_still_counts_as_a_call(self):
        # It is a thing the reviewer asked for, and a review that spent its
        # budget being refused must not look like one that read nothing.
        from code_gantry.reviewer import OpenAIReviewer

        r = _reviewer()
        r.reader.calls.append(
            ToolCall(tool="read_file", detail="gone.rb", lines=0, refusal="no such path")
        )
        assert OpenAIReviewer._tool_counts(r) == {"read_file": 1}

    def test_the_counts_and_the_record_agree_on_the_total(self):
        # Two derivations of one ledger is how they drift, which is the reason
        # `_render_call` is shared rather than restated.
        from code_gantry.reviewer import OpenAIReviewer

        r = _reviewer()
        for i in range(5):
            r.reader.calls.append(ToolCall(tool="search", detail=f"p{i}", lines=1))
        counts = OpenAIReviewer._tool_counts(r)
        assert sum(counts.values()) == len(OpenAIReviewer._looked_at(r))
