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

  alias CodeGantryDaemon.{Bay, Host, Pickup, Placements, Status}

  @impl true
  def start(_type, _args) do
    host = Host.load!(System.get_env("CODE_GANTRY_HOST_FILE") || Host.path())
    File.mkdir_p!(Host.state_dir())
    # The address `bin/daemon` talks to. Written by the daemon rather than
    # computed by the script, so the two cannot disagree about the hostname.
    File.write!(Path.join(Host.state_dir(), "node"), Atom.to_string(node()) <> "\n")

    children =
      [
        {Registry, keys: :unique, name: CodeGantryDaemon.Registry},
        {Status, host},
        {DynamicSupervisor, name: CodeGantryDaemon.Bays, strategy: :one_for_one}
      ] ++ if(host.pickup_seconds > 0, do: [{Pickup, host}], else: [])

    {:ok, sup} = Supervisor.start_link(children, strategy: :one_for_one, name: CodeGantryDaemon.Supervisor)
    start_bays(host)
    {:ok, sup}
  end

  @doc "Every bay of this host — the host file's and the placed ones — as a running Bay."
  def start_bays(host) do
    for bay <- Placements.all(host), do: start_bay(host, bay)
  end

  @doc "One bay under the bays supervisor; a name already running is refused by its registration."
  def start_bay(host, bay) do
    DynamicSupervisor.start_child(CodeGantryDaemon.Bays, {Bay, {host, bay}})
  end
end
