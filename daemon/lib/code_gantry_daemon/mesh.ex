defmodule CodeGantryDaemon.Mesh do
  @moduledoc """
  The daemons' connections to each other.

  Every host dials every peer the host file names and keeps trying, so a
  machine that was asleep, or off the link, joins on its own when it comes
  back. Nothing here is required for a host to work: a daemon with no peers
  runs its bays exactly as it would alone, which is why a failed connection
  is logged and never raised.

  The cookie is the only credential, and both ends must run an Erlang close
  enough to speak the distribution protocol, which is why `bin/daemon` goes
  through the pinned toolchain.

  What the mesh carries is a nudge: the host that has just taken new code
  tells the others to take it now rather than at their next tick. It moves
  no state. The code still travels through the git remote, and the nudge
  only shortens the wait.
  """
  use GenServer
  require Logger

  alias CodeGantryDaemon.{Host, Pickup, Records}

  @retry_ms 30_000

  def start_link(host), do: GenServer.start_link(__MODULE__, host, name: __MODULE__)

  @doc "The peers reachable right now."
  def peers, do: Node.list()

  @doc """
  Tell every connected peer to pick code up now. Returns how many were
  told. A cast rather than a call: the sender has already done its own
  work and must not wait on, or fail with, somebody else's fetch.
  """
  def nudge do
    nodes = Node.list()
    Enum.each(nodes, fn node -> :rpc.cast(node, Pickup, :nudged, []) end)
    if nodes != [], do: Logger.info("mesh: nudged #{Enum.join(nodes, ", ")}")
    length(nodes)
  end

  @impl true
  def init(host) do
    :net_kernel.monitor_nodes(true)
    send(self(), :dial)
    {:ok, host}
  end

  @impl true
  def handle_info(:dial, host) do
    for node <- Host.peer_nodes(host), node not in Node.list() do
      Node.connect(node)
    end

    Process.send_after(self(), :dial, @retry_ms)
    {:noreply, host}
  end

  def handle_info({:nodeup, node}, host) do
    Logger.info("mesh: #{node} joined")
    Records.join(node)
    {:noreply, host}
  end

  def handle_info({:nodedown, node}, host) do
    Logger.info("mesh: #{node} left")
    {:noreply, host}
  end

  def handle_info(_other, host), do: {:noreply, host}
end
