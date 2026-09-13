defmodule CodeGantryDaemon.Bay do
  @moduledoc """
  One bay: its checkout exists, and a run occupies it.

  The checkout is made with the target's `bin/mk-bay` when it is missing —
  run from the primary copy when that holds the script, else from another
  bay of the same repository, since the primary is a person's checkout on
  whatever branch they need and a bay is always on the project branch. A run is `code-gantry run` under a run id the daemon
  chose, so a run that dies can be resumed by name. How a run ends decides
  what happens next, by the CLI's exit code: 0 finished, 1 failed before or
  outside a stage, 2 escalated, 3 paused — all four stop the bay and say so
  in the status file, since each wants a person — and anything else is a
  crash, resumed after a backoff.

  A person answers through `retry/1`, reached by `bin/daemon retry <bay>`.
  How the last run ended decides what a retry is: a run that failed or
  finished has nothing to continue, so a new run starts; one that escalated,
  paused or died is resumed under its id, since its stage is still there.
  """
  # Long enough for `terminate` to ask the run to stop, wait, and kill what
  # is left. The default is five seconds, which is the same as the grace
  # period — so the supervisor would kill this process while it was still
  # waiting, and the run would be orphaned exactly as before.
  use GenServer, shutdown: 20_000

  # What a run gets between being asked to stop and being made to. It closes
  # its semaphore and its presence on the way out, and a killed one leaves
  # both to the kernel.
  @grace_ms 5_000
  require Logger

  alias CodeGantryDaemon.{Host, Command, Status}

  @backoff_seconds [10, 30, 60, 120, 300]

  def start_link({host, bay}), do: GenServer.start_link(__MODULE__, {host, bay}, name: via(bay.name))

  # Local to this node. Every host has a bay1, and a cluster-wide name
  # would resolve the duplicate by killing one of them on the first
  # connection between two daemons; a verb reaches a bay through its node.
  def via(name), do: {:via, Registry, {CodeGantryDaemon.Registry, {__MODULE__, name}}}

  @doc "The bays of this host with a run live."
  def running(host) do
    for bay <- CodeGantryDaemon.Placements.all(host),
        pid = GenServer.whereis(via(bay.name)),
        pid != nil,
        GenServer.call(pid, :running?),
        do: bay.name
  end

  @doc """
  Ask the run in this bay to stop at its next seam, and resume it from the
  new code when it has. `:pausing` when a run was asked; `:idle` when none
  is live, in which case the bay's next run is on the new code anyway.
  """
  def pause_for_pickup(name) do
    case GenServer.whereis(via(name)) do
      nil -> :idle
      pid -> GenServer.call(pid, :pause_for_pickup, 60_000)
    end
  end

  @doc """
  Launch again. `{:ok, mode, run_id}` names what was started; a bay with a
  run live refuses with its id, since the run is the thing to talk to.
  """
  def retry(name) do
    case GenServer.whereis(via(name)) do
      nil -> {:error, :no_such_bay}
      pid -> GenServer.call(pid, :retry)
    end
  end

  @impl true
  def init({host, bay}) do
    # Without this the supervisor's shutdown never reaches `terminate`, so
    # the run this bay started outlives the daemon that started it. Three
    # of four did, and had to be killed by hand.
    Process.flag(:trap_exit, true)
    state = %{host: host, bay: bay, port: nil, log: nil, run_id: nil, crashes: 0, mode: :run, last: nil, resume_after_pause: false}
    Status.put(bay.name, :starting, nil, Host.project_of(host, bay))
    {:ok, state, {:continue, :ensure_checkout}}
  end

  @impl true
  def handle_continue(:ensure_checkout, %{host: host, bay: bay} = state) do
    dir = Host.bay_dir(host, bay)
    source = mk_bay_source(host, dir)

    cond do
      File.dir?(dir) ->
        {:noreply, state, {:continue, :launch}}

      source == nil ->
        # A bay that cannot be made is reported and left alone: the daemon
        # stays up for the bays it can run and the status file says why.
        why = "no bin/mk-bay in #{host.primary} or in any bay of it; is one of them on the project branch and pulled?"
        Logger.error("#{bay.name}: #{why}")
        Status.put(bay.name, :failed, why, Host.project_of(host, bay))
        {:noreply, state}

      true ->
        Logger.info("#{bay.name}: making #{dir} from #{source}")
        Status.put(bay.name, :making, nil, Host.project_of(host, bay))
        args = ["bin/mk-bay", bay.name, Integer.to_string(bay.offset)] ++ if(host.branch, do: [host.branch], else: [])
        # The script names the bay after the checkout it runs from; told
        # the repository's name, it names the bay after that instead.
        env = Host.env(host) ++ [{"MK_BAY_PROJECT", Path.basename(host.primary)}]

        case Command.run(args, source, env) do
          {_out, 0} ->
            {:noreply, state, {:continue, :launch}}

          {out, status} ->
            Logger.error("#{bay.name}: mk-bay exited #{status}:\n#{out}")
            Status.put(bay.name, :failed, "mk-bay exited #{status}; see the daemon log", Host.project_of(host, bay))
            {:noreply, state}
        end
    end
  end

  def handle_continue(:launch, %{host: host, bay: bay} = state) do
    run_id = state.run_id || new_run_id(bay)
    config = Host.bay_config(host, bay)

    args =
      case state.mode do
        :run -> ["run", config, "--run-id", run_id]
        :resume -> ["resume", config, run_id]
      end

    log_path = Path.join(Host.state_dir(), "#{bay.name}.log")
    Logger.info("#{bay.name}: #{Enum.join(args, " ")}")
    {port, log} = Command.start(Command.code_gantry(host, args), host.code_gantry, Host.env(host), log_path)
    Status.put(bay.name, :running, run_id, Host.project_of(host, bay))
    {:noreply, %{state | port: port, log: log, run_id: run_id}}
  end

  @impl true
  def handle_info({port, {:data, {:eol, line}}}, %{port: port, log: log} = state) do
    IO.write(log, line <> "\n")
    {:noreply, state}
  end

  def handle_info({port, {:data, {:noeol, chunk}}}, %{port: port, log: log} = state) do
    IO.write(log, chunk)
    {:noreply, state}
  end

  def handle_info({port, {:exit_status, status}}, %{port: port, bay: bay, host: host} = state) do
    File.close(state.log)
    state = %{state | port: nil, log: nil}

    case status do
      0 ->
        Logger.info("#{bay.name}: run #{state.run_id} finished")
        Status.put(bay.name, :finished, state.run_id, Host.project_of(host, bay))
        {:noreply, %{state | last: :finished}}

      1 ->
        Logger.warning("#{bay.name}: run #{state.run_id} failed before or outside a stage (exit 1)#{last_lines(bay)}")
        Status.put(bay.name, :failed, state.run_id, Host.project_of(host, bay))
        {:noreply, %{state | last: :failed}}

      2 ->
        Logger.warning("#{bay.name}: run #{state.run_id} escalated to a person#{last_lines(bay)}")
        Status.put(bay.name, :escalated, state.run_id, Host.project_of(host, bay))
        {:noreply, %{state | last: :escalated}}

      3 when state.resume_after_pause ->
        # Paused for a code pickup: the resume starts a new process, from
        # the code now on disk.
        Logger.info("#{bay.name}: run #{state.run_id} paused for the code pickup; resuming on the new code")
        Status.put(bay.name, :resuming, state.run_id, Host.project_of(host, bay))
        Process.send_after(self(), :relaunch, 1_000)
        {:noreply, %{state | last: :paused, mode: :resume, resume_after_pause: false}}

      3 ->
        Logger.info("#{bay.name}: run #{state.run_id} paused")
        Status.put(bay.name, :paused, state.run_id, Host.project_of(host, bay))
        {:noreply, %{state | last: :paused}}

      other ->
        wait = Enum.at(@backoff_seconds, min(state.crashes, length(@backoff_seconds) - 1))
        Logger.warning("#{bay.name}: run #{state.run_id} died (exit #{other}); resuming in #{wait}s")
        Status.put(bay.name, :crashed, state.run_id, Host.project_of(host, bay))
        Process.send_after(self(), :relaunch, wait * 1000)
        {:noreply, %{state | crashes: state.crashes + 1, mode: :resume, last: :crashed}}
    end
  end

  # A backoff that fires after a person already relaunched must not start a
  # second run in the bay.
  def handle_info(:relaunch, %{port: port} = state) when is_port(port), do: {:noreply, state}
  def handle_info(:relaunch, state), do: {:noreply, state, {:continue, :launch}}
  def handle_info(_other, state), do: {:noreply, state}

  @impl true
  def handle_call(:running?, _from, state), do: {:reply, is_port(state.port), state}

  def handle_call(:pause_for_pickup, _from, %{port: port, host: host, bay: bay} = state) when is_port(port) do
    config = Host.bay_config(host, bay)
    {out, status} = Command.run(Command.code_gantry(host, ["pause", config, state.run_id, "--note", "code pickup"]), host.code_gantry, Host.env(host))
    if status != 0, do: Logger.warning("#{bay.name}: pause for pickup exited #{status}: #{String.trim(out)}")
    {:reply, :pausing, %{state | resume_after_pause: status == 0}}
  end

  def handle_call(:pause_for_pickup, _from, state), do: {:reply, :idle, state}

  def handle_call(:retry, _from, %{port: port} = state) when is_port(port) do
    {:reply, {:error, {:running, state.run_id}}, state}
  end

  # Never launched: the checkout is what to try again.
  def handle_call(:retry, _from, %{run_id: nil} = state) do
    {:reply, {:ok, :run, nil}, state, {:continue, :ensure_checkout}}
  end

  def handle_call(:retry, _from, state) do
    {mode, run_id} =
      if state.last in [:escalated, :paused, :crashed],
        do: {:resume, state.run_id},
        else: {:run, new_run_id(state.bay, state.run_id)}

    {:reply, {:ok, mode, run_id}, %{state | mode: mode, run_id: run_id, crashes: 0}, {:continue, :launch}}
  end

  # The exit code says which kind of end; the run's own output says why.
  # Its last lines travel into the daemon log beside the verdict, so a
  # failure is readable without opening the bay's log.
  defp last_lines(bay, count \\ 8) do
    case File.read(Path.join(Host.state_dir(), "#{bay.name}.log")) do
      {:ok, text} ->
        text
        |> String.split("\n", trim: true)
        |> Enum.take(-count)
        |> Enum.map_join("", &("\n  " <> &1))

      _ ->
        ""
    end
  end


  @impl true
  def terminate(_reason, %{port: port, bay: bay}) when is_port(port) do
    Logger.info("#{bay.name}: stopping its run")
    Command.stop_tree(port, @grace_ms)
    Port.close(port)
  catch
    _, _ -> :ok
  end

  def terminate(_reason, _state), do: :ok

  # A run id is the second the run started in this bay, so two runs never
  # share one: a retry inside the same second waits for the next.
  # The first checkout of the repository that carries the script: the
  # primary copy, then its bays in name order, never the bay being made.
  defp mk_bay_source(host, dir) do
    siblings = Path.wildcard(Path.join(Path.dirname(host.primary), Path.basename(host.primary) <> "-*"))

    [host.primary | Enum.sort(siblings)]
    |> Enum.reject(&(&1 == dir))
    |> Enum.find(&File.regular?(Path.join([&1, "bin", "mk-bay"])))
  end

  defp new_run_id(bay, previous \\ nil) do
    now = DateTime.utc_now()
    id = "#{Calendar.strftime(now, "%Y%m%d-%H%M%S")}-#{bay.name}"

    if id == previous do
      {ms, _} = now.microsecond
      Process.sleep(1000 - div(ms, 1000))
      new_run_id(bay, previous)
    else
      id
    end
  end
end
