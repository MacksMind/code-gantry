defmodule CodeGantryDaemon.Findings do
  @moduledoc """
  The findings waiting on a person, for every project placed on this
  host, read through the CLI's `--json` face and never from the table:
  the derivation of views stays in one language. Read again on a clock,
  on request, and after every answer; each read is announced on the
  `dashboard` topic so a page showing them changes with them.

  An answer is one `ledger answer` call, so a click on the dashboard
  writes the same event the CLI would, and the disposition means what
  the views say it means.
  """
  use GenServer

  require Logger

  alias CodeGantryDaemon.{Command, Host, Placements}

  @topic "dashboard"
  @refresh_ms 60_000

  def start_link(host), do: GenServer.start_link(__MODULE__, host, name: __MODULE__)

  @doc "Every placed project's findings for a person: `[%{project, config, findings, read_at, error}]`."
  def all, do: GenServer.call(__MODULE__, :all)

  @doc "Read every project again now."
  def refresh, do: GenServer.call(__MODULE__, :refresh, 60_000)

  @doc """
  Answer one finding: `fold`, `discard`, `debt` or `raise`, with the text
  the disposition takes and the key it targets, both optional. Answers
  `{:ok, finding}` as the ledger now holds it, or `{:error, output}`.
  """
  def answer(config, finding_id, disposition, opts \\ []) do
    GenServer.call(__MODULE__, {:answer, config, finding_id, disposition, opts}, 60_000)
  end

  def topic, do: @topic

  @impl true
  def init(host) do
    {:ok, %{host: host, projects: []}, {:continue, :refresh}}
  end

  @impl true
  def handle_continue(:refresh, state), do: {:noreply, read(state)}

  @impl true
  def handle_info(:refresh, state), do: {:noreply, read(state)}

  @impl true
  def handle_call(:all, _from, state), do: {:reply, state.projects, state}
  def handle_call(:refresh, _from, state), do: {:reply, :ok, read(state)}

  def handle_call({:answer, config, finding_id, disposition, opts}, _from, %{host: host} = state) do
    args =
      ["ledger", "answer", finding_id, disposition, "--json"] ++
        for({flag, key} <- [{"--text", :text}, {"--target", :target}], value = opts[key], value not in [nil, ""], do: [flag, value])
        |> List.flatten()

    reply =
      case Command.stdout(Command.code_gantry(host, args ++ ["--config", config]), host.code_gantry, Host.env(host)) do
        {out, 0} -> {:ok, Jason.decode!(out)}
        {out, status} -> {:error, "ledger answer exited #{status}: #{String.trim(out)}"}
      end

    {:reply, reply, read(state)}
  end

  defp read(%{host: host} = state) do
    Process.send_after(self(), :refresh, @refresh_ms)

    projects =
      for {config, bay} <- placed(host) do
        {findings, error} = findings_of(host, config)
        %{project: Host.project_of(host, bay), config: config, findings: findings, read_at: DateTime.utc_now(), error: error}
      end

    Phoenix.PubSub.broadcast(CodeGantryDaemon.PubSub, @topic, {:findings, projects})
    %{state | projects: projects}
  end

  # One read per project, however many bays work it.
  defp placed(host) do
    host
    |> Placements.all()
    |> Enum.map(&{Host.bay_config(host, &1), &1})
    |> Enum.uniq_by(fn {config, _} -> config end)
  end

  defp findings_of(host, config) do
    args = ["ledger", "findings", "--for-human", "--json", "--config", config]

    case Command.stdout(Command.code_gantry(host, args), host.code_gantry, Host.env(host)) do
      {out, 0} ->
        case Jason.decode(out) do
          {:ok, findings} when is_list(findings) -> {findings, nil}
          _ -> {[], "ledger findings printed something other than a list"}
        end

      {out, status} ->
        Logger.warning("findings of #{config}: ledger findings exited #{status}: #{String.slice(String.trim(out), 0, 200)}")
        {[], "ledger findings exited #{status}"}
    end
  end
end
