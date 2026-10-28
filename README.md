# tiny-issue-tracker

A small issue tracker that keeps everything in one SQLite file. It has two front ends that share the same database:

- `issues`, a command-line tool for people, scripts and AI coding agents
- `issues-board`, a PyQt6 Kanban board that refreshes itself when the database changes

There is no server and no account. The whole tracker is one `issues.db` file that sits next to your project.

---

## Contents

- [Install and run](#install-and-run)
- [Where the database lives](#where-the-database-lives)
- [Concepts](#concepts)
- [CLI reference](#cli-reference)
- [Worked example](#worked-example)
- [Customizing board columns](#customizing-board-columns)
- [The Kanban board (GUI)](#the-kanban-board-gui)
- [Using with AI agents](#using-with-ai-agents)
- [Schema (for direct SQL)](#schema-for-direct-sql)

---

## Install and run

Requirements: Python 3.11 or later and [uv](https://docs.astral.sh/uv/).

```sh
uv sync                      # create the venv and install dependencies (PyQt6)
uv run issues init           # create issues.db in the current directory
uv run issues add "First issue"
uv run issues list
```

To open the Kanban board:

```sh
uv run issues gui            # through the CLI
uv run issues-board          # or the board's own entry point
uv run issues-board --db path/to/issues.db
```

The CLI does not import Qt, so `issues` keeps working even if PyQt6 cannot load (for example on a headless machine).

All examples below are written as `issues ...`. Inside the project, run them as `uv run issues ...`.

## Where the database lives

Each command picks the database in this order:

1. `--db PATH`, the global option placed before the subcommand: `issues --db ~/work/issues.db list`
2. The `ISSUES_DB` environment variable
3. The nearest `issues.db`, searching the current directory first and then each parent directory
4. `./issues.db` in the current directory, created on first use

So if you run `issues init` once at the root of a repository, every command run from a subdirectory finds that database.

A new database is created with the default columns (Backlog, Todo, In Progress, Review, Done) and the triage labels.

**Comment author.** Comments are signed with `ISSUES_AUTHOR` when it is set, and with your OS user name otherwise. You can also pass `--author NAME` to a single `comment` command. Giving each agent its own `ISSUES_AUTHOR` (for example `ISSUES_AUTHOR=claude`) lets you see who wrote which note.

## Concepts

### Issue types

| Type    | Purpose                                   | Allowed parent              | Can have children |
|---------|-------------------------------------------|-----------------------------|-------------------|
| `saga`  | Large initiative or theme                 | none                        | yes (epics, tasks, bugs) |
| `epic`  | Deliverable feature area within a saga    | a saga, or none             | yes (stories, tasks, bugs) |
| `story` | A user-visible piece of work (the default)| an epic, or none            | yes (tasks, bugs) |
| `task`  | Technical work item                       | saga, epic, story, or none  | no |
| `bug`   | Defect                                    | saga, epic, story, or none  | no |

The hierarchy is built only from parent links and has at most three levels: **saga > epic > story**. Tasks and bugs can hang off any of those three levels, but nothing can be placed under a task or bug.

These rules are checked on every create and edit. That includes changing the type of an issue that already has children. For example, you cannot turn an epic that has stories into a task. If a change breaks the rules, the command fails with an error and nothing is changed.

Deleting an issue does not delete its children. They stay in the tracker with no parent.

### Priority

Priority is a number from `0` to `4`. It is shown as `P0` to `P4`, and `P0` is the most urgent.

| Priority | Meaning |
|----------|---------|
| `P0` | Critical, drop everything |
| `P1` | High |
| `P2` | Normal (the default) |
| `P3` | Low |
| `P4` | Lowest, someday |

Lists, boards and trees put the most urgent issues first. Issues with the same priority are ordered by most recently updated.

### States (board columns)

The state of an issue is the board column it sits in. You can define your own states and their order. A new database starts with:

```
Backlog -> Todo -> In Progress -> Review -> Done
```

A new issue goes into the first column unless you pass `-s STATE`. State names are not case-sensitive (`in progress` matches `In Progress`). See [Customizing board columns](#customizing-board-columns).

### Labels

Labels are free-form tags. A label is created the first time you use it. Every new database comes with these triage labels:

| Label | Meaning | Typical next step |
|-------|---------|-------------------|
| `needs-triage`    | New and not yet reviewed | Someone checks it and sets priority, type and parent |
| `needs-info`      | Cannot be acted on yet because details, a reproduction or a decision are missing | Ask in a comment, then remove the label once the answer arrives |
| `ready-for-agent` | Clearly specified and small enough for an AI agent to do on its own | An agent picks it up |
| `ready-for-human` | Ready to work on, but needs human judgment, access or design input | A person picks it up |
| `wontfix`         | Deliberately not going to be done | Move it to the last column, or delete it |

### Comments

Comments are notes added to an issue, such as progress updates, questions or decisions. Each comment stores its author and a timestamp. They can only be added, not edited or deleted. `issues show ID` lists them.

## CLI reference

```
issues [--db PATH] [--json] <command> [args]
```

- `--db PATH`: use this database file (see [Where the database lives](#where-the-database-lives)).
- `--json`: print machine-readable JSON. Read commands print the objects they return. Commands that change something print the resulting object (issue, state, and so on).
- Global options go **before** the command: `issues --json list`, not `issues list --json`.
- Wherever a body text is accepted (`add -b`, `edit -b`, `comment`), `-` means "read the text from stdin".
- On failure the CLI prints `error: <message>` to stderr and exits non-zero. It never asks interactive questions.

### `init`

Creates the database in the current directory, or at the `--db` path, and prints its path.

```sh
issues init
issues --db ~/notes/issues.db init
```

### `add`

```
issues add TITLE [-t TYPE] [-p 0-4] [--parent ID] [-s STATE] [-l LABEL]... [-b BODY|-]
```

Prints `#<id> <title>`.

```sh
issues add "Checkout fails with empty cart" -t bug -p 1 -l needs-triage
issues add "Password reset" -t story --parent 4 -s Todo -b "As a user I want to reset my password."
git diff | issues add "Review this refactor" -t task -b -
```

`-l` can be repeated: `-l backend -l needs-triage`.

### `show`

Shows all fields, the labels, the parent, the direct children and every comment.

```sh
issues show 12
issues --json show 12
```

### `list`

```
issues list [-s STATE] [-t TYPE] [-l LABEL] [--parent ID] [-q TEXT] [--sort priority|id|updated]
```

Each issue is printed on one line in the form `#12  P1  story  In Progress  Title  [label1, label2]`.

```sh
issues list                                   # everything, most urgent first
issues list -s "In Progress"
issues list -t bug -l needs-triage
issues list --parent 3                        # direct children of #3 only
issues list -q timeout                        # title or body contains "timeout" (case-insensitive)
issues list --sort updated                    # most recently changed first
```

`--sort` takes one of these values:

- `priority` (the default): ordered by priority, then most recently updated, then ID
- `id`: ordered by ID
- `updated`: most recently updated first

### `edit`

```
issues edit ID [--title T] [-t TYPE] [-p N] [--parent ID|none] [-b BODY|-]
```

Only the fields you pass are changed.

```sh
issues edit 12 -p 0
issues edit 12 --title "Checkout 500s when cart is empty"
issues edit 7 -t epic --parent 2
issues edit 7 --parent none                   # detach from its parent
cat notes.md | issues edit 7 -b -             # replace the body from stdin
```

To change an issue's state, use `move`. To change its labels, use `label`.

### `move`

Moves an issue to another column. The state name is not case-sensitive.

```sh
issues move 12 "in progress"
issues move 12 done
```

### `label`

```
issues label ID [+name ...] [-name ...]
```

A `+name` adds a label and a `-name` removes it. You can do both in one call.

```sh
issues label 12 +needs-triage
issues label 12 +ready-for-agent -needs-triage -needs-info
issues label 12 -- -wontfix                   # use `--` if your shell or argparse needs it
```

### `labels`

Lists every label and how many issues use it.

```sh
issues labels
```

### `comment`

```
issues comment ID TEXT|- [--author NAME]
```

```sh
issues comment 12 "Reproduced on main; stack trace in body."
issues comment 12 "Fixed in a1b2c3d" --author claude
pytest 2>&1 | tail -20 | issues comment 12 -
```

### `delete`

Deletes an issue together with its labels and comments. Its children are kept and lose their parent.

```sh
issues delete 12
```

### `tree`

Prints the hierarchy with indentation. Pass an ID to show only that issue's subtree.

```sh
issues tree
issues tree 1
```

```
#1 [saga] P1 Self-service accounts (Todo)
  #2 [epic] P1 Authentication (In Progress)
    #4 [story] P1 Password reset (Todo)
    #5 [story] P2 Remember me (Backlog)
  #3 [epic] P2 Profile page (Backlog)
#9 [bug] P1 Checkout fails with empty cart (Todo)
```

### `board`

```
issues board [-t TYPE] [-l LABEL] [--parent ID]
```

Prints the board as text: one section per column, with the cards in each column ordered by priority.

```sh
issues board
issues board -l ready-for-agent
issues board --parent 2                       # only the stories/tasks of epic #2
```

### `bugs`

A shortcut for `issues list -t bug`. It lists all bugs in every state.

```sh
issues bugs
```

### `states` and `state`

```
issues states
issues state add NAME [--position N]
issues state rename OLD NEW
issues state move NAME POSITION
issues state rm NAME [--move-to OTHER]
```

See [Customizing board columns](#customizing-board-columns).

### `gui`

Opens the Kanban board on the selected database.

```sh
issues gui
issues --db ~/work/issues.db gui
```

## Worked example

**1. Plan a saga, its epics and their stories.**

```sh
issues init
issues add "Self-service accounts" -t saga -p 1                       # -> #1
issues add "Authentication" -t epic --parent 1 -p 1                   # -> #2
issues add "Profile page"   -t epic --parent 1                        # -> #3
issues add "Password reset" -t story --parent 2 -p 1 \
  -b "Send a one-time link by email; link expires after 30 min."      # -> #4
issues add "Remember me"    -t story --parent 2                       # -> #5
issues add "Avatar upload"  -t story --parent 3 -p 3                  # -> #6
issues add "Add rate limit to reset endpoint" -t task --parent 4      # -> #7
```

The tracker enforces the hierarchy rules. For example, this command fails because a story can only have an epic as its parent:

```sh
issues add "Oops" -t story --parent 1
# error: ...   (exit code 1)
```

**2. Triage an incoming bug.**

```sh
issues add "Reset email never arrives" -t bug -l needs-triage         # -> #8
issues list -l needs-triage

# Needs more detail first:
issues label 8 +needs-info -needs-triage
issues comment 8 "Which mail provider? Anything in the spam folder?"

# The reporter answers; it is now clear and small:
issues comment 8 "Gmail only. SPF record missing for the reset sender." --author alice
issues edit 8 -p 0 --parent 4
issues label 8 -needs-info +ready-for-agent
```

**3. Move cards across the board.**

```sh
issues move 2 "In Progress"
issues move 4 todo
issues move 8 "in progress"
issues comment 8 "Added SPF include; waiting for DNS."
issues move 8 review
issues move 8 done
```

**4. Look at the result.**

```sh
issues tree 1        # the saga with its epics, stories, tasks and the bug under #4
issues board         # columns Backlog / Todo / In Progress / Review / Done
issues show 8        # full history of the bug, including all comments
```

## Customizing board columns

Columns are ordered by position. Positions start at 0, which is the leftmost column. New issues go into the leftmost column. By convention, the rightmost column is where finished work goes.

```sh
issues states                                   # show the columns in order
issues state add Blocked --position 3           # insert as the 4th column
issues state add Released                       # append at the end
issues state rename Review "Code Review"
issues state move Blocked 1                     # becomes the 2nd column
issues state rm Blocked --move-to Todo          # move its issues to Todo, then remove it
```

Removing a state that still has issues fails unless you pass `--move-to OTHER`. You cannot remove the last remaining state.

The board has the same controls. Open **Manage Columns** in the toolbar to add, rename, delete or reorder columns (Move up / Move down). If you delete a column that still has cards, the board asks which column to move them to.

## The Kanban board (GUI)

```sh
uv run issues gui        # or: uv run issues-board [--db PATH]
```

- **Toolbar**: New Issue, Refresh, Manage Columns, and the filters:
  - **Type**: show only one issue type
  - **Label**: show only issues with that label (for example `ready-for-human`)
  - **Parent**: show only the direct children of a saga or epic
  - **Search**: matches text in the title or body
- **Columns**: one column per state, from left to right. The header shows the column name and how many cards it holds. Cards are ordered by priority, with P0 at the top. Scroll sideways if there are many columns.
- **Cards**: each card shows the `#id`, the title, a colored type badge (bugs are red), the priority, the label chips and the parent (`↑ #id`).
- **Drag and drop**: drag a card to another column to change its state.
- **Edit dialog**: double-click a card to edit its title, type, priority, parent, state, labels and body.
  - The parent list only offers parents that are valid for the chosen type.
  - Enter labels as a comma-separated list.
  - The dialog shows the existing comments and has a box for adding a new one.
  - **Delete** removes the issue after you confirm.
- **Live refresh**: the board watches the database file and also refreshes every few seconds. Changes made with the CLI, whether by you, a script or an AI agent, appear without reloading. Leaving the board open while an agent works is a good way to follow its progress.

## Using with AI agents

The CLI is designed so that coding agents can use it without help:

- **Non-interactive**: commands never prompt and never open an editor.
- **`--json`**: every command can print structured output. Read commands print the objects. Commands that change something print the resulting issue or state, so an agent can read the new ID directly:
  ```sh
  issues --json add "Write migration" -t task --parent 4
  issues --json show 4
  issues --json list -l ready-for-agent
  ```
- **Bodies from stdin**: pass `-` as the body text so that long, multi-line or quote-heavy text needs no shell escaping:
  ```sh
  issues add "Design notes" -t story --parent 2 -b - <<'EOF'
  Multi-line markdown, "quotes", `backticks` -- all fine.
  EOF
  ```
- **Exit codes**: `0` means success. Any other value means failure, and the reason is printed as `error: ...` on stderr. Tracker errors such as an unknown ID, an invalid hierarchy or an unknown state exit with `1`.
- **Attribution**: set `ISSUES_AUTHOR` in the agent's environment so its comments are marked as its own.

### Suggested triage workflow

1. New reports come in with the `needs-triage` label.
2. Triage each one, done by a person or by an agent you ask to triage:
   - Set the type, priority and parent (`edit`).
   - If something is unclear, add `needs-info` and ask in a `comment`.
   - When it is actionable, replace the triage label with `ready-for-agent` (well specified and safe to do on its own) or `ready-for-human` (needs judgment, credentials or a design decision).
   - If it will not be done, add `wontfix` and move it to the last column.
3. Agents pull work with `issues --json list -l ready-for-agent -s Todo`. They `move` the issue to In Progress, post progress as `comment`s and move it to Review when finished.
4. A person reviews it and moves it to Done.

### Snippet for CLAUDE.md / AGENTS.md

Paste this into your project's agent instructions:

````markdown
## Issue tracking

This project tracks work with the `issues` CLI (SQLite file `issues.db` at the repo root).
Run it as `uv run issues ...`. Always pass `--json` (before the subcommand) when you need to
read results, e.g. `uv run issues --json list -l ready-for-agent`.

- Hierarchy: saga > epic > story, via `--parent ID`. A story's parent must be an epic, an
  epic's parent a saga. Tasks and bugs may hang under a saga, epic or story.
  Create: `uv run issues add "Title" -t story --parent 12 -p 2 -l needs-triage -b -` (body on stdin).
- Priority: `-p 0` (critical) .. `-p 4` (lowest); default 2.
- Triage labels: `needs-triage` (new), `needs-info` (blocked on a question; ask via comment),
  `ready-for-agent` (you may pick it up), `ready-for-human` (do not pick up), `wontfix`.
  Change with `uv run issues label ID +add -remove`.
- Only work on issues labeled `ready-for-agent` unless told otherwise.
- When you start: `uv run issues move ID "In Progress"`. When done: `uv run issues move ID Review`.
  Never move issues to Done yourself.
- Log progress, decisions and blockers with `uv run issues comment ID -` (text on stdin).
- If you discover new work or bugs, file them with `uv run issues add` under the right parent
  and label them `needs-triage` instead of silently expanding scope.
- Check `uv run issues states` for the actual column names; they are user-defined.
````

## Schema (for direct SQL)

The database is an ordinary SQLite file, so you can query it with `sqlite3 issues.db`. Foreign keys are enabled and `PRAGMA user_version` is `1`. Timestamps are ISO-8601 UTC strings with seconds precision.

| Table | Columns | Notes |
|-------|---------|-------|
| `states`       | `id`, `name` (unique, case-insensitive), `position` | Board columns, ordered by `position` |
| `issues`       | `id`, `title`, `body`, `type`, `priority`, `state_id` -> `states`, `parent_id` -> `issues`, `created_at`, `updated_at` | `type` is one of saga/epic/story/task/bug. `priority` is 0-4. When the parent is deleted, `parent_id` is set to NULL |
| `labels`       | `id`, `name` (unique, case-insensitive) | |
| `issue_labels` | `issue_id`, `label_id` | Many-to-many link between issues and labels. Rows are deleted together with the issue or label |
| `comments`     | `id`, `issue_id`, `author`, `body`, `created_at` | Deleted together with the issue |

Example queries:

```sql
-- open P0/P1 bugs with their column
SELECT i.id, i.priority, s.name AS state, i.title
FROM issues i JOIN states s ON s.id = i.state_id
WHERE i.type = 'bug' AND i.priority <= 1
ORDER BY i.priority, i.id;

-- issues per label
SELECT l.name, COUNT(il.issue_id) FROM labels l
LEFT JOIN issue_labels il ON il.label_id = l.id GROUP BY l.id ORDER BY l.name;
```

It is safe to read the database directly. To change data, prefer the CLI: it checks the hierarchy rules, updates `updated_at` and creates labels for you.
