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

      presence <run-id> <bay>  ->  alive <ref>
      <the connection closes>  ->  the run is no longer alive

      runs                     ->  <origin> <run-id>        (one per line)
                                   unreachable <origin>
                                   end

  `label` is the rest of the line: who is asking, shown to whoever is
  waiting behind them. A presence is held the same way a semaphore is, and
  for the same reason: the connection is the claim that the process is
  there, and nothing has to be expired when it is not.
  """
  use GenServer
  require Logger

  alias CodeGantryDaemon.{Host, Runs, Semaphore}

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
    Process.flag(:trap_exit, true)
    case start(host) do
      {:ok, listen} ->
        server = self()
        acceptor = spawn_link(fn -> accept(listen, host, server) end)
        {:ok, %{host: host, listen: listen, acceptor: acceptor, held: %{}}}

      {:error, reason} ->
        # A daemon with no semaphore still runs its bays, on a lock that
        # reaches only their own machine. Refusing to start is the whole
        # of the damage, and it must stay that way: crashing here instead
        # would be restarted, and restarted again, until the supervisor
        # gave up and took every run on this host down with it.
        Logger.error("semaphore: not listening on #{path()}: #{inspect(reason)}")
        :ignore
    end
  end

  # The table this host writes its requests into, then the door. Either can
  # refuse — an Mnesia that never started, a path that cannot be bound —
  # and neither may raise.
  defp start(host) do
    with :ok <- Semaphore.start(host.origin),
         :ok <- Runs.start(host.origin) do
      # A Unix socket outlives the process that made it, so a daemon that
      # was killed leaves a path that binding refuses.
      File.rm(path())
      :gen_tcp.listen(0, [{:ifaddr, {:local, path()}}, :binary, packet: :line, active: false, backlog: 64])
    end
  rescue
    e -> {:error, e}
  catch
    :exit, reason -> {:error, reason}
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
    {:reply, ref, watch(state, pid, {:semaphore, ref})}
  end

  def handle_call({:presence, run_id, bay}, {pid, _}, state) do
    ref = Runs.began(state.host.origin, run_id, bay)
    {:reply, ref, watch(state, pid, {:run, ref})}
  end

  @impl true
  def handle_info({:DOWN, monitor, :process, _pid, _reason}, state) do
    {holding, held} = Map.pop(state.held, monitor)
    case holding do
      {:semaphore, ref} -> Semaphore.release(state.host.origin, ref)
      {:run, ref} -> Runs.ended(state.host.origin, ref)
      nil -> :ok
    end

    {:noreply, %{state | held: held}}
  end

  def handle_info({:EXIT, pid, reason}, %{acceptor: pid} = state) do
    {:stop, reason, state}
  end

  def handle_info(_other, state), do: {:noreply, state}

  # Whatever a connection holds, the server is what gives it back: a
  # handler that crashes, or is killed, has released nothing itself.
  defp watch(state, pid, holding) do
    %{state | held: Map.put(state.held, Process.monitor(pid), holding)}
  end

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

    case :gen_tcp.recv(socket, 0) do
      {:ok, line} -> asked(socket, host, server, String.split(String.trim(line), " ", parts: 3))
      {:error, _} -> :ok
    end

    :gen_tcp.close(socket)
  end

  defp asked(socket, host, server, ["acquire", name, label]), do: hold(socket, host, server, name, label)
  defp asked(socket, host, server, ["acquire", name]), do: hold(socket, host, server, name, "a run")
  defp asked(socket, _host, server, ["presence", run_id, bay]), do: alive(socket, server, run_id, bay)
  defp asked(socket, _host, server, ["presence", run_id]), do: alive(socket, server, run_id, "")

  defp asked(socket, _host, _server, ["runs" | _]) do
    for {origin, runs} <- Runs.all() do
      case runs do
        :unreachable -> :gen_tcp.send(socket, "unreachable #{origin}\n")
        ids -> for id <- ids, do: :gen_tcp.send(socket, "#{origin} #{id}\n")
      end
    end

    # Said explicitly, because a reader cannot tell a host with no runs
    # from an answer that stopped early any other way.
    :gen_tcp.send(socket, "end\n")
  end

  defp asked(socket, _host, _server, _other) do
    :gen_tcp.send(socket, "error expected: acquire <name> <label>, presence <run-id> <bay>, or runs\n")
  end

  defp alive(socket, server, run_id, bay) do
    ref = GenServer.call(server, {:presence, run_id, bay})
    :inet.setopts(socket, active: true)
    :gen_tcp.send(socket, "alive #{ref}\n")
    closed(socket)
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
