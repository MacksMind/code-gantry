defmodule CodeGantryDaemon.Pickup do
  @moduledoc """
  How a landing to code-gantry reaches this daemon: on a nudge from the
  host that already has it, or failing that on a clock, fetch the
  checkout's branch; if origin is ahead and the checkout is clean,
  fast-forward; compile `daemon/` and load the changed modules into this VM;
  and when the Python side changed, pause every running bay so its next
  run starts from the new code. Nothing here moves a checkout that has
  local changes or has diverged — those are reported and left alone.

  **What it acts on is the checkout moving, not the fetch bringing
  something.** Those are the same event on a host that only ever receives
  code, and different on the host where the code is written: there the
  commits are already in the checkout and origin is never ahead, so a
  pickup that asked only "is origin ahead" found nothing to do and left
  that host's bays running code from before the change — for hours,
  measured, while every other host had moved on. The commit this daemon
  last looked at is kept in its state directory, and anything between that
  and the checkout's head is what it acts on, however it got there.
  """
  use GenServer
  require Logger

  alias CodeGantryDaemon.{Bay, Command, Mesh, Status}

  def start_link(host), do: GenServer.start_link(__MODULE__, host, name: __MODULE__)

  @impl true
  def init(host) do
    # The bays this daemon is about to start will run the code that is in
    # the checkout now, so that is what it has last looked at. Seeding it
    # here rather than leaving yesterday's mark is what stops a restart
    # pausing every bay over a change they already have.
    mark(host, head(host.code_gantry))
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
    look(host) <> reconciled(host)
  end

  defp look(host) do
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
      new != old and not ancestor?(dir, old, new) ->
        Status.put(:code, :held, "#{short(old)}: diverged from origin at #{short(new)}")
        "code: held at #{short(old)}: diverged from origin at #{short(new)}"

      new != old ->
        {_, 0} = Command.run(["git", "merge", "-q", "--ff-only", new], dir, [])
        act(host, dir, "#{short(old)} -> #{short(new)}", old)

      true ->
        act(host, dir, "at #{short(old)}", nil)
    end
  end

  # Everything between the commit this daemon last acted on and the one the
  # checkout holds now — whether the fetch above brought it or a person
  # committed it here. The two are the same on a host that only receives
  # code and different on the host where it is written, and only this reads
  # both.
  defp act(host, dir, how, merged_from) do
    now = head(dir)
    # What this daemon last acted on, or — when it has no mark, which is a
    # daemon that has never looked — whatever the fetch just brought in.
    # Not knowing what was last seen is no reason to ignore what has
    # plainly just arrived.
    seen = seen(host) || merged_from

    parts =
      if seen in [nil, now] do
        []
      else
        {files, 0} = Command.run(["git", "diff", "--name-only", seen, now], dir, [])
        changed = String.split(files, "\n", trim: true)
        elixir_part(host, changed) ++ python_part(host, changed)
      end

    failed = Enum.any?(parts, &String.contains?(&1, "failed"))
    # Marked only when the work it stands for is done, so a compile that
    # failed is looked at again rather than skipped as already seen.
    if not failed, do: mark(host, now)

    if failed,
      do: Status.put(:code, :failed, "#{short(now)} on disk; running #{short(seen)}"),
      else: Status.put(:code, :ok, short(now))

    "code: #{how}" <> Enum.map_join(parts, "", &("; " <> &1))
  end

  # The commit this daemon last acted on, in its state directory rather
  # than in this process: a restart of the daemon is not a reason to pause
  # every bay, and neither is a restart of this GenServer.
  defp seen(host) do
    case File.read(mark_path(host)) do
      {:ok, sha} -> String.trim(sha)
      _ -> nil
    end
  end

  defp mark(host, sha) do
    _ = host
    File.mkdir_p!(CodeGantryDaemon.Host.state_dir())
    File.write!(mark_path(host), sha <> "\n")
  end

  defp mark_path(_host), do: Path.join(CodeGantryDaemon.Host.state_dir(), "picked_up")

  @doc "Compile and load the checkout as it is, fetching nothing. For a local test; never nudges."
  def reload(host) do
    dir = host.code_gantry
    mark = if dirty?(dir), do: "+dirty", else: ""
    line = "code: local #{short(head(dir))}#{mark}" <> Enum.map_join(load_daemon(host), "", &("; " <> &1))
    Status.put(:code, :ok, "local #{short(head(dir))}#{mark}")
    line <> reconciled(host)
  end

  # Every time the pickup looks, not only when it loads. Loading a module
  # starts no process, so a version that declares a new child has it only
  # once the running tree is brought up to match — and a change to the
  # pickup itself takes effect a pickup later, so a load that carries this
  # code cannot be the load that acts on it. Asking every time is what
  # closes that gap, and what repairs a child that is missing for any
  # other reason. It is a few calls against the supervisor when the tree
  # is already right, which is almost always.
  defp reconciled(host) do
    case CodeGantryDaemon.Application.reconcile(host) do
      [] -> ""
      started -> "; started #{Enum.map_join(started, ", ", &inspect/1)}"
    end
  end

  # -- the Elixir side ------------------------------------------------------

  defp elixir_part(host, changed) do
    if Enum.any?(changed, &String.starts_with?(&1, "daemon/")),
      do: load_daemon(host, "daemon/mix.lock" in changed),
      else: []
  end

  # The lock moving is the one sign a dependency changed, and fetching is
  # a network call, so it is done only then; a compile on a lock that
  # moved without a fetch fails, and is reported as that.
  defp load_daemon(host, fetch_deps \\ false) do
    daemon = Path.join(host.code_gantry, "daemon")
    env = [{"MIX_ENV", "prod"}]

    fetched =
      if fetch_deps do
        case Command.run(["mix", "deps.get"], daemon, env) do
          {_, 0} -> ["daemon: deps fetched"]
          {out, status} -> ["daemon: deps.get failed (#{status}): #{out |> String.trim() |> String.slice(0, 300)}"]
        end
      else
        []
      end

    case Command.run(["mix", "compile"], daemon, env) do
      {out, 0} ->
        own = own_app(daemon)

        # Every application on the path, so a dependency's modules are
        # found when first called; only the daemon's own purged and
        # loaded, because a dependency's processes run inside its modules
        # and a purge kills what is still in them.
        loaded =
          for ebin <- Path.wildcard(Path.join(daemon, "_build/prod/lib/*/ebin")),
              app <- Path.wildcard(Path.join(ebin, "*.app")),
              Code.prepend_path(ebin),
              Path.basename(app, ".app") == own,
              mod <- modules_of(app),
              reduce: 0 do
            n ->
              :code.soft_purge(mod)
              case :code.load_file(mod) do
                {:module, ^mod} -> n + 1
                _ -> n
              end
          end

        _ = out
        fetched ++ ["daemon: #{loaded} module(s) loaded"]

      {out, status} ->
        fetched ++ ["daemon: compile failed (#{status}): #{out |> String.trim() |> String.slice(0, 300)}"]
    end
  end

  # The application `mix.exs` names, read from the file: the daemon has
  # no Mix at runtime to ask.
  defp own_app(daemon) do
    case Regex.run(~r/app:\s*:(\w+)/, File.read!(Path.join(daemon, "mix.exs"))) do
      [_, app] -> app
      _ -> "code_gantry_daemon"
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
