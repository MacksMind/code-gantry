defmodule CodeGantryDaemon.Status do
  @moduledoc """
  What the daemon knows right now, as one plain-text file a person or
  `bin/daemon status` can read: one line per bay. Rewritten
  on every change through a temporary sibling, so a reader never sees half
  a file.
  """
  use Agent

  alias CodeGantryDaemon.Host

  def start_link(host), do: Agent.start_link(fn -> %{host: host, rows: %{}} end, name: __MODULE__)

  def put(name, state, detail) do
    Agent.update(__MODULE__, fn s ->
      s = put_in(s.rows[name], {state, detail, DateTime.utc_now()})
      write(s)
      s
    end)
  end

  def path, do: Path.join(Host.state_dir(), "status")

  @doc "The host this daemon runs, as loaded at start."
  def host, do: Agent.get(__MODULE__, & &1.host)

  defp write(%{host: host, rows: rows}) do
    lines =
      [
        "origin #{host.origin}  pid #{System.pid()}  written #{DateTime.utc_now() |> DateTime.truncate(:second) |> DateTime.to_iso8601()}"
      ] ++
        Enum.map(Enum.sort_by(rows, fn {k, _} -> to_string(k) end), fn {name, {state, detail, at}} ->
          "#{name} #{state} #{detail || "-"} since #{DateTime.truncate(at, :second) |> DateTime.to_iso8601()}"
        end)

    tmp = path() <> ".tmp"
    File.write!(tmp, Enum.join(lines, "\n") <> "\n")
    File.rename!(tmp, path())
  end
end
