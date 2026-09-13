defmodule CodeGantryDaemon.Web do
  @moduledoc """
  The dashboard: a Phoenix LiveView page this daemon serves on the host
  file's `address` and `dashboard_port`, over the same network the mesh
  uses. One row per bay across all hosts, and the findings that need a
  person with their dispositions as buttons.

  The endpoint's configuration is written from the host file here, before
  it starts, rather than in a config file: the daemon has one input, and
  nothing tracked names a host. The network is the boundary — the address
  is a Tailscale one — so origins are not checked. `dashboard_port: 0`
  starts the endpoint without a listener, which is what the tests want.
  """

  alias CodeGantryDaemon.Host

  @dependencies [:phoenix_pubsub, :phoenix, :phoenix_live_view, :phoenix_html, :bandit, :jason]

  @doc """
  The endpoint's child spec, its configuration taken from the host.

  The dependencies' applications are started here as well, because this
  is evaluated wherever the daemon's children are — at boot, and by the
  pickup's reconcile on a VM that started before the dependencies
  existed, where an application the VM did not start with is simply
  unavailable until something starts it. Evaluated while the child list
  is built, so it runs before the PubSub child that needs it.
  """
  def child_spec(%Host{} = host) do
    for app <- @dependencies, do: {:ok, _} = Application.ensure_all_started(app)
    Application.put_env(:code_gantry_daemon, CodeGantryDaemon.Web.Endpoint, config(host))
    CodeGantryDaemon.Web.Endpoint.child_spec([])
  end

  defp config(host) do
    secret = secret(host)
    # `Map.get`, not the field: a daemon that took this code on a hot
    # load holds a host struct built by the module from before the field
    # existed, and the first reconcile after the load is what starts this.
    port = Map.get(host, :dashboard_port) || 4040

    [
      adapter: Bandit.PhoenixAdapter,
      server: port != 0,
      http: [ip: ip(host), port: port],
      url: [host: host.address || "localhost"],
      check_origin: false,
      secret_key_base: secret,
      live_view: [signing_salt: binary_part(secret, 0, 16)],
      pubsub_server: CodeGantryDaemon.PubSub,
      render_errors: [formats: [html: CodeGantryDaemon.Web.ErrorHTML], layout: false]
    ]
  end

  # The address the host file names, which may be a name; the loopback
  # when it names nothing this machine can resolve.
  defp ip(host) do
    with address when is_binary(address) <- Map.get(host, :address),
         {:ok, ip} <- :inet.getaddr(String.to_charlist(address), :inet) do
      ip
    else
      _ -> {127, 0, 0, 1}
    end
  end

  # Stable across restarts, so a page open through one is not signed out
  # by it; derived from the one credential the daemon holds.
  defp secret(_host) do
    cookie = Path.join(Host.state_dir(), "cookie")
    seed = case File.read(cookie), do: ({:ok, text} -> text; _ -> "no cookie")
    :crypto.hash(:sha512, "dashboard " <> seed) |> Base.encode64()
  end
end
