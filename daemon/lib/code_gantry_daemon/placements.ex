defmodule CodeGantryDaemon.Placements do
  @moduledoc """
  Where work has been placed on this host: the bays a person added through
  `place`, kept beside the status file so the next start reads them back,
  on top of the bays the host file seeds. Orchestration state — nothing the
  ledger holds, and nothing lost if it goes but the placements themselves.
  """

  alias CodeGantryDaemon.Host

  def path, do: Path.join(Host.state_dir(), "placements.exs")

  @doc "The bays placed on this host after its host file, in the order placed."
  def load do
    case File.read(path()) do
      {:ok, text} ->
        {terms, _} = Code.eval_string(text)
        Enum.map(terms, &bay/1)

      _ ->
        []
    end
  end

  @doc """
  Every bay of this host: the host file's, then the placed ones. A
  placement that names a host-file bay overrides it — that is how a bay's
  scope changes without the host file changing.
  """
  def all(host) do
    placed = load()
    by_name = Map.new(placed, &{&1.name, &1})
    seeded = Enum.map(host.bays, &Map.get(by_name, &1.name, &1))
    names = Enum.map(host.bays, & &1.name)
    seeded ++ Enum.reject(placed, &(&1.name in names))
  end

  @doc "Remember a bay's scope, adding the placement if the host file seeded the bay."
  def set_scope(host, name, scope) do
    case Enum.find(all(host), &(&1.name == name)) do
      nil ->
        {:error, :no_such_bay}

      bay ->
        bay = %{bay | scope: scope}
        rest = Enum.reject(load(), &(&1.name == name))
        write(rest ++ [bay])
        {:ok, bay}
    end
  end

  @doc "Remember a placement. `{:error, :exists}` when the name is taken."
  def add(bay) do
    placed = load()

    if Enum.any?(placed, &(&1.name == bay.name)) do
      {:error, :exists}
    else
      write(placed ++ [bay])
      :ok
    end
  end

  defp bay(terms) do
    %{
      name: Keyword.fetch!(terms, :name),
      offset: Keyword.fetch!(terms, :offset),
      scope: Keyword.get(terms, :scope, [])
    }
  end

  defp write(bays) do
    File.mkdir_p!(Host.state_dir())

    text =
      "[\n" <>
        Enum.map_join(bays, ",\n", fn b ->
          "  [name: #{inspect(b.name)}, offset: #{b.offset}, scope: #{inspect(b.scope)}]"
        end) <> "\n]\n"

    tmp = path() <> ".tmp"
    File.write!(tmp, text)
    File.rename!(tmp, path())
  end
end
