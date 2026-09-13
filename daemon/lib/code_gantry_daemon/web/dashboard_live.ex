defmodule CodeGantryDaemon.Web.DashboardLive do
  @moduledoc """
  One page: every bay on every host, what each semaphore is holding, and
  what is waiting on a person as cards. A card carries the thing, the
  card an investigation attached with its recommendation as the one-click
  answer, the thread since, and the ways to act by hand: a finding's
  dispositions, an item's landing or strike or hand-over to the fleet, a
  question for the next investigation, and a move to another project.
  Every action goes through `Waiting.act/3`, one CLI call.
  """
  use Phoenix.LiveView

  alias CodeGantryDaemon.{Records, Semaphore, Waiting}

  @tick_ms 5_000

  @impl true
  def mount(_params, _session, socket) do
    if connected?(socket) do
      Phoenix.PubSub.subscribe(CodeGantryDaemon.PubSub, Waiting.topic())
      :timer.send_interval(@tick_ms, :tick)
    end

    {:ok, socket |> assign(projects: Waiting.all(), notice: nil) |> read_hosts()}
  end

  @impl true
  def handle_info({:waiting, projects}, socket), do: {:noreply, assign(socket, projects: projects)}
  def handle_info(:tick, socket), do: {:noreply, read_hosts(socket)}

  @impl true
  def handle_event("act", %{"action" => action, "config" => config} = params, socket) do
    case Waiting.act(config, action, params) do
      {:ok, line} when is_binary(line) -> {:noreply, assign(socket, notice: line)}
      {:ok, _} -> {:noreply, assign(socket, notice: "#{params["about"]}: #{action}")}
      {:error, why} -> {:noreply, assign(socket, notice: why)}
    end
  end

  def handle_event("refresh", _params, socket) do
    Waiting.refresh()
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

    <h2>Waiting on a person <button phx-click="refresh">read again</button></h2>
    <p :if={@notice} class="notice">{@notice}</p>
    <div :for={project <- @projects}>
      <h3>{project.project} <small class="empty">read {since(project.read_at, @now)}</small></h3>
      <p :if={project.error} class="error">{project.error}</p>
      <p :if={project.waiting == [] and !project.error} class="empty">nothing waiting</p>
      <.card :for={w <- project.waiting} w={w} project={project} />
    </div>
    """
  end

  attr :w, :map, required: true
  attr :project, :map, required: true

  defp card(assigns) do
    ~H"""
    <div class="card" id={"waiting-#{@w["id"]}"}>
      <div class="meta">
        {@w["id"]} · {@w["kind"]}<span :if={@w["kind"] == "finding" and @w["keys"] != []}> · on {Enum.join(@w["keys"], ", ")}</span><span :if={@w["subject"]}> · {@w["subject"]}</span><span :if={@w["since"]}> · opened {@w["since"]}</span>
      </div>
      <div class="title"><strong>{@w["title"]}</strong></div>
      <div :if={@w["text"] != "" and @w["text"] != @w["title"]} class="claim">{@w["text"]}</div>
      <div :if={@w["total"]} class="claim"><em>total:</em> {@w["total"]}</div>

      <.recommendation :if={@w["recommendation"]} w={@w} project={@project} rec={@w["recommendation"]} />

      <div :if={@w["thread"] != []} class="thread">
        <div :for={entry <- @w["thread"]} class={"entry entry-#{entry["kind"]}"}>
          <span class="meta">{entry["kind"]} · {entry["by"]} · {entry["at"]}</span>
          <span :if={entry["kind"] == "asked"}> {entry["text"]}</span>
          <span :if={entry["kind"] == "recommended"}> {rec_line(entry["card"])}</span>
        </div>
      </div>

      <form :if={@w["kind"] == "finding"} id={"answer-#{@w["id"]}"} phx-submit="act" class="actions">
        <input type="hidden" name="action" value="answer" />
        <input type="hidden" name="about" value={@w["id"]} />
        <input type="hidden" name="config" value={@project.config} />
        <input type="text" name="text" placeholder="text: the sentence a fold carries, the debt entry, or why a person must decide" />
        <input type="text" name="target" placeholder="target key (fold, debt)" size="14" />
        <button type="submit" name="disposition" value="fold">fold</button>
        <button type="submit" name="disposition" value="discard">discard</button>
        <button type="submit" name="disposition" value="debt">debt</button>
        <button type="submit" name="disposition" value="raise">raise</button>
      </form>

      <div :if={@w["kind"] == "item"} class="item-actions">
        <form id={"land-#{@w["id"]}"} phx-submit="act">
          <input type="hidden" name="action" value="land" />
          <input type="hidden" name="about" value={@w["id"]} />
          <input type="hidden" name="config" value={@project.config} />
          <input type="text" name="sha" placeholder="commit sha" size="14" />
          <button type="submit">landed</button>
        </form>
        <form id={"strike-#{@w["id"]}"} phx-submit="act">
          <input type="hidden" name="action" value="strike" />
          <input type="hidden" name="about" value={@w["id"]} />
          <input type="hidden" name="config" value={@project.config} />
          <input type="text" name="text" placeholder="why it is struck" />
          <button type="submit">strike</button>
        </form>
        <form id={"fleet-#{@w["id"]}"} phx-submit="act">
          <input type="hidden" name="action" value="fleet" />
          <input type="hidden" name="about" value={@w["id"]} />
          <input type="hidden" name="config" value={@project.config} />
          <button type="submit">to the fleet</button>
        </form>
      </div>

      <form id={"investigate-#{@w["id"]}"} phx-submit="act">
        <input type="hidden" name="action" value="investigate" />
        <input type="hidden" name="about" value={@w["id"]} />
        <input type="hidden" name="config" value={@project.config} />
        <button type="submit">investigate</button>
      </form>

      <form id={"ask-#{@w["id"]}"} phx-submit="act">
        <input type="hidden" name="action" value="ask" />
        <input type="hidden" name="about" value={@w["id"]} />
        <input type="hidden" name="config" value={@project.config} />
        <input type="text" name="text" placeholder="a question for the next investigation" />
        <button type="submit">ask</button>
      </form>

      <form :if={@project.projects != []} id={"move-#{@w["id"]}"} phx-submit="act">
        <input type="hidden" name="action" value="move" />
        <input type="hidden" name="about" value={@w["id"]} />
        <input type="hidden" name="config" value={@project.config} />
        <select name="to">
          <option :for={p <- @project.projects} value={p.config}>{p.project}</option>
        </select>
        <input :if={@w["kind"] == "item"} type="text" name="under" placeholder="section key there" size="14" />
        <button type="submit">move</button>
      </form>
    </div>
    """
  end

  attr :w, :map, required: true
  attr :project, :map, required: true
  attr :rec, :map, required: true

  # The card's recommendation, and the one click that takes it: `ledger
  # accept`, which applies whatever the card recommends as the events the
  # answer would have been.
  defp recommendation(assigns) do
    rec = assigns.rec["recommend"] || %{}
    assigns = assign(assigns, rec: rec)

    ~H"""
    <div class="recommendation">
      <div :if={@rec["says"]}><em>says:</em> {@rec["says"]}</div>
      <div :if={is_list(@rec["anchors"]) and @rec["anchors"] != []}><em>anchors to:</em> {Enum.join(@rec["anchors"], ", ")}</div>
      <div :if={@rec["checked"]}><em>checked:</em> {@rec["checked"]}</div>
      <div><em>recommend:</em> <strong>{@rec["disposition"]}</strong><span :if={@rec["text"]}> — {@rec["text"]}</span><span :if={@rec["target"]}> under {@rec["target"]}</span><span :if={@rec["to"]}> to {@rec["to"]}</span><span :if={@rec["sha"]}> at {@rec["sha"]}</span></div>
      <div :if={is_list(@rec["landings"]) and @rec["landings"] != []}><em>landings:</em> {Enum.map_join(@rec["landings"], ", ", &"#{&1["key"]} #{&1["sha"]}")}</div>
      <div :if={@rec["would_write"]}><em>would write:</em> {@rec["would_write"]}</div>
      <form id={"accept-#{@w["id"]}"} phx-submit="act">
        <input type="hidden" name="action" value="accept" />
        <input type="hidden" name="about" value={@w["id"]} />
        <input type="hidden" name="config" value={@project.config} />
        <button type="submit" class="accept">accept: {@rec["disposition"]}</button>
      </form>
    </div>
    """
  end

  defp rec_line(card) when is_map(card) do
    rec = card["recommend"] || %{}
    "#{rec["disposition"]}" <> if(rec["text"], do: " — #{rec["text"]}", else: "")
  end

  defp rec_line(_), do: ""

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
