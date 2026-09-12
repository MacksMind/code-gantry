"""The argv the daemon composes, parsed by the CLI it drives.

The daemon's own tests run against a fake `code-gantry` that accepts any
argv, so each shape is checked here through the real entry point with the
command's body replaced. These are the three shapes `daemon/lib` builds and
`daemon/test/daemon_test.exs` pins on its side; a new one belongs in both.
"""

import pytest
from click.testing import CliRunner

from code_gantry import cli, ledgercli

SHAPES = {
    "run": (cli.run, ["run", "cfg.yaml", "--run-id", "20260911-204745-bay1"]),
    "resume": (cli.resume, ["resume", "cfg.yaml", "20260911-204745-bay1"]),
}


@pytest.mark.parametrize("name", SHAPES)
def test_the_daemons_argv_parses(name, monkeypatch):
    command, argv = SHAPES[name]
    seen = {}
    monkeypatch.setattr(command, "callback", lambda **kw: seen.update(kw))
    result = CliRunner().invoke(cli.main, argv, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert str(seen["config_path"]) == "cfg.yaml"
    if "20260911-204745-bay1" in argv:
        assert seen["run_id"] == "20260911-204745-bay1"
