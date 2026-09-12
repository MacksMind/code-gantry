defmodule CodeGantryDaemon.Status do
  @moduledoc """
  What the daemon knows right now, as one plain-text file a person or
  `bin/daemon status` can read: one line per bay. Rewritten
  on every change through a temporary sibling, so a reader never sees half
  a file.
  """
  use Agent

  alias CodeGantryDaemon.{Host, Records}

  def start_link(host) do
    # This host's own table, since Status is what writes its rows.
    :ok = Records.start(host.origin)
    Agent.start_link(fn -> %{host: host, rows: %{}} end, name: __MODULE__)
  end

  def put(name, state, detail, project \\ nil) do
    Agent.update(__MODULE__, fn s ->
      s = put_in(s.rows[name], {state, detail, DateTime.utc_now(), project})
      write(s)

      Records.put(s.host.origin, to_string(name), %{
        repo: Path.basename(s.host.primary),
        project: project,
        state: to_string(state),
        detail: detail
      })

      s
    end)
  end

  def path, do: Path.join(Host.state_dir(), "status")

  @doc "The host this daemon runs, as loaded at start."
  def host, do: Agent.get(__MODULE__, & &1.host)

  @stale_after_seconds 600

  @doc """
  Every bay on every host that has joined, rendered. Read from the shared
  records rather than this host's own rows, so one daemon answers for the
  whole mesh. A host whose rows have stopped moving is marked rather than
  hidden: gone quiet and gone are different, and only one of them is
  visible from here.
  """
  def render_all do
    rows = Records.all()

    if rows == [] do
      "no bays recorded"
    else
      rows
      |> Enum.sort_by(&{&1.origin, &1.name})
      |> Enum.map(&row_line/1)
      |> Enum.join("\n")
    end
  end

  defp row_line(r) do
    age = DateTime.diff(DateTime.utc_now(), r.since)
    stale = if age > @stale_after_seconds, do: "  (stale #{age}s)", else: ""

    "#{r.origin} #{r.repo || "-"} #{r.name} #{r.project || "-"} #{r.state} " <>
      "#{r.detail || "-"} since #{DateTime.truncate(r.since, :second) |> DateTime.to_iso8601()}#{stale}"
  end

  defp write(%{host: host, rows: rows}) do
    lines =
      [
        "origin #{host.origin}  pid #{System.pid()}  written #{DateTime.utc_now() |> DateTime.truncate(:second) |> DateTime.to_iso8601()}"
      ] ++
        Enum.map(Enum.sort_by(rows, fn {k, _} -> to_string(k) end), fn {name, {state, detail, at, project}} ->
          "#{name} #{state} #{detail || "-"} since #{DateTime.truncate(at, :second) |> DateTime.to_iso8601()}" <>
            if(project, do: " #{project}", else: "")
        end)

    tmp = path() <> ".tmp"
    File.write!(tmp, Enum.join(lines, "\n") <> "\n")
    File.rename!(tmp, path())
  end
end
