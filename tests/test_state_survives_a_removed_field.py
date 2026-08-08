"""A field removed mid-run must not strand the run that persisted it.

`current` is a `Stage` dumped into the checkpoint, so it carries whatever
fields `Stage` had when the stage was derived. `Stage` is `extra="forbid"` —
right for config, where an unknown key is a typo — and rebuilding with
`Stage(**fields)` therefore raises the moment a field is deleted from the
model. Not at the next fresh run: at the next *resume*, of a run already
hours deep, with the failure landing as a pydantic error from inside a node.

Found before it happened, by checking the live checkpoint before deleting
`kind` and `command` with script stages. It held `kind: "agent"` on a run 34
stages in.

The state's own schema-filtered merge already works this way — `driver._merge`
drops keys `RunState` does not declare — so this is the same rule applied to
the one structure that rebuilds a pydantic model out of the checkpoint.
"""

import pytest


class TestRebuildingAStageIgnoresFieldsThatNoLongerExist:
    def test_a_retired_field_in_the_checkpoint_is_dropped(self):
        from types import SimpleNamespace

        from orchestrator.nodes import current_stage

        state = {
            "current": {
                "id": "s",
                "instruction": "do it",
                "edit_files": ["app/**"],
                # A field written by a build of this same run that still had
                # it. Deliberately not a real retired name: this pins the
                # mechanism, and a test naming today's removal would stop
                # testing anything the day that name is forgotten.
                "a_field_this_model_no_longer_has": "whatever",
            }
        }
        stage = current_stage(state, SimpleNamespace())
        assert stage.id == "s"
        assert stage.edit_files == ["app/**"]

    def test_a_live_field_is_still_carried(self):
        # The filter must not quietly discard something real; that would be a
        # worse failure than the one it prevents, because nothing would raise.
        from types import SimpleNamespace

        from orchestrator.nodes import current_stage

        stage = current_stage(
            {"current": {"id": "s", "instruction": "do it",
                         "excerpt_base_sha": "abc123"}},
            SimpleNamespace(),
        )
        assert stage.excerpt_base_sha == "abc123"

    def test_no_stage_is_still_no_stage(self):
        from types import SimpleNamespace

        from orchestrator.nodes import current_stage

        assert current_stage({}, SimpleNamespace()) is None
