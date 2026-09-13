defmodule CodeGantryDaemon.Control do
  @moduledoc """
  What a person can say to a running daemon, one function per verb, each
  answering a line of text. `bin/daemon` reaches these over the node the
  daemon started as, so nothing here reads a file or a pid: the daemon
  answers from its own state, and the same verbs serve a mesh later.
  """

  alias CodeGantryDaemon.{Application, Bay, Complete, Host, Mesh, Pickup, Placements, Runs, Semaphore, Status}

  @doc "The commit this daemon's checkout is at, and the node answering: what a pickup is confirmed by."
  def version(host \\ nil) do
    host = host || Status.host()
    {sha, 0} = CodeGantryDaemon.Command.run(["git", "rev-parse", "HEAD"], host.code_gantry, [])
    {porcelain, 0} = CodeGantryDaemon.Command.run(["git", "status", "--porcelain"], host.code_gantry, [])
    mark = if String.trim(porcelain) == "", do: "", else: "+dirty"
    "code-gantry #{String.slice(String.trim(sha), 0, 12)}#{mark} on #{node()} (#{host.origin})"
  end

  @doc """
  Pick up this daemon's code from origin now, rather than waiting to be
  told. Passes the nudge on if it moved, so saying this to one host moves
  the mesh.
  """
  def pickup, do: Pickup.take(Status.host())

  @doc "Tell every connected peer to pick code up now."
  def nudge, do: "nudged #{Mesh.nudge()} peer(s)"

  @doc "The peers this daemon can see."
  def peers do
    case Mesh.peers() do
      [] -> "no peers connected"
      nodes -> Enum.join(nodes, "\n")
    end
  end

  @doc "Every bay on every host that has joined."
  def status, do: Status.render_all()

  @doc """
  What is held and who is waiting, across every host. A queue nobody can
  see is a queue nobody can tell is stuck.
  """
  def holds do
    case Semaphore.all() do
      [] -> "nothing held"
      entries -> entries |> Enum.group_by(& &1.name) |> Enum.sort() |> Enum.map_join("\n", &held_line/1)
    end
  end

  defp held_line({name, entries}) do
    [holder | waiting] = Enum.sort_by(entries, &{&1.at, &1.ref})
    behind = if waiting == [], do: "", else: ", #{length(waiting)} waiting: " <> Enum.map_join(waiting, ", ", & &1.label)
    "#{name} held by #{holder.label} (#{holder.origin}) since #{stamp(holder.at)}#{behind}"
  end

  defp stamp(at) do
    at |> DateTime.from_unix!(:microsecond) |> DateTime.truncate(:second) |> DateTime.to_iso8601()
  end

  @doc """
  Which runs are alive, on every host that answered. A host that could not
  be asked is named as such rather than shown with no runs: unreachable and
  finished must not read alike, or a claim gets given away while the run
  holding it is working behind a link that is down only from here.
  """
  def runs do
    case Runs.all() do
      empty when empty == %{} -> "no runs"
      hosts -> hosts |> Enum.sort() |> Enum.map_join("\n", &runs_line/1)
    end
  end

  defp runs_line({origin, :unreachable}), do: "#{origin} unreachable"
  defp runs_line({origin, []}), do: "#{origin} no runs"
  defp runs_line({origin, ids}), do: Enum.map_join(ids, "\n", &"#{origin} #{&1}")

  @doc "Compile and load the local checkout as it is: for a test on one host; fetches nothing, nudges nobody."
  def reload(host \\ nil), do: Pickup.reload(host || Status.host())

  @doc """
  Place a bay on this host: remembered for the next start, its checkout
  made if it is missing, and a run started in it. Placing a bay the host
  already has — from its host file or an earlier placement — replaces
  what it works, which is how a bay moves between projects; only a bay
  with a run live is refused, because the run would finish on one project
  while the record said another.
  """
  def place(name, offset, config \\ nil) do
    host = Status.host()
    bay = if config, do: %{name: name, offset: offset, config: config}, else: %{name: name, offset: offset}
    on = if config, do: " on #{config}", else: ""

    case Bay.live(name) do
      {:running, run_id} ->
        "#{name} is running #{run_id}; pause or stop it first"

      idle_or_absent ->
        :ok = Placements.put(bay)
        if idle_or_absent == :idle, do: Application.stop_bay(name)

        case Application.start_bay(host, bay) do
          {:ok, _} -> "#{name}: placed at offset #{offset}#{on}; making its checkout if it is missing, then starting a run"
          {:error, reason} -> "#{name}: placed, but could not start: #{inspect(reason)}"
        end
    end
  end

  @doc """
  A run found `project` complete: mark it so, and ask every other bay on
  it here to stop at its seam. Called by the bay whose run said so, and
  by a peer's daemon through `wound_down/3`.
  """
  def wind_down(project, run_id, origin) do
    Complete.mark(project, run_id)
    host = Status.host()

    for bay <- Placements.all(host), Host.project_of(host, bay) == project, do: Bay.wind_down(bay.name)

    _ = origin
    :ok
  end

  @doc "A peer's run found `project` complete; wind this host's bays on it down too."
  def wound_down(project, run_id, origin) do
    require Logger
    Logger.info("#{origin}: #{project} is complete (run #{run_id}); winding this host's bays on it down")
    wind_down(project, run_id, origin)
  end

  @doc """
  A complete project may have work again: forget the mark and start every
  bay idle on it, here and on every peer. What a person says after adding
  to the plan, and what the dashboard says after handing an item to the
  fleet or moving one in.
  """
  def wake(project, told_peers \\ false) do
    Complete.clear(project)
    host = Status.host()

    started =
      for bay <- Placements.all(host), Host.project_of(host, bay) == project, Bay.idle_complete?(bay.name) do
        {:ok, _mode, run_id} = Bay.retry(bay.name)
        "#{bay.name} #{run_id}"
      end

    peers = if told_peers, do: 0, else: Mesh.tell_peers(__MODULE__, :wake, [project, true])
    "#{project}: woken; started #{if started == [], do: "nothing", else: Enum.join(started, ", ")}; #{peers} peer(s) told"
  end

  @doc """
  Have a bay on `project` with no run live investigate one thing waiting
  on a person. A bay whose last run finished or found the project
  complete is preferred, since its checkout is at the tip and holds no
  stage; every idle bay is tried before giving up.
  """
  def investigate(project, about) do
    host = Status.host()

    idle =
      for bay <- Placements.all(host),
          Host.project_of(host, bay) == project,
          Bay.live(bay.name) == :idle,
          do: bay

    rested = fn bay -> match?({s, _, _, _} when s in [:finished, :complete], Status.get(bay.name)) end

    Enum.sort_by(idle, &(if rested.(&1), do: 0, else: 1))
    |> Enum.reduce_while("no idle bay on #{project}: every bay has a run live, or none is placed on it", fn bay, why ->
      case Bay.investigate(bay.name, about) do
        {:ok, _} -> {:halt, "#{about}: investigating in #{bay.name}; the card arrives on the dashboard when it is written"}
        {:error, :rate_limited} -> {:cont, "#{bay.name} has investigated its share this hour; " <> why}
        {:error, {:investigating, other}} -> {:cont, "#{bay.name} is investigating #{other}; " <> why}
        {:error, _} -> {:cont, why}
      end
    end)
  end

  def retry(name) do
    case Bay.retry(name) do
      {:ok, :run, nil} -> "#{name}: making the checkout again"
      {:ok, mode, run_id} -> "#{name}: #{mode} #{run_id} started"
      {:error, {:running, run_id}} -> "#{name} is running #{run_id}; nothing to retry"
      {:error, {:investigating, about}} -> "#{name} is investigating #{about}; wait for the card"
      {:error, :no_such_bay} -> "no bay named #{name} in the host file"
    end
  end
end
