defmodule CodeGantryDaemon.Control do
  @moduledoc """
  What a person can say to a running daemon, one function per verb, each
  answering a line of text. `bin/daemon` reaches these over the node the
  daemon started as, so nothing here reads a file or a pid: the daemon
  answers from its own state, and the same verbs serve a mesh later.
  """

  alias CodeGantryDaemon.{Application, Bay, Placements, Status}

  @doc """
  Add a bay to this host: remembered for the next start, its checkout made
  if it is missing, and a run started in it. A name the host already has —
  from its host file or an earlier placement — is refused.
  """
  def place(name, offset, scope \\ []) do
    host = Status.host()
    bay = %{name: name, offset: offset, scope: scope}

    if Enum.any?(Placements.all(host), &(&1.name == name)) do
      "#{name} is already placed"
    else
      :ok = Placements.add(bay)
      with_scope = if scope == [], do: "", else: " with scope #{Enum.join(scope, " ")}"

      case Application.start_bay(host, bay) do
        {:ok, _} -> "#{name}: placed at offset #{offset}#{with_scope}; making its checkout if it is missing, then starting a run"
        {:error, reason} -> "#{name}: placed, but could not start: #{inspect(reason)}"
      end
    end
  end

  @doc "Change what a bay draws from: the keys given, or the whole plan when none."
  def scope(name, keys) do
    host = Status.host()

    case Placements.set_scope(host, name, keys) do
      {:error, :no_such_bay} ->
        "no bay named #{name} on this host"

      {:ok, _bay} ->
        what = if keys == [], do: "the whole plan", else: "scope #{Enum.join(keys, " ")}"

        case Bay.rescope(name, keys) do
          {:started, run_id} -> "#{name}: #{what}; run #{run_id} started"
          {:running, run_id} -> "#{name}: #{what} from its next run; #{run_id} is still running"
          {:error, :no_such_bay} -> "#{name}: #{what} remembered; the bay is not running on this daemon"
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
