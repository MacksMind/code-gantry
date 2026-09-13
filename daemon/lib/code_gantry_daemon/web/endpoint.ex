defmodule CodeGantryDaemon.Web.Endpoint do
  use Phoenix.Endpoint, otp_app: :code_gantry_daemon

  @session_options [store: :cookie, key: "_code_gantry_dashboard", signing_salt: "dashboard-session", same_site: "Lax"]

  socket "/live", Phoenix.LiveView.Socket, websocket: [connect_info: [session: @session_options]]

  # The browser side of LiveView, served from the dependencies as shipped:
  # no bundler, no node, nothing built.
  plug Plug.Static, at: "/assets/phoenix", from: {:phoenix, "priv/static"}, only: ~w(phoenix.min.js)
  plug Plug.Static, at: "/assets/live_view", from: {:phoenix_live_view, "priv/static"}, only: ~w(phoenix_live_view.min.js)

  plug Plug.Session, @session_options
  plug CodeGantryDaemon.Web.Router
end
