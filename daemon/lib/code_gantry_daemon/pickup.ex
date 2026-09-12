defmodule CodeGantryDaemon.Pickup do
  @moduledoc """
  How a landing to code-gantry reaches this daemon: on a nudge from the
  host that already has it, or failing that on a clock, fetch the
  checkout's branch; if origin is ahead and the checkout is clean,
  fast-forward; compile `daemon/` and load the changed modules into this VM;
  and when the Python side changed, pause every running bay so its next
  run starts from the new code. Nothing here moves a checkout that has
  local changes or has diverged — those are reported and left alone.
  """
  use GenServer
  require Logger

  alias CodeGantryDaemon.{Bay, Command, Mesh, Status}

  def start_link(host), do: GenServer.start_link(__MODULE__, host, name: __MODULE__)

  @impl true
  def init(host) do
    if host.pickup_seconds > 0, do: Process.send_after(self(), :tick, host.pickup_seconds * 1000)
    {:ok, host}
  end

  @doc """
  A peer has taken new code and is telling this host to take it now. A
  cast, so the peer that has already done its own work never waits on
  this one's fetch.
  """
  def nudged, do: GenServer.cast(__MODULE__, :nudged)

  @impl true
  def handle_info(:tick, host) do
    Logger.info(take(host))
    Process.send_after(self(), :tick, host.pickup_seconds * 1000)
    {:noreply, host}
  end

  @impl true
  def handle_cast(:nudged, host) do
    Logger.info("nudged: " <> take(host))
    {:noreply, host}
  end

  @impl true
  def handle_call(:now, _from, host), do: {:reply, take(host), host}

  @doc """
  A pickup, and a nudge to the peers if it moved this checkout.

  Whether it moved is read from the checkout's head before and after,
  rather than from the line the pickup renders: the sha is a fact, and a
  sentence is a rendering that another case can render the same way. The
  nudge cannot storm, because a host only passes it on when its own head
  moved, and a host already holding the code moves nowhere.
  """
  def take(host) do
    before = head(host.code_gantry)
    line = tick(host)
    if head(host.code_gantry) != before, do: Mesh.nudge()
    line
  end

  @doc "One pickup, now. Answers a line saying what happened."
  def tick(host) do
    dir = host.code_gantry
    old = head(dir)

    cond do
      dirty?(dir) ->
        Status.put(:code, :held, "#{short(old)}: local changes")
        "code: held at #{short(old)}: local changes"

      true ->
        case Command.run(["git", "fetch", "-q", "origin", host.code_branch], dir, []) do
          {out, 0} -> after_fetch(host, dir, old, out)
          {out, _} ->
            Status.put(:code, :held, "#{short(old)}: fetch failed")
            "code: held at #{short(old)}: fetch failed: #{String.trim(out)}"
        end
    end
  end

  defp after_fetch(host, dir, old, _out) do
    new = rev(dir, "origin/#{host.code_branch}")

    cond do
      new == old ->
        Status.put(:code, :ok, short(old))
        "code: at #{short(old)}"

      not ancestor?(dir, old, new) ->
        Status.put(:code, :held, "#{short(old)}: diverged from origin at #{short(new)}")
        "code: held at #{short(old)}: diverged from origin at #{short(new)}"

      true ->
        {files, 0} = Command.run(["git", "diff", "--name-only", old, new], dir, [])
        changed = String.split(files, "\n", trim: true)
        {_, 0} = Command.run(["git", "merge", "-q", "--ff-only", new], dir, [])
        parts = [] ++ elixir_part(host, changed) ++ python_part(host, changed)
        line = "code: #{short(old)} -> #{short(new)}" <> Enum.map_join(parts, "", &("; " <> &1))

        if Enum.any?(parts, &String.contains?(&1, "failed")),
          do: Status.put(:code, :failed, "#{short(new)} on disk; running #{short(old)}"),
          else: Status.put(:code, :ok, short(new))

        line
    end
  end

  @doc "Compile and load the checkout as it is, fetching nothing. For a local test; never nudges."
  def reload(host) do
    dir = host.code_gantry
    mark = if dirty?(dir), do: "+dirty", else: ""
    line = "code: local #{short(head(dir))}#{mark}" <> Enum.map_join(load_daemon(host), "", &("; " <> &1))
    Status.put(:code, :ok, "local #{short(head(dir))}#{mark}")
    line
  end

  # -- the Elixir side ------------------------------------------------------

  defp elixir_part(host, changed) do
    if Enum.any?(changed, &String.starts_with?(&1, "daemon/")), do: load_daemon(host), else: []
  end

  defp load_daemon(host) do
    daemon = Path.join(host.code_gantry, "daemon")

    case Command.run(["mix", "compile"], daemon, [{"MIX_ENV", "prod"}]) do
      {out, 0} ->
        loaded =
          for ebin <- Path.wildcard(Path.join(daemon, "_build/prod/lib/*/ebin")),
              app <- Path.wildcard(Path.join(ebin, "*.app")),
              mod <- modules_of(app),
              reduce: 0 do
            n ->
              Code.prepend_path(ebin)
              :code.soft_purge(mod)
              case :code.load_file(mod) do
                {:module, ^mod} -> n + 1
                _ -> n
              end
          end

        _ = out
        # Loading a module does not start a process. A version that
        # declares a new child has it only once the running tree is
        # brought up to match, and that must happen here rather than
        # waiting for somebody to restart the host.
        started = CodeGantryDaemon.Application.reconcile(host)
        added = if started == [], do: "", else: ", started #{Enum.map_join(started, ", ", &inspect/1)}"
        ["daemon: #{loaded} module(s) loaded#{added}"]

      {out, status} ->
        ["daemon: compile failed (#{status}): #{out |> String.trim() |> String.slice(0, 300)}"]
    end
  end

  defp modules_of(app_file) do
    {:ok, [{:application, _, props}]} = :file.consult(String.to_charlist(app_file))
    Keyword.get(props, :modules, [])
  end

  # -- the Python side ------------------------------------------------------

  @python ~w(src/ prompts/ pyproject.toml uv.lock)

  defp python_part(host, changed) do
    if Enum.any?(changed, fn f -> Enum.any?(@python, &String.starts_with?(f, &1)) end) do
      pausing = for bay <- Bay.running(host), Bay.pause_for_pickup(bay) == :pausing, do: bay
      ["python: #{length(pausing)} bay(s) pausing"]
    else
      []
    end
  end

  # -- git ------------------------------------------------------------------

  defp dirty?(dir) do
    {out, 0} = Command.run(["git", "status", "--porcelain"], dir, [])
    String.trim(out) != ""
  end

  defp head(dir), do: rev(dir, "HEAD")

  defp rev(dir, ref) do
    {out, 0} = Command.run(["git", "rev-parse", ref], dir, [])
    String.trim(out)
  end

  defp ancestor?(dir, a, b) do
    {_, status} = Command.run(["git", "merge-base", "--is-ancestor", a, b], dir, [])
    status == 0
  end

  defp short(sha), do: String.slice(sha, 0, 12)
end
