"""The smoke stand-in must answer with fields the real schemas require.

`scripts/smoke.py` is not run by pytest — it drives the installed console
script against a synthetic repository, which is the one thing the unit tests
cannot do. That also means nothing tells it when a schema moves underneath it,
and it has drifted twice: `ReviewVerdict` gained a required `record`, and the
executor stopped being a subprocess entirely while the stand-in was still a fake
`aider` binary on PATH.

This is the cheap half of the guard — the fields, checked against the real
models, so a schema change fails in the suite rather than eight minutes into a
smoke run.
"""

import pytest


def _stub_verdicts():
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "smoke_mod", pathlib.Path("scripts/smoke.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestTheStandInSatisfiesTheRealSchema:
    def test_the_review_verdict_has_every_required_field(self):
        from orchestrator.reviewer import ReviewVerdict

        required = {
            n for n, f in ReviewVerdict.model_fields.items() if f.is_required()
        }
        src = __import__("pathlib").Path("scripts/smoke.py").read_text()
        for field in required:
            assert f'"{field}"' in src, (
                f"smoke.py's stub verdict omits the required field {field!r}"
            )

    def test_no_fake_aider_remains(self):
        # The executor is in-process. A binary on PATH stands in for nothing,
        # and having one meant the smoke test drove a path production had
        # stopped taking.
        src = __import__("pathlib").Path("scripts/smoke.py").read_text()
        assert "write_fake_aider" not in src
        assert "FAKE_AIDER" not in src
        assert "--real-aider" not in src
