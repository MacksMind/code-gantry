defmodule CodeGantryDaemon.Host do
  @moduledoc """
  The host file: `~/.config/code_gantry/host.exs`, evaluated to a keyword
  list. It names this machine and what it holds, which is why it lives
  outside every checkout and is never tracked.

      [
        origin: "spark",
        code_gantry: "/home/you/projects/code-gantry",
        primary: "/home/you/projects/app/acme_app",
        config: "docs/technical_debt/code_gantry.yaml",
        branch: "technical-debt",                # optional; what a new bay starts on
        command: ["uv", "run", "code-gantry"],   # optional; what runs the CLI
        bays: [
          [name: "bay1", offset: 100],
          [name: "bay2", offset: 200, scope: ["td.010"]]
        ]
      ]
  """

  defstruct [:origin, :code_gantry, :primary, :config, :branch, bays: [], command: ["uv", "run", "code-gantry"]]

  def path, do: Path.join([System.user_home!(), ".config", "code_gantry", "host.exs"])

  def state_dir do
    System.get_env("CODE_GANTRY_DAEMON_STATE") ||
      Path.join([System.user_home!(), ".local", "state", "code_gantry", "daemon"])
  end

  def load!, do: load!(path())

  def load!(file) do
    {terms, _} = Code.eval_file(file)

    bays =
      Enum.map(Keyword.fetch!(terms, :bays), fn bay ->
        %{
          name: Keyword.fetch!(bay, :name),
          offset: Keyword.fetch!(bay, :offset),
          scope: Keyword.get(bay, :scope, [])
        }
      end)

    %__MODULE__{
      origin: Keyword.fetch!(terms, :origin),
      code_gantry: Path.expand(Keyword.fetch!(terms, :code_gantry)),
      primary: Path.expand(Keyword.fetch!(terms, :primary)),
      config: Keyword.fetch!(terms, :config),
      command: Keyword.get(terms, :command, ["uv", "run", "code-gantry"]),
      branch: Keyword.get(terms, :branch),
      bays: bays
    }
  end

  @doc "Where a bay's checkout is: beside the primary copy, named `<repo>-<bay>`."
  def bay_dir(%__MODULE__{primary: primary}, %{name: name}) do
    Path.join(Path.dirname(primary), "#{Path.basename(primary)}-#{name}")
  end

  def bay_config(host, bay), do: Path.join(bay_dir(host, bay), host.config)

  @doc "The environment every command the daemon runs is given."
  def env(%__MODULE__{origin: origin}) do
    [{"CODE_GANTRY_ORIGIN", origin}]
  end
end
