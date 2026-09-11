defmodule CodeGantryDaemon.Bay do
  @moduledoc """
  One bay: its checkout exists, and a run occupies it.

  The checkout is made with the target's `bin/mk-bay` from the primary copy
  when it is missing. A run is `code-gantry run` under a run id the daemon
  chose, so a run that dies can be resumed by name. How a run ends decides
  what happens next, by the CLI's exit code: 0 finished, 1 failed before or
  outside a stage, 2 escalated, 3 paused — all four stop the bay and say so
  in the status file, since each wants a person — and anything else is a
  crash, resumed after a backoff.
  """
  use GenServer
  require Logger

  alias CodeGantryDaemon.{Host, Command, Status}

  @backoff_seconds [10, 30, 60, 120, 300]

  def start_link({host, bay}), do: GenServer.start_link(__MODULE__, {host, bay}, name: via(bay.name))

  def via(name), do: {:global, {__MODULE__, name}}

  @impl true
  def init({host, bay}) do
    state = %{host: host, bay: bay, port: nil, log: nil, run_id: nil, crashes: 0, mode: :run}
    Status.put(bay.name, :starting, nil)
    {:ok, state, {:continue, :ensure_checkout}}
  end

  @impl true
  def handle_continue(:ensure_checkout, %{host: host, bay: bay} = state) do
    dir = Host.bay_dir(host, bay)

    unless File.dir?(dir) do
      Logger.info("#{bay.name}: making #{dir}")
      Status.put(bay.name, :making, nil)

      {out, status} =
        Command.run(["bin/mk-bay", bay.name, Integer.to_string(bay.offset)], host.primary, Host.env(host))

      if status != 0 do
        Logger.error("#{bay.name}: mk-bay failed (#{status}):\n#{out}")
        Status.put(bay.name, :failed, "mk-bay exited #{status}")
        throw({:stop, :mk_bay_failed})
      end
    end

    {:noreply, state, {:continue, :launch}}
  end

  def handle_continue(:launch, %{host: host, bay: bay} = state) do
    run_id = state.run_id || new_run_id(bay)
    config = Host.bay_config(host, bay)

    args =
      case state.mode do
        :run -> ["run", config, "--run-id", run_id] ++ Enum.flat_map(bay.scope, &["--scope", &1])
        :resume -> ["resume", config, "--run-id", run_id]
      end

    log_path = Path.join(Host.state_dir(), "#{bay.name}.log")
    Logger.info("#{bay.name}: #{Enum.join(args, " ")}")
    {port, log} = Command.start(Command.code_gantry(host, args), host.code_gantry, Host.env(host), log_path)
    Status.put(bay.name, :running, run_id)
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

  def handle_info({port, {:exit_status, status}}, %{port: port, bay: bay} = state) do
    File.close(state.log)
    state = %{state | port: nil, log: nil}

    case status do
      0 ->
        Logger.info("#{bay.name}: run #{state.run_id} finished")
        Status.put(bay.name, :finished, state.run_id)
        {:noreply, state}

      1 ->
        Logger.warning("#{bay.name}: run #{state.run_id} failed before or outside a stage (exit 1)")
        Status.put(bay.name, :failed, state.run_id)
        {:noreply, state}

      2 ->
        Logger.warning("#{bay.name}: run #{state.run_id} escalated to a person")
        Status.put(bay.name, :escalated, state.run_id)
        {:noreply, state}

      3 ->
        Logger.info("#{bay.name}: run #{state.run_id} paused")
        Status.put(bay.name, :paused, state.run_id)
        {:noreply, state}

      other ->
        wait = Enum.at(@backoff_seconds, min(state.crashes, length(@backoff_seconds) - 1))
        Logger.warning("#{bay.name}: run #{state.run_id} died (exit #{other}); resuming in #{wait}s")
        Status.put(bay.name, :crashed, state.run_id)
        Process.send_after(self(), :relaunch, wait * 1000)
        {:noreply, %{state | crashes: state.crashes + 1, mode: :resume}}
    end
  end

  def handle_info(:relaunch, state), do: {:noreply, state, {:continue, :launch}}

  def handle_info(_other, state), do: {:noreply, state}

  @impl true
  def terminate(_reason, %{port: port}) when is_port(port) do
    # The run is its own process group; closing the port ends it.
    Port.close(port)
  catch
    _, _ -> :ok
  end

  def terminate(_reason, _state), do: :ok

  defp new_run_id(bay) do
    stamp = Calendar.strftime(DateTime.utc_now(), "%Y%m%d-%H%M%S")
    "#{stamp}-#{bay.name}"
  end
end
