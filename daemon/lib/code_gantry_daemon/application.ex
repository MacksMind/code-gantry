defmodule CodeGantryDaemon.Application do
  @moduledoc """
  The per-host daemon: what stays alive on a host between runs and across
  them. It supervises one `code-gantry run` per bay, makes a bay that does
  not exist yet, syncs the ledger on a clock, and writes a status file.
  Nothing it holds is state the ledger does not hold; losing it loses
  nothing, and a run started by hand with no daemon behaves the same.
  """
  use Application

  alias CodeGantryDaemon.{Host, Status, Sync, Bay}

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
        {Sync, host}
      ] ++ Enum.map(host.bays, fn bay -> Supervisor.child_spec({Bay, {host, bay}}, id: {Bay, bay.name}) end)

    Supervisor.start_link(children, strategy: :one_for_one, name: CodeGantryDaemon.Supervisor)
  end
end
