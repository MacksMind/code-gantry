defmodule CodeGantryDaemonTest do
  @moduledoc """
  The daemon against a fake CLI: a script that records what it was asked to
  run and exits with the code a file names. What is pinned is the seam —
  which command a bay runs, what the status file says after each exit, and
  that a missing bay is made with mk-bay — not any run's behaviour.
  """
  use ExUnit.Case

  alias CodeGantryDaemon.{Host, Bay, Status, Sync}

  setup do
    root = Path.join(System.tmp_dir!(), "cgd-#{System.unique_integer([:positive])}")
    state = Path.join(root, "state")
    primary = Path.join(root, "repo")
    File.mkdir_p!(Path.join(primary, "bin"))
    File.mkdir_p!(state)

    # The fake `code-gantry`: logs its argv, exits with the code in EXIT_FILE.
    fake = Path.join(root, "fake-cli")
    File.write!(fake, """
    #!/usr/bin/env bash
    echo "argv: $*" >> "#{root}/calls"
    echo "line one"
    exit "$(cat "#{root}/exit" 2>/dev/null || echo 0)"
    """)
    File.chmod!(fake, 0o755)

    # The fake mk-bay: makes the directory and records the call.
    mk = Path.join([primary, "bin", "mk-bay"])
    File.write!(mk, """
    #!/usr/bin/env bash
    echo "mk-bay $*" >> "#{root}/calls"
    mkdir -p "$(dirname "#{primary}")/repo-$1"
    """)
    File.chmod!(mk, 0o755)

    host = %Host{
      origin: "test-host",
      code_gantry: root,
      primary: primary,
      config: "cfg.yaml",
      sync_seconds: 3600,
      branch: "work",
      command: [fake],
      bays: [%{name: "bay1", offset: 100, scope: ["p.001"]}]
    }

    System.put_env("CODE_GANTRY_DAEMON_STATE", state)
    on_exit(fn -> System.delete_env("CODE_GANTRY_DAEMON_STATE") end)
    {:ok, _} = Status.start_link(host)
    %{root: root, host: host, state: state}
  end

  defp calls(root), do: File.read!(Path.join(root, "calls"))

  defp wait_for(fun, tries \\ 50) do
    cond do
      fun.() -> :ok
      tries == 0 -> flunk("condition never held")
      true -> Process.sleep(100); wait_for(fun, tries - 1)
    end
  end

  defp status(state) do
    case File.read(Path.join(state, "status")) do
      {:ok, text} -> text
      _ -> ""
    end
  end

  test "a missing bay is made with mk-bay, then a run starts in it with its scope", %{root: root, host: host, state: state} do
    File.write!(Path.join(root, "exit"), "0")
    {:ok, _} = Bay.start_link({host, hd(host.bays)})
    wait_for(fn -> File.exists?(Path.join(root, "calls")) and String.contains?(calls(root), "argv:") end)
    wait_for(fn -> String.contains?(status(state), "bay1 finished") end)
    log = calls(root)
    assert log =~ "mk-bay bay1 100 work"
    assert log =~ ~r/argv: run .*repo-bay1\/cfg.yaml --run-id \d{8}-\d{6}-bay1 --scope p.001/
    assert File.read!(Path.join(state, "bay1.log")) =~ "line one"
  end

  test "a run that stops for a person is not restarted", %{root: root, host: host, state: state} do
    File.write!(Path.join(root, "exit"), "2")
    {:ok, _} = Bay.start_link({host, hd(host.bays)})
    wait_for(fn -> String.contains?(status(state), "bay1 escalated") end)
    Process.sleep(300)
    assert length(String.split(calls(root), "argv:")) - 1 == 1
  end

  test "a paused run waits", %{root: root, host: host, state: state} do
    File.write!(Path.join(root, "exit"), "3")
    {:ok, _} = Bay.start_link({host, hd(host.bays)})
    wait_for(fn -> String.contains?(status(state), "bay1 paused") end)
  end

  test "a run that dies is resumed under the same id after a backoff", %{root: root, host: host, state: state} do
    File.write!(Path.join(root, "exit"), "137")
    {:ok, pid} = Bay.start_link({host, hd(host.bays)})
    wait_for(fn -> String.contains?(status(state), "bay1 crashed") end)
    File.write!(Path.join(root, "exit"), "0")
    send(pid, :relaunch)
    wait_for(fn -> String.contains?(status(state), "bay1 finished") end)
    [first, second] = Regex.scan(~r/argv: (run|resume) \S+ --run-id (\S+)/, calls(root)) |> Enum.map(fn [_, verb, id] -> {verb, id} end)
    assert {"run", id} = first
    assert {"resume", ^id} = second
  end

  test "the sync loop runs the ledger sync and records the result", %{root: root, host: host, state: state} do
    File.write!(Path.join(root, "exit"), "0")
    {:ok, _} = Sync.start_link(host)
    wait_for(fn -> String.contains?(status(state), "sync ok") end)
    assert calls(root) =~ "argv: ledger sync #{Path.join(host.primary, "cfg.yaml")}"
  end
end
