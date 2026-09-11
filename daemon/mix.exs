defmodule CodeGantryDaemon.MixProject do
  use Mix.Project

  def project do
    [
      app: :code_gantry_daemon,
      version: "0.1.0",
      elixir: "~> 1.14",
      start_permanent: Mix.env() == :prod,
      # The tests start the pieces they need against a fake CLI; the
      # application itself would read the host file.
      aliases: [test: "test --no-start"],
      deps: []
    ]
  end

  def application do
    [
      extra_applications: [:logger],
      mod: {CodeGantryDaemon.Application, []}
    ]
  end
end
