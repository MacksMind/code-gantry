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
