defmodule CodeGantryDaemon.Owned do
  @moduledoc """
  Tables in Mnesia that one host owns: a kind of record, one table per
  origin, and the host named by the origin is the only writer of its own.

  Two hosts therefore never define the same table, which is what lets
  their schemas merge when they meet — a shared table defined
  independently on both sides refuses to merge, and none of the state
  here is worth resolving that for. Reading is the other way round: every
  table of a kind is read together, wherever it lives, so a peer that has
  joined contributes its rows with no further arrangement.

  A table a peer owns has its only copy on that peer, so reading it is a
  live question asked of that machine. When the machine is gone the read
  fails and the rows are treated as absent, which is the right answer for
  everything kept here: it is all statements about processes that are
  running, and a host that is gone is running nothing.

  The tables are RAM only. Nothing here survives a restart and nothing
  here should.
  """
  require Logger

  @doc "This host's table of one kind, created if it is not there yet."
  def start(prefix, origin, attributes) do
    :mnesia.start()
    table = table_for(prefix, origin)

    case :mnesia.create_table(table, attributes: attributes, ram_copies: [node()]) do
      {:atomic, :ok} -> :ok
      {:aborted, {:already_exists, ^table}} -> :ok
      {:aborted, reason} -> {:error, reason}
    end
  end

  @doc "The table a host owns, for one kind of record. One per origin, by construction."
  def table_for(prefix, origin), do: :"#{prefix}#{origin}"

  @doc "Every table of one kind the cluster knows about, this host's among them."
  def tables(prefix) do
    Enum.filter(:mnesia.system_info(:tables), fn table ->
      String.starts_with?(Atom.to_string(table), prefix)
    end)
  end

  @doc "The origin that owns a table."
  def origin_of(prefix, table) do
    Atom.to_string(table) |> String.replace_prefix(prefix, "")
  end

  @doc """
  Rows of one table matching a pattern, and none when the host that owns
  it cannot be asked. An unreachable peer is not an error here: its rows
  describe its own live processes, which are unreachable too.
  """
  def rows(table, pattern) do
    case read(table, pattern) do
      {:ok, rows} -> rows
      :unreachable -> []
    end
  end

  @doc """
  The same read, keeping the difference between "no rows" and "could not
  ask". Every table but this host's own has its only copy on the host that
  owns it, so a read of one is a live question put to that machine.

  The difference matters wherever absence is taken for a fact. A host that
  is merely off the link is not a host whose runs have stopped, and
  answering an empty list for one would be answering a question about the
  network as though it were about the work.
  """
  def read(table, pattern) do
    {:ok, :mnesia.dirty_match_object(pattern |> put_elem(0, table))}
  rescue
    _ -> :unreachable
  catch
    :exit, _ -> :unreachable
  end

  @doc "Bring a peer's tables into view, and ours into its."
  @doc """
  Forget a node that has gone: its tables leave this schema. A daemon
  that restarts makes its tables again with new identities, and a schema
  still holding the old ones refuses to merge with it (`Bad cookie in
  table definition`), which left a restarted peer's bays invisible for as
  long as the other host stayed up. Answers a suffix for the log line.
  """
  def forget(node) do
    case :mnesia.del_table_copy(:schema, node) do
      {:atomic, :ok} -> "; its tables forgotten"
      {:aborted, reason} -> "; could not forget its tables: #{inspect(reason)}"
    end
  end

  def join(node) do
    case :mnesia.change_config(:extra_db_nodes, [node]) do
      {:ok, _} ->
        Logger.info("mnesia: schemas merged with #{node}")
        :ok

      {:error, reason} ->
        Logger.warning("mnesia: could not merge schemas with #{node}: #{inspect(reason)}")
        {:error, reason}
    end
  end
end
