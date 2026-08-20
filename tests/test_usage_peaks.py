"""A total from a tool loop is not a context figure.

`CLAUDE.md` records this rule and it was written about the **planner**, which
had only totals and is the role that has actually overrun a window — rejected
at 1,103,000 tokens against a 1,000,000 ceiling with nothing recorded that
would have seen it coming. `PlannerUsage` grew `peak_prompt_tokens` then. The
reviewer, which shares neither that type nor that fix, did not.

The cost was a wrong reading rather than an outage. Eleven reviewer records on
one run carried totals between 473,595 and 2,211,906 and no peak at all, and
the log line renders the total as `(N prompt, M cached)` — which reads exactly
like a context figure. It was reported twice as the reviewer running close to
its ceiling. It was not: the totals track the *call count* almost exactly,
because a tool loop re-sends the conversation every turn, and the real contexts
were about 45-70k.
"""

import json

from code_gantry.openaiclient import TokenUsage, extract_usage, merge_usage


class Reported:
    """What a provider hands back for one turn."""

    def __init__(self, prompt, completion=0, cached=0):
        self.input_tokens = prompt
        self.output_tokens = completion
        self.input_tokens_details = type(
            "D", (), {"cached_tokens": cached}
        )()


class TestOneReadingIsItsOwnPeak:
    def test_a_single_call_peaks_at_its_prompt(self):
        got = extract_usage(Reported(50_000, completion=100, cached=10))
        assert got.prompt_tokens == 50_000
        assert got.peak_prompt_tokens == 50_000

    def test_an_absent_usage_block_peaks_at_zero(self):
        assert extract_usage(None).peak_prompt_tokens == 0


class TestMergingATooLoop:
    def test_totals_add_and_the_peak_does_not(self):
        # Three turns of a tool loop, each re-sending a growing conversation.
        turns = [extract_usage(Reported(p)) for p in (40_000, 47_000, 45_000)]
        got = turns[0]
        for t in turns[1:]:
            got = merge_usage(got, t)
        assert got.prompt_tokens == 132_000, "the bill is the sum"
        assert got.peak_prompt_tokens == 47_000, "the context is the largest turn"

    def test_the_peak_survives_a_smaller_final_turn(self):
        # The naive alternative — keeping the last turn — is wrong whenever the
        # loop's last call is a short one, which is the common shape: the model
        # stops asking and answers.
        got = merge_usage(
            extract_usage(Reported(900_000)), extract_usage(Reported(12_000))
        )
        assert got.peak_prompt_tokens == 900_000

    def test_positional_construction_still_works(self):
        # `cache_write_tokens` carries a comment saying it went last for this
        # reason. A new field in the middle silently reassigns every positional
        # caller, and this type is built positionally.
        u = TokenUsage(11, 22, 33, 44)
        assert (u.prompt_tokens, u.completion_tokens) == (11, 22)
        assert (u.cached_tokens, u.cache_write_tokens) == (33, 44)
        assert u.peak_prompt_tokens == 0


class TestTheReviewerRecordsIt:
    def test_the_usage_block_carries_every_field(self):
        # Written by walking the dataclass rather than by a list of keys. The
        # hand-written list is what dropped the peak here, and it is the same
        # defect `executor-loop.json` and `planner.json` were both fixed for:
        # wherever a subset is written out by hand, the hand is the defect.
        import dataclasses

        from code_gantry.reviewer import ReviewOutcome

        out = ReviewOutcome(verdict="approved", summary="fine")
        out.usage = TokenUsage(
            prompt_tokens=1_505_234, completion_tokens=900,
            cached_tokens=1_482_160, cache_write_tokens=23_000,
            peak_prompt_tokens=47_100,
        )
        block = json.loads(json.dumps(out.as_dict()))["usage"]
        assert set(block) == {f.name for f in dataclasses.fields(TokenUsage)}
        assert block["peak_prompt_tokens"] == 47_100
        assert block["prompt_tokens"] == 1_505_234
