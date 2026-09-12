defmodule CodeGantryDaemon.Semaphore do
  @moduledoc """
  One holder at a time for a name, across every host in the mesh.

  The name is the thing being serialised, not the machine: the planner
  semaphore is named for the ledger, so the four bays of two hosts working
  one project queue behind each other and a second project's derivation is
  not delayed by any of them.

  **A hold dissolves the instant its holder dies.** That is the property
  the whole design turns on, and it is why this is here rather than in the
  table the ledger lives in: a run that is killed, a machine that sleeps or
  drops off the link, must not leave a name held. A request is a row in RAM
  belonging to a socket the asking process holds open, so the kernel
  reports the death and nothing has to be expired, renewed or cleaned up.
  Compare `hostlock.py`, which has the same property from `flock` and only
  the reach of one machine.

  The order is a queue, not a lock. Each host writes its own requests into
  its own table — nobody writes a shared row, so there is nothing to
  contend over and nothing to merge — and the holder is *derived*: the
  oldest outstanding request for a name, everywhere, ties broken by the
  reference, which carries the node that made it. Every host computes that
  from the same rows and reaches the same answer, which is what makes it a
  semaphore rather than four opinions.

  Two consequences worth stating, because both are chosen:

  A clock that is wrong makes the queue unfair, never unsafe. The order is
  the same on every host whatever the timestamps say, so skew decides who
  goes first and never how many go at once.

  A partition grants on both sides. A host that cannot see another's table
  cannot see its requests, and proceeds. That is the behaviour of every
  host today, where there is no cross-host lock at all, and the cost of a
  double derivation is a planner call, not a broken tree.
  """

  alias CodeGantryDaemon.Owned

  @attributes [:ref, :name, :label, :at]
  @prefix "waits@"

  @doc "This host's table of requests, created if it is not there yet."
  def start(origin), do: Owned.start(@prefix, origin, @attributes)

  @doc "The table a host owns."
  def table_for(origin), do: Owned.table_for(@prefix, origin)

  @doc """
  Ask for `name`, as `label` — who is asking, for a waiter to be told and
  an operator to read. Answers a reference; asking is not being granted,
  and `granted?/2` is the question. `at` is when the request was made, and
  is a parameter so a test can build a queue.
  """
  def request(origin, name, label, at \\ nil) do
    table = table_for(origin)
    ref = "#{node()}/#{System.unique_integer([:positive])}"
    :mnesia.dirty_write(table, {table, ref, name, label, at || System.os_time(:microsecond)})
    ref
  end

  @doc "Give up a request, held or still waiting."
  def release(origin, ref) do
    :mnesia.dirty_delete(table_for(origin), ref)
    :ok
  end

  @doc "Whether this request is the one holding the name right now."
  def granted?(name, ref) do
    case holder(name) do
      %{ref: ^ref} -> true
      _ -> false
    end
  end

  @doc "Who holds the name, or nil. The oldest request wins."
  def holder(name), do: List.first(queue(name))

  @doc "Everyone asking for one name, the holder first."
  def queue(name) do
    for(
      table <- Owned.tables(@prefix),
      row <- Owned.rows(table, {:_, :_, name, :_, :_}),
      do: entry(table, row)
    )
    |> ordered()
  end

  @doc "Every request on every name, for an operator asking what is held."
  def all do
    for table <- Owned.tables(@prefix),
        row <- Owned.rows(table, {:_, :_, :_, :_, :_}),
        do: entry(table, row)
  end

  defp entry(table, {_t, ref, name, label, at}) do
    %{origin: Owned.origin_of(@prefix, table), ref: ref, name: name, label: label, at: at}
  end

  # The one order, computed the same way on every host: oldest first, and
  # the reference — which names the node that made it — breaks a tie. Two
  # hosts reading the same rows must not disagree about who is first.
  defp ordered(entries), do: Enum.sort_by(entries, &{&1.at, &1.ref})
end
