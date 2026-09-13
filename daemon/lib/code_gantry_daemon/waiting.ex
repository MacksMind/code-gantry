defmodule CodeGantryDaemon.Waiting do
  @moduledoc """
  What is waiting on a person, for every project placed on this host:
  findings that need a human and open human-owned items, each with the
  card an investigation attached and the thread since, read through the
  CLI's `ledger waiting --json` and never from the table. Read again on a
  clock, on request, and after every action; each read is announced on
  the `dashboard` topic so a page showing it changes with it.

  Every action is one CLI call — `answer`, `ask`, `move`, `land`,
  `strike`, or `plan edit --owner pipeline` — so a click on the dashboard
  writes the event the CLI would.
  """
  use GenServer

  require Logger

  alias CodeGantryDaemon.{Command, Host, Placements}

  @topic "dashboard"
  @refresh_ms 60_000
  @skip ~w(/.code_gantry/ /node_modules/ /deps/ /_build/ /vendor/)

  def start_link(host), do: GenServer.start_link(__MODULE__, host, name: __MODULE__)

  @doc """
  Every placed project: `[%{project, config, waiting, projects, read_at,
  error}]`; `projects` are the others in its repository. The last reading,
  from a table rather than the server, so a page mounting never waits on
  a read in progress: one read is a CLI call per project, seconds each.
  """
  def all do
    # No table yet — the server is starting, or a daemon that took this
    # code on a hot load still runs the process that never made one —
    # is no reading yet, and the page fills in on the next broadcast.
    with tid when tid != :undefined <- :ets.whereis(__MODULE__),
         [{:projects, projects}] <- :ets.lookup(tid, :projects) do
      projects
    else
      _ -> []
    end
  end

  @doc "Read every project again now, and wait for it."
  def refresh, do: GenServer.call(__MODULE__, :refresh, 60_000)

  @doc """
  Act on one thing, by the config of the project it is in. Actions:
  `answer` (a finding: `disposition`, `text`, `target`), `ask` (`text`),
  `move` (`to`, a config path; `under`, a section key for an item),
  `land` (an item: `sha`), `strike` (an item: `text`), `fleet` (an item
  handed to the pipeline), `accept` (the card's recommendation, applied as the events the answer
  would have been), `investigate` (the daemon starts the investigator on
  it in an idle bay). Answers `{:ok, decoded}` or
  `{:error, why}`.
  """
  def act(config, action, params) do
    GenServer.call(__MODULE__, {:act, config, action, params}, 60_000)
  end

  def topic, do: @topic

  @impl true
  def init(host) do
    :ets.new(__MODULE__, [:named_table, :public, read_concurrency: true])
    {:ok, %{host: host, projects: [], timer: nil}, {:continue, :refresh}}
  end

  @impl true
  def handle_continue(:refresh, state), do: {:noreply, read(state)}

  @impl true
  def handle_info(:refresh, state), do: {:noreply, read(state)}

  @impl true
  def handle_call(:refresh, _from, state), do: {:reply, :ok, read(state)}

  # Not a ledger write: the daemon starts the investigator in an idle bay.
  def handle_call({:act, config, "investigate", %{"about" => about}}, _from, state) do
    {:reply, {:ok, CodeGantryDaemon.Control.investigate(project_of(config), about)}, state}
  end

  def handle_call({:act, config, action, params}, _from, %{host: host} = state) do
    reply =
      case argv(action, params) do
        {:ok, {:move_to, to_config, under, id, new_section}} -> move_to(host, config, to_config, under, id, new_section)
        {:ok, args} -> cli(host, args ++ ["--config", config])
        {:error, why} -> {:error, why}
      end

    # Work came back in reach of the fleet: a project a run found
    # complete may not be any more.
    case {reply, action} do
      {{:ok, _}, "fleet"} -> CodeGantryDaemon.Control.wake(project_of(config))
      {{:ok, _}, "move"} -> CodeGantryDaemon.Control.wake(project_of(params["to"]))
      {{:ok, _}, "move_to"} -> CodeGantryDaemon.Control.wake(params["to"] |> String.split("|") |> hd() |> project_of())
      {{:ok, %{"disposition" => "pipeline"}}, "accept"} -> CodeGantryDaemon.Control.wake(project_of(config))
      {{:ok, %{"disposition" => "move", "to" => to}}, "accept"} when is_binary(to) -> CodeGantryDaemon.Control.wake(project_of(to))
      _ -> :ok
    end

    {:reply, reply, read(state)}
  end

  defp argv("accept", %{"about" => id}), do: {:ok, ["ledger", "accept", id, "--json"]}

  defp argv("answer", %{"about" => id, "disposition" => disposition} = p) when disposition in ~w(amend discard debt raise),
    do: {:ok, ["ledger", "answer", id, disposition, "--json"] ++ flag("--text", p["text"]) ++ flag("--target", p["target"])}

  defp argv("ask", %{"about" => id, "text" => text}) when is_binary(text) and text != "",
    do: {:ok, ["ledger", "ask", id, "--text", text, "--json"]}

  defp argv("move", %{"about" => id, "to" => to} = p) when is_binary(to) and to != "",
    do: {:ok, ["ledger", "move", id, "--to", to, "--json"] ++ flag("--under", p["under"])}

  # The dashboard's move: `to` is `<config>|<section key>`, and a new
  # section's title, when given, is made under that key first and becomes
  # the place the thing goes.
  defp argv("move_to", %{"about" => id, "to" => to} = p) when is_binary(to) and to != "" do
    case String.split(to, "|", parts: 2) do
      [config, under] -> {:ok, {:move_to, config, under, id, p["new_section"]}}
      _ -> {:error, "move needs a project and a section"}
    end
  end

  defp argv("land", %{"about" => id, "sha" => sha}) when is_binary(sha) and sha != "",
    do: {:ok, ["ledger", "land", id, sha]}

  defp argv("strike", %{"about" => id, "text" => text}) when is_binary(text) and text != "",
    do: {:ok, ["ledger", "strike", id, text]}

  defp argv("fleet", %{"about" => id}), do: {:ok, ["plan", "edit", id, "--owner", "pipeline"]}

  defp argv(action, params), do: {:error, "#{action} needs more than #{inspect(Map.keys(params))}"}

  defp project_of(config), do: config |> Path.dirname() |> Path.basename()

  defp move_to(host, config, to_config, under, id, new_section) do
    with {:ok, under} <- section_for(host, to_config, under, new_section),
         {:ok, moved} <- cli(host, ["ledger", "move", id, "--to", to_config, "--json", "--under", under, "--config", config]) do
      {:ok, Map.put(moved, "disposition", "move")}
    end
  end

  defp section_for(_host, _to_config, under, title) when title in [nil, ""], do: {:ok, under}

  defp section_for(host, to_config, under, title) do
    case cli(host, ["plan", "add", "--under", under, "--kind", "section", "--title", title, "--config", to_config]) do
      {:ok, key} when is_binary(key) -> {:ok, key |> String.split() |> hd()}
      {:ok, other} -> {:error, "plan add answered #{inspect(other)}"}
      {:error, why} -> {:error, why}
    end
  end

  defp flag(_name, value) when value in [nil, ""], do: []
  defp flag(name, value), do: [name, value]

  defp cli(host, args) do
    case Command.stdout(Command.code_gantry(host, args), host.code_gantry, Host.env(host)) do
      {out, 0} ->
        case Jason.decode(out) do
          {:ok, decoded} -> {:ok, decoded}
          _ -> {:ok, String.trim(out)}
        end

      {out, status} ->
        {:error, "#{Enum.join(Enum.take(args, 2), " ")} exited #{status}: #{String.trim(out)}"}
    end
  end

  defp read(%{host: host} = state) do
    # One clock, re-armed on every read. Arming another each time made
    # every action a person took add a reading a minute for the life of
    # the daemon, and the page repainted every few seconds.
    # `Map.get`: a process hot-loaded into this code holds a state map
    # from before the field existed.
    if Map.get(state, :timer), do: Process.cancel_timer(state.timer)
    timer = Process.send_after(self(), :refresh, @refresh_ms)

    projects =
      for {config, bay} <- placed(host) do
        {waiting, error} = waiting_of(host, config)

        %{
          project: Host.project_of(host, bay),
          config: config,
          waiting: waiting,
          projects: other_projects(host, bay, config),
          read_at: DateTime.utc_now(),
          error: error
        }
      end

    :ets.insert(__MODULE__, {:projects, projects})
    # Local: the daemons are one Erlang cluster and PubSub spans it, so a
    # cluster-wide broadcast repainted every host's page with this host's
    # reading, whose paths are not that host's.
    Phoenix.PubSub.local_broadcast(CodeGantryDaemon.PubSub, @topic, {:waiting, projects})
    Map.merge(state, %{projects: projects, timer: timer})
  end

  # One read per project, however many bays work it: the same config in
  # two bays is two paths and one project.
  defp placed(host) do
    host
    |> Placements.all()
    |> Enum.uniq_by(&Host.project_of(host, &1))
    |> Enum.map(&{Host.bay_config(host, &1), &1})
  end

  # The repository's other projects, as the checkout holds them: every
  # `code_gantry.yaml` up to three directories down but this one's, named
  # by its directory, the way a placement names a project. Never `**`: a
  # Rails checkout's `tmp/`, `node_modules/` and `log/` are hundreds of
  # thousands of entries, and one walk held this server for minutes.
  defp other_projects(host, bay, config) do
    dir = Host.bay_dir(host, bay)

    ~w(*/code_gantry.yaml */*/code_gantry.yaml */*/*/code_gantry.yaml)
    |> Enum.flat_map(&Path.wildcard(Path.join(dir, &1)))
    |> Enum.reject(fn path -> path == config or Enum.any?(@skip, &String.contains?(path, &1)) end)
    |> Enum.map(&%{project: &1 |> Path.dirname() |> Path.basename(), config: &1, sections: sections_of(host, &1)})
    |> Enum.sort_by(& &1.project)
  end

  # The places a thing can be moved under in another project: its
  # documents and sections, in plan order, with depth for indenting.
  defp sections_of(host, config) do
    case Command.stdout(Command.code_gantry(host, ["plan", "sections", "--json", "--config", config]), host.code_gantry, Host.env(host)) do
      {out, 0} ->
        case Jason.decode(out) do
          {:ok, rows} when is_list(rows) -> rows
          _ -> []
        end

      _ ->
        []
    end
  end

  defp waiting_of(host, config) do
    case Command.stdout(Command.code_gantry(host, ["ledger", "waiting", "--json", "--config", config]), host.code_gantry, Host.env(host)) do
      {out, 0} ->
        case Jason.decode(out) do
          {:ok, rows} when is_list(rows) -> {rows, nil}
          _ -> {[], "ledger waiting printed something other than a list"}
        end

      {out, status} ->
        Logger.warning("waiting of #{config}: ledger waiting exited #{status}: #{String.slice(String.trim(out), 0, 200)}")
        {[], "ledger waiting exited #{status}"}
    end
  end
end
