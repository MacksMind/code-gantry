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

    socket =
      socket
      |> assign(notice: nil, filter: %{"project" => "", "kind" => "", "q" => ""}, latest: Waiting.all(), listing: nil)
      |> stream_configure(:cards, dom_id: &"waiting-#{&1.id}")
      |> read_hosts()

    {:ok, socket}
  end

  # The filter lives in the URL, so a reload and a shared link keep it.
  @impl true
  def handle_params(params, _uri, socket) do
    filter = %{"project" => params["project"] || "", "kind" => params["kind"] || "", "q" => params["q"] || ""}
    {:noreply, socket |> assign(filter: filter) |> cards(socket.assigns.latest)}
  end

  @impl true
  def handle_info({:waiting, projects}, socket), do: {:noreply, socket |> assign(latest: projects) |> cards(projects)}
  def handle_info(:tick, socket), do: {:noreply, read_hosts(socket)}

  # The cards are a stream, and a reading is applied as a diff: a card
  # whose record changed is inserted again and patched in place, a card
  # that went is deleted, and a card that is the same is not touched at
  # all — a reset would remove and re-add every one, and a dropdown a
  # person is reading does not survive its element being replaced. A
  # page that repainted every card on every reading is where this
  # started. Nothing inside a card reads the clock, for the same reason.
  defp cards(socket, projects) do
    seen = Map.get(socket.assigns, :seen, %{})
    # A card is its record and the project it belongs to; the reading's
    # time and error are the page's, not the card's, or every card would
    # compare as changed at every reading.
    filter = socket.assigns.filter

    entries =
      for project <- projects,
          filter["project"] in ["", project.project],
          w <- project.waiting,
          filter["kind"] in ["", w["kind"]],
          matches?(w, filter["q"]),
          do: %{id: w["id"], w: w, project: Map.take(project, [:project, :config, :projects])}
    now = Map.new(entries, &{&1.id, &1})
    gone = for id <- Map.keys(seen), not Map.has_key?(now, id), do: id
    changed = for entry <- entries, Map.get(seen, entry.id) != entry, do: entry

    socket =
      socket
      |> assign(projects: Enum.map(projects, &Map.drop(&1, [:waiting])), cards: length(entries), seen: now)
      |> stream(:cards, changed)

    Enum.reduce(gone, socket, fn id, s -> stream_delete_by_dom_id(s, :cards, "waiting-#{id}") end)
  end

  defp matches?(_w, q) when q in [nil, ""], do: true

  defp matches?(w, q) do
    needle = String.downcase(q)
    Enum.any?([w["id"], w["title"], w["text"], w["subject"]], &(is_binary(&1) and String.contains?(String.downcase(&1), needle)))
  end

  @impl true
  def handle_event("act", %{"action" => action, "config" => config} = params, socket) do
    case Waiting.act(config, action, params) do
      {:ok, line} when is_binary(line) -> {:noreply, assign(socket, notice: line)}
      {:ok, _} -> {:noreply, assign(socket, notice: "#{params["about"]}: #{action}")}
      {:error, why} -> {:noreply, assign(socket, notice: why)}
    end
  end

  def handle_event("filter", params, socket) do
    query = for k <- ~w(project kind q), v = params[k], v not in [nil, ""], do: {k, v}
    {:noreply, push_patch(socket, to: "/?" <> URI.encode_query(query))}
  end

  # One of a project's lists — rework, drawn, pending, drawable — opened
  # and closed by its count.
  def handle_event("drawable", %{"project" => project} = params, socket) do
    which = {project, params["which"] || "drawable"}
    {:noreply, assign(socket, listing: if(socket.assigns.listing == which, do: nil, else: which))}
  end

  # A verb on one bay, reaching the daemon of the host the row belongs to.
  def handle_event("bay", %{"origin" => origin, "bay" => bay, "verb" => verb}, socket) when verb in ~w(run retry pause kill) do
    line = CodeGantryDaemon.Control.on(origin, String.to_existing_atom(verb), [bay])
    {:noreply, socket |> assign(notice: line) |> read_hosts()}
  end

  # A verb on a project: hold and wake speak to every peer themselves.
  def handle_event("project", %{"project" => project, "verb" => verb}, socket) when verb in ~w(hold wake) do
    line = apply(CodeGantryDaemon.Control, String.to_existing_atom(verb), [project])
    {:noreply, socket |> assign(notice: line) |> read_hosts()}
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
      <tr><th>host</th><th>repository</th><th>bay</th><th>project</th><th>state</th><th>detail</th><th>since</th><th></th></tr>
      <tr :for={row <- @rows} class={if stale?(row, @now), do: "stale", else: ""}>
        <td>{row.origin}</td>
        <td>{row.repo || "-"}</td>
        <td>{row.name}</td>
        <td>{row.project || "-"}</td>
        <td class={"state-#{row.state}"}>{row.state}</td>
        <td class="detail">{row.detail || "-"}</td>
        <td>{since(row.since, @now)}</td>
        <td class="controls">
          <button :for={verb <- verbs_for(row)} phx-click="bay" phx-value-origin={row.origin} phx-value-bay={row.name} phx-value-verb={verb} data-confirm={if verb == "kill", do: "Kill the run in #{row.origin}/#{row.name}?"}>{verb}</button>
        </td>
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
    <form id="filter" phx-change="filter" class="filter">
      <select name="project">
        <option value="" selected={@filter["project"] == ""}>every project</option>
        <option :for={p <- @projects} value={p.project} selected={@filter["project"] == p.project}>{p.project}</option>
      </select>
      <select name="kind">
        <option value="" selected={@filter["kind"] == ""}>items and findings</option>
        <option value="item" selected={@filter["kind"] == "item"}>items</option>
        <option value="finding" selected={@filter["kind"] == "finding"}>findings</option>
      </select>
      <input type="text" name="q" value={@filter["q"]} placeholder="text in the id, title or body" phx-debounce="300" />
      <span class="empty">{@cards} shown</span>
    </form>
    <p :if={@notice} class="notice">{@notice}</p>
    <div :for={project <- @projects}>
      <% nxt = Map.get(project, :next, %{rework: [], drawn: [], pending: []}) %>
      <p class="empty">
        {project.project}: read at {DateTime.truncate(project.read_at, :second) |> DateTime.to_time() |> Time.to_string()} UTC
        · a run does, in order:
        <button phx-click="drawable" phx-value-project={project.project} phx-value-which="rework" class="linkish">{length(nxt.rework)} to rework</button>,
        <button phx-click="drawable" phx-value-project={project.project} phx-value-which="drawn" class="linkish">{length(nxt.drawn)} drawn and waiting</button>,
        then draws from <button phx-click="drawable" phx-value-project={project.project} phx-value-which="drawable" class="linkish">{length(Map.get(project, :drawable, []))} drawable</button>;
        <button phx-click="drawable" phx-value-project={project.project} phx-value-which="pending" class="linkish">{length(nxt.pending)} candidate(s) pending</button>
        · <button phx-click="project" phx-value-project={project.project} phx-value-verb="wake">wake</button>
        <button phx-click="project" phx-value-project={project.project} phx-value-verb="hold" data-confirm={"Hold #{project.project}: stop every running bay on it at its seam, on every host?"}>hold</button><span :if={project.error} class="error"> — {project.error}</span>
      </p>
      <ul :if={@listing == {project.project, "rework"}} class="drawable">
        <li :for={r <- nxt.rework}><code>{r["stage_id"]}</code> on <.keys keys={r["landing"]["keys"] || []} project={project.project} /> — {r["reason"]}</li>
        <li :if={nxt.rework == []} class="empty">nothing to rework</li>
      </ul>
      <ul :if={@listing == {project.project, "drawn"}} class="drawable">
        <li :for={d <- nxt.drawn}><code>{d["stage_id"]}</code> on <.keys keys={d["keys"]} project={project.project} /> — drawn by {d["by_run"]}: {String.slice(d["fields"]["instruction"] || "", 0, 160)}</li>
        <li :if={nxt.drawn == []} class="empty">nothing drawn and waiting</li>
      </ul>
      <ul :if={@listing == {project.project, "drawable"}} class="drawable">
        <li :for={item <- Map.get(project, :drawable, [])}><a href={"/thing?project=#{project.project}&id=#{item["key"]}"}><code>{item["key"]}</code></a> {item["title"]}</li>
        <li :if={Map.get(project, :drawable, []) == []} class="empty">nothing the fleet can draw</li>
      </ul>
      <ul :if={@listing == {project.project, "pending"}} class="drawable">
        <li :for={c <- nxt.pending}><code>{c["stage_id"]}</code> on <.keys keys={c["landing"]["keys"] || []} project={project.project} /> — {c["branch"]} {String.slice(c["sha"], 0, 12)}</li>
        <li :if={nxt.pending == []} class="empty">no candidate pending</li>
      </ul>
    </div>
    <p :if={@cards == 0} class="empty">nothing waiting</p>
    <div id="cards" phx-update="stream">
      <.card :for={{dom_id, entry} <- @streams.cards} id={dom_id} w={entry.w} project={entry.project} />
    </div>
    """
  end

  attr :keys, :list, required: true
  attr :project, :string, required: true

  defp keys(assigns) do
    ~H"""
    <span :for={key <- @keys}><a href={"/thing?project=#{@project}&id=#{key}"}>{key}</a> </span>
    """
  end

  attr :id, :string, required: true
  attr :w, :map, required: true
  attr :project, :map, required: true

  defp card(assigns) do
    ~H"""
    <div class="card" id={@id}>
      <div class="meta">
        {@project.project} · <a href={"/thing?project=#{@project.project}&id=#{@w["id"]}"}>{@w["id"]}</a> · {@w["kind"]}<span :if={@w["kind"] == "finding" and @w["keys"] != []}> · on <span :for={key <- @w["keys"]}><a href={"/thing?project=#{@project.project}&id=#{key}"}>{key}</a> </span></span><span :if={@w["subject"]}> · {@w["subject"]}</span><span :if={@w["since"]}> · opened {@w["since"]}</span>
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
        <input type="text" name="text" placeholder="text: the sentence an amend writes, the debt entry, or why a person must decide" />
        <input type="text" name="target" placeholder="target key (amend, debt)" size="14" />
        <button type="submit" name="disposition" value="amend">amend</button>
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
        <input type="hidden" name="action" value="move_to" />
        <input type="hidden" name="about" value={@w["id"]} />
        <input type="hidden" name="config" value={@project.config} />
        <select name="to">
          <optgroup :for={p <- @project.projects} label={p.project}>
            <option :for={s <- p.sections} value={"#{p.config}|#{s["key"]}"}>{String.duplicate("\u00a0\u00a0", s["depth"] || 0)}{s["title"]} ({s["key"]})</option>
          </optgroup>
        </select>
        <input type="text" name="new_section" placeholder="or a new section under it, titled…" size="28" />
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

  # What a person can do to a bay in its state. `code` rows are the host's
  # code line, not a bay.
  defp verbs_for(%{name: "code"}), do: []
  defp verbs_for(%{state: state}) when state in ["running", "investigating", "winding_down", "pausing"], do: ~w(pause kill)
  defp verbs_for(%{state: state}) when state in ["paused", "escalated", "crashed", "killed"], do: ~w(retry)
  defp verbs_for(%{state: state}) when state in ["finished", "complete", "failed", "idle"], do: ~w(run)
  defp verbs_for(_), do: []

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
