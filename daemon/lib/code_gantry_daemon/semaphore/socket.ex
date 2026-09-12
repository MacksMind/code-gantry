defmodule CodeGantryDaemon.Semaphore.Socket do
  @moduledoc """
  The door a run knocks on to take a semaphore, and the reason a hold
  cannot outlive its holder.

  A Unix socket under the daemon's state directory, so only this host's
  runs can reach it and the path needs no port, no address and no
  credential — the directory is the permission. A run connects, asks for a
  name, and is told `held` when it is first in the queue. **It then holds
  the connection for as long as it holds the semaphore, and closing it is
  how it lets go.** A run that is killed, or whose machine goes away,
  closes it without meaning to, which is the same thing and the point.

  The release is the owner's job, never the handler's. One process serves
  each connection and the server monitors it, so a handler that crashes,
  is killed, or exits by any route at all has its request withdrawn by
  something that is still alive. A release written into the handler would
  be skipped by exactly the deaths this exists to survive.

  The protocol is one line each way:

      acquire <name> <label>   ->  waiting <who holds it>   (only if it waits)
                                   held <ref>
      <the connection closes>  ->  released

  `label` is the rest of the line: who is asking, shown to whoever is
  waiting behind them.
  """
  use GenServer
  require Logger

  alias CodeGantryDaemon.{Host, Semaphore}

  # How often a waiter looks again. Holds here are derivations, which run
  # in minutes, so this is far below anything it delays and far above
  # anything it costs — one remote read per waiter, and only while a name
  # is contended.
  @poll_ms 500

  @doc "Where a run connects. Beside the daemon's other state, so finding the daemon is finding this."
  def path, do: Path.join(Host.state_dir(), "semaphore.sock")

  def start_link(host), do: GenServer.start_link(__MODULE__, host, name: __MODULE__)

  @impl true
  def init(host) do
    :ok = Semaphore.start(host.origin)
    Process.flag(:trap_exit, true)
    # A Unix socket outlives the process that made it, so a daemon that was
    # killed leaves a path that accepts nothing and refuses to be bound.
    File.rm(path())

    case :gen_tcp.listen(0, [{:ifaddr, {:local, path()}}, :binary, packet: :line, active: false, backlog: 64]) do
      {:ok, listen} ->
        server = self()
        acceptor = spawn_link(fn -> accept(listen, host, server) end)
        {:ok, %{host: host, listen: listen, acceptor: acceptor, held: %{}}}

      {:error, reason} ->
        # A daemon with no semaphore still runs its bays; the runs fall
        # back to the lock that only reaches this machine.
        Logger.error("semaphore: cannot listen on #{path()}: #{inspect(reason)}")
        :ignore
    end
  end

  @impl true
  def terminate(_reason, %{listen: listen}) do
    :gen_tcp.close(listen)
    File.rm(path())
    :ok
  end

  def terminate(_reason, _state), do: :ok

  @impl true
  def handle_call({:request, name, label}, {pid, _}, state) do
    ref = Semaphore.request(state.host.origin, name, label)
    monitor = Process.monitor(pid)
    {:reply, ref, %{state | held: Map.put(state.held, monitor, ref)}}
  end

  @impl true
  def handle_info({:DOWN, monitor, :process, _pid, _reason}, state) do
    {ref, held} = Map.pop(state.held, monitor)
    if ref, do: Semaphore.release(state.host.origin, ref)
    {:noreply, %{state | held: held}}
  end

  def handle_info({:EXIT, pid, reason}, %{acceptor: pid} = state) do
    {:stop, reason, state}
  end

  def handle_info(_other, state), do: {:noreply, state}

  defp accept(listen, host, server) do
    case :gen_tcp.accept(listen) do
      {:ok, socket} ->
        handler = spawn(fn -> serve(socket, host, server) end)
        :ok = :gen_tcp.controlling_process(socket, handler)
        send(handler, :yours)
        accept(listen, host, server)

      {:error, :closed} ->
        :ok

      {:error, reason} ->
        Logger.warning("semaphore: accept failed: #{inspect(reason)}")
        accept(listen, host, server)
    end
  end

  defp serve(socket, host, server) do
    receive do: (:yours -> :ok)

    with {:ok, line} <- :gen_tcp.recv(socket, 0),
         ["acquire", name, label] <- String.split(String.trim(line), " ", parts: 3) do
      hold(socket, host, server, name, label)
    else
      ["acquire", name] -> hold(socket, host, server, name, "a run")
      {:error, _} -> :ok
      _ -> :gen_tcp.send(socket, "error expected: acquire <name> <label>\n")
    end

    :gen_tcp.close(socket)
  end

  defp hold(socket, _host, server, name, label) do
    ref = GenServer.call(server, {:request, name, label})
    # Active from here, so the connection closing reaches this process as a
    # message whether it is waiting its turn or holding the name.
    :inet.setopts(socket, active: true)
    wait(socket, name, ref, false)
  end

  defp wait(socket, name, ref, told) do
    cond do
      Semaphore.granted?(name, ref) ->
        :gen_tcp.send(socket, "held #{ref}\n")
        closed(socket)

      told ->
        closed_or_after(socket, name, ref, told)

      true ->
        holder = Semaphore.holder(name)
        :gen_tcp.send(socket, "waiting #{(holder && holder.label) || "another run"}\n")
        closed_or_after(socket, name, ref, true)
    end
  end

  defp closed_or_after(socket, name, ref, told) do
    receive do
      {:tcp_closed, _} -> :ok
      {:tcp_error, _, _} -> :ok
    after
      @poll_ms -> wait(socket, name, ref, told)
    end
  end

  defp closed(socket) do
    receive do
      {:tcp_closed, _} -> :ok
      {:tcp_error, _, _} -> :ok
      # Anything a holder sends while holding is ignored; the connection is
      # the message.
      {:tcp, _, _} -> closed(socket)
    end
  end
end
