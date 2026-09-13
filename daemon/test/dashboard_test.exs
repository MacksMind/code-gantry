defmodule CodeGantryDaemon.DashboardTest do
  @moduledoc """
  The dashboard against a fake CLI: the findings it shows are what
  `ledger findings --json` answers, and an answer is one `ledger answer`
  call, so a person's click writes the same event the CLI would.
  """
  use ExUnit.Case, async: false

  import Phoenix.ConnTest
  import Phoenix.LiveViewTest

  alias CodeGantryDaemon.{Findings, Host, Status, Web}

  @endpoint CodeGantryDaemon.Web.Endpoint

  setup do
    root = Path.join(System.tmp_dir!(), "cgw-#{System.os_time(:microsecond)}-#{System.unique_integer([:positive])}")
    File.rm_rf!(root)
    on_exit(fn -> File.rm_rf!(root) end)
    state = Path.join(root, "state")
    primary = Path.join(root, "repo")
    File.mkdir_p!(Path.join([root, "repo-bay1", "docs", "p"]))
    File.mkdir_p!(state)

    File.write!(Path.join(root, "findings.json"), """
    [
      {"id": "f1", "keys": ["p.004"], "by": "planner", "claim": "Two callers remain, both admin-only.",
       "needs": "human", "status": "open", "total": null, "opened_at": "2026-09-13T10:00:00+00:00",
       "stage_id": null, "run_id": "r1", "subject": "callers", "disposition": null, "answer_text": null}
    ]
    """)

    fake = Path.join(root, "fake-cli")
    File.write!(fake, """
    #!/usr/bin/env bash
    echo "argv: $*" >> "#{root}/calls"
    case "$1 $2" in
      "ledger findings") cat "#{root}/findings.json" ;;
      "ledger answer") echo '{"id": "'"$3"'", "status": "discarded", "disposition": "'"$4"'"}'; echo "[]" > "#{root}/findings.json" ;;
    esac
    exit 0
    """)
    File.chmod!(fake, 0o755)

    host = %Host{
      origin: "web-host",
      code_gantry: root,
      primary: primary,
      config: "docs/p/code_gantry.yaml",
      branch: "work",
      code_branch: "work",
      pickup_seconds: 0,
      dashboard_port: 0,
      command: [fake],
      bays: [%{name: "bay1", offset: 100}]
    }

    System.put_env("CODE_GANTRY_DAEMON_STATE", state)
    on_exit(fn -> System.delete_env("CODE_GANTRY_DAEMON_STATE") end)
    {:ok, _} = Status.start_link(host)
    # Built first, as `Application.children/1` builds it: the endpoint's
    # spec is what starts the dependencies' applications, PubSub's among
    # them, and the tests run with none started.
    web = Web.child_spec(host)
    start_supervised!({Phoenix.PubSub, name: CodeGantryDaemon.PubSub})
    start_supervised!({Findings, host})
    start_supervised!(web)
    %{root: root, host: host}
  end

  defp calls(root), do: File.read!(Path.join(root, "calls"))

  test "findings waiting on a person are cards with the four dispositions", %{root: root} do
    {:ok, view, html} = live(build_conn(), "/")
    assert html =~ "Two callers remain, both admin-only."
    assert html =~ "p.004"
    for disposition <- ~w(fold discard debt raise) do
      assert has_element?(view, "form[phx-submit=answer] button[value=#{disposition}]")
    end
    # The findings came from the ledger's JSON face, for the placed project.
    assert calls(root) =~ ~r/argv: ledger findings --for-human --json --config \S+repo-bay1\/docs\/p\/code_gantry.yaml/
  end

  test "an answer is one ledger answer call, and the cards are read again", %{root: root} do
    {:ok, view, _html} = live(build_conn(), "/")

    view
    |> form("form[phx-submit=answer][id='answer-f1']", %{"text" => ""})
    |> render_submit(%{"disposition" => "discard"})

    assert calls(root) =~ ~r/argv: ledger answer f1 discard --json --config \S+repo-bay1\/docs\/p\/code_gantry.yaml/
    refute render(view) =~ "Two callers remain"
  end

  test "the endpoint's spec puts back the OTP applications a pruned code path lost", %{host: host} do
    # Mix prunes the code path at boot to what the project declared then;
    # a daemon that took its first dependencies on a hot load has no
    # `public_key` on its path, and Phoenix will not start without it.
    ebin = :code.lib_dir(:public_key) |> Path.join("ebin")
    assert true == :code.del_path(String.to_charlist(ebin))
    assert :code.lib_dir(:public_key) == {:error, :bad_name}
    _ = Web.child_spec(host)
    assert :code.lib_dir(:public_key) |> to_string() |> String.ends_with?("/ebin") == false
    assert :code.lib_dir(:public_key) != {:error, :bad_name}
  end

  test "a loopback listener is added only when the endpoint is bound elsewhere", %{host: host} do
    host = %{host | dashboard_port: 4321}
    assert Web.loopback_spec(host, {100, 64, 0, 1}).id == CodeGantryDaemon.Web.Loopback
    assert Web.loopback_spec(host, {127, 0, 0, 1}) == nil
    assert Web.loopback_spec(host, {0, 0, 0, 0}) == nil
    assert Web.loopback_spec(%{host | dashboard_port: 0}, {100, 64, 0, 1}) == nil
  end

  test "the loopback listener serves the dashboard", %{host: host} do
    {:ok, probe} = :gen_tcp.listen(0, [:binary, active: false, ip: {127, 0, 0, 1}])
    {:ok, {_, port}} = :inet.sockname(probe)
    :gen_tcp.close(probe)
    host = %{host | dashboard_port: port}
    start_supervised!(Web.loopback_spec(host, {100, 64, 0, 1}))
    {:ok, socket} = :gen_tcp.connect({127, 0, 0, 1}, port, [:binary, active: false])
    :ok = :gen_tcp.send(socket, "GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
    {:ok, response} = :gen_tcp.recv(socket, 0, 5_000)
    assert response =~ "200 OK"
    :gen_tcp.close(socket)
  end

  test "every bay on every host is a row", %{host: host} do
    Status.put("bay1", :running, "20260913-000000-bay1", Host.project_of(host, hd(host.bays)))
    {:ok, _view, html} = live(build_conn(), "/")
    assert html =~ "web-host" and html =~ "bay1" and html =~ "running" and html =~ "20260913-000000-bay1"
  end
end
