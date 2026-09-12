defmodule CodeGantryDaemon.Records do
  @moduledoc """
  The bays of every host: what a bay is doing now, on which host, for
  which project. That is orchestration state, which is the daemons'; the
  ledger in the table is the project's record and is not this.

  One `CodeGantryDaemon.Owned` table per origin, each host writing only
  its own and reading everyone's. A daemon that has just started is the
  authority on its own bays, which is why nothing here is kept.
  """
  alias CodeGantryDaemon.Owned

  @attributes [:name, :repo, :project, :state, :detail, :since]
  @prefix "bays@"

  @doc "This host's table, created if it is not there yet."
  def start(origin), do: Owned.start(@prefix, origin, @attributes)

  @doc "The table a host owns. One per origin, by construction."
  def table_for(origin), do: Owned.table_for(@prefix, origin)

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
  Every host's rows, each naming the origin that wrote it. A peer that has
  joined contributes its bays with no further arrangement, and a peer that
  has gone simply stops appearing.
  """
  def all do
    for table <- Owned.tables(@prefix),
        {_t, name, repo, project, state, detail, since} <-
          Owned.rows(table, {:_, :_, :_, :_, :_, :_, :_}) do
      %{
        origin: Owned.origin_of(@prefix, table),
        name: name,
        repo: repo,
        project: project,
        state: state,
        detail: detail,
        since: since
      }
    end
  end
end
