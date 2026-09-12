defmodule CodeGantryDaemonTest do
  @moduledoc """
  The daemon against a fake CLI: a script that records what it was asked to
  run and exits with the code a file names. What is pinned is the seam —
  which command a bay runs, what the status file says after each exit, and
  that a missing bay is made with mk-bay — not any run's behaviour.
  """
  use ExUnit.Case

  alias CodeGantryDaemon.{Host, Bay, Status}

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
      branch: "work",
      command: [fake],
      bays: [%{name: "bay1", offset: 100, scope: ["p.001"]}]
    }

    System.put_env("CODE_GANTRY_DAEMON_STATE", state)
    on_exit(fn -> System.delete_env("CODE_GANTRY_DAEMON_STATE") end)
    {:ok, _} = Status.start_link(host)
    start_supervised!({Registry, keys: :unique, name: CodeGantryDaemon.Registry})
    start_supervised!({DynamicSupervisor, name: CodeGantryDaemon.Bays, strategy: :one_for_one})
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

  test "a bay's name is local to its node, never global", %{root: root, host: host, state: state} do
    # Every host has a bay1. `:global` is cluster-wide and resolves a
    # duplicate by killing one registrant, so the first Node.connect between
    # two daemons would take a bay down on one of them.
    File.write!(Path.join(root, "hold"), "")
    {:ok, pid} = Bay.start_link({host, hd(host.bays)})
    wait_for(fn -> String.contains?(status(state), "bay1 running") end)
    assert GenServer.whereis(Bay.via("bay1")) == pid
    assert :global.whereis_name({Bay, "bay1"}) == :undefined
    assert :global.registered_names() == []
    File.rm!(Path.join(root, "hold"))
  end

  describe "place" do
    alias CodeGantryDaemon.{Control, Placements}

    test "placing a bay makes its checkout, starts a run, and is remembered", %{root: root, host: host, state: state} do
      File.write!(Path.join(root, "exit"), "0")
      line = Control.place("bay3", 300, ["p.002"])
      assert line =~ ~r/^bay3: placed at offset 300/
      wait_for(fn -> String.contains?(status(state), "bay3 finished") end)
      assert calls(root) =~ "mk-bay bay3 300 work"
      assert calls(root) =~ ~r/argv: run .*repo-bay3\/cfg.yaml --run-id \d{8}-\d{6}-bay3 --scope p.002/
      # The placement outlives this daemon: the next start reads it back.
      assert Enum.map(Placements.load(), & &1.name) == ["bay3"]
      assert Enum.map(Placements.all(host), & &1.name) == ["bay1", "bay3"]
    end

    test "a name already placed, or in the host file, is refused", %{root: root, host: host} do
      File.write!(Path.join(root, "hold"), "")
      assert Control.place("bay3", 300) =~ ~r/^bay3: placed/
      assert Control.place("bay3", 301) == "bay3 is already placed"
      assert Control.place("bay1", 100) == "bay1 is already placed"
      assert Enum.map(Placements.load(), & &1.offset) == [300]
      File.rm!(Path.join(root, "hold"))
      _ = host
    end

    test "a placement from an earlier start is a bay again", %{root: root, host: host, state: state} do
      File.write!(Path.join(root, "exit"), "0")
      :ok = Placements.add(%{name: "bay4", offset: 400, scope: []})
      CodeGantryDaemon.Application.start_bays(host)
      wait_for(fn -> String.contains?(status(state), "bay4 finished") end)
      assert calls(root) =~ "mk-bay bay4 400 work"
    end

    test "with no mk-bay in the primary, an existing bay of the repo makes the new one", %{root: root, host: host, state: state} do
      # The primary is a person's checkout, on whatever branch they need; a
      # bay is always on the project branch and carries the script.
      File.write!(Path.join(root, "exit"), "0")
      File.rm!(Path.join([host.primary, "bin", "mk-bay"]))
      existing = Path.join(Path.dirname(host.primary), "repo-bay1")
      File.mkdir_p!(Path.join(existing, "bin"))
      File.write!(Path.join([existing, "bin", "mk-bay"]), """
      #!/usr/bin/env bash
      echo "mk-bay-from-bay1 $* project=$MK_BAY_PROJECT" >> "#{root}/calls"
      mkdir -p "$(dirname "#{host.primary}")/${MK_BAY_PROJECT}-$1"
      """)
      File.chmod!(Path.join([existing, "bin", "mk-bay"]), 0o755)
      assert Control.place("bay5", 500) =~ ~r/^bay5: placed/
      wait_for(fn -> String.contains?(status(state), "bay5 finished") end)
      assert calls(root) =~ "mk-bay-from-bay1 bay5 500 work project=repo"
      assert File.dir?(Path.join(Path.dirname(host.primary), "repo-bay5"))
    end
  end

  describe "scope" do
    alias CodeGantryDaemon.{Control, Placements}

    test "a finished bay given a new scope starts a run under it", %{root: root, host: host, state: state} do
      File.write!(Path.join(root, "exit"), "0")
      {:ok, _} = Bay.start_link({host, hd(host.bays)})
      wait_for(fn -> String.contains?(status(state), "bay1 finished") end)
      assert Control.scope("bay1", ["p.003", "p.004"]) =~ ~r/^bay1: scope p.003 p.004; run \d{8}-\d{6}-bay1 started$/
      wait_for(fn -> length(Regex.scan(~r/argv: run/, calls(root))) == 2 end)
      assert calls(root) =~ ~r/argv: run \S+ --run-id \S+ --scope p.003 --scope p.004/
      # Remembered past this daemon, overriding what the host file said.
      assert [%{name: "bay1", offset: 100, scope: ["p.003", "p.004"]}] = Placements.load()
      assert Enum.map(Placements.all(host), &{&1.name, &1.scope}) == [{"bay1", ["p.003", "p.004"]}]
    end

    test "no keys means the whole plan", %{root: root, host: host, state: state} do
      File.write!(Path.join(root, "exit"), "0")
      {:ok, _} = Bay.start_link({host, hd(host.bays)})
      wait_for(fn -> String.contains?(status(state), "bay1 finished") end)
      assert Control.scope("bay1", []) =~ ~r/^bay1: the whole plan; run \S+ started$/
      wait_for(fn -> length(Regex.scan(~r/argv: run/, calls(root))) == 2 end)
      [_, second] = Regex.scan(~r/argv: run [^\n]*/, calls(root))
      refute hd(second) =~ "--scope"
    end

    test "a running bay keeps its scope until its run ends", %{root: root, host: host, state: state} do
      File.write!(Path.join(root, "hold"), "")
      {:ok, _} = Bay.start_link({host, hd(host.bays)})
      wait_for(fn -> String.contains?(status(state), "bay1 running") end)
      assert Control.scope("bay1", ["p.009"]) =~ ~r/^bay1: scope p.009 from its next run; \S+ is still running$/
      assert [%{scope: ["p.009"]}] = Placements.load()
      File.rm!(Path.join(root, "hold"))
      wait_for(fn -> String.contains?(status(state), "bay1 finished") end)
      assert length(Regex.scan(~r/argv: run/, calls(root))) == 1
    end

    test "an unknown bay is refused" do
      assert Control.scope("bay9", []) == "no bay named bay9 on this host"
    end
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
