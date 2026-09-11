defmodule CodeGantryDaemon.Sync do
  @moduledoc """
  The ledger sync on a clock: `code-gantry ledger sync` against a bay's
  config, which pushes this origin's events to its ref on the remote and
  fetches every other origin's. The four seams inside a run do the same
  for themselves; this is for what happens between runs and for a laptop
  that has just reopened.
  """
  use GenServer
  require Logger

  alias CodeGantryDaemon.{Host, Command, Status}

  def start_link(host), do: GenServer.start_link(__MODULE__, host, name: __MODULE__)

  @impl true
  def init(host) do
    send(self(), :tick)
    {:ok, host}
  end

  @impl true
  def handle_info(:tick, host) do
    config = sync_config(host)
    {out, status} = Command.run(Command.code_gantry(host, ["ledger", "sync", "--config", config]), host.code_gantry, Host.env(host))

    if status == 0 do
      Status.put(:sync, :ok, String.trim(out))
    else
      Logger.warning("ledger sync exited #{status}: #{String.trim(out)}")
      Status.put(:sync, :failed, "exit #{status}")
    end

    Process.send_after(self(), :tick, host.sync_seconds * 1000)
    {:noreply, host}
  end

  # Any bay's config names the ledger and the remote, so the first bay that
  # exists serves. The primary copy's config serves only on a host with no
  # bay yet: the primary is a person's tree, on whatever branch they need.
  defp sync_config(host) do
    case Enum.find(host.bays, &File.dir?(Host.bay_dir(host, &1))) do
      nil -> Path.join(host.primary, host.config)
      bay -> Host.bay_config(host, bay)
    end
  end
end
