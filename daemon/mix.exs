defmodule CodeGantryDaemon.MixProject do
  use Mix.Project

  def project do
    [
      app: :code_gantry_daemon,
      version: "0.1.0",
      # Phoenix wants 1.15; `.tool-versions` pins 1.18.4 on every host.
      elixir: "~> 1.15",
      start_permanent: Mix.env() == :prod,
      # The tests start the pieces they need against a fake CLI; the
      # application itself would read the host file.
      aliases: [test: "test --no-start"],
      # The dashboard's, built once by the pickup's compile; only the
      # daemon's own modules are hot-loaded.
      deps: [
        {:phoenix, "~> 1.8"},
        {:phoenix_live_view, "~> 1.2"},
        {:phoenix_html, "~> 4.3"},
        {:phoenix_pubsub, "~> 2.3"},
        {:bandit, "~> 1.12"},
        {:jason, "~> 1.4"},
        # What LiveView's test helpers parse rendered HTML with.
        {:lazy_html, ">= 0.1.0", only: :test}
      ]
    ]
  end

  def application do
    [
      # :mnesia holds the bay records every host shares.
      extra_applications: [:logger, :mnesia],
      mod: {CodeGantryDaemon.Application, []}
    ]
  end
end
