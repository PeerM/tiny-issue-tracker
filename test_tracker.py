import io
import json

import pytest

import tracker as t
from tracker import TrackerError


@pytest.fixture
def conn(tmp_path):
    c = t.connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("ISSUES_DB", raising=False)
    path = tmp_path / "cli.db"
    assert t.main(["--db", str(path), "init"]) == 0
    return path


def run(db, capsys, *args, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    capsys.readouterr()
    code = t.main(["--db", str(db), *args])
    out, err = capsys.readouterr()
    return code, out, err


def jrun(db, capsys, *args, **kw):
    code, out, err = run(db, capsys, "--json", *args, **kw)
    assert code == 0, err
    return json.loads(out)


# ---------------------------------------------------------------- setup / find_db

def test_new_db_is_seeded(conn):
    assert [s["name"] for s in t.list_states(conn)] == ["Backlog", "Todo", "In Progress", "Review", "Done"]
    assert [s["position"] for s in t.list_states(conn)] == [0, 1, 2, 3, 4]
    names = {lab["name"] for lab in t.list_labels(conn)}
    assert {"needs-triage", "needs-info", "ready-for-agent", "ready-for-human", "wontfix"} <= names
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_reconnect_does_not_reseed(tmp_path):
    c = t.connect(tmp_path / "a.db")
    t.remove_state(c, "Review")
    c.close()
    c = t.connect(tmp_path / "a.db")
    assert len(t.list_states(c)) == 4
    c.close()


def test_find_db_resolution_order(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    sub = root / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    monkeypatch.delenv("ISSUES_DB", raising=False)
    # 4. fallback: ./issues.db in cwd
    assert t.find_db() == sub / "issues.db"
    # 3. nearest issues.db walking up
    (root / "issues.db").touch()
    assert t.find_db() == root / "issues.db"
    (root / "a" / "issues.db").touch()
    assert t.find_db() == root / "a" / "issues.db"
    # 2. env
    monkeypatch.setenv("ISSUES_DB", str(tmp_path / "env.db"))
    assert t.find_db() == tmp_path / "env.db"
    # 1. explicit
    assert t.find_db(str(tmp_path / "x.db")) == tmp_path / "x.db"


def test_connect_none_uses_find_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ISSUES_DB", str(tmp_path / "env.db"))
    t.connect().close()
    assert (tmp_path / "env.db").exists()


# ---------------------------------------------------------------- hierarchy

def test_valid_hierarchy(conn):
    saga = t.create_issue(conn, "S", type="saga")
    epic = t.create_issue(conn, "E", type="epic", parent_id=saga)
    story = t.create_issue(conn, "St", type="story", parent_id=epic)
    for parent in (saga, epic, story, None):
        t.create_issue(conn, "T", type="task", parent_id=parent)
        t.create_issue(conn, "B", type="bug", parent_id=parent)
    assert t.get_issue(conn, story)["parent"] == {"id": epic, "title": "E", "type": "epic"}
    assert [c["id"] for c in t.get_issue(conn, saga)["children"]][0] == epic


@pytest.mark.parametrize("child,parent", [
    ("saga", "saga"), ("epic", "epic"), ("epic", "story"), ("story", "saga"),
    ("story", "story"), ("task", "task"), ("bug", "bug"), ("story", "task"),
])
def test_invalid_parent_types(conn, child, parent):
    pid = t.create_issue(conn, "P", type=parent)
    with pytest.raises(TrackerError):
        t.create_issue(conn, "C", type=child, parent_id=pid)


def test_missing_parent_and_self_parent(conn):
    with pytest.raises(TrackerError):
        t.create_issue(conn, "x", type="task", parent_id=999)
    e = t.create_issue(conn, "E", type="epic")
    with pytest.raises(TrackerError):
        t.update_issue(conn, e, parent_id=e)


def test_type_change_revalidates_children(conn):
    saga = t.create_issue(conn, "S", type="saga")
    epic = t.create_issue(conn, "E", type="epic", parent_id=saga)
    story = t.create_issue(conn, "St", type="story", parent_id=epic)
    with pytest.raises(TrackerError):  # epic has a story child -> cannot become task
        t.update_issue(conn, epic, type="task")
    with pytest.raises(TrackerError):  # saga has an epic child
        t.update_issue(conn, saga, type="epic")
    with pytest.raises(TrackerError):  # story under epic cannot become an epic
        t.update_issue(conn, story, type="epic")
    t.update_issue(conn, story, type="task")  # task under epic is fine
    assert t.get_issue(conn, story)["type"] == "task"
    # detach then retype works
    t.update_issue(conn, epic, parent_id=None)
    assert t.get_issue(conn, epic)["parent"] is None


def test_validation_errors(conn):
    with pytest.raises(TrackerError):
        t.create_issue(conn, "x", type="feature")
    with pytest.raises(TrackerError):
        t.create_issue(conn, "x", priority=5)
    with pytest.raises(TrackerError):
        t.create_issue(conn, "   ")
    with pytest.raises(TrackerError):
        t.create_issue(conn, "x", state="Nope")
    i = t.create_issue(conn, "x")
    with pytest.raises(TrackerError):
        t.update_issue(conn, i, state="Done")
    with pytest.raises(TrackerError):
        t.get_issue(conn, 12345)


# ---------------------------------------------------------------- issues

def test_create_defaults_and_update(conn):
    i = t.create_issue(conn, "Hello", body="b", labels=["x", "A"])
    issue = t.get_issue(conn, i)
    assert issue["type"] == "story" and issue["priority"] == 2 and issue["state"] == "Backlog"
    assert issue["labels"] == ["A", "x"]
    assert set(issue) >= {"id", "title", "body", "type", "priority", "state", "parent_id", "labels",
                          "created_at", "updated_at", "children", "comments", "parent"}
    t.update_issue(conn, i, title="New", priority=0, body="")
    issue = t.get_issue(conn, i)
    assert (issue["title"], issue["priority"], issue["body"]) == ("New", 0, "")
    t.move_issue(conn, i, "in progress")
    assert t.get_issue(conn, i)["state"] == "In Progress"


def test_list_filters_and_sorting(conn):
    a = t.create_issue(conn, "Alpha login", priority=3, labels=["ui"])
    b = t.create_issue(conn, "Beta", priority=0, type="bug", body="crash on LOGIN")
    c = t.create_issue(conn, "Gamma", priority=1, state="Todo")
    assert [i["id"] for i in t.list_issues(conn)] == [b, c, a]
    assert [i["id"] for i in t.list_issues(conn, sort="id")] == [a, b, c]
    assert [i["id"] for i in t.list_issues(conn, search="login", sort="id")] == [a, b]
    assert [i["id"] for i in t.list_issues(conn, type="bug")] == [b]
    assert [i["id"] for i in t.list_issues(conn, label="UI")] == [a]
    assert [i["id"] for i in t.list_issues(conn, state="todo")] == [c]
    assert "children" not in t.list_issues(conn)[0]
    with pytest.raises(TrackerError):
        t.list_issues(conn, sort="bogus")


def test_parent_filter_direct_children_only(conn):
    saga = t.create_issue(conn, "S", type="saga")
    epic = t.create_issue(conn, "E", type="epic", parent_id=saga)
    t.create_issue(conn, "St", type="story", parent_id=epic)
    assert [i["id"] for i in t.list_issues(conn, parent_id=saga)] == [epic]


def test_delete_orphans_children(conn):
    epic = t.create_issue(conn, "E", type="epic")
    story = t.create_issue(conn, "St", type="story", parent_id=epic)
    t.add_comment(conn, epic, "note")
    t.add_labels(conn, epic, ["x"])
    t.delete_issue(conn, epic)
    assert t.get_issue(conn, story)["parent_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 0
    with pytest.raises(TrackerError):
        t.delete_issue(conn, epic)


# ---------------------------------------------------------------- states

def test_states_crud(conn):
    s = t.add_state(conn, "Blocked", position=1)
    assert s["name"] == "Blocked" and s["position"] == 1
    assert [x["name"] for x in t.list_states(conn)][:3] == ["Backlog", "Blocked", "Todo"]
    t.add_state(conn, "Archive")
    assert t.list_states(conn)[-1]["name"] == "Archive"
    assert [x["position"] for x in t.list_states(conn)] == list(range(7))
    with pytest.raises(TrackerError):
        t.add_state(conn, "blocked")  # case-insensitive unique
    t.rename_state(conn, "blocked", "On Hold")
    assert t.list_states(conn)[1]["name"] == "On Hold"
    t.move_state(conn, "On Hold", 4)
    names = [x["name"] for x in t.list_states(conn)]
    assert names.index("On Hold") == 4 and [x["position"] for x in t.list_states(conn)] == list(range(7))
    t.move_state(conn, "Archive", 0)
    assert t.list_states(conn)[0]["name"] == "Archive"


def test_remove_state(conn):
    i = t.create_issue(conn, "x", state="Review")
    with pytest.raises(TrackerError):
        t.remove_state(conn, "Review")
    with pytest.raises(TrackerError):
        t.remove_state(conn, "Review", move_to="Review")
    t.remove_state(conn, "Review", move_to="done")
    assert t.get_issue(conn, i)["state"] == "Done"
    t.remove_state(conn, "Todo")  # empty: no move_to needed
    assert [x["name"] for x in t.list_states(conn)] == ["Backlog", "In Progress", "Done"]
    assert [x["position"] for x in t.list_states(conn)] == [0, 1, 2]
    t.remove_state(conn, "Backlog")
    t.remove_state(conn, "In Progress")
    with pytest.raises(TrackerError):
        t.remove_state(conn, "Done", move_to=None)  # last one (has issue too)
    t.delete_issue(conn, i)
    with pytest.raises(TrackerError):
        t.remove_state(conn, "Done")


def test_new_issue_goes_to_first_state(conn):
    t.move_state(conn, "Todo", 0)
    assert t.get_issue(conn, t.create_issue(conn, "x"))["state"] == "Todo"


# ---------------------------------------------------------------- labels / comments / views

def test_labels(conn):
    i = t.create_issue(conn, "x")
    t.add_labels(conn, i, ["needs-triage", "New-Label", "new-label"])
    assert t.get_issue(conn, i)["labels"] == ["needs-triage", "New-Label"]
    t.remove_labels(conn, i, ["NEEDS-TRIAGE", "not-there"])
    assert t.get_issue(conn, i)["labels"] == ["New-Label"]
    counts = {lab["name"]: lab["count"] for lab in t.list_labels(conn)}
    assert counts["New-Label"] == 1 and counts["wontfix"] == 0
    names = [lab["name"] for lab in t.list_labels(conn)]
    assert names == sorted(names, key=str.lower)


def test_comments(conn, monkeypatch):
    i = t.create_issue(conn, "x")
    monkeypatch.setenv("ISSUES_AUTHOR", "agent-7")
    c1 = t.add_comment(conn, i, "first")
    c2 = t.add_comment(conn, i, "second", author="bob")
    comments = t.get_issue(conn, i)["comments"]
    assert [(c["id"], c["author"], c["body"]) for c in comments] == [(c1, "agent-7", "first"), (c2, "bob", "second")]
    assert set(comments[0]) == {"id", "author", "body", "created_at"}
    with pytest.raises(TrackerError):
        t.add_comment(conn, i, "  ")
    with pytest.raises(TrackerError):
        t.add_comment(conn, 999, "x")


def test_board_priority_sorting(conn):
    low = t.create_issue(conn, "low", priority=4)
    high = t.create_issue(conn, "high", priority=0)
    mid = t.create_issue(conn, "mid", priority=2, type="bug")
    t.create_issue(conn, "done", state="Done")
    cols = t.board(conn)
    assert [s["name"] for s, _ in cols] == ["Backlog", "Todo", "In Progress", "Review", "Done"]
    assert [i["id"] for i in cols[0][1]] == [high, mid, low]
    assert len(cols[4][1]) == 1 and cols[1][1] == []
    bugs = t.board(conn, type="bug")
    assert [i["id"] for i in bugs[0][1]] == [mid] and bugs[4][1] == []
    with pytest.raises(TrackerError):
        t.board(conn, state="Todo")


def test_tree(conn):
    saga = t.create_issue(conn, "S", type="saga")
    e1 = t.create_issue(conn, "E1", type="epic", parent_id=saga, priority=3)
    e2 = t.create_issue(conn, "E2", type="epic", parent_id=saga, priority=0)
    st = t.create_issue(conn, "St", type="story", parent_id=e1)
    loose = t.create_issue(conn, "loose bug", type="bug", priority=0)
    roots = t.tree(conn)
    assert [r["id"] for r in roots] == [loose, saga]
    s = roots[1]
    assert [c["id"] for c in s["children"]] == [e2, e1]
    assert s["children"][1]["children"][0]["id"] == st
    sub = t.tree(conn, e1)
    assert len(sub) == 1 and sub[0]["id"] == e1 and sub[0]["children"][0]["id"] == st


# ---------------------------------------------------------------- CLI

def test_cli_init_prints_path(tmp_path, capsys):
    p = tmp_path / "new.db"
    assert t.main(["--db", str(p), "init"]) == 0
    assert str(p.resolve()) in capsys.readouterr().out
    assert p.exists()


def test_cli_add_show_list(db, capsys):
    code, out, _ = run(db, capsys, "add", "My saga", "-t", "saga", "-p", "1")
    assert code == 0 and out.strip() == "#1 My saga"
    issue = jrun(db, capsys, "add", "Epic", "-t", "epic", "--parent", "1", "-l", "ui", "-l", "needs-triage",
                 "-s", "todo")
    assert issue["parent_id"] == 1 and issue["labels"] == ["needs-triage", "ui"] and issue["state"] == "Todo"
    shown = jrun(db, capsys, "show", "1")
    assert shown["children"][0]["id"] == issue["id"]
    listed = jrun(db, capsys, "list", "-t", "epic")
    assert [i["id"] for i in listed] == [issue["id"]]
    code, out, _ = run(db, capsys, "list")
    assert "#1" in out and "My saga" in out and "[needs-triage, ui]" in out
    code, out, _ = run(db, capsys, "show", "2")
    assert code == 0 and "parent: #1" in out


def test_cli_stdin_body_and_comment(db, capsys, monkeypatch):
    issue = jrun(db, capsys, "add", "Bug", "-t", "bug", "-b", "-", stdin="line1\nline2\n", monkeypatch=monkeypatch)
    assert issue["body"] == "line1\nline2\n"
    c = jrun(db, capsys, "comment", str(issue["id"]), "-", "--author", "me", stdin="from stdin",
             monkeypatch=monkeypatch)
    assert c["body"] == "from stdin" and c["author"] == "me"
    edited = jrun(db, capsys, "edit", str(issue["id"]), "-b", "-", stdin="new body", monkeypatch=monkeypatch)
    assert edited["body"] == "new body"


def test_cli_edit_move_delete(db, capsys):
    jrun(db, capsys, "add", "E", "-t", "epic")
    jrun(db, capsys, "add", "S", "-t", "story", "--parent", "1")
    out = jrun(db, capsys, "edit", "2", "--title", "Story!", "-p", "0", "--parent", "none")
    assert out["title"] == "Story!" and out["priority"] == 0 and out["parent_id"] is None
    assert jrun(db, capsys, "edit", "2", "--parent", "1")["parent_id"] == 1
    assert jrun(db, capsys, "move", "2", "in progress")["state"] == "In Progress"
    assert jrun(db, capsys, "delete", "1") == {"deleted": 1}
    assert jrun(db, capsys, "show", "2")["parent_id"] is None
    assert jrun(db, capsys, "bugs") == []


def test_cli_label_plus_minus(db, capsys):
    jrun(db, capsys, "add", "x", "-l", "wontfix")
    out = jrun(db, capsys, "label", "1", "+needs-triage", "-wontfix", "plain")
    assert out["labels"] == ["needs-triage", "plain"]
    out = jrun(db, capsys, "label", "1", "--", "-plain")
    assert out["labels"] == ["needs-triage"]
    code, out, _ = run(db, capsys, "label", "1", "-needs-triage")
    assert code == 0 and "labels: -" in out
    labels = jrun(db, capsys, "labels")
    assert {"name": "plain", "count": 0} in labels


def test_cli_board_tree_states(db, capsys):
    jrun(db, capsys, "add", "S", "-t", "saga", "-p", "1")
    jrun(db, capsys, "add", "E", "-t", "epic", "--parent", "1", "-s", "In Progress")
    code, out, _ = run(db, capsys, "tree")
    assert out.splitlines() == ["#1 [saga] P1 S (Backlog)", "  #2 [epic] P2 E (In Progress)"]
    code, out, _ = run(db, capsys, "board")
    assert "== Backlog (1) ==" in out and "== In Progress (1) ==" in out
    b = jrun(db, capsys, "board", "-t", "epic")
    assert [len(col["issues"]) for col in b] == [0, 0, 1, 0, 0]
    assert jrun(db, capsys, "tree", "2")[0]["id"] == 2
    st = jrun(db, capsys, "state", "add", "Blocked", "--position", "1")
    assert st["position"] == 1
    names = [s["name"] for s in jrun(db, capsys, "state", "move", "Blocked", "3")]
    assert names.index("Blocked") == 3
    assert jrun(db, capsys, "state", "rename", "Blocked", "Waiting")[3]["name"] == "Waiting"
    code, _, err = run(db, capsys, "state", "rm", "In Progress")
    assert code == 1 and err.startswith("error:")
    states = jrun(db, capsys, "state", "rm", "In Progress", "--move-to", "Review")
    assert "In Progress" not in [s["name"] for s in states]
    assert jrun(db, capsys, "show", "2")["state"] == "Review"
    assert len(jrun(db, capsys, "states")) == 5


@pytest.mark.parametrize("args", [
    ["show", "99"],
    ["add", "x", "-t", "feature"],
    ["add", "x", "-p", "9"],
    ["add", "s", "-t", "story", "--parent", "42"],
    ["move", "1", "Nowhere"],
    ["edit", "1"],
    ["frobnicate"],
    ["label", "1"],
])
def test_cli_errors_exit_1(db, capsys, args):
    run(db, capsys, "add", "x")
    code, out, err = run(db, capsys, *args)
    assert code == 1
    assert err.startswith("error:")


def test_cli_hierarchy_error(db, capsys):
    run(db, capsys, "add", "t", "-t", "task")
    code, _, err = run(db, capsys, "add", "s", "-t", "story", "--parent", "1")
    assert code == 1 and "cannot have a task parent" in err
