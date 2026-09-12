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

  @doc "Every bay of this host: the host file's, then the placed ones."
  def all(host) do
    seeded = Enum.map(host.bays, & &1.name)
    host.bays ++ Enum.reject(load(), &(&1.name in seeded))
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
    base = %{name: Keyword.fetch!(terms, :name), offset: Keyword.fetch!(terms, :offset)}
    case Keyword.get(terms, :config) do
      nil -> base
      config -> Map.put(base, :config, config)
    end
  end

  defp write(bays) do
    File.mkdir_p!(Host.state_dir())

    text =
      "[\n" <>
        Enum.map_join(bays, ",\n", fn b ->
          "  [name: #{inspect(b.name)}, offset: #{b.offset}" <>
            if(Map.get(b, :config), do: ", config: #{inspect(b.config)}", else: "") <> "]"
        end) <> "\n]\n"

    tmp = path() <> ".tmp"
    File.write!(tmp, text)
    File.rename!(tmp, path())
  end
end
