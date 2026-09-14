defmodule CodeGantryDaemon.Web.Router do
  use Phoenix.Router
  import Phoenix.LiveView.Router

  pipeline :browser do
    plug :accepts, ["html"]
    plug :fetch_session
    plug :fetch_live_flash
    plug :put_root_layout, html: {CodeGantryDaemon.Web.Layouts, :root}
    plug :protect_from_forgery
    plug :put_secure_browser_headers
  end

  scope "/", CodeGantryDaemon.Web do
    pipe_through :browser
    live "/", DashboardLive
    live "/thing", ThingLive
  end
end
