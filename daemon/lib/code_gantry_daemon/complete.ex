defmodule CodeGantryDaemon.Complete do
  @moduledoc """
  Which projects a run has found nothing left to draw in, one file per
  project in the state directory, so the next start leaves them idle
  rather than paying a planner call per bay to be told again. A run that
  exits 0 is that verdict — the CLI's contract — and the mark is cleared
  by a person asking for a run (`retry`, `place`, `wake`) or by an action
  that puts work back in reach (an item handed to the fleet, a thing
  moved in), because the daemon cannot see the ledger change.
  """

  alias CodeGantryDaemon.Host

  def dir, do: Path.join(Host.state_dir(), "complete")

  def mark(project, run_id) do
    File.mkdir_p!(dir())
    File.write!(path(project), "#{run_id} #{DateTime.utc_now() |> DateTime.truncate(:second) |> DateTime.to_iso8601()}\n")
    :ok
  end

  def clear(project) do
    File.rm(path(project))
    :ok
  end

  def complete?(project), do: File.exists?(path(project))

  @doc "The run and the time that found the project complete, or nil."
  def since(project) do
    case File.read(path(project)) do
      {:ok, text} -> String.trim(text)
      _ -> nil
    end
  end

  defp path(project), do: Path.join(dir(), project)
end
