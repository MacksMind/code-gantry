defmodule CodeGantryDaemon.Web.ThingLive do
  @moduledoc """
  One thing, whole, read like a ticket: the item or finding at the top
  as it was written, where it sits, its state; then everything that
  happened to it, oldest first — claims, landings, findings opened and
  answered, cards and questions; then every action a person can take on
  it. `ledger thing --json` is what it reads; `Waiting.act/3` is what
  its actions write through, the same as a card's.
  """
  use Phoenix.LiveView

  alias CodeGantryDaemon.Waiting

  @impl true
  def mount(%{"project" => project, "id" => id}, _session, socket) do
    socket = assign(socket, project: project, id: id, notice: nil, config: nil, thing: nil, error: nil, projects: [])
    {:ok, load(socket)}
  end

  def mount(_params, _session, socket) do
    {:ok, assign(socket, project: nil, id: nil, notice: nil, config: nil, thing: nil, error: "a thing is named by ?project=…&id=…", projects: [])}
  end

  @impl true
  def handle_event("act", %{"action" => action} = params, socket) do
    params = Map.put_new(params, "about", socket.assigns.id)

    case Waiting.act(socket.assigns.config, action, params) do
      {:ok, line} when is_binary(line) -> {:noreply, socket |> assign(notice: line) |> load()}
      {:ok, _} -> {:noreply, socket |> assign(notice: "#{socket.assigns.id}: #{action}") |> load()}
      {:error, why} -> {:noreply, assign(socket, notice: why)}
    end
  end

  defp load(%{assigns: %{project: project, id: id}} = socket) do
    case Enum.find(Waiting.all(), &(&1.project == project)) do
      nil ->
        assign(socket, error: "no project named #{project} on this host")

      entry ->
        case Waiting.thing(entry.config, id) do
          {:ok, thing} -> assign(socket, config: entry.config, thing: thing, error: nil, projects: entry.projects)
          {:error, why} -> assign(socket, config: entry.config, error: why)
        end
    end
  end

  @impl true
  def render(assigns) do
    ~H"""
    <p><a href="/">← every bay and everything waiting</a></p>
    <p :if={@error} class="error">{@error}</p>
    <p :if={@notice} class="notice">{@notice}</p>
    <div :if={@thing} class="thing">
      <.head thing={@thing} project={@project} />
      <h2>History</h2>
      <ol class="history">
        <li :for={h <- @thing["history"]}>
          <span class="meta">{String.slice(h["at"], 0, 19)} · {h["kind"]} · {h["actor"] || h["run_id"] || h["origin"]}<span :if={h["sha"]}> · <code>{String.slice(h["sha"], 0, 12)}</code></span></span>
          <div :if={h["summary"] != ""} class="claim">{h["summary"]}</div>
        </li>
      </ol>
      <h2>Act on it</h2>
      <.actions thing={@thing} project={@project} config={@config} projects={@projects} id={@id} />
    </div>
    """
  end

  attr :thing, :map, required: true
  attr :project, :string, required: true

  defp head(%{thing: %{"kind" => "finding"}} = assigns) do
    ~H"""
    <% f = @thing["finding"] %>
    <div class="meta">{@project} · finding {f["id"]} · {f["status"]} · needs {f["needs"]} · by {f["by"]}<span :if={f["subject"]}> · {f["subject"]}</span><span :if={f["opened_at"]}> · opened {f["opened_at"]}</span></div>
    <h1>{f["subject"] || String.slice(f["claim"], 0, 80)}</h1>
    <div class="claim">{f["claim"]}</div>
    <div :if={f["total"]} class="claim"><em>total:</em> {f["total"]}</div>
    <p :if={@thing["items"] != []} class="meta">
      about: <span :for={item <- @thing["items"]}><a href={"/thing?project=#{@project}&id=#{item["key"]}"}>{item["key"]}</a> {item["title"]} ({item["state"]["state"]}) </span>
    </p>
    <.recommendation :if={@thing["recommendation"]} rec={@thing["recommendation"]["recommend"] || %{}} />
    """
  end

  defp head(assigns) do
    ~H"""
    <% item = @thing["item"] %>
    <div class="meta">
      {@project} · {item["kind"]} {item["key"]} · {item["state"]["state"]} · {item["owner"]}-owned
      <span :if={@thing["ancestors"] != []}> · under {Enum.map_join(@thing["ancestors"], " › ", & &1["title"])}</span>
    </div>
    <h1>{item["title"]}</h1>
    <div :if={item["body"] != ""} class="claim">{item["body"]}</div>
    <p :for={mark <- item["marks"]} class="mark">{mark}</p>
    <p :if={item["state"]["question"]} class="claim"><em>held on:</em> {item["state"]["question"]}</p>
    <.recommendation :if={@thing["recommendation"]} rec={@thing["recommendation"]["recommend"] || %{}} />
    """
  end

  attr :rec, :map, required: true

  defp recommendation(assigns) do
    ~H"""
    <div class="recommendation">
      <div><em>a card recommends:</em> <strong>{@rec["disposition"]}</strong><span :if={@rec["text"]}> — {@rec["text"]}</span></div>
      <form id="accept" phx-submit="act">
        <input type="hidden" name="action" value="accept" />
        <button type="submit" class="accept">accept: {@rec["disposition"]}</button>
      </form>
    </div>
    """
  end

  attr :thing, :map, required: true
  attr :project, :string, required: true
  attr :config, :string, required: true
  attr :projects, :list, required: true
  attr :id, :string, required: true

  defp actions(%{thing: %{"kind" => "finding"}} = assigns) do
    ~H"""
    <form id="answer" phx-submit="act">
      <input type="hidden" name="action" value="answer" />
      <input type="text" name="text" placeholder="text: the sentence an amend writes, the debt entry, or why a person must decide" />
      <input type="text" name="target" placeholder="target key (amend, debt)" size="14" />
      <button type="submit" name="disposition" value="amend">amend</button>
      <button type="submit" name="disposition" value="discard">discard</button>
      <button type="submit" name="disposition" value="debt">debt</button>
      <button type="submit" name="disposition" value="raise">raise</button>
    </form>
    <.common id={@id} projects={@projects} kind="finding" />
    """
  end

  defp actions(assigns) do
    ~H"""
    <% item = @thing["item"] %>
    <form id="edit" phx-submit="act" class="edit">
      <input type="hidden" name="action" value="edit" />
      <input type="text" name="title" value={item["title"]} />
      <textarea name="body" rows="8">{item["body"]}</textarea>
      <button type="submit">save the item</button>
    </form>
    <div class="item-actions">
      <form id="land" phx-submit="act">
        <input type="hidden" name="action" value="land" />
        <input type="text" name="sha" placeholder="commit sha" size="14" />
        <button type="submit">landed</button>
      </form>
      <form id="strike" phx-submit="act">
        <input type="hidden" name="action" value="strike" />
        <input type="text" name="text" placeholder="why it is struck" />
        <button type="submit">strike</button>
      </form>
      <form :if={item["owner"] == "human"} id="fleet" phx-submit="act">
        <input type="hidden" name="action" value="fleet" />
        <button type="submit">to the fleet</button>
      </form>
      <form :if={item["owner"] == "pipeline"} id="person" phx-submit="act">
        <input type="hidden" name="action" value="person" />
        <button type="submit">to a person</button>
      </form>
    </div>
    <.common id={@id} projects={@projects} kind="item" />
    """
  end

  attr :id, :string, required: true
  attr :projects, :list, required: true
  attr :kind, :string, required: true

  defp common(assigns) do
    ~H"""
    <form id="investigate" phx-submit="act">
      <input type="hidden" name="action" value="investigate" />
      <button type="submit">investigate</button>
    </form>
    <form id="ask" phx-submit="act">
      <input type="hidden" name="action" value="ask" />
      <input type="text" name="text" placeholder="a question for the next investigation" />
      <button type="submit">ask</button>
    </form>
    <form :if={@projects != []} id="move" phx-submit="act">
      <input type="hidden" name="action" value="move_to" />
      <select name="to">
        <optgroup :for={p <- @projects} label={p.project}>
          <option :for={s <- p.sections} value={"#{p.config}|#{s["key"]}"}>{String.duplicate("  ", s["depth"] || 0)}{s["title"]} ({s["key"]})</option>
        </optgroup>
      </select>
      <input type="text" name="new_section" placeholder="or a new section under it, titled…" size="28" />
      <button type="submit">move</button>
    </form>
    """
  end
end
