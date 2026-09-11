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
    root = Path.join(System.tmp_dir!(), "cgd-#{System.os_time(:microsecond)}-#{System.unique_integer([:positive])}")
    File.rm_rf!(root)
    on_exit(fn -> File.rm_rf!(root) end)
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
    while [ -f "#{root}/hold" ]; do sleep 0.1; done
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

  test "a run that fails carries its last lines into the daemon log", %{root: root, host: host, state: state} do
    File.write!(Path.join(root, "exit"), "1")
    log = ExUnit.CaptureLog.capture_log(fn ->
      {:ok, _} = Bay.start_link({host, hd(host.bays)})
      wait_for(fn -> String.contains?(status(state), "bay1 failed") end)
    end)
    assert log =~ "failed before or outside a stage (exit 1)\n  line one"
  end

  test "a run that dies is resumed under the same id after a backoff", %{root: root, host: host, state: state} do
    File.write!(Path.join(root, "exit"), "137")
    {:ok, pid} = Bay.start_link({host, hd(host.bays)})
    wait_for(fn -> String.contains?(status(state), "bay1 crashed") end)
    File.write!(Path.join(root, "exit"), "0")
    send(pid, :relaunch)
    wait_for(fn -> String.contains?(status(state), "bay1 finished") end)
    [first, second] = Regex.scan(~r/argv: (run|resume) \S+ (?:--run-id )?(\S+)/, calls(root)) |> Enum.map(fn [_, verb, id] -> {verb, id} end)
    assert {"run", id} = first
    assert {"resume", ^id} = second
  end

  test "a primary copy without mk-bay leaves the bay failed and the daemon up", %{root: root, host: host, state: state} do
    File.rm!(Path.join([host.primary, "bin", "mk-bay"]))
    {:ok, pid} = Bay.start_link({host, hd(host.bays)})
    wait_for(fn -> String.contains?(status(state), "bay1 failed") end)
    assert Process.alive?(pid)
    assert status(state) =~ "no bin/mk-bay"
    refute File.exists?(Path.join(root, "calls"))
  end

  test "a program that cannot be spawned reads as a failed command", %{root: root, host: host} do
    alias CodeGantryDaemon.Command
    {out, status} = Command.run(["bin/does-not-exist"], host.primary, [])
    assert status == 127 and out =~ "could not start bin/does-not-exist"
    File.write!(Path.join([host.primary, "bin", "bad-interp"]), "#!/nowhere/bash\necho hi\n")
    File.chmod!(Path.join([host.primary, "bin", "bad-interp"]), 0o755)
    # A missing interpreter fails inside the child on some systems and at
    # spawn on others; either way it is a failed command, not a crash.
    {_out, status} = Command.run(["bin/bad-interp"], host.primary, [])
    assert status != 0
    _ = root
  end

  test "the sync loop reads a bay's config, never the primary's, once a bay exists", %{root: root, host: host, state: state} do
    File.write!(Path.join(root, "exit"), "0")
    File.mkdir_p!(Host.bay_dir(host, hd(host.bays)))
    {:ok, _} = Sync.start_link(host)
    wait_for(fn -> String.contains?(status(state), "sync ok") end)
    assert calls(root) =~ "argv: ledger sync --config #{Path.join(Host.bay_dir(host, hd(host.bays)), "cfg.yaml")}"
    refute calls(root) =~ host.primary <> "/cfg.yaml"
  end

  test "with no bay made yet the sync falls back to the primary copy's config", %{root: root, host: host, state: state} do
    File.write!(Path.join(root, "exit"), "0")
    {:ok, _} = Sync.start_link(host)
    wait_for(fn -> String.contains?(status(state), "sync ok") end)
    assert calls(root) =~ "argv: ledger sync --config #{Path.join(host.primary, "cfg.yaml")}"
  end

  describe "retry" do
    defp launches(root) do
      Regex.scan(~r/argv: (run|resume) \S+ (?:--run-id )?(\S+)/, calls(root))
      |> Enum.map(fn [_, verb, id] -> {verb, id} end)
    end

    test "a failed run told to try again starts a new run", %{root: root, host: host, state: state} do
      File.write!(Path.join(root, "exit"), "1")
      {:ok, _} = Bay.start_link({host, hd(host.bays)})
      wait_for(fn -> String.contains?(status(state), "bay1 failed") end)
      File.write!(Path.join(root, "exit"), "0")
      assert {:ok, :run, id} = Bay.retry("bay1")
      wait_for(fn -> String.contains?(status(state), "bay1 finished #{id}") end)
      assert [{"run", first}, {"run", ^id}] = launches(root)
      assert first != id
    end

    test "an escalated run told to try again is resumed under its id", %{root: root, host: host, state: state} do
      File.write!(Path.join(root, "exit"), "2")
      {:ok, _} = Bay.start_link({host, hd(host.bays)})
      wait_for(fn -> String.contains?(status(state), "bay1 escalated") end)
      File.write!(Path.join(root, "exit"), "0")
      assert {:ok, :resume, id} = Bay.retry("bay1")
      wait_for(fn -> String.contains?(status(state), "bay1 finished") end)
      assert [{"run", ^id}, {"resume", ^id}] = launches(root)
    end

    test "a running bay refuses and names its run", %{root: root, host: host, state: state} do
      File.write!(Path.join(root, "hold"), "")
      {:ok, _} = Bay.start_link({host, hd(host.bays)})
      wait_for(fn -> String.contains?(status(state), "bay1 running") end)
      assert {:error, {:running, id}} = Bay.retry("bay1")
      assert id =~ ~r/-bay1$/
      File.rm!(Path.join(root, "hold"))
      wait_for(fn -> String.contains?(status(state), "bay1 finished") end)
      assert length(launches(root)) == 1
    end

    test "a bay the host file does not name is refused" do
      assert {:error, :no_such_bay} = Bay.retry("bay9")
    end

    test "the control line says what happened", %{root: root, host: host, state: state} do
      alias CodeGantryDaemon.Control
      File.write!(Path.join(root, "exit"), "1")
      {:ok, _} = Bay.start_link({host, hd(host.bays)})
      wait_for(fn -> String.contains?(status(state), "bay1 failed") end)
      assert Control.retry("bay1") =~ ~r/^bay1: run \d{8}-\d{6}-bay1 started$/
      assert Control.retry("bay9") == "no bay named bay9 in the host file"
    end
  end
end
