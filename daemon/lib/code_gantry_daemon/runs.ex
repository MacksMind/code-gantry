defmodule CodeGantryDaemon.Runs do
  @moduledoc """
  Which runs are alive, on every host in the mesh.

  A run announces itself over the daemon's socket and holds the
  connection for its life, exactly as it holds a semaphore, so the row
  disappears the instant the process does. That covers a run nothing
  supervises: the announcement is in the run's own code, not in the
  daemon's record of its bays, so a run started by hand in a terminal is
  as visible here as one the daemon started.

  This exists because a claim in the ledger is a lease and the lease had
  no reader off its own machine. `release_dead_holders` reads pid
  liveness, which is meaningless across hosts, so a run that died on one
  machine held its stage against every other until a run started there
  again. Answering "is that run still alive" from anywhere is what lets
  another host give the stage back.

  **A host that cannot be asked is not a host with no runs.** Every
  origin's table lives only on its own machine, so the read either
  answers or fails, and the two are kept apart all the way out to the
  caller: absence is a fact only about a host that answered.
  """
  alias CodeGantryDaemon.Owned

  @attributes [:ref, :run_id, :bay, :at]
  @prefix "runs@"

  @doc "This host's table of live runs, created if it is not there yet."
  def start(origin), do: Owned.start(@prefix, origin, @attributes)

  @doc "The table a host owns."
  def table_for(origin), do: Owned.table_for(@prefix, origin)

  @doc "Announce a run as alive here. Answers the reference that holds it."
  def began(origin, run_id, bay) do
    table = table_for(origin)
    ref = "#{node()}/#{System.unique_integer([:positive])}"
    :mnesia.dirty_write(table, {table, ref, run_id, bay, System.os_time(:microsecond)})
    ref
  end

  @doc "Withdraw one announcement."
  def ended(origin, ref) do
    :mnesia.dirty_delete(table_for(origin), ref)
    :ok
  end

  @doc """
  Every origin the mesh knows of, each with the runs alive on it or
  `:unreachable` when its host could not be asked. The caller must keep
  the two apart; that is the whole point of answering them separately.
  """
  def all do
    for table <- Owned.tables(@prefix), into: %{} do
      origin = Owned.origin_of(@prefix, table)

      case Owned.read(table, {:_, :_, :_, :_, :_}) do
        {:ok, rows} -> {origin, Enum.map(rows, fn {_t, _ref, run_id, _bay, _at} -> run_id end)}
        :unreachable -> {origin, :unreachable}
      end
    end
  end
end
