"""The per-attempt record must carry the whole usage, not four fields of it.

`_write_loop_record`'s own docstring explains that enumerating keys is what
makes the next field go missing — it was written after ten fields of twenty
reached the artifact and the four omitted were the entire answer to "why did
this attempt end". The outer record walks `dataclasses.fields` for exactly
that reason.

The nested `usage` block did not, and the prediction came true twice.
`peak_prompt_tokens` and `provider_cost_usd` were both added to `TokenUsage`
and neither reached this file. The second one cost a wrong reading the same
afternoon: asked whether a provider had reported a cost, `usage.get(...)`
returned `None` for an absent key and that was read as "the provider reported
nothing" — an absent field answering wrongly rather than failing to answer,
which is the defect the docstring names.
"""

import dataclasses

from code_gantry.executor import ExecutionResult, _write_loop_record
from code_gantry.openaiclient import TokenUsage


def _record(tmp_path, usage):
    _write_loop_record(tmp_path, ExecutionResult(ok=True, usage=usage))
    import json

    return json.loads((tmp_path / "executor-loop.json").read_text())


class TestUsageIsWrittenWhole:
    def test_every_field_of_the_usage_type_is_present(self, tmp_path):
        rec = _record(tmp_path, TokenUsage(1, 2, 3, 4, 5, 0.5))
        expected = {f.name for f in dataclasses.fields(TokenUsage)}
        assert set(rec["usage"]) == expected

    def test_a_reported_cost_survives(self, tmp_path):
        rec = _record(tmp_path, TokenUsage(1, 2, 3, 4, 5, 0.5))
        assert rec["usage"]["provider_cost_usd"] == 0.5

    def test_an_unreported_cost_is_written_as_null_not_omitted(self, tmp_path):
        """Present-and-null and absent are different facts, and `.get()`
        cannot tell them apart — which is how this was misread."""
        rec = _record(tmp_path, TokenUsage(1, 2, 3, 4, 5))
        assert "provider_cost_usd" in rec["usage"]
        assert rec["usage"]["provider_cost_usd"] is None

    def test_no_usage_at_all_still_writes_a_record(self, tmp_path):
        rec = _record(tmp_path, None)
        assert rec["usage"] == {}
        assert rec["ok"] is True
