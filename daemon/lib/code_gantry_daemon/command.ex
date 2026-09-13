defmodule CodeGantryDaemon.Command do
  @moduledoc """
  Running a program as a port with its output streamed to a log file, and
  the exit status delivered to the owner. `uv run code-gantry ...` is run
  from the code-gantry checkout so the pinned environment is the one used.
  """

  @doc "Start `argv` in `cwd`; output lines go to `log_path`. Returns the port."
  def start(argv, cwd, env, log_path) do
    File.mkdir_p!(Path.dirname(log_path))
    {:ok, log} = File.open(log_path, [:append, :utf8])

    port =
      Port.open({:spawn_executable, executable(hd(argv), cwd)}, [
        :binary,
        :exit_status,
        :stderr_to_stdout,
        {:line, 65_536},
        {:args, tl(argv)},
        {:cd, cwd},
        {:env, Enum.map(env, fn {k, v} -> {String.to_charlist(k), String.to_charlist(v)} end)}
      ])

    {port, log}
  end

  @doc """
  Stop the process a port is running, and everything it started.

  Closing the port is not stopping the program. The port's own child is a
  launcher — `uv run code-gantry` — and the run is its child, so closing
  the port drops the pipe and leaves both alive: measured, with three of
  four runs outliving the daemon that started them and having to be killed
  by hand. The tree is walked and the leaves are signalled first, so a
  parent is not left waiting on a child nobody is going to reap.

  `TERM` first, because a run that is asked to stop closes its semaphore
  and its presence on the way out and a killed one leaves both to the
  kernel. `KILL` after the grace period for whatever ignored it.
  """
  def stop_tree(port, grace_ms \\ 5_000) when is_port(port) do
    case Port.info(port, :os_pid) do
      {:os_pid, pid} ->
        signal(pid, "TERM")
        Process.sleep(grace_ms)
        signal(pid, "KILL")
        :ok

      _ ->
        :ok
    end
  end

  defp signal(pid, name) do
    for child <- children(pid), do: signal(child, name)
    System.cmd("kill", ["-#{name}", to_string(pid)], stderr_to_stdout: true)
  catch
    _, _ -> :ok
  end

  defp children(pid) do
    case System.cmd("pgrep", ["-P", to_string(pid)], stderr_to_stdout: true) do
      {out, 0} ->
        out
        |> String.split("\n", trim: true)
        |> Enum.map(&Integer.parse(String.trim(&1)))
        |> Enum.flat_map(fn
          {n, _} -> [n]
          :error -> []
        end)

      _ ->
        []
    end
  catch
    _, _ -> []
  end

  @doc "Run `argv` to completion in `cwd`; returns `{output, status}`."
  def run(argv, cwd, env) do
    [program | args] = argv
    path = executable(program, cwd)

    System.cmd(path, args, cd: cwd, env: env, stderr_to_stdout: true)
  rescue
    e in ErlangError ->
      # Spawning failed before the program ran: the path, its interpreter or
      # the directory. Answered like a program that could not start.
      {"could not start #{Enum.join(argv, " ")} in #{cwd}: #{inspect(e.original)} " <>
         "(the path, its first line's interpreter, or the directory is missing)", 127}
  end

  # A program named with a path is taken from `cwd`; a bare name from PATH.
  defp executable(program, cwd) do
    cond do
      String.contains?(program, "/") -> Path.expand(program, cwd)
      true -> System.find_executable(program) || raise "no #{program} on PATH"
    end
  end

  def code_gantry(host, args), do: host.command ++ args
end
