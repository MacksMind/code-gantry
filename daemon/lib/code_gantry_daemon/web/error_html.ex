defmodule CodeGantryDaemon.Web.ErrorHTML do
  @moduledoc "What an error renders as: the status line, nothing styled."
  def render(template, _assigns), do: Phoenix.Controller.status_message_from_template(template)
end
