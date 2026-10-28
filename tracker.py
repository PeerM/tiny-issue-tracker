"""tiny-issue-tracker: SQLite data layer + argparse CLI (stdlib only).

The public functions below are used by gui.py; the CLI entry point is main().
"""

from __future__ import annotations

import argparse
import datetime as _dt
import getpass
import json
import os
import pathlib
import sqlite3
import sys

__all__ = [
    "TrackerError", "TYPES", "PRIORITIES", "find_db", "connect",
    "list_states", "add_state", "rename_state", "move_state", "remove_state",
    "create_issue", "get_issue", "list_issues", "update_issue", "move_issue", "delete_issue",
    "list_labels", "add_labels", "remove_labels", "add_comment", "board", "tree", "main",
]


class TrackerError(Exception):
    """Any user-facing error (validation, missing objects, ...)."""


TYPES = ("saga", "epic", "story", "task", "bug")
PRIORITIES = range(0, 5)
DB_NAME = "issues.db"
DEFAULT_STATES = ("Backlog", "Todo", "In Progress", "Review", "Done")
DEFAULT_LABELS = ("needs-triage", "needs-info", "ready-for-agent", "ready-for-human", "wontfix")
# allowed parent types per issue type (no parent is always allowed)
ALLOWED_PARENTS = {
    "saga": (),
    "epic": ("saga",),
    "story": ("epic",),
    "task": ("saga", "epic", "story"),
    "bug": ("saga", "epic", "story"),
}
SORTS = ("priority", "id", "updated")

SCHEMA = """
CREATE TABLE IF NOT EXISTS states (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, position INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS issues (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  type TEXT NOT NULL DEFAULT 'story' CHECK (type IN ('saga','epic','story','task','bug')),
  priority INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 0 AND 4),
  state_id INTEGER NOT NULL REFERENCES states(id),
  parent_id INTEGER REFERENCES issues(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS labels (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS issue_labels (
  issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
  label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
  PRIMARY KEY (issue_id, label_id));
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
  author TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
"""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- connection

def find_db(explicit: str | None = None) -> pathlib.Path:
    """Resolve the DB path: explicit > $ISSUES_DB > nearest issues.db upwards > ./issues.db."""
    if explicit:
        return pathlib.Path(explicit)
    env = os.environ.get("ISSUES_DB")
    if env:
        return pathlib.Path(env)
    cwd = pathlib.Path.cwd()
    for d in (cwd, *cwd.parents):
        if (d / DB_NAME).is_file():
            return d / DB_NAME
    return cwd / DB_NAME


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    """Open (and initialise if new) the tracker database."""
    p = find_db(None) if path is None else pathlib.Path(path)
    try:
        conn = sqlite3.connect(p, timeout=10)
    except sqlite3.Error as e:
        raise TrackerError(f"cannot open database {p}: {e}") from e
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
        conn.executescript(SCHEMA)
        if not conn.execute("SELECT 1 FROM states").fetchone():
            conn.executemany("INSERT INTO states(name, position) VALUES (?, ?)",
                             [(n, i) for i, n in enumerate(DEFAULT_STATES)])
        conn.executemany("INSERT OR IGNORE INTO labels(name) VALUES (?)", [(n,) for n in DEFAULT_LABELS])
        conn.execute("PRAGMA user_version=1")
        conn.commit()
    return conn


# --------------------------------------------------------------------------- states

def list_states(conn) -> list[dict]:
    rows = conn.execute("SELECT id, name, position FROM states ORDER BY position, id")
    return [dict(r) for r in rows]


def _state(conn, name) -> sqlite3.Row:
    row = conn.execute("SELECT id, name, position FROM states WHERE name = ?", (str(name).strip(),)).fetchone()
    if row is None:
        names = ", ".join(s["name"] for s in list_states(conn))
        raise TrackerError(f"unknown state {name!r} (states: {names})")
    return row


def _renumber(conn, ids: list[int]) -> None:
    conn.executemany("UPDATE states SET position = ? WHERE id = ?", [(i, sid) for i, sid in enumerate(ids)])


def _clean_name(name, what="name") -> str:
    name = str(name or "").strip()
    if not name:
        raise TrackerError(f"{what} must not be empty")
    return name


def add_state(conn, name, position: int | None = None) -> dict:
    name = _clean_name(name, "state name")
    ids = [s["id"] for s in list_states(conn)]
    try:
        cur = conn.execute("INSERT INTO states(name, position) VALUES (?, ?)", (name, len(ids)))
    except sqlite3.IntegrityError:
        raise TrackerError(f"state {name!r} already exists") from None
    pos = len(ids) if position is None else max(0, min(int(position), len(ids)))
    ids.insert(pos, cur.lastrowid)
    _renumber(conn, ids)
    conn.commit()
    return dict(_state(conn, name))


def rename_state(conn, old, new) -> None:
    row, new = _state(conn, old), _clean_name(new, "state name")
    try:
        conn.execute("UPDATE states SET name = ? WHERE id = ?", (new, row["id"]))
    except sqlite3.IntegrityError:
        raise TrackerError(f"state {new!r} already exists") from None
    conn.commit()


def move_state(conn, name, position: int) -> None:
    row = _state(conn, name)
    ids = [s["id"] for s in list_states(conn) if s["id"] != row["id"]]
    ids.insert(max(0, min(int(position), len(ids))), row["id"])
    _renumber(conn, ids)
    conn.commit()


def remove_state(conn, name, move_to: str | None = None) -> None:
    row = _state(conn, name)
    if len(list_states(conn)) <= 1:
        raise TrackerError("cannot remove the last remaining state")
    count = conn.execute("SELECT COUNT(*) FROM issues WHERE state_id = ?", (row["id"],)).fetchone()[0]
    if count:
        if move_to is None:
            raise TrackerError(f"state {row['name']!r} has {count} issue(s); give a state to move them to")
        target = _state(conn, move_to)
        if target["id"] == row["id"]:
            raise TrackerError("cannot move issues to the state being removed")
        conn.execute("UPDATE issues SET state_id = ?, updated_at = ? WHERE state_id = ?",
                     (target["id"], _now(), row["id"]))
    conn.execute("DELETE FROM states WHERE id = ?", (row["id"],))
    _renumber(conn, [s["id"] for s in list_states(conn)])
    conn.commit()


# --------------------------------------------------------------------------- issues

_ISSUE_SELECT = """
SELECT i.id, i.title, i.body, i.type, i.priority, s.name AS state, i.parent_id,
       i.created_at, i.updated_at
FROM issues i JOIN states s ON s.id = i.state_id
"""


def _labels_for(conn, ids) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {i: [] for i in ids}
    if not ids:
        return out
    q = ("SELECT il.issue_id, l.name FROM issue_labels il JOIN labels l ON l.id = il.label_id "
         f"WHERE il.issue_id IN ({','.join('?' * len(ids))}) ORDER BY l.name COLLATE NOCASE")
    for r in conn.execute(q, list(ids)):
        out[r["issue_id"]].append(r["name"])
    return out


def _rows_to_issues(conn, rows) -> list[dict]:
    issues = [dict(r) for r in rows]
    labels = _labels_for(conn, [i["id"] for i in issues])
    for i in issues:
        i["labels"] = labels[i["id"]]
    return issues


def _issue_row(conn, issue_id) -> sqlite3.Row:
    try:
        issue_id = int(issue_id)
    except (TypeError, ValueError):
        raise TrackerError(f"invalid issue id {issue_id!r}") from None
    row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    if row is None:
        raise TrackerError(f"issue #{issue_id} not found")
    return row


def _check_type(type_) -> str:
    if type_ not in TYPES:
        raise TrackerError(f"invalid type {type_!r} (choose from {', '.join(TYPES)})")
    return type_


def _check_priority(priority) -> int:
    try:
        p = int(priority)
    except (TypeError, ValueError):
        p = -1
    if isinstance(priority, bool) or p not in PRIORITIES:
        raise TrackerError(f"invalid priority {priority!r} (0 = highest .. 4 = lowest)")
    return p


def _validate_hierarchy(conn, issue_id, type_, parent_id) -> None:
    """Check that `type_` may sit under `parent_id` and still holds its existing children."""
    if parent_id is not None:
        parent = _issue_row(conn, parent_id)
        if issue_id is not None and parent["id"] == int(issue_id):
            raise TrackerError("an issue cannot be its own parent")
        if parent["type"] not in ALLOWED_PARENTS[type_]:
            allowed = " or ".join(ALLOWED_PARENTS[type_]) or "nothing (must be top-level)"
            raise TrackerError(f"a {type_} cannot have a {parent['type']} parent (allowed: {allowed})")
    if issue_id is not None:
        for c in conn.execute("SELECT id, type FROM issues WHERE parent_id = ?", (int(issue_id),)):
            if type_ not in ALLOWED_PARENTS[c["type"]]:
                raise TrackerError(f"a {type_} cannot be the parent of {c['type']} #{c['id']}")


def create_issue(conn, title, *, type="story", priority=2, parent_id=None, state=None,
                 body="", labels=()) -> int:
    title = _clean_name(title, "title")
    type_ = _check_type(type)
    priority = _check_priority(priority)
    parent_id = None if parent_id is None else int(parent_id)
    _validate_hierarchy(conn, None, type_, parent_id)
    if state is None:
        states = list_states(conn)
        if not states:
            raise TrackerError("no states defined")
        state_id = states[0]["id"]
    else:
        state_id = _state(conn, state)["id"]
    now = _now()
    cur = conn.execute(
        "INSERT INTO issues(title, body, type, priority, state_id, parent_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (title, body or "", type_, priority, state_id, parent_id, now, now))
    issue_id = cur.lastrowid
    _attach_labels(conn, issue_id, labels)
    conn.commit()
    return issue_id


def get_issue(conn, issue_id) -> dict:
    row = _issue_row(conn, issue_id)
    issue = _rows_to_issues(conn, conn.execute(_ISSUE_SELECT + "WHERE i.id = ?", (row["id"],)))[0]
    issue["children"] = [dict(r) for r in conn.execute(
        "SELECT i.id, i.title, i.type, s.name AS state, i.priority FROM issues i "
        "JOIN states s ON s.id = i.state_id WHERE i.parent_id = ? ORDER BY i.priority, i.id", (row["id"],))]
    issue["comments"] = [dict(r) for r in conn.execute(
        "SELECT id, author, body, created_at FROM comments WHERE issue_id = ? ORDER BY id", (row["id"],))]
    parent = None
    if row["parent_id"] is not None:
        p = conn.execute("SELECT id, title, type FROM issues WHERE id = ?", (row["parent_id"],)).fetchone()
        parent = dict(p) if p else None
    issue["parent"] = parent
    return issue


def list_issues(conn, *, state=None, type=None, label=None, parent_id=None,
                search=None, sort="priority") -> list[dict]:
    where, params = [], []
    if state is not None:
        where.append("i.state_id = ?")
        params.append(_state(conn, state)["id"])
    if type is not None:
        where.append("i.type = ?")
        params.append(_check_type(type))
    if label is not None:
        where.append("i.id IN (SELECT il.issue_id FROM issue_labels il JOIN labels l "
                     "ON l.id = il.label_id WHERE l.name = ?)")
        params.append(str(label).strip())
    if parent_id is not None:
        where.append("i.parent_id = ?")
        params.append(int(parent_id))
    if search:
        esc = str(search).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(i.title LIKE ? ESCAPE '\\' OR i.body LIKE ? ESCAPE '\\')")
        params += [f"%{esc}%"] * 2
    orders = {"priority": "i.priority ASC, i.updated_at DESC, i.id ASC",
              "id": "i.id ASC", "updated": "i.updated_at DESC, i.id DESC"}
    if sort not in orders:
        raise TrackerError(f"invalid sort {sort!r} (choose from {', '.join(SORTS)})")
    sql = _ISSUE_SELECT + (f"WHERE {' AND '.join(where)} " if where else "") + f"ORDER BY {orders[sort]}"
    return _rows_to_issues(conn, conn.execute(sql, params))


def update_issue(conn, issue_id, **fields) -> None:
    row = _issue_row(conn, issue_id)
    unknown = set(fields) - {"title", "body", "type", "priority", "parent_id"}
    if unknown:
        raise TrackerError(f"cannot update field(s): {', '.join(sorted(unknown))}")
    new = dict(row)
    if "title" in fields:
        new["title"] = _clean_name(fields["title"], "title")
    if "body" in fields:
        new["body"] = fields["body"] or ""
    if "type" in fields:
        new["type"] = _check_type(fields["type"])
    if "priority" in fields:
        new["priority"] = _check_priority(fields["priority"])
    if "parent_id" in fields:
        new["parent_id"] = None if fields["parent_id"] is None else int(fields["parent_id"])
    _validate_hierarchy(conn, row["id"], new["type"], new["parent_id"])
    conn.execute("UPDATE issues SET title=?, body=?, type=?, priority=?, parent_id=?, updated_at=? WHERE id=?",
                 (new["title"], new["body"], new["type"], new["priority"], new["parent_id"], _now(), row["id"]))
    conn.commit()


def move_issue(conn, issue_id, state: str) -> None:
    row, st = _issue_row(conn, issue_id), _state(conn, state)
    conn.execute("UPDATE issues SET state_id = ?, updated_at = ? WHERE id = ?", (st["id"], _now(), row["id"]))
    conn.commit()


def delete_issue(conn, issue_id) -> None:
    row = _issue_row(conn, issue_id)
    conn.execute("DELETE FROM issues WHERE id = ?", (row["id"],))  # children: ON DELETE SET NULL
    conn.commit()


# --------------------------------------------------------------------------- labels / comments

def list_labels(conn) -> list[dict]:
    rows = conn.execute("SELECT l.name, COUNT(il.issue_id) AS count FROM labels l "
                        "LEFT JOIN issue_labels il ON il.label_id = l.id "
                        "GROUP BY l.id ORDER BY l.name COLLATE NOCASE")
    return [dict(r) for r in rows]


def _label_names(names) -> list[str]:
    if isinstance(names, str):
        names = [names]
    return [n for n in (str(x).strip() for x in names) if n]


def _attach_labels(conn, issue_id, names) -> None:
    for name in _label_names(names):
        conn.execute("INSERT OR IGNORE INTO labels(name) VALUES (?)", (name,))
        lid = conn.execute("SELECT id FROM labels WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute("INSERT OR IGNORE INTO issue_labels(issue_id, label_id) VALUES (?, ?)", (issue_id, lid))


def add_labels(conn, issue_id, names) -> None:
    row = _issue_row(conn, issue_id)
    _attach_labels(conn, row["id"], names)
    conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (_now(), row["id"]))
    conn.commit()


def remove_labels(conn, issue_id, names) -> None:
    row = _issue_row(conn, issue_id)
    for name in _label_names(names):
        conn.execute("DELETE FROM issue_labels WHERE issue_id = ? AND label_id IN "
                     "(SELECT id FROM labels WHERE name = ?)", (row["id"], name))
    conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (_now(), row["id"]))
    conn.commit()


def _default_author() -> str:
    author = os.environ.get("ISSUES_AUTHOR")
    if author:
        return author
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - getuser can raise various errors
        return "unknown"


def add_comment(conn, issue_id, body, author=None) -> int:
    row = _issue_row(conn, issue_id)
    body = str(body or "").strip()
    if not body:
        raise TrackerError("comment must not be empty")
    now = _now()
    cur = conn.execute("INSERT INTO comments(issue_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                       (row["id"], author or _default_author(), body, now))
    conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (now, row["id"]))
    conn.commit()
    return cur.lastrowid


# --------------------------------------------------------------------------- views

def board(conn, **filters) -> list[tuple[dict, list[dict]]]:
    if "state" in filters:
        raise TrackerError("board() does not take a state filter")
    issues = list_issues(conn, **filters)
    return [(s, [i for i in issues if i["state"] == s["name"]]) for s in list_states(conn)]


def tree(conn, root_id=None) -> list[dict]:
    issues = list_issues(conn)
    by_parent: dict[int | None, list[dict]] = {}
    for i in issues:
        by_parent.setdefault(i["parent_id"], []).append(i)

    def build(issue: dict, seen: frozenset) -> dict:
        kids = [c for c in by_parent.get(issue["id"], []) if c["id"] not in seen]
        return {**issue, "children": [build(c, seen | {c["id"]}) for c in kids]}

    if root_id is None:
        roots = by_parent.get(None, [])
    else:
        rid = _issue_row(conn, root_id)["id"]
        roots = [i for i in issues if i["id"] == rid]
    return [build(r, frozenset({r["id"]})) for r in roots]


# --------------------------------------------------------------------------- CLI

class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # exit code 1 (not argparse's 2), never prompt
        raise _UsageError(f"{message}\n{self.format_usage().strip()}")


def _read_text(value):
    return sys.stdin.read() if value == "-" else value


def _issue_line(i: dict) -> str:
    labels = f"  [{', '.join(i['labels'])}]" if i.get("labels") else ""
    return f"#{i['id']:<4} P{i['priority']}  {i['type']:<5}  {i['state']:<12} {i['title']}{labels}"


def _print_tree(nodes, depth=0):
    for n in nodes:
        print(f"{'  ' * depth}#{n['id']} [{n['type']}] P{n['priority']} {n['title']} ({n['state']})")
        _print_tree(n["children"], depth + 1)


def _print_issue(i: dict) -> None:
    print(f"#{i['id']} {i['title']}")
    print(f"type: {i['type']}   priority: P{i['priority']}   state: {i['state']}")
    if i["parent"]:
        print(f"parent: #{i['parent']['id']} [{i['parent']['type']}] {i['parent']['title']}")
    print(f"labels: {', '.join(i['labels']) or '-'}")
    print(f"created: {i['created_at']}   updated: {i['updated_at']}")
    if i["body"]:
        print("\n" + i["body"].rstrip())
    if i["children"]:
        print("\nchildren:")
        for c in i["children"]:
            print(f"  #{c['id']} [{c['type']}] P{c['priority']} {c['title']} ({c['state']})")
    if i["comments"]:
        print("\ncomments:")
        for c in i["comments"]:
            print(f"  [{c['created_at']}] {c['author']}:")
            for line in c["body"].splitlines():
                print(f"    {line}")


def _parent_arg(value: str):
    if value.lower() in ("none", "null", "0", ""):
        return None
    try:
        return int(value.lstrip("#"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid parent {value!r} (issue id or 'none')") from None


def _id_arg(value: str) -> int:
    try:
        return int(value.lstrip("#"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid issue id {value!r}") from None


def _build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="issues", description="Tiny SQLite issue tracker.", allow_abbrev=False)
    p.add_argument("--db", metavar="PATH", help="database file (default: $ISSUES_DB or nearest issues.db)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND", required=True)

    def cmd(name, help_):
        return sub.add_parser(name, help=help_, description=help_, allow_abbrev=False)

    cmd("init", "create the database (in cwd or --db) and print its path")
    a = cmd("add", "create an issue")
    a.add_argument("title")
    a.add_argument("-t", "--type", default="story", choices=TYPES)
    a.add_argument("-p", "--priority", type=int, default=2, help="0 (highest) .. 4 (lowest)")
    a.add_argument("--parent", type=_id_arg)
    a.add_argument("-s", "--state")
    a.add_argument("-l", "--label", action="append", default=[], help="repeatable")
    a.add_argument("-b", "--body", default="", help="text, or '-' to read stdin")
    cmd("show", "show an issue").add_argument("id", type=_id_arg)
    ls = cmd("list", "list issues")
    for x in (ls, cmd("bugs", "list bugs (same as list -t bug)")):
        x.add_argument("-s", "--state")
        x.add_argument("-l", "--label")
        x.add_argument("--parent", type=_id_arg)
        x.add_argument("-q", "--search")
        x.add_argument("--sort", choices=SORTS, default="priority")
    ls.add_argument("-t", "--type", choices=TYPES)
    e = cmd("edit", "edit an issue")
    e.add_argument("id", type=_id_arg)
    e.add_argument("--title")
    e.add_argument("-t", "--type", choices=TYPES)
    e.add_argument("-p", "--priority", type=int)
    e.add_argument("--parent", type=_parent_arg, default=argparse.SUPPRESS, help="issue id or 'none'")
    e.add_argument("-b", "--body", help="text, or '-' to read stdin")
    m = cmd("move", "move an issue to a state")
    m.add_argument("id", type=_id_arg)
    m.add_argument("state")
    lb = cmd("label", "add/remove labels: +name adds, -name removes")
    lb.add_argument("id", type=_id_arg)
    lb.add_argument("changes", nargs=argparse.REMAINDER, metavar="+name|-name")
    cmd("labels", "list labels with usage counts")
    c = cmd("comment", "add a comment")
    c.add_argument("id", type=_id_arg)
    c.add_argument("text", help="text, or '-' to read stdin")
    c.add_argument("--author")
    cmd("delete", "delete an issue (children are kept, detached)").add_argument("id", type=_id_arg)
    cmd("tree", "show the hierarchy").add_argument("id", nargs="?", type=_id_arg)
    b = cmd("board", "text Kanban board")
    b.add_argument("-t", "--type", choices=TYPES)
    b.add_argument("-l", "--label")
    b.add_argument("--parent", type=_id_arg)
    cmd("states", "list states (columns)")
    st = cmd("state", "manage states").add_subparsers(dest="state_cmd", metavar="ACTION", required=True)
    x = st.add_parser("add", help="add a state")
    x.add_argument("name")
    x.add_argument("--position", type=int)
    x = st.add_parser("rename", help="rename a state")
    x.add_argument("old")
    x.add_argument("new")
    x = st.add_parser("move", help="reorder a state (0-based position)")
    x.add_argument("name")
    x.add_argument("position", type=int)
    x = st.add_parser("rm", help="remove a state")
    x.add_argument("name")
    x.add_argument("--move-to")
    cmd("gui", "launch the Kanban GUI")
    return p


def _split_label_changes(changes):
    add, remove = [], []
    for ch in changes:
        if ch == "--":
            continue
        if ch.startswith("-"):
            remove.append(ch[1:])
        else:
            add.append(ch[1:] if ch.startswith("+") else ch)
    if not [n for n in add + remove if n.strip()]:
        raise TrackerError("no label changes given (use +name / -name)")
    return add, remove


def _run(args) -> int:
    out = (lambda obj: print(json.dumps(obj, indent=2, ensure_ascii=False))) if args.json else None

    if args.cmd == "init":
        path = pathlib.Path(args.db) if args.db else pathlib.Path.cwd() / DB_NAME
        connect(path).close()
        path = path.resolve()
        out({"path": str(path)}) if out else print(path)
        return 0
    if args.cmd == "gui":
        import gui  # lazy: the CLI must work without Qt installed

        path = str(find_db(args.db).resolve())
        os.environ["ISSUES_DB"] = path  # gui.main() parses sys.argv; both point at the same DB
        sys.argv = ["issues-board", "--db", path]
        return gui.main() or 0

    conn = connect(find_db(args.db))
    try:
        return _dispatch(conn, args, out)
    finally:
        conn.close()


def _dispatch(conn, args, out) -> int:
    def show(issue_id):
        issue = get_issue(conn, issue_id)
        out(issue) if out else _print_issue(issue)

    def print_list(issues):
        if out:
            out(issues)
        else:
            for i in issues:
                print(_issue_line(i))
            if not issues:
                print("(no issues)")

    cmd = args.cmd
    if cmd == "add":
        iid = create_issue(conn, args.title, type=args.type, priority=args.priority, parent_id=args.parent,
                           state=args.state, body=_read_text(args.body), labels=args.label)
        out(get_issue(conn, iid)) if out else print(f"#{iid} {get_issue(conn, iid)['title']}")
    elif cmd == "show":
        show(args.id)
    elif cmd in ("list", "bugs"):
        print_list(list_issues(conn, state=args.state, type="bug" if cmd == "bugs" else args.type,
                               label=args.label, parent_id=args.parent, search=args.search, sort=args.sort))
    elif cmd == "edit":
        fields = {k: v for k, v in (("title", args.title), ("type", args.type), ("priority", args.priority),
                                     ("body", _read_text(args.body))) if v is not None}
        if hasattr(args, "parent"):
            fields["parent_id"] = args.parent
        if not fields:
            raise TrackerError("nothing to change (see: issues edit --help)")
        update_issue(conn, args.id, **fields)
        out(get_issue(conn, args.id)) if out else print(f"updated #{args.id}")
    elif cmd == "move":
        move_issue(conn, args.id, args.state)
        issue = get_issue(conn, args.id)
        out(issue) if out else print(f"#{issue['id']} -> {issue['state']}")
    elif cmd == "label":
        add, remove = _split_label_changes(args.changes)
        _issue_row(conn, args.id)
        if add:
            add_labels(conn, args.id, add)
        if remove:
            remove_labels(conn, args.id, remove)
        issue = get_issue(conn, args.id)
        out(issue) if out else print(f"#{issue['id']} labels: {', '.join(issue['labels']) or '-'}")
    elif cmd == "labels":
        labels = list_labels(conn)
        if out:
            out(labels)
        else:
            for lab in labels:
                print(f"{lab['count']:>4}  {lab['name']}")
    elif cmd == "comment":
        cid = add_comment(conn, args.id, _read_text(args.text), author=args.author)
        comment = next(c for c in get_issue(conn, args.id)["comments"] if c["id"] == cid)
        out({**comment, "issue_id": args.id}) if out else print(f"comment {cid} added to #{args.id}")
    elif cmd == "delete":
        delete_issue(conn, args.id)
        out({"deleted": args.id}) if out else print(f"deleted #{args.id}")
    elif cmd == "tree":
        nodes = tree(conn, args.id)
        out(nodes) if out else (_print_tree(nodes) if nodes else print("(no issues)"))
    elif cmd == "board":
        cols = board(conn, type=args.type, label=args.label, parent_id=args.parent)
        if out:
            out([{"state": s, "issues": issues} for s, issues in cols])
        else:
            for s, issues in cols:
                print(f"== {s['name']} ({len(issues)}) ==")
                for i in issues:
                    print("  " + _issue_line(i))
                print()
    elif cmd == "states":
        states = list_states(conn)
        if out:
            out(states)
        else:
            for s in states:
                print(f"{s['position']}  {s['name']}")
    elif cmd == "state":
        sc = args.state_cmd
        if sc == "add":
            state = add_state(conn, args.name, args.position)
            out(state) if out else print(f"added state {state['name']!r} at position {state['position']}")
            return 0
        if sc == "rename":
            rename_state(conn, args.old, args.new)
            msg = f"renamed state {args.old!r} to {args.new!r}"
        elif sc == "move":
            move_state(conn, args.name, args.position)
            msg = f"moved state {args.name!r}"
        else:
            remove_state(conn, args.name, args.move_to)
            msg = f"removed state {args.name!r}"
        out(list_states(conn)) if out else print(msg)
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:  # Windows consoles/pipes default to cp1252; titles may contain any unicode
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        return _run(_build_parser().parse_args(argv))
    except (TrackerError, _UsageError, sqlite3.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except SystemExit as e:  # --help
        return e.code if isinstance(e.code, int) else 0


if __name__ == "__main__":
    sys.exit(main())
