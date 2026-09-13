defmodule CodeGantryDaemon.Web.DashboardLive do
  @moduledoc """
  One page: every bay on every host, what each semaphore is holding, and
  the findings waiting on a person as cards, each with the four
  dispositions. A card's answer goes through `Findings.answer/4`, which is
  one `ledger answer` call; the page then shows what the ledger now says.
  """
  use Phoenix.LiveView

  alias CodeGantryDaemon.{Findings, Records, Semaphore}

  @tick_ms 5_000

  @impl true
  def mount(_params, _session, socket) do
    if connected?(socket) do
      Phoenix.PubSub.subscribe(CodeGantryDaemon.PubSub, Findings.topic())
      :timer.send_interval(@tick_ms, :tick)
    end

    {:ok, socket |> assign(projects: Findings.all(), notice: nil) |> read_hosts()}
  end

  @impl true
  def handle_info({:findings, projects}, socket), do: {:noreply, assign(socket, projects: projects)}
  def handle_info(:tick, socket), do: {:noreply, read_hosts(socket)}

  @impl true
  def handle_event("answer", %{"finding" => id, "config" => config, "disposition" => disposition} = params, socket) do
    case Findings.answer(config, id, disposition, text: params["text"], target: params["target"]) do
      {:ok, finding} -> {:noreply, assign(socket, notice: "#{finding["id"]}: #{disposition}")}
      {:error, why} -> {:noreply, assign(socket, notice: why)}
    end
  end

  def handle_event("refresh", _params, socket) do
    Findings.refresh()
    {:noreply, socket |> read_hosts() |> assign(notice: nil)}
  end

  defp read_hosts(socket) do
    assign(socket, rows: Enum.sort_by(Records.all(), &{&1.origin, &1.name}), holds: Semaphore.all(), now: DateTime.utc_now())
  end

  @impl true
  def render(assigns) do
    ~H"""
    <h1>CodeGantry</h1>

    <h2>Bays</h2>
    <table>
      <tr><th>host</th><th>repository</th><th>bay</th><th>project</th><th>state</th><th>detail</th><th>since</th></tr>
      <tr :for={row <- @rows} class={if stale?(row, @now), do: "stale", else: ""}>
        <td>{row.origin}</td>
        <td>{row.repo || "-"}</td>
        <td>{row.name}</td>
        <td>{row.project || "-"}</td>
        <td class={"state-#{row.state}"}>{row.state}</td>
        <td class="detail">{row.detail || "-"}</td>
        <td>{since(row.since, @now)}</td>
      </tr>
    </table>
    <p :if={@rows == []} class="empty">no bays recorded</p>

    <h2>Held</h2>
    <p :if={@holds == []} class="empty">nothing held</p>
    <table :if={@holds != []}>
      <tr><th>semaphore</th><th>held by</th><th>host</th><th>waiting</th></tr>
      <tr :for={{name, entries} <- Enum.group_by(@holds, & &1.name) |> Enum.sort()}>
        <% [holder | waiting] = Enum.sort_by(entries, &{&1.at, &1.ref}) %>
        <td>{name}</td>
        <td>{holder.label}</td>
        <td>{holder.origin}</td>
        <td class="detail">{Enum.map_join(waiting, ", ", & &1.label)}</td>
      </tr>
    </table>

    <h2>For a person <button phx-click="refresh">read again</button></h2>
    <p :if={@notice} class="notice">{@notice}</p>
    <div :for={project <- @projects}>
      <h3>{project.project} <small class="empty">read {since(project.read_at, @now)}</small></h3>
      <p :if={project.error} class="error">{project.error}</p>
      <p :if={project.findings == [] and !project.error} class="empty">nothing waiting</p>
      <div :for={f <- project.findings} class="card" id={"finding-#{f["id"]}"}>
        <div class="meta">
          {f["id"]} · on {Enum.join(f["keys"] || [], ", ")} · by {f["by"]}<span :if={f["subject"]}> · {f["subject"]}</span><span :if={f["opened_at"]}> · opened {f["opened_at"]}</span>
        </div>
        <div class="claim">{f["claim"]}</div>
        <div :if={f["total"]} class="claim"><em>total:</em> {f["total"]}</div>
        <form id={"answer-#{f["id"]}"} phx-submit="answer">
          <input type="hidden" name="finding" value={f["id"]} />
          <input type="hidden" name="config" value={project.config} />
          <input type="text" name="text" placeholder="text: the sentence a fold carries, the debt entry, or why a person must decide" />
          <input type="text" name="target" placeholder="target key (fold, debt)" size="14" />
          <button type="submit" name="disposition" value="fold">fold</button>
          <button type="submit" name="disposition" value="discard">discard</button>
          <button type="submit" name="disposition" value="debt">debt</button>
          <button type="submit" name="disposition" value="raise">raise</button>
        </form>
      </div>
    </div>
    """
  end

  @stale_after_seconds 600

  defp stale?(row, now), do: DateTime.diff(now, row.since) > @stale_after_seconds

  defp since(at, now) do
    seconds = DateTime.diff(now, at)

    cond do
      seconds < 90 -> "#{seconds}s ago"
      seconds < 5400 -> "#{div(seconds, 60)}m ago"
      true -> "#{Float.round(seconds / 3600, 1)}h ago"
    end
  end
end
