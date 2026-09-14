defmodule CodeGantryDaemon.DashboardTest do
  @moduledoc """
  The dashboard against a fake CLI: what it shows is what `ledger waiting
  --json` answers, and every action is one CLI call, so a person's click
  writes the same event the CLI would.
  """
  use ExUnit.Case, async: false

  import Phoenix.ConnTest
  import Phoenix.LiveViewTest

  alias CodeGantryDaemon.{Host, Status, Waiting, Web}

  @endpoint CodeGantryDaemon.Web.Endpoint

  setup do
    root = Path.join(System.tmp_dir!(), "cgw-#{System.os_time(:microsecond)}-#{System.unique_integer([:positive])}")
    File.rm_rf!(root)
    on_exit(fn -> File.rm_rf!(root) end)
    state = Path.join(root, "state")
    primary = Path.join(root, "repo")
    bay = Path.join(root, "repo-bay1")
    File.mkdir_p!(Path.join([bay, "docs", "p"]))
    File.mkdir_p!(Path.join([bay, "docs", "general"]))
    File.write!(Path.join([bay, "docs", "general", "code_gantry.yaml"]), "project_branch: general\n")
    File.mkdir_p!(Path.join([bay, "docs", "p", ".code_gantry", "runs"]))
    File.write!(Path.join([bay, "docs", "p", ".code_gantry", "runs", "code_gantry.yaml"]), "not a project\n")
    File.mkdir_p!(state)

    File.write!(Path.join(root, "waiting.json"), """
    [
      {"id": "p.006", "kind": "item", "title": "Delete the columns", "text": "They are unread.", "keys": ["p.006"],
       "since": null, "subject": null, "total": null, "recommendation": null, "thread": []},
      {"id": "f1", "kind": "finding", "title": "callers", "text": "Two callers remain, both admin-only.", "keys": ["p.004"],
       "since": "2026-09-13T10:00:00+00:00", "subject": "callers", "total": null,
       "recommendation": {"says": "two callers", "anchors": ["app/x.rb:12"], "checked": "both admin-only",
                          "recommend": {"disposition": "discard", "text": "duplicate of p.002"}, "would_write": null},
       "thread": [{"kind": "recommended", "by": "claude -p", "at": "2026-09-13T11:00:00+00:00",
                   "card": {"recommend": {"disposition": "discard", "text": "duplicate of p.002"}}},
                  {"kind": "asked", "by": "mack", "at": "2026-09-13T11:05:00+00:00", "text": "which caller?"}]}
    ]
    """)

    fake = Path.join(root, "fake-cli")
    File.write!(fake, """
    #!/usr/bin/env bash
    echo "argv: $*" >> "#{root}/calls"
    case "$1 $2" in
      "ledger waiting") case "$*" in *docs/p/*) cat "#{root}/waiting.json" ;; *) echo "[]" ;; esac ;;
      "ledger answer"|"ledger accept") echo '{"about": "'"$3"'", "disposition": "discard", "applied": ["discard f1"]}'; echo "[]" > "#{root}/waiting.json" ;;
      "ledger move"|"ledger land"|"ledger strike"|"plan edit") echo '{"done": true}' ;;
      "ledger ask") echo '{"id": "'"$3"'"}' ;;
      "plan sections") echo '[{"key": "g.001", "kind": "document", "title": "General debt", "parent": null, "depth": 0}, {"key": "g.002", "kind": "section", "title": "Inherited", "parent": "g.001", "depth": 1}]' ;;
      "plan add") echo "g.009" ;;
      "ledger thing") echo '{"kind": "item", "item": {"key": "p.006", "kind": "item", "title": "Delete the columns", "body": "They are unread.", "owner": "human", "marks": [], "state": {"state": "open", "question": null}}, "ancestors": [{"title": "Demo plan"}, {"title": "Later"}], "children": [], "findings": [], "recommendation": null, "thread": [], "history": [{"seq": 1, "at": "2026-09-13T10:00:00+00:00", "kind": "node.upserted", "actor": "mack", "run_id": null, "origin": "web-host", "sha": null, "summary": "Delete the columns"}, {"seq": 9, "at": "2026-09-13T11:00:00+00:00", "kind": "claimed", "actor": null, "run_id": "r1", "origin": "web-host", "sha": null, "summary": ""}]}' ;;
      "ledger show") case "$*" in *docs/p/*) echo '[{"key": "p.004", "title": "Add the new route", "owner": "pipeline", "state": {"state": "open"}}, {"key": "p.008", "title": "A stray N+1", "owner": "pipeline", "state": {"state": "open"}}]' ;; *) echo "[]" ;; esac ;;
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
    start_supervised!({Registry, keys: :unique, name: CodeGantryDaemon.Registry})
    start_supervised!({DynamicSupervisor, name: CodeGantryDaemon.Bays, strategy: :one_for_one})
    # Built first, as `Application.children/1` builds it: the endpoint's
    # spec is what starts the dependencies' applications, PubSub's among
    # them, and the tests run with none started.
    web = Web.child_spec(host)
    start_supervised!({Phoenix.PubSub, name: CodeGantryDaemon.PubSub})
    start_supervised!({Waiting, host})
    # The first read is under way when the server answers; a page reads
    # the table, so wait for the reading the tests are about.
    :ok = Waiting.refresh()
    start_supervised!(web)
    %{root: root, host: host, bay: bay}
  end

  defp calls(root), do: File.read!(Path.join(root, "calls"))

  test "what is waiting is read through the ledger's JSON face, for the placed project", %{root: root} do
    {:ok, _view, html} = live(build_conn(), "/")
    assert html =~ "Delete the columns" and html =~ "Two callers remain, both admin-only."
    assert calls(root) =~ ~r/argv: ledger waiting --json --config \S+repo-bay1\/docs\/p\/code_gantry.yaml/
  end

  test "a finding's card shows its recommendation, its thread, and the four dispositions", %{} do
    {:ok, view, html} = live(build_conn(), "/")
    assert html =~ "both admin-only" and html =~ "duplicate of p.002" and html =~ "which caller?"
    assert has_element?(view, "form#accept-f1 button", "accept: discard")
    for disposition <- ~w(amend discard debt raise) do
      assert has_element?(view, "form#answer-f1 button[value=#{disposition}]")
    end
  end

  test "accepting the card is one ledger accept call", %{root: root} do
    {:ok, view, _html} = live(build_conn(), "/")
    view |> form("form#accept-f1") |> render_submit()
    assert calls(root) =~ ~r/argv: ledger accept f1 --json --config \S+docs\/p\/code_gantry.yaml/
    refute render(view) =~ "Two callers remain"
  end

  test "an item is landed, struck, or handed to the fleet", %{root: root} do
    {:ok, view, _html} = live(build_conn(), "/")
    view |> form("form[id=\'land-p.006\']", %{"sha" => "abc123"}) |> render_submit()
    assert calls(root) =~ ~r/argv: ledger land p.006 abc123 --config /
    view |> form("form[id=\'strike-p.006\']", %{"text" => "not doing it"}) |> render_submit()
    assert calls(root) =~ ~r/argv: ledger strike p.006 not doing it --config /
    view |> form("form[id=\'fleet-p.006\']") |> render_submit()
    assert calls(root) =~ ~r/argv: plan edit p.006 --owner pipeline --config /
  end

  test "a question and a move are one call each, the move to a section of another project", %{root: root, bay: bay} do
    {:ok, view, html} = live(build_conn(), "/")
    # The other project is found in the checkout, its sections are the
    # choices, indented by depth; the run's own artifacts are not a project.
    assert html =~ ~s(<optgroup label="general">)
    assert html =~ ~s|<option value="#{bay}/docs/general/code_gantry.yaml\|g.002">\u00a0\u00a0Inherited (g.002)</option>|
    refute html =~ ~s(label="runs")
    view |> form("form#ask-f1", %{"text" => "and the other one?"}) |> render_submit()
    assert calls(root) =~ ~r/argv: ledger ask f1 --text and the other one\? --json --config /
    view |> form("form[id='move-p.006']", %{"to" => "#{bay}/docs/general/code_gantry.yaml|g.002"}) |> render_submit()
    assert calls(root) =~ ~r/argv: ledger move p.006 --to \S+docs\/general\/code_gantry.yaml --json --under g.002 --config /
    # A new section titled in the box is made under the chosen one first.
    view |> form("form[id='move-p.006']", %{"to" => "#{bay}/docs/general/code_gantry.yaml|g.001", "new_section" => "Later"}) |> render_submit()
    assert calls(root) =~ ~r/argv: plan add --under g.001 --kind section --title Later --config \S+docs\/general\/code_gantry.yaml/
    assert calls(root) =~ ~r/argv: ledger move p.006 --to \S+docs\/general\/code_gantry.yaml --json --under g.009 --config /
  end

  test "investigate asks the daemon for an idle bay on the project", %{} do
    {:ok, view, _html} = live(build_conn(), "/")
    assert has_element?(view, "form#investigate-f1 button", "investigate")
    html = view |> form("form#investigate-f1") |> render_submit()
    # No bay process is running in this test, so the daemon says so rather than starting anything.
    assert html =~ "no idle bay on p"
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

  test "the endpoint's spec puts back the OTP applications a pruned code path lost", %{host: host} do
    ebin = :code.lib_dir(:public_key) |> Path.join("ebin")
    assert true == :code.del_path(String.to_charlist(ebin))
    assert :code.lib_dir(:public_key) == {:error, :bad_name}
    _ = Web.child_spec(host)
    assert :code.lib_dir(:public_key) != {:error, :bad_name}
  end

  test "a page never waits on a read: the queue is the last reading, from a table", %{root: root} do
    # A read is a CLI call per project, seconds each; a mount that waited
    # on the server mid-read answered 500 after the call's timeout.
    File.write!(Path.join(root, "hold-waiting"), "")
    fake = Path.join(root, "fake-cli")
    slow = ~s|"ledger waiting") while [ -f "#{root}/hold-waiting" ]; do sleep 0.1; done; case|
    File.write!(fake, String.replace(File.read!(fake), ~s|"ledger waiting") case|, slow))
    task = Task.async(fn -> Waiting.refresh() end)
    {time, {:ok, _view, html}} = :timer.tc(fn -> live(build_conn(), "/") end)
    assert html =~ "Delete the columns"
    assert time < 2_000_000
    File.rm!(Path.join(root, "hold-waiting"))
    assert Task.await(task, 10_000) == :ok
  end

  test "two bays on one project are one section, read once", %{root: root, host: host} do
    File.mkdir_p!(Path.join([root, "repo-bay2", "docs", "p"]))
    :ok = CodeGantryDaemon.Placements.put(%{name: "bay2", offset: 200})
    File.rm!(Path.join(root, "calls"))
    :ok = Waiting.refresh()
    _ = host
    assert Enum.count(Waiting.all(), &(&1.project == "p")) == 1
    assert length(Regex.scan(~r/argv: ledger waiting --json --config \S+docs\/p\/code_gantry.yaml/, calls(root))) == 1
  end

  test "the queue keeps one refresh clock however many reads it does" do
    # Each read armed another sixty-second timer; every action a person
    # took added a reading a minute, and the page repainted every few seconds.
    pid = Process.whereis(Waiting)
    first = :sys.get_state(pid).timer
    assert is_reference(first)
    :ok = Waiting.refresh()
    :ok = Waiting.refresh()
    last = :sys.get_state(pid).timer
    assert last != first
    assert Process.read_timer(first) == false
    assert is_integer(Process.read_timer(last))
  end

  test "every project in the checkout is read, placed or not", %{root: root} do
    # The general project has no bay; what waits on a person there waits all the same.
    assert calls(root) =~ ~r/argv: ledger waiting --json --config \S+repo-bay1\/docs\/general\/code_gantry.yaml/
    assert Enum.map(Waiting.all(), & &1.project) == ["p", "general"]
  end

  test "the filter narrows by project, kind and text, and lives in the URL", %{} do
    {:ok, _view, html} = live(build_conn(), "/")
    assert html =~ "waiting-p.006" and html =~ "waiting-f1" and html =~ "2 shown"
    {:ok, _view, html} = live(build_conn(), "/?kind=item")
    assert html =~ "waiting-p.006" and not (html =~ "waiting-f1")
    {:ok, _view, html} = live(build_conn(), "/?q=callers")
    assert html =~ "waiting-f1" and not (html =~ "waiting-p.006")
    {:ok, _view, html} = live(build_conn(), "/?project=general")
    assert html =~ "0 shown" and not (html =~ "waiting-p.006")
    # Changing the form patches the URL, which is what a reload keeps.
    {:ok, view, _} = live(build_conn(), "/")
    view |> form("form#filter", %{"kind" => "finding"}) |> render_change()
    assert_patch(view, "/?kind=finding")
    refute render(view) =~ "waiting-p.006"
  end

  test "each project shows how much the fleet can draw, and lists it on request", %{root: root} do
    {:ok, view, html} = live(build_conn(), "/")
    assert calls(root) =~ ~r/argv: ledger show --drawable --json --config \S+docs\/p\/code_gantry.yaml/
    assert html =~ "2 drawable" and html =~ "0 drawable"
    refute html =~ "Add the new route"
    html = view |> element("button.linkish[phx-value-project=p]") |> render_click()
    assert html =~ "<code>p.004</code></a> Add the new route" and html =~ "A stray N+1"
    html = view |> element("button.linkish[phx-value-project=p]") |> render_click()
    refute html =~ "Add the new route"
  end

  test "a thing reads like a ticket, and every action is on it", %{root: root} do
    {:ok, view, html} = live(build_conn(), "/thing?project=p&id=p.006")
    assert calls(root) =~ ~r/argv: ledger thing p.006 --json --config \S+docs\/p\/code_gantry.yaml/
    assert html =~ "<h1>Delete the columns</h1>" and html =~ "They are unread." and html =~ "under Demo plan › Later"
    assert html =~ "node.upserted" and html =~ "claimed"
    view |> form("form#strike", %{"text" => "never doing this"}) |> render_submit()
    assert calls(root) =~ ~r/argv: ledger strike p.006 never doing this --config /
    view |> form("form#edit", %{"title" => "Keep the columns", "body" => "Decided: they stay.\n\nBecause."}) |> render_submit()
    assert calls(root) =~ ~r/argv: plan edit p.006 --title Keep the columns --body-file (\S+)/
    [_, path] = Regex.run(~r/--body-file (\S+)/, calls(root))
    assert File.read!(path) == "Decided: they stay.\n\nBecause."
    # The cards and the drawable list link here.
    {:ok, _view, home} = live(build_conn(), "/")
    assert home =~ ~s(<a href="/thing?project=p&amp;id=p.006">p.006</a>)
    # A finding's keys link to the items it is about.
    assert home =~ ~s(<a href="/thing?project=p&amp;id=p.004">p.004</a>)
  end

  test "before the first reading there is nothing, not an error" do
    stop_supervised!(Waiting)
    assert Waiting.all() == []
  end

  test "a bay row carries the verbs for its state, and they reach the daemon", %{root: root, host: host} do
    Status.put("bay1", :finished, "20260913-000000-bay1", "p")
    {:ok, view, html} = live(build_conn(), "/")
    assert html =~ ~s(phx-value-bay="bay1" phx-value-verb="run")
    refute html =~ ~s(phx-value-bay="bay1" phx-value-verb="kill")
    # No Bay process is running for bay1 here, so the daemon says so.
    html = view |> element(~s(button[phx-value-bay="bay1"][phx-value-verb="run"])) |> render_click()
    assert html =~ "no bay named bay1"
    # With a Bay process, run starts one: the fake CLI records it.
    {:ok, _} = CodeGantryDaemon.Application.start_bay(host, hd(host.bays))
    html = view |> element(~s(button[phx-value-bay="bay1"][phx-value-verb="run"])) |> render_click()
    assert html =~ ~r/bay1: run \d{8}-\d{6}-bay1 started|bay1 is running/
    wait = fn f -> Enum.find_value(1..50, fn _ -> f.() || (Process.sleep(100) && nil) end) end
    assert wait.(fn -> calls(root) =~ ~r/argv: run / end), "the bay never started a run"
    # And a project line offers hold and wake.
    assert has_element?(view, ~s(button[phx-value-project="p"][phx-value-verb="hold"]))
    assert has_element?(view, ~s(button[phx-value-project="p"][phx-value-verb="wake"]))
  end

  test "every bay on every host is a row", %{host: host} do
    Status.put("bay1", :running, "20260913-000000-bay1", Host.project_of(host, hd(host.bays)))
    {:ok, _view, html} = live(build_conn(), "/")
    assert html =~ "web-host" and html =~ "bay1" and html =~ "running" and html =~ "20260913-000000-bay1"
  end
end
