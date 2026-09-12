defmodule CodeGantryDaemon.Records do
  @moduledoc """
  The bays of every host, in Mnesia, which is where orchestration state
  lives: what a bay is doing now, on which host, for which project. The
  ledger in the table is the project's record and is not this.

  Each host owns exactly one table, named for its origin, and writes only
  its own rows. Two hosts therefore never define the same table, which is
  what lets their schemas merge when they meet: a shared table defined
  independently on both sides refuses to merge, and the state here is too
  cheap to be worth resolving that.

  The tables are RAM only. Nothing here survives a restart and nothing
  here should: it is a statement about processes that are running, and a
  daemon that has just started is the authority on its own bays.
  """
  require Logger

  @attributes [:name, :repo, :project, :state, :detail, :since]
  @prefix "bays@"

  @doc "This host's table, created if it is not there yet."
  def start(origin) do
    :mnesia.start()
    table = table_for(origin)

    case :mnesia.create_table(table, attributes: @attributes, ram_copies: [node()]) do
      {:atomic, :ok} -> :ok
      {:aborted, {:already_exists, ^table}} -> :ok
      {:aborted, reason} -> {:error, reason}
    end
  end

  @doc "The table a host owns. One per origin, by construction."
  def table_for(origin), do: :"#{@prefix}#{origin}"

  @doc """
  Write one bay row. Only ever called for this host's own origin: a host
  is the single writer of its own table, so two daemons never contend.
  """
  def put(origin, name, fields) do
    table = table_for(origin)

    :mnesia.dirty_write(
      table,
      {table, name, fields[:repo], fields[:project], fields[:state], fields[:detail],
       DateTime.utc_now()}
    )

    :ok
  end

  @doc """
  Every host's rows, each naming the origin that wrote it.

  Read from whatever tables the cluster knows about, so a peer that has
  joined contributes its bays with no further arrangement, and a peer
  that has gone simply stops appearing.
  """
  def all do
    for table <- tables(), row <- rows(table) do
      {_t, name, repo, project, state, detail, since} = row
      %{
        origin: origin_of(table),
        name: name,
        repo: repo,
        project: project,
        state: state,
        detail: detail,
        since: since
      }
    end
  end

  @doc "Bring a peer's tables into view, and ours into its."
  def join(node) do
    case :mnesia.change_config(:extra_db_nodes, [node]) do
      {:ok, _} ->
        Logger.info("records: schemas merged with #{node}")
        :ok

      {:error, reason} ->
        Logger.warning("records: could not merge schemas with #{node}: #{inspect(reason)}")
        {:error, reason}
    end
  end

  defp tables do
    Enum.filter(:mnesia.system_info(:tables), fn t ->
      String.starts_with?(Atom.to_string(t), @prefix)
    end)
  end

  defp rows(table) do
    :mnesia.dirty_match_object({table, :_, :_, :_, :_, :_, :_})
  rescue
    _ -> []
  catch
    :exit, _ -> []
  end

  defp origin_of(table) do
    Atom.to_string(table) |> String.replace_prefix(@prefix, "")
  end
end
