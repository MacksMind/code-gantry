defmodule CodeGantryDaemon.Control do
  @moduledoc """
  What a person can say to a running daemon, one function per verb, each
  answering a line of text. `bin/daemon` reaches these over the node the
  daemon started as, so nothing here reads a file or a pid: the daemon
  answers from its own state, and the same verbs serve a mesh later.
  """

  alias CodeGantryDaemon.{Application, Bay, Pickup, Placements, Status}

  @doc "Pick up this daemon's code from origin now, rather than at the next tick."
  def pickup, do: Pickup.tick(Status.host())

  @doc "Compile and load the local checkout as it is: for a test on one host; fetches nothing, nudges nobody."
  def reload(host \\ nil), do: Pickup.reload(host || Status.host())

  @doc """
  Add a bay to this host: remembered for the next start, its checkout made
  if it is missing, and a run started in it. A name the host already has —
  from its host file or an earlier placement — is refused.
  """
  def place(name, offset, config \\ nil) do
    host = Status.host()
    bay = if config, do: %{name: name, offset: offset, config: config}, else: %{name: name, offset: offset}
    on = if config, do: " on #{config}", else: ""

    if Enum.any?(Placements.all(host), &(&1.name == name)) do
      "#{name} is already placed"
    else
      :ok = Placements.add(bay)

      case Application.start_bay(host, bay) do
        {:ok, _} -> "#{name}: placed at offset #{offset}#{on}; making its checkout if it is missing, then starting a run"
        {:error, reason} -> "#{name}: placed, but could not start: #{inspect(reason)}"
      end
    end
  end

  def retry(name) do
    case Bay.retry(name) do
      {:ok, :run, nil} -> "#{name}: making the checkout again"
      {:ok, mode, run_id} -> "#{name}: #{mode} #{run_id} started"
      {:error, {:running, run_id}} -> "#{name} is running #{run_id}; nothing to retry"
      {:error, :no_such_bay} -> "no bay named #{name} in the host file"
    end
  end
end
