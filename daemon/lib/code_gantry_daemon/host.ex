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
        code_branch: "elixir-daemon",            # optional; what the daemon picks its own code up from (default: the checkout's branch)
        pickup_seconds: 900,                     # optional; the fallback tick when no nudge came. 0 never ticks
        address: "192.0.2.10",                 # optional; how other hosts reach this one (default: the hostname)
        peers: ["192.0.2.11"],                 # optional; the other hosts' addresses
        command: ["uv", "run", "code-gantry"],   # optional; what runs the CLI
        bays: [
          [name: "bay1", offset: 100],
          [name: "bay2", offset: 200]
        ]
      ]
  """

  defstruct [:origin, :code_gantry, :primary, :config, :branch, :code_branch, :address,
             pickup_seconds: 900, bays: [], peers: [], command: ["uv", "run", "code-gantry"]]

  @node_base "code_gantry_daemon"

  @doc """
  This daemon's node name, long rather than short, because a short name
  cannot be reached from another machine. The address comes from the host
  file, so nothing tracked names a host.
  """
  def node_name(%__MODULE__{address: address}), do: :"#{@node_base}@#{address}"

  @doc """
  Whether a node that has just connected is another daemon.

  Every verb `bin/daemon` speaks starts a throwaway node of its own, and
  those must not be taken for peers: treating one as a join merges Mnesia
  schemas with something about to vanish and sets off a pickup on every
  command a person types. Decided by the node's base name rather than by
  the host file's `peers:`, because a host that names no peers still
  accepts the dial of one that does, and must recognise it when it lands.
  """
  def daemon_node?(node) do
    node |> Atom.to_string() |> String.split("@") |> hd() == @node_base
  end

  @doc "The other hosts' node names, this one never among them."
  def peer_nodes(%__MODULE__{address: address, peers: peers}) do
    peers
    |> Enum.reject(&(&1 == address))
    |> Enum.map(&:"#{@node_base}@#{&1}")
  end

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
        %{name: Keyword.fetch!(bay, :name), offset: Keyword.fetch!(bay, :offset)}
      end)

    %__MODULE__{
      origin: Keyword.fetch!(terms, :origin),
      code_gantry: Path.expand(Keyword.fetch!(terms, :code_gantry)),
      primary: Path.expand(Keyword.fetch!(terms, :primary)),
      config: Keyword.fetch!(terms, :config),
      command: Keyword.get(terms, :command, ["uv", "run", "code-gantry"]),
      branch: Keyword.get(terms, :branch),
      code_branch: Keyword.get(terms, :code_branch) || checkout_branch(Keyword.fetch!(terms, :code_gantry)),
      pickup_seconds: Keyword.get(terms, :pickup_seconds, 900),
      address: Keyword.get(terms, :address) || hostname(),
      peers: Keyword.get(terms, :peers, []),
      bays: bays
    }
  end

  defp hostname do
    {:ok, name} = :inet.gethostname()
    to_string(name)
  end

  defp checkout_branch(dir) do
    case System.cmd("git", ["rev-parse", "--abbrev-ref", "HEAD"], cd: Path.expand(dir), stderr_to_stdout: true) do
      {out, 0} -> String.trim(out)
      _ -> "main"
    end
  end

  @doc "Where a bay's checkout is: beside the primary copy, named `<repo>-<bay>`."
  def bay_dir(%__MODULE__{primary: primary}, %{name: name}) do
    Path.join(Path.dirname(primary), "#{Path.basename(primary)}-#{name}")
  end

  @doc "The config a bay works: its own when placed with one, else the host's."
  def bay_config(host, bay), do: Path.join(bay_dir(host, bay), Map.get(bay, :config) || host.config)

  @doc "The project a bay works, as its config's directory is named: `docs/technical_debt/code_gantry.yaml` is `technical_debt`."
  def project_of(host, bay), do: (Map.get(bay, :config) || host.config) |> Path.dirname() |> Path.basename()

  @doc "The environment every command the daemon runs is given."
  def env(%__MODULE__{origin: origin}) do
    [{"CODE_GANTRY_ORIGIN", origin}]
  end
end
