defmodule CodeGantryDaemon.Control do
  @moduledoc """
  What a person can say to a running daemon, one function per verb, each
  answering a line of text. `bin/daemon` reaches these over the node the
  daemon started as, so nothing here reads a file or a pid: the daemon
  answers from its own state, and the same verbs serve a mesh later.
  """

  alias CodeGantryDaemon.{Application, Bay, Mesh, Pickup, Placements, Status}

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
