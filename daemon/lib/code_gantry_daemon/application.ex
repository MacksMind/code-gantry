defmodule CodeGantryDaemon.Application do
  @moduledoc """
  The per-host daemon: what stays alive on a host between runs and across
  them. It supervises one `code-gantry run` per bay, makes a bay that does
  not exist yet, and writes a status file. The record of the work is the
  ledger, which every host writes directly; the daemon carries none of it.
  Nothing it holds is state the ledger does not hold; losing it loses
  nothing, and a run started by hand with no daemon behaves the same.
  """
  use Application

  alias CodeGantryDaemon.{Bay, Findings, Host, Mesh, Pickup, Placements, Semaphore, Status, Web}

  @impl true
  def start(_type, _args) do
    host = Host.load!(System.get_env("CODE_GANTRY_HOST_FILE") || Host.path())
    File.mkdir_p!(Host.state_dir())
    # The address `bin/daemon` talks to. Written by the daemon rather than
    # computed by the script, so the two cannot disagree about the hostname.
    File.write!(Path.join(Host.state_dir(), "node"), Atom.to_string(node()) <> "\n")

    {:ok, sup} = Supervisor.start_link(children(host), strategy: :one_for_one, name: CodeGantryDaemon.Supervisor)
    start_bays(host)
    {:ok, sup}
  end

  @doc """
  What this version of the daemon runs. One list, read both by a daemon
  starting and by a daemon that has just loaded new code, because a
  version that declares a child here and never starts it is a feature
  that exists everywhere except where it runs.
  """
  def children(host) do
    [
      {Registry, keys: :unique, name: CodeGantryDaemon.Registry},
      {Status, host},
      {DynamicSupervisor, name: CodeGantryDaemon.Bays, strategy: :one_for_one},
      # Before the pickup, so a pickup that moves the code has somewhere
      # to send its nudge. The pickup runs whether or not a tick is
      # configured: `pickup_seconds: 0` turns off the clock, not the
      # ability to be told.
      {Mesh, host},
      {Pickup, host},
      # After the mesh, so the first run to ask sees whatever peers are
      # already reachable rather than only this host's own queue.
      {Semaphore.Socket, host},
      # The dashboard: what it announces on, what it shows, and the page.
      {Phoenix.PubSub, name: CodeGantryDaemon.PubSub},
      {Findings, host},
      Web.child_spec(host)
    ]
  end

  @doc """
  Bring the running tree up to what this version declares, and answer what
  had to be started. **Loading a module does not start a process**: a
  child added in a new version is absent from a tree that is already
  running, so a hot reload would carry the code and leave the feature
  dormant until somebody restarted the host — which costs every run on it.
  Called by the pickup after it loads, so a new child arrives the way
  every other change does.

  A child that is present but not running was refused at boot, or stopped;
  it is dropped and started again, which makes this a repair as well as an
  addition. Everything here is idempotent: a tree that is already right is
  left alone.
  """
  def reconcile(host) do
    # Nothing to bring up to date when no tree is running: a run driving
    # the pickup directly, or a test, is not a daemon.
    if Process.whereis(CodeGantryDaemon.Supervisor) == nil do
      []
    else
      for spec <- children(host), started = start_missing(spec), started != nil, do: started
    end
  end

  # Never raises. This is called from the pickup, and a reload that throws
  # because one child would not start loses the load it just did.
  defp start_missing(spec) do
    id = Supervisor.child_spec(spec, []).id

    case start_child(spec) do
      # A pid, and only a pid. A child that refuses answers `{:ok,
      # :undefined}`, and reporting that as started would say so on every
      # pickup for as long as it kept refusing.
      {:ok, pid} when is_pid(pid) ->
        id

      {:error, :already_present} ->
        Supervisor.delete_child(CodeGantryDaemon.Supervisor, id)

        case start_child(spec) do
          {:ok, pid} when is_pid(pid) -> id
          _ -> nil
        end

      _ ->
        nil
    end
  end

  defp start_child(spec) do
    Supervisor.start_child(CodeGantryDaemon.Supervisor, spec)
  rescue
    e -> {:error, e}
  catch
    :exit, reason -> {:error, reason}
  end

  @doc "Every bay of this host — the host file's and the placed ones — as a running Bay."
  def start_bays(host) do
    for bay <- Placements.all(host), do: start_bay(host, bay)
  end

  @doc "One bay under the bays supervisor; a name already running is refused by its registration."
  def start_bay(host, bay) do
    DynamicSupervisor.start_child(CodeGantryDaemon.Bays, {Bay, {host, bay}})
  end

  @doc "Take a bay's process down so it can be started again with a new placement. `:ok` when there was none."
  def stop_bay(name) do
    case GenServer.whereis(Bay.via(name)) do
      nil -> :ok
      pid -> DynamicSupervisor.terminate_child(CodeGantryDaemon.Bays, pid)
    end
  end
end
