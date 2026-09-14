defmodule CodeGantryDaemon.Web.Layouts do
  use Phoenix.Component

  def root(assigns) do
    ~H"""
    <!DOCTYPE html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="csrf-token" content={Plug.CSRFProtection.get_csrf_token()} />
        <title>CodeGantry</title>
        <style>
          body { font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; margin: 0; padding: 16px 24px; color: #1a1a1a; background: #fafaf8; }
          h1 { font-size: 18px; margin: 0 0 12px; }
          h2 { font-size: 15px; margin: 24px 0 8px; }
          table { border-collapse: collapse; width: 100%; }
          th, td { text-align: left; padding: 4px 10px 4px 0; border-bottom: 1px solid #e6e4df; vertical-align: top; white-space: nowrap; }
          th { font-weight: 600; color: #555; }
          td.detail { white-space: normal; }
          .state-running { color: #1f6f3f; } .state-escalated, .state-failed, .state-crashed { color: #a32d2d; } .state-paused { color: #8a6d00; }
          .stale { color: #999; }
          .card { border: 1px solid #ddd; border-radius: 6px; padding: 12px 14px; margin: 10px 0; background: #fff; max-width: 900px; }
          .card .meta { color: #666; font-size: 12px; margin-bottom: 6px; }
          .card .claim { white-space: pre-wrap; }
          .card form { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
          .card input[type=text] { flex: 1 1 320px; padding: 4px 6px; border: 1px solid #ccc; border-radius: 4px; }
          .card button { padding: 4px 10px; border: 1px solid #999; border-radius: 4px; background: #f4f4f2; cursor: pointer; }
          .card button:hover { background: #e8e8e4; }
          .error { color: #a32d2d; }
          .recommendation { border-left: 3px solid #1f6f3f; padding: 6px 10px; margin: 8px 0; background: #f3f8f4; }
          .recommendation button.accept { background: #1f6f3f; color: #fff; border-color: #1f6f3f; }
          .thread { margin: 8px 0; font-size: 13px; }
          .thread .entry { padding: 2px 0; }
          .item-actions { display: flex; flex-wrap: wrap; gap: 6px 18px; }
          .card form { margin-top: 8px; }
          .empty { color: #666; }
          td.controls button { padding: 1px 7px; margin-right: 4px; border: 1px solid #999; border-radius: 4px; background: #f4f4f2; cursor: pointer; font-size: 12px; }
          form.filter { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 6px 0 10px; }
          form.filter input[type=text] { flex: 1 1 260px; padding: 4px 6px; border: 1px solid #ccc; border-radius: 4px; }
          button.linkish { background: none; border: none; padding: 0; color: #1f4f8f; cursor: pointer; text-decoration: underline; font: inherit; }
          ul.drawable { margin: 0 0 8px 12px; padding-left: 12px; font-size: 13px; }
        </style>
        <script src="/assets/phoenix/phoenix.min.js"></script>
        <script src="/assets/live_view/phoenix_live_view.min.js"></script>
        <script>
          window.addEventListener("DOMContentLoaded", function () {
            var token = document.querySelector("meta[name='csrf-token']").getAttribute("content");
            var socket = new LiveView.LiveSocket("/live", Phoenix.Socket, {params: {_csrf_token: token}});
            socket.connect();
          });
        </script>
      </head>
      <body>
        {@inner_content}
      </body>
    </html>
    """
  end
end
