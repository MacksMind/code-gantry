defmodule CodeGantryDaemonTest do
  @moduledoc """
  The daemon against a fake CLI: a script that records what it was asked to
  run and exits with the code a file names. What is pinned is the seam —
  which command a bay runs, what the status file says after each exit, and
  that a missing bay is made with mk-bay — not any run's behaviour.
  """
  use ExUnit.Case

  alias CodeGantryDaemon.{Application, Control, Host, Bay, Mesh, Records, Runs, Semaphore, Status}

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
    case "$1" in run|resume) while [ -f "#{root}/hold" ]; do sleep 0.1; done ;; esac
    case "$1" in pause) exit 0 ;; esac
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
      code_branch: "work",
      pickup_seconds: 0,
      command: [fake],
      bays: [%{name: "bay1", offset: 100}]
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

  defp connect(path) do
    {:ok, socket} = :gen_tcp.connect({:local, path}, 0, [:binary, packet: :line, active: false])
    socket
  end

  defp acquire(path, name, label) do
    socket = connect(path)
    :ok = :gen_tcp.send(socket, "acquire #{name} #{label}\n")
    socket
  end

  defp line(socket, timeout \\ 2_000) do
    case :gen_tcp.recv(socket, 0, timeout) do
      {:ok, data} -> String.trim(data)
      other -> other
    end
  end

  defp status(state) do
    case File.read(Path.join(state, "status")) do
      {:ok, text} -> text
      _ -> ""
    end
  end

  test "a missing bay is made with mk-bay, then a run starts in it", %{root: root, host: host, state: state} do
    File.write!(Path.join(root, "exit"), "0")
    {:ok, _} = Bay.start_link({host, hd(host.bays)})
    wait_for(fn -> File.exists?(Path.join(root, "calls")) and String.contains?(calls(root), "argv:") end)
    wait_for(fn -> String.contains?(status(state), "bay1 finished") end)
    log = calls(root)
    assert log =~ "mk-bay bay1 100 work"
    assert log =~ ~r/argv: run .*repo-bay1\/cfg.yaml --run-id \d{8}-\d{6}-bay1$/m
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
      line = Control.place("bay3", 300)
      assert line =~ ~r/^bay3: placed at offset 300/
      wait_for(fn -> String.contains?(status(state), "bay3 finished") end)
      assert calls(root) =~ "mk-bay bay3 300 work"
      assert calls(root) =~ ~r/argv: run .*repo-bay3\/cfg.yaml --run-id \d{8}-\d{6}-bay3$/m
      # The placement outlives this daemon: the next start reads it back.
      assert Enum.map(Placements.load(), & &1.name) == ["bay3"]
      assert Enum.map(Placements.all(host), & &1.name) == ["bay1", "bay3"]
    end

    test "a bay with a run live keeps its placement", %{root: root, host: host} do
      File.write!(Path.join(root, "hold"), "")
      assert Control.place("bay3", 300) =~ ~r/^bay3: placed/
      wait_for(fn -> "bay3" in Bay.running(host) end)
      assert Control.place("bay3", 301) =~ ~r/^bay3 is running \d{8}-\d{6}-bay3; pause or stop it first$/
      assert Enum.map(Placements.load(), & &1.offset) == [300]
      File.rm!(Path.join(root, "hold"))
    end

    test "a placement from an earlier start is a bay again", %{root: root, host: host, state: state} do
      File.write!(Path.join(root, "exit"), "0")
      :ok = Placements.put(%{name: "bay4", offset: 400})
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

  describe "a placement's project" do
    alias CodeGantryDaemon.{Control, Placements}

    test "a bay placed with a config works that project", %{root: root, state: state} do
      File.write!(Path.join(root, "exit"), "0")
      line = Control.place("bay6", 600, "docs/rails_6/code_gantry.yaml")
      assert line =~ ~r/^bay6: placed at offset 600 on docs\/rails_6\/code_gantry.yaml/
      wait_for(fn -> String.contains?(status(state), "bay6 finished") end)
      assert calls(root) =~ ~r/argv: run \S+repo-bay6\/docs\/rails_6\/code_gantry.yaml --run-id/
      assert [%{name: "bay6", offset: 600, config: "docs/rails_6/code_gantry.yaml"}] = Placements.load()
      assert status(state) =~ ~r/^bay6 finished \S+ since \S+ rails_6$/m
    end

    test "a bay that is not running is placed again on another project", %{root: root, state: state} do
      File.write!(Path.join(root, "exit"), "0")
      assert Control.place("bay7", 700, "docs/a/code_gantry.yaml") =~ ~r/^bay7: placed/
      wait_for(fn -> String.contains?(status(state), "bay7 finished") end)
      assert Control.place("bay7", 700, "docs/b/code_gantry.yaml") =~ ~r/^bay7: placed at offset 700 on docs\/b\/code_gantry.yaml/
      wait_for(fn -> calls(root) =~ ~r/argv: run \S+repo-bay7\/docs\/b\/code_gantry.yaml/ end)
      # One placement per name, the latest, and the next start reads that one.
      assert [%{name: "bay7", offset: 700, config: "docs/b/code_gantry.yaml"}] = Placements.load()
      assert status(state) =~ ~r/^bay7 finished \S+ since \S+ b$/m
    end

    test "a host-file bay is placed on a project without editing the host file", %{root: root, host: host, state: state} do
      File.write!(Path.join(root, "exit"), "0")
      assert Control.place("bay1", 100, "docs/b/code_gantry.yaml") =~ ~r/^bay1: placed at offset 100 on docs\/b\/code_gantry.yaml/
      wait_for(fn -> String.contains?(status(state), "bay1 finished") end)
      assert calls(root) =~ ~r/argv: run \S+repo-bay1\/docs\/b\/code_gantry.yaml/
      # The host file still seeds the bay; the placement says what it works.
      assert [%{name: "bay1", offset: 100, config: "docs/b/code_gantry.yaml"}] = Placements.all(host)
    end
  end

  describe "version" do
    test "answers the commit the daemon's checkout is at and this node", %{root: root, host: host} do
      {cg, _other} = code_repo(root)
      host = with_code(host, cg)
      line = CodeGantryDaemon.Control.version(host)
      assert line =~ ~r/^code-gantry [0-9a-f]{12} on \S+ \(#{host.origin}\)$/
      File.write!(Path.join([cg, "src", "x.py"]), "x = 3\n")
      assert CodeGantryDaemon.Control.version(host) =~ ~r/\+dirty on/
    end
  end

  describe "pickup" do
    alias CodeGantryDaemon.{Control, Pickup}

    # A code-gantry checkout with a bare origin: `daemon/` is a tiny Mix
    # project whose one module reports a version, `src/` stands in for the
    # Python side. `other` is another host's clone, which pushes changes.
    defp code_repo(root) do
      bare = Path.join(root, "code-gantry.git")
      cg = Path.join(root, "code-gantry")
      other = Path.join(root, "other")
      sh!(root, ["git", "init", "-q", "--bare", bare])
      File.mkdir_p!(Path.join([cg, "daemon", "lib"]))
      File.mkdir_p!(Path.join(cg, "src"))
      File.write!(Path.join([cg, "daemon", "mix.exs"]), """
      defmodule PickupProbe.MixProject do
        use Mix.Project
        def project, do: [app: :pickup_probe, version: "0.1.0", elixir: "~> 1.14", deps: []]
      end
      """)
      write_probe(cg, 1)
      File.write!(Path.join([cg, "src", "x.py"]), "x = 1\n")
      File.write!(Path.join(cg, ".gitignore"), "daemon/_build/\n")
      sh!(cg, ["git", "init", "-q", "-b", "work"])
      sh!(cg, ["git", "config", "user.email", "t@example.com"])
      sh!(cg, ["git", "config", "user.name", "T"])
      sh!(cg, ["git", "config", "commit.gpgsign", "false"])
      sh!(cg, ["git", "add", "-A"])
      sh!(cg, ["git", "commit", "-qm", "one"])
      sh!(cg, ["git", "remote", "add", "origin", bare])
      sh!(cg, ["git", "push", "-q", "origin", "work"])
      sh!(root, ["git", "clone", "-q", "-b", "work", bare, other])
      sh!(other, ["git", "config", "user.email", "o@example.com"])
      sh!(other, ["git", "config", "user.name", "O"])
      sh!(other, ["git", "config", "commit.gpgsign", "false"])
      {cg, other}
    end

    defp write_probe(dir, version, body \\ nil) do
      File.write!(
        Path.join([dir, "daemon", "lib", "probe.ex"]),
        body || "defmodule PickupProbe do\n  def version, do: #{version}\nend\n"
      )
    end

    defp other_pushes(other, fun) do
      fun.()
      sh!(other, ["git", "add", "-A"])
      sh!(other, ["git", "commit", "-qm", "from elsewhere"])
      sh!(other, ["git", "push", "-q", "origin", "work"])
    end

    defp sh!(cwd, [cmd | args]) do
      {out, 0} = System.cmd(cmd, args, cd: cwd, stderr_to_stdout: true)
      out
    end

    defp with_code(host, cg), do: %{host | code_gantry: cg, code_branch: "work"}

    test "a pushed change under daemon/ is fetched, compiled and loaded into this VM", %{root: root, host: host, state: state} do
      {cg, other} = code_repo(root)
      host = with_code(host, cg)
      assert Pickup.tick(host) =~ ~r/^code: at [0-9a-f]{12}$/
      other_pushes(other, fn -> write_probe(other, 2) end)
      line = Pickup.tick(host)
      assert line =~ ~r/^code: [0-9a-f]{12} -> [0-9a-f]{12}; daemon: 1 module\(s\) loaded$/
      assert PickupProbe.version() == 2
      assert status(state) =~ ~r/code ok [0-9a-f]{12}/
      assert sh!(cg, ["git", "rev-parse", "HEAD"]) == sh!(other, ["git", "rev-parse", "HEAD"])
    end

    test "a pushed change to the daemon's lock file fetches its dependencies before compiling", %{root: root, host: host} do
      {cg, other} = code_repo(root)
      host = with_code(host, cg)
      assert Pickup.tick(host) =~ ~r/^code: at [0-9a-f]{12}$/
      other_pushes(other, fn -> File.write!(Path.join([other, "daemon", "mix.lock"]), "%{}\n") end)
      assert Pickup.tick(host) =~ ~r/; daemon: deps fetched; daemon: 1 module\(s\) loaded$/
      # And not when the lock did not move: fetching is a network call.
      other_pushes(other, fn -> write_probe(other, 3) end)
      assert Pickup.tick(host) =~ ~r/; daemon: 1 module\(s\) loaded$/
    end

    test "a load carries the daemon's own modules and only puts its dependencies on the path", %{root: root, host: host} do
      # A dependency's processes run inside its modules; purging and
      # reloading them on every pickup would kill what they supervise.
      # Its modules are found on the path when first called.
      {cg, other} = code_repo(root)
      host = with_code(host, cg)
      ebin = Path.join([cg, "daemon", "_build", "prod", "lib", "extra_dep", "ebin"])
      File.mkdir_p!(ebin)
      [{ExtraDep, beam}] = Code.compile_string("defmodule ExtraDep do\n  def here, do: :yes\nend\n")
      File.write!(Path.join(ebin, "Elixir.ExtraDep.beam"), beam)
      File.write!(Path.join(ebin, "extra_dep.app"), "{application, extra_dep, [{modules, ['Elixir.ExtraDep']}]}.\n")
      :code.purge(ExtraDep)
      :code.delete(ExtraDep)
      other_pushes(other, fn -> write_probe(other, 2) end)
      assert Pickup.tick(host) =~ ~r/daemon: 1 module\(s\) loaded$/
      refute :erlang.module_loaded(ExtraDep)
      assert ExtraDep.here() == :yes
    end

    test "the pickup starts a child the running tree lacks, whether or not code moved", %{root: root, host: host} do
      # The deficiency this closes: a version that declares a new child
      # carried its code on a reload and left the feature dormant until
      # somebody restarted the host, which costs every run on it. Asked on
      # every look rather than only on a load, because a change to the
      # pickup itself takes effect a pickup later — the load that carries
      # this code cannot be the load that acts on it.
      {cg, _other} = code_repo(root)
      host = %{with_code(host, cg) | origin: "s#{System.unique_integer([:positive])}"}
      on_exit(fn -> :mnesia.delete_table(Semaphore.table_for(host.origin)) end)

      start_supervised!(%{
        id: :tree,
        start: {Supervisor, :start_link, [[], [strategy: :one_for_one, name: CodeGantryDaemon.Supervisor]]},
        type: :supervisor
      })

      refute Process.whereis(Semaphore.Socket)
      line = Pickup.tick(host)
      assert line =~ "started"
      assert line =~ "CodeGantryDaemon.Semaphore.Socket"
      assert Process.whereis(Semaphore.Socket), "the pickup looked and left the child missing"

      refute Pickup.tick(host) =~ "started", "a tree that is already right is left alone"

      # A child this version no longer declares — renamed, or gone — is
      # taken down, or it runs on beside its replacement until a restart.
      {:ok, _} = Supervisor.start_child(CodeGantryDaemon.Supervisor, %{id: :stray, start: {Agent, :start_link, [fn -> :old end]}})
      assert Pickup.tick(host) =~ "stopped :stray"
      refute Enum.any?(Supervisor.which_children(CodeGantryDaemon.Supervisor), fn {id, _, _, _} -> id == :stray end)
    end

    test "a commit made in this checkout is picked up, with origin never ahead", %{root: root, host: host, state: state} do
      # The host where the code is written. Its commits are already in the
      # checkout and origin is never ahead of it, so a pickup that asked
      # only "is origin ahead" found nothing to do and left this host's
      # bays running code from before the change — measured, for hours,
      # while every other host had moved on.
      {cg, _other} = code_repo(root)
      host = with_code(host, cg)
      assert Pickup.tick(host) =~ ~r/^code: at [0-9a-f]{12}/

      File.write!(Path.join([cg, "src", "thing.py"]), "changed here\n")
      sh!(cg, ["git", "add", "-A"])
      sh!(cg, ["git", "commit", "-qm", "written on this host"])
      sh!(cg, ["git", "push", "-q", "origin", "work"])

      File.write!(Path.join(root, "hold"), "")
      {:ok, _} = CodeGantryDaemon.Application.start_bay(host, %{name: "bay1", offset: 100})
      wait_for(fn -> status(state) =~ "bay1 running" end)

      line = Pickup.tick(host)
      assert line =~ "python: 1 bay(s) pausing", line
      assert status(state) =~ ~r/code ok [0-9a-f]{12}/
    end

    test "a checkout that has not moved since the last look does nothing", %{root: root, host: host} do
      {cg, _other} = code_repo(root)
      host = with_code(host, cg)
      Pickup.tick(host)
      line = Pickup.tick(host)
      refute line =~ "loaded"
      refute line =~ "pause"
    end

    test "a change under src/ pauses each running bay and resumes it on the new code", %{root: root, host: host, state: state} do
      {cg, other} = code_repo(root)
      host = with_code(host, cg)
      Control.reload(host)
      File.write!(Path.join(root, "hold"), "")
      {:ok, _} = Bay.start_link({host, hd(host.bays)})
      wait_for(fn -> String.contains?(status(state), "bay1 running") end)
      other_pushes(other, fn -> File.write!(Path.join([other, "src", "x.py"]), "x = 2\n") end)
      assert Pickup.tick(host) =~ ~r/python: 1 bay\(s\) pausing/
      assert calls(root) =~ ~r/argv: pause \S+cfg.yaml \d{8}-\d{6}-bay1/
      # The run reads the flag at its next seam and exits 3; the bay resumes it.
      File.write!(Path.join(root, "exit"), "3")
      File.rm!(Path.join(root, "hold"))
      wait_for(fn -> calls(root) =~ ~r/argv: resume/ end)
      [[_, id]] = Regex.scan(~r/argv: run \S+ --run-id (\S+)/, calls(root))
      assert calls(root) =~ "argv: resume #{Path.join(Host.bay_dir(host, hd(host.bays)), "cfg.yaml")} #{id}"
    end

    test "a checkout with local changes is left alone and said so", %{root: root, host: host, state: state} do
      {cg, other} = code_repo(root)
      host = with_code(host, cg)
      Control.reload(host)
      File.write!(Path.join([cg, "src", "x.py"]), "x = 'mine'\n")
      other_pushes(other, fn -> write_probe(other, 3) end)
      assert Pickup.tick(host) =~ ~r/^code: held at [0-9a-f]{12}: local changes/
      assert PickupProbe.version() == 1
      assert status(state) =~ "code held"
    end

    test "a change that does not compile keeps the running code and says so", %{root: root, host: host, state: state} do
      {cg, other} = code_repo(root)
      host = with_code(host, cg)
      Control.reload(host)
      other_pushes(other, fn -> write_probe(other, 0, "defmodule PickupProbe do\n  def version, do:\nend\n") end)
      assert Pickup.tick(host) =~ ~r/daemon: compile failed/
      assert PickupProbe.version() == 1
      assert status(state) =~ "code failed"
    end

    test "reload compiles and loads the local checkout without fetching", %{root: root, host: host} do
      {cg, _other} = code_repo(root)
      host = with_code(host, cg)
      Pickup.tick(host)
      write_probe(cg, 7)
      assert Control.reload(host) =~ ~r/^code: local [0-9a-f]{12}\+dirty; daemon: 1 module\(s\) loaded$/
      assert PickupProbe.version() == 7
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

  describe "the host file names this node and its peers" do
    defp write_host(root, extra) do
      file = Path.join(root, "host.exs")
      File.write!(file, """
      [
        origin: "test-host",
        code_gantry: "#{root}",
        primary: "#{root}",
        config: "cfg.yaml",
        pickup_seconds: 0,
        #{extra}
        bays: []
      ]
      """)
      file
    end

    test "address and peers are read", %{root: root} do
      host = Host.load!(write_host(root, ~s|address: "10.0.0.1", peers: ["10.0.0.2", "10.0.0.3"],|))
      assert host.address == "10.0.0.1"
      assert host.peers == ["10.0.0.2", "10.0.0.3"]
    end

    test "a host file naming neither still loads", %{root: root} do
      host = Host.load!(write_host(root, ""))
      assert host.address != nil, "an unnamed address falls back to something dialable"
      assert host.peers == []
    end

    test "the node name is long, so it can be reached from another host" do
      host = %Host{origin: "o", address: "10.0.0.1"}
      assert Host.node_name(host) == :"code_gantry_daemon@10.0.0.1"
    end

    test "only a node named as a daemon is another daemon" do
      assert Host.daemon_node?(:"code_gantry_daemon@10.0.0.2")
      assert Host.daemon_node?(:"code_gantry_daemon@spark.example.ts.net")
      # Every verb `bin/daemon` speaks starts one of these and drops it a
      # moment later. Treating one as a host arriving merges Mnesia
      # schemas with something about to vanish and sets off a pickup on
      # every command a person types.
      refute Host.daemon_node?(:"ctl-4821@10.0.0.1")
      refute Host.daemon_node?(:"probe-7@10.0.0.1")
      refute Host.daemon_node?(:nonode@nohost)
    end

    test "peer nodes are named the same way and never include this node" do
      host = %Host{origin: "o", address: "10.0.0.1", peers: ["10.0.0.2", "10.0.0.1"]}
      assert Host.peer_nodes(host) == [:"code_gantry_daemon@10.0.0.2"]
    end
  end


  describe "the mesh" do
    test "a control node is not listed as a peer" do
      # Every verb `bin/daemon` speaks connects a node of its own, and
      # listing those makes a mesh of one host look like a mesh of several.
      refute Enum.any?(Mesh.peers(), &(not Host.daemon_node?(&1)))
    end

    test "a nudge with nobody connected tells nobody rather than raising" do
      assert Mesh.nudge() == 0
    end

    test "a control node connecting is not a host arriving", %{host: host} do
      import ExUnit.CaptureLog

      {:ok, pid} = Mesh.start_link(%{host | address: "203.0.113.1", peers: []})

      log = capture_log(fn ->
        send(pid, {:nodeup, :"ctl-9182@203.0.113.4"})
        send(pid, {:nodedown, :"ctl-9182@203.0.113.4"})
        _ = :sys.get_state(pid)
      end)

      refute log =~ "joined", "a throwaway node was taken for a peer"
      refute log =~ "left"
      assert Process.alive?(pid)
      GenServer.stop(pid)
    end

    test "a daemon connecting is", %{host: host} do
      import ExUnit.CaptureLog

      {:ok, pid} = Mesh.start_link(%{host | address: "203.0.113.1", peers: []})

      log = capture_log(fn ->
        send(pid, {:nodeup, :"code_gantry_daemon@203.0.113.9"})
        _ = :sys.get_state(pid)
      end)

      assert log =~ "joined"
      GenServer.stop(pid)
    end

    test "it survives a peer it cannot reach", %{host: host} do
      # The laptop is asleep, or the link is down. A daemon that cannot
      # reach its peer is a daemon that works alone, never one that stops.
      host = %{host | address: "203.0.113.1", peers: ["203.0.113.9"]}
      {:ok, pid} = Mesh.start_link(host)
      Process.sleep(100)
      assert Process.alive?(pid)
      GenServer.stop(pid)
    end
  end



  describe "the planner semaphore" do
    setup do
      origin = "s#{System.unique_integer([:positive])}"
      :ok = Semaphore.start(origin)
      on_exit(fn -> :mnesia.delete_table(Semaphore.table_for(origin)) end)
      %{origin: origin}
    end

    test "a server that starts again holds nothing it was watching before", %{origin: origin} do
      # Every request is held open by a socket the server owns, so a row
      # that outlived the server watching it is a name held by nobody and
      # nothing will ever give it back. Mnesia belongs to the VM, not to
      # the server, so a restart of one and not the other leaves exactly
      # that — measured in the wild as a semaphore held for half an hour
      # by a run that had long since moved on.
      Semaphore.request(origin, "planner-abc", "a run that is gone", 1_000)
      assert Semaphore.holder("planner-abc")
      :ok = Semaphore.start(origin)
      refute Semaphore.holder("planner-abc"), "a stale request survived the server that held it"
    end

    test "a request nobody is ahead of is granted at once", %{origin: origin} do
      ref = Semaphore.request(origin, "planner-abc", "bay1")
      assert Semaphore.granted?("planner-abc", ref)
    end

    test "the older request holds it and the younger waits", %{origin: origin} do
      first = Semaphore.request(origin, "planner-abc", "bay1", 1_000)
      second = Semaphore.request(origin, "planner-abc", "bay2", 2_000)
      assert Semaphore.granted?("planner-abc", first)
      refute Semaphore.granted?("planner-abc", second)
    end

    test "a request in another host's table is seen, and can outrank this one", %{origin: origin} do
      # The whole point: the lock this replaces could not see another
      # machine at all, so two hosts derived against one ledger at once.
      other = "s#{System.unique_integer([:positive])}"
      :ok = Semaphore.start(other)
      on_exit(fn -> :mnesia.delete_table(Semaphore.table_for(other)) end)

      theirs = Semaphore.request(other, "planner-abc", "spark bay1", 1_000)
      mine = Semaphore.request(origin, "planner-abc", "mac bay1", 2_000)

      refute Semaphore.granted?("planner-abc", mine)
      assert Semaphore.granted?("planner-abc", theirs)
    end

    test "releasing hands it to the next in line", %{origin: origin} do
      first = Semaphore.request(origin, "planner-abc", "bay1", 1_000)
      second = Semaphore.request(origin, "planner-abc", "bay2", 2_000)
      :ok = Semaphore.release(origin, first)
      assert Semaphore.granted?("planner-abc", second)
    end

    test "two requests made in the same instant still have exactly one holder", %{origin: origin} do
      # Clocks on two hosts agree to the microsecond often enough, and a
      # tie that both sides break differently is two planners.
      a = Semaphore.request(origin, "planner-abc", "bay1", 5_000)
      b = Semaphore.request(origin, "planner-abc", "bay2", 5_000)
      assert Enum.count([a, b], &Semaphore.granted?("planner-abc", &1)) == 1
    end

    test "a name is a semaphore of its own", %{origin: origin} do
      one = Semaphore.request(origin, "planner-abc", "bay1", 1_000)
      two = Semaphore.request(origin, "planner-def", "bay2", 2_000)
      assert Semaphore.granted?("planner-abc", one)
      assert Semaphore.granted?("planner-def", two), "one project's derivation must not block another's"
    end

    test "what is held and who is behind it can be read from any host", %{origin: origin} do
      name = "planner-#{origin}"
      Semaphore.request(origin, name, "spark bay1", 1_000)
      Semaphore.request(origin, name, "host-a bay2", 2_000)
      line = Control.holds() |> String.split("\n") |> Enum.find(&String.starts_with?(&1, name))
      assert line =~ "held by spark bay1"
      assert line =~ "1 waiting: host-a bay2"
    end

    test "the holder says who it is, so a waiter can name what it waits for", %{origin: origin} do
      Semaphore.request(origin, "planner-abc", "host-a bay1 run-7", 1_000)
      assert %{label: "host-a bay1 run-7", origin: ^origin} = Semaphore.holder("planner-abc")
    end
  end


  describe "the semaphore socket" do
    setup %{host: host} do
      # A name of this test's own. The tables are the VM's, so a name
      # shared between tests is a queue shared between them.
      origin = "s#{System.unique_integer([:positive])}"
      host = %{host | origin: origin}
      on_exit(fn -> :mnesia.delete_table(Semaphore.table_for(origin)) end)
      start_supervised!({Semaphore.Socket, host})
      %{origin: origin, host: host, path: Semaphore.Socket.path(), name: "planner-#{origin}"}
    end

    test "a run that asks for a free semaphore is told it holds it", %{path: path, name: name} do
      socket = acquire(path, name, "bay1")
      assert "held" <> _ = line(socket)
      :gen_tcp.close(socket)
    end

    test "a second run waits, and is granted the moment the first lets go", %{path: path, name: name} do
      first = acquire(path, name, "bay1")
      assert "held" <> _ = line(first)

      second = acquire(path, name, "bay2")
      assert "waiting bay1" == line(second), "a waiter is told who it is behind"

      # Nothing is granted while the first holds it.
      assert {:error, :timeout} = line(second, 300)

      :gen_tcp.close(first)
      assert "held" <> _ = line(second, 5_000)
      :gen_tcp.close(second)
    end

    test "a caller with something better to do is told the name is busy", %{path: path, name: name} do
      # The lander's shape: a bay that cannot have it works a stage
      # instead. Waiting would cost it a whole compose — a full suite —
      # for a job somebody else is already doing.
      first = acquire(path, name, "bay1")
      assert "held" <> _ = line(first)

      second = connect(path)
      :ok = :gen_tcp.send(second, "try #{name} bay2\n")
      assert "busy bay1" == line(second)
      :gen_tcp.close(second)

      :gen_tcp.close(first)
    end

    test "a refused attempt leaves nothing queued behind it", %{path: path, name: name} do
      # A request left standing would reach the front later and be granted
      # to a connection nobody is holding.
      first = acquire(path, name, "bay1")
      assert "held" <> _ = line(first)
      second = connect(path)
      :ok = :gen_tcp.send(second, "try #{name} bay2\n")
      assert "busy" <> _ = line(second)
      :gen_tcp.close(second)

      wait_for(fn -> length(Semaphore.queue(name)) == 1 end)
      :gen_tcp.close(first)
      wait_for(fn -> Semaphore.holder(name) == nil end)
    end

    test "an attempt at a free name holds it for as long as the connection", %{path: path, name: name} do
      socket = connect(path)
      :ok = :gen_tcp.send(socket, "try #{name} bay1\n")
      assert "held" <> _ = line(socket)
      assert Semaphore.holder(name).label == "bay1"
      :gen_tcp.close(socket)
      wait_for(fn -> Semaphore.holder(name) == nil end)
    end

    test "a holder that dies releases what it held", %{path: path, name: name} do
      # The property the whole design turns on. The run is killed, or the
      # machine it ran on drops off; nothing gets a chance to say so.
      test = self()

      owner =
        spawn(fn ->
          socket = acquire(path, name, "bay1")
          send(test, {:said, line(socket)})
          receive do: (:never -> :ok)
        end)

      assert_receive {:said, "held" <> _}, 2_000
      assert Semaphore.holder(name).label == "bay1"
      Process.exit(owner, :kill)
      wait_for(fn -> Semaphore.holder(name) == nil end)
    end

    test "a run announces itself and stops being alive when it dies", %{path: path, origin: origin} do
      # The lease a claim in the ledger is had no reader off its own
      # machine: pid liveness means nothing across hosts, so a run that
      # died on one held its stage against every other. This is what any
      # host can ask instead.
      test = self()

      run =
        spawn(fn ->
          socket = connect(path)
          :ok = :gen_tcp.send(socket, "presence 20260912-1-bay1 host-a/target\n")
          send(test, {:said, line(socket)})
          receive do: (:never -> :ok)
        end)

      assert_receive {:said, "alive " <> _}, 2_000
      assert Runs.all()[origin] == ["20260912-1-bay1"]
      Process.exit(run, :kill)
      wait_for(fn -> Runs.all()[origin] == [] end)
    end

    test "the runs of every host are answered, and a host that cannot be asked is said to be", %{path: path, origin: origin} do
      # Absence is a fact only about a host that answered. A host merely
      # off the link is not a host whose runs have stopped, and saying so
      # would be answering a question about the network as though it were
      # about the work.
      gone = "s#{System.unique_integer([:positive])}"
      :ok = Runs.start(gone)
      on_exit(fn -> :mnesia.delete_table(Runs.table_for(gone)) end)
      :ok = :mnesia.dirty_write({Runs.table_for(gone), "r", "a-run", "bay", 1})

      socket = connect(path)
      :ok = :gen_tcp.send(socket, "runs\n")
      lines = Stream.repeatedly(fn -> line(socket) end) |> Enum.take_while(&(&1 != "end"))
      :gen_tcp.close(socket)

      assert "#{gone} a-run" in lines
      refute Enum.any?(lines, &String.starts_with?(&1, "unreachable")),
             "every table here is readable, so nothing may be reported unreachable"
      assert Runs.all()[origin] == []
    end

    test "a request nobody can read is refused rather than left hanging", %{path: path} do
      socket = connect(path)
      :ok = :gen_tcp.send(socket, "hello\n")
      assert "error " <> _ = line(socket)
    end

    test "a door that cannot be opened refuses rather than crashing", %{host: host} do
      # Whatever goes wrong here, the daemon keeps running its bays: a
      # crash would be restarted until the supervisor gave up and took
      # every run on the host down with it. The runs fall back to a lock
      # that reaches only this machine, which is where they started.
      stop_supervised!(Semaphore.Socket)
      System.put_env("CODE_GANTRY_DAEMON_STATE", "/nonexistent/nowhere")
      assert :ignore == Semaphore.Socket.start_link(host)
    end

    test "the socket is remade over a stale file a dead daemon left", %{host: host, path: path, name: name} do
      # A unix socket outlives the process that made it, so a daemon that
      # was killed leaves a path that binding refuses. Starting again must
      # take it back rather than come up with no door.
      stop_supervised!(Semaphore.Socket)
      File.write!(path, "")
      start_supervised!({Semaphore.Socket, host})
      socket = acquire(path, name, "bay1")
      assert "held" <> _ = line(socket)
      :gen_tcp.close(socket)
    end
  end


  describe "a reload brings the running tree up to the new code" do
    setup %{host: host} do
      # A tree of this test's own under the name the daemon uses, empty, so
      # reconcile has something to add to.
      origin = "s#{System.unique_integer([:positive])}"
      host = %{host | origin: origin}

      start_supervised!(%{
        id: :tree,
        start: {Supervisor, :start_link, [[], [strategy: :one_for_one, name: CodeGantryDaemon.Supervisor]]},
        type: :supervisor
      })

      on_exit(fn -> :mnesia.delete_table(Semaphore.table_for(origin)) end)
      %{host: host, origin: origin}
    end

    defp running, do: for({id, pid, _, _} <- Supervisor.which_children(CodeGantryDaemon.Supervisor), is_pid(pid), do: id)

    test "a child this version declares and the running tree lacks is started", %{host: host} do
      # The whole point: loading a module starts no process, so a hot
      # reload would carry the code and leave the feature dormant until a
      # restart — which costs every run on the host.
      refute Semaphore.Socket in running()
      started = Application.reconcile(host)
      assert Semaphore.Socket in started
      assert Semaphore.Socket in running()
    end

    test "a child that refuses is not reported as started", %{host: host} do
      # `{:ok, :undefined}` is what a child answers when it declines to
      # run. Counting that as started says so on every pickup for as long
      # as it keeps refusing, and hides that anything is wrong.
      System.put_env("CODE_GANTRY_DAEMON_STATE", "/nonexistent/nowhere")
      refute Semaphore.Socket in Application.reconcile(host)
      refute Process.whereis(Semaphore.Socket)
      # And it keeps refusing quietly rather than announcing itself again.
      refute Semaphore.Socket in Application.reconcile(host)
    end

    test "a tree that is already right is left alone", %{host: host} do
      Application.reconcile(host)
      before = running()
      assert Application.reconcile(host) == []
      assert running() == before
    end

    test "a child that is present but not running is started again", %{host: host} do
      Application.reconcile(host)
      pid = Process.whereis(Semaphore.Socket)
      # What a child that refused at boot looks like: still declared,
      # nothing running. Reconcile is a repair, not only an addition.
      :ok = Supervisor.terminate_child(CodeGantryDaemon.Supervisor, Semaphore.Socket)
      refute Semaphore.Socket in running()
      assert Semaphore.Socket in Application.reconcile(host)
      assert Process.whereis(Semaphore.Socket) != pid
      assert Semaphore.Socket in running()
    end

    test "what the daemon starts with and what a reload adds are one list", %{host: host} do
      ids = Enum.map(Application.children(host), &Supervisor.child_spec(&1, []).id)
      assert Semaphore.Socket in ids
      assert Enum.uniq(ids) == ids, "a child declared twice is started twice"
    end
  end


  describe "bay records in Mnesia" do
    setup do
      origin = "o#{System.unique_integer([:positive])}"
      :ok = Records.start(origin)
      on_exit(fn -> :mnesia.delete_table(Records.table_for(origin)) end)
      %{origin: origin}
    end

    test "a host writes its own bays and reads them back", %{origin: origin} do
      Records.put(origin, "bay1", %{repo: "r", project: "p", state: "running", detail: "run-1"})
      assert [row] = Records.all()  |> Enum.filter(&(&1.origin == origin))
      assert row.name == "bay1" and row.project == "p" and row.state == "running"
      assert row.since != nil, "a row carries when it was written, or staleness cannot be seen"
    end

    test "every host's rows are read together, each still naming its origin", %{origin: origin} do
      other = "o#{System.unique_integer([:positive])}"
      :ok = Records.start(other)
      on_exit(fn -> :mnesia.delete_table(Records.table_for(other)) end)
      Records.put(origin, "bay1", %{repo: "r", project: "p", state: "running", detail: "d"})
      Records.put(other, "bay1", %{repo: "r", project: "q", state: "stopped", detail: "d"})

      rows = Records.all() |> Enum.filter(&(&1.origin in [origin, other]))
      assert length(rows) == 2, "one host's table must not hide another's"
      assert Enum.sort(Enum.map(rows, & &1.origin)) == Enum.sort([origin, other])
    end

    test "a read that cannot be answered says so rather than answering nothing", %{origin: origin} do
      # The same path a read of a down host's table takes: its only copy is
      # on that machine, so the read fails rather than returning empty. The
      # two must not arrive at the caller looking alike.
      alias CodeGantryDaemon.Owned
      assert {:ok, []} = Owned.read(Records.table_for(origin), {:_, :_, :_, :_, :_, :_, :_})
      assert :unreachable = Owned.read(:"bays@nobody-here", {:_, :_, :_, :_, :_, :_, :_})
      assert Owned.rows(:"bays@nobody-here", {:_, :_, :_, :_, :_, :_, :_}) == []
    end

    test "a host owns one table, so two hosts never define the same one", %{origin: origin} do
      other = "o#{System.unique_integer([:positive])}"
      refute Records.table_for(origin) == Records.table_for(other)
    end
  end

end
