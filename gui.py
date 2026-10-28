"""PyQt6 Kanban board for the tiny issue tracker.

Uses only the public API of ``tracker``. Run with ``uv run issues-board [--db PATH]``.
"""
from __future__ import annotations

import argparse
import html
import os
import sqlite3
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QFileSystemWatcher, QMimeData, QPoint, Qt, QTimer
from PyQt6.QtGui import QAction, QDrag, QGuiApplication, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QLayout, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QTextBrowser, QToolBar,
    QVBoxLayout, QWidget,
)

import tracker

MIME = "application/x-tiny-issue-id"
COL_W = 300
PRIO_NAMES = ("critical", "high", "medium", "low", "minimal")
# Which issue types may act as parent for a given type (mirrors tracker's hierarchy rules).
PARENT_TYPES = {
    "saga": (),
    "epic": ("saga",),
    "story": ("epic",),
    "task": ("saga", "epic", "story"),
    "bug": ("saga", "epic", "story"),
}
CHILD_TYPE = {"saga": "epic", "epic": "story", "story": "task"}

STYLE = """
QMainWindow, QDialog { background: #f6f7f9; }
QWidget { font-family: "Segoe UI", "Inter", "Helvetica Neue", sans-serif; font-size: 10pt; color: #1f2937; }
QToolBar { background: #ffffff; border: none; border-bottom: 1px solid #e5e7eb; padding: 6px 8px; spacing: 8px; }
QToolButton { padding: 5px 10px; border-radius: 6px; font-weight: 600; }
QToolButton:hover { background: #eef2ff; }
QToolButton:pressed { background: #e0e7ff; }
QLineEdit, QComboBox, QPlainTextEdit, QTextBrowser, QListWidget {
    background: #ffffff; border: 1px solid #d1d5db; border-radius: 6px; padding: 4px 6px; }
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border-color: #6366f1; }
QComboBox:disabled { color: #9ca3af; background: #f3f4f6; }
QAbstractItemView { background: #ffffff; color: #1f2937; selection-background-color: #e0e7ff;
    selection-color: #1e1b4b; outline: none; }
QListWidget::item { padding: 6px 4px; }
QPushButton { background: #ffffff; border: 1px solid #d1d5db; border-radius: 6px; padding: 5px 14px; }
QPushButton:hover { background: #f3f4f6; }
QPushButton:disabled { color: #9ca3af; }
QPushButton#primary { background: #4f46e5; color: #ffffff; border-color: #4f46e5; font-weight: 600; }
QPushButton#primary:hover { background: #4338ca; }
QPushButton#danger { color: #b91c1c; border-color: #fca5a5; }
QPushButton#danger:hover { background: #fef2f2; }
QStatusBar { background: #ffffff; border-top: 1px solid #e5e7eb; color: #6b7280; }
QScrollArea { background: transparent; border: none; }
QWidget#vp, QWidget#cards, QWidget#boardInner { background: transparent; }
QFrame#column { background: #eceff3; border: 2px solid transparent; border-radius: 10px; }
QFrame#column[drop="true"] { background: #e0e7ff; border: 2px dashed #6366f1; }
QLabel#colTitle { font-weight: 700; font-size: 11pt; }
QLabel#colCount { background: #d5d9e0; color: #374151; border-radius: 9px; padding: 1px 8px; font-size: 9pt; font-weight: 600; }
QFrame#card { background: #ffffff; border: 1px solid #e2e5ea; border-left: 4px solid #94a3b8; border-radius: 8px; }
QFrame#card:hover { border-top-color: #a5b4fc; border-right-color: #a5b4fc; border-bottom-color: #a5b4fc; }
QFrame#card[itype="saga"]  { border-left-color: #7c3aed; }
QFrame#card[itype="epic"]  { border-left-color: #2563eb; }
QFrame#card[itype="story"] { border-left-color: #059669; }
QFrame#card[itype="task"]  { border-left-color: #64748b; }
QFrame#card[itype="bug"]   { border-left-color: #dc2626; background: #fff5f5; }
QLabel#cardId { color: #6b7280; font-size: 9pt; font-weight: 600; }
QLabel#cardTitle { font-weight: 600; }
QLabel#badge { border-radius: 4px; padding: 1px 6px; font-size: 8pt; font-weight: 700; color: #ffffff; }
QLabel#badge[itype="saga"]  { background: #7c3aed; }
QLabel#badge[itype="epic"]  { background: #2563eb; }
QLabel#badge[itype="story"] { background: #059669; }
QLabel#badge[itype="task"]  { background: #64748b; }
QLabel#badge[itype="bug"]   { background: #dc2626; }
QLabel#prio { border-radius: 4px; padding: 1px 6px; font-size: 8pt; font-weight: 700; }
QLabel#prio[p="0"] { background: #fee2e2; color: #991b1b; }
QLabel#prio[p="1"] { background: #ffedd5; color: #9a3412; }
QLabel#prio[p="2"] { background: #fef9c3; color: #854d0e; }
QLabel#prio[p="3"] { background: #e0f2fe; color: #075985; }
QLabel#prio[p="4"] { background: #f1f5f9; color: #475569; }
QLabel#chip { background: #eef2ff; color: #3730a3; border-radius: 8px; padding: 1px 7px; font-size: 8pt; }
QLabel#parentRef, QLabel#muted { color: #6b7280; font-size: 9pt; }
QLabel#empty { color: #9ca3af; font-style: italic; padding: 12px; }
"""


def _label(text: str, name: str, **props) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName(name)
    for key, value in props.items():
        lbl.setProperty(key, value)
    return lbl


def warn(parent: QWidget | None, err: Exception | str) -> None:
    QMessageBox.warning(parent, "Issue tracker", str(err))


def parse_labels(text: str) -> list[str]:
    seen: dict[str, str] = {}
    for part in text.split(","):
        name = part.strip()
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return list(seen.values())


# --------------------------------------------------------------------------- board widgets

class Card(QFrame):
    """One issue on the board. Draggable; double-click opens the editor."""

    def __init__(self, issue: dict, titles: dict[int, str], main: "BoardWindow"):
        super().__init__()
        self.issue_id = issue["id"]
        self.main = main
        self._press: QPoint | None = None
        self.setObjectName("card")
        self.setProperty("itype", issue["type"])
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 9)
        lay.setSpacing(5)
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(_label(f"#{issue['id']}", "cardId"))
        top.addStretch()
        top.addWidget(_label(issue["type"].upper(), "badge", itype=issue["type"]))
        prio = issue["priority"]
        top.addWidget(_label(f"P{prio}", "prio", p=str(prio)))
        lay.addLayout(top)

        title = _label(issue["title"], "cardTitle")
        title.setWordWrap(True)
        lay.addWidget(title)

        if issue.get("labels"):
            lay.addLayout(self._chips(issue["labels"], COL_W - 60))
        parent_id = issue.get("parent_id")
        if parent_id:
            ref = _label(f"↑ #{parent_id}  {titles.get(parent_id, '')}", "parentRef")
            ref.setWordWrap(True)
            ref.setToolTip(f"Parent #{parent_id}: {titles.get(parent_id, '?')}")
            lay.addWidget(ref)
        body = (issue.get("body") or "").strip()
        tip = f"#{issue['id']} {issue['title']}\n{issue['type']} · P{prio} {PRIO_NAMES[prio]}"
        self.setToolTip(tip + (f"\n\n{body[:400]}" if body else ""))

    @staticmethod
    def _chips(labels: list[str], width: int) -> QVBoxLayout:
        """Simple wrapping row layout for label chips."""
        box = QVBoxLayout()
        box.setSpacing(3)
        row, used = None, width
        for name in labels:
            chip = _label(name, "chip")
            w = chip.fontMetrics().horizontalAdvance(name) + 22
            if row is None or used + w > width:
                row = QHBoxLayout()
                row.setSpacing(4)
                row.addStretch()
                box.addLayout(row)
                used = 0
            row.insertWidget(row.count() - 1, chip)
            used += w
        return box

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press is None or not (e.buttons() & Qt.MouseButton.LeftButton):
            return
        if (e.position().toPoint() - self._press).manhattanLength() < QApplication.startDragDistance():
            return
        hot, self._press = self._press, None
        mime = QMimeData()
        mime.setData(MIME, str(self.issue_id).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self.grab())
        drag.setHotSpot(hot)
        self.main.begin_drag()
        try:
            drag.exec(Qt.DropAction.MoveAction)
        finally:
            self.main.end_drag()

    def mouseReleaseEvent(self, e):
        self._press = None
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        self._press = None
        self.main.edit_issue(self.issue_id)


class Column(QFrame):
    """A Kanban column (= tracker state). Accepts dropped cards."""

    def __init__(self, main: "BoardWindow"):
        super().__init__()
        self.main = main
        self.state_name = ""
        self.issue_ids: set[int] = set()
        self._sig: object = None
        self.setObjectName("column")
        self.setAcceptDrops(True)
        self.setFixedWidth(COL_W)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 10, 4, 8)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.setContentsMargins(4, 0, 8, 0)
        self.title = _label("", "colTitle")
        self.count = _label("0", "colCount")
        head.addWidget(self.title)
        head.addStretch()
        head.addWidget(self.count)
        lay.addLayout(head)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.viewport().setObjectName("vp")
        inner = QWidget()
        inner.setObjectName("cards")
        self.cards = QVBoxLayout(inner)
        self.cards.setContentsMargins(0, 0, 4, 0)
        self.cards.setSpacing(8)
        self.cards.addStretch()
        self.scroll.setWidget(inner)
        lay.addWidget(self.scroll)

    def set_data(self, state: dict, issues: list[dict], titles: dict[int, str]) -> None:
        self.state_name = state["name"]
        self.title.setText(state["name"])
        self.count.setText(str(len(issues)))
        sig = [(i["id"], i["title"], i["type"], i["priority"], tuple(i.get("labels") or ()),
                i.get("parent_id"), titles.get(i.get("parent_id")), i.get("updated_at"))
               for i in issues]
        if sig == self._sig:
            return  # nothing changed: keep widgets (no flicker, scroll position untouched)
        self._sig = sig
        bar = self.scroll.verticalScrollBar()
        pos = bar.value()
        while self.cards.count() > 1:
            w = self.cards.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        for idx, issue in enumerate(issues):
            self.cards.insertWidget(idx, Card(issue, titles, self.main))
        if not issues:
            self.cards.insertWidget(0, _label("No issues", "empty"))
        self.issue_ids = {i["id"] for i in issues}
        if pos:
            QTimer.singleShot(0, lambda: bar.setValue(pos))

    def invalidate(self) -> None:
        self._sig = None

    def _highlight(self, on: bool) -> None:
        self.setProperty("drop", on)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(MIME):
            e.acceptProposedAction()
            self._highlight(True)

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(MIME):
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._highlight(False)

    def dropEvent(self, e):
        self._highlight(False)
        if not e.mimeData().hasFormat(MIME):
            return
        issue_id = int(e.mimeData().data(MIME).data().decode())
        e.acceptProposedAction()
        if issue_id not in self.issue_ids:
            self.main.move_issue(issue_id, self.state_name)

    def mouseDoubleClickEvent(self, e):
        self.main.new_issue(state=self.state_name)


# --------------------------------------------------------------------------- dialogs

class IssueDialog(QDialog):
    """Create (issue_id=None) or edit an issue."""

    def __init__(self, conn, issue_id: int | None = None, parent: QWidget | None = None, *,
                 state: str | None = None, parent_issue: dict | None = None):
        super().__init__(parent)
        self.conn = conn
        self.issue = tracker.get_issue(conn, issue_id) if issue_id is not None else None
        self.created_id: int | None = None
        self.all_issues = tracker.list_issues(conn, sort="id")
        self.setWindowTitle(f"Edit #{issue_id}" if self.issue else "New issue")
        self.resize(640, 760 if self.issue else 560)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        if self.issue:
            i = self.issue
            info = f"#{i['id']} · created {i['created_at']} · updated {i['updated_at']}"
            if i.get("children"):
                info += f" · {len(i['children'])} child issue(s)"
            lay.addWidget(_label(info, "muted"))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.title_edit = QLineEdit()
        self.type_cb = QComboBox()
        for t in tracker.TYPES:
            self.type_cb.addItem(t, t)
        self.prio_cb = QComboBox()
        for p in tracker.PRIORITIES:
            self.prio_cb.addItem(f"P{p} – {PRIO_NAMES[p]}", p)
        self.parent_cb = QComboBox()
        self.parent_cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.state_cb = QComboBox()
        for s in tracker.list_states(conn):
            self.state_cb.addItem(s["name"], s["name"])
        self.labels_edit = QLineEdit()
        self.labels_edit.setPlaceholderText("comma-separated, e.g. needs-triage, ui")
        known = ", ".join(lbl["name"] for lbl in tracker.list_labels(conn))
        self.labels_edit.setToolTip(f"Known labels: {known}" if known else "")
        self.body_edit = QPlainTextEdit()
        self.body_edit.setPlaceholderText("Description (markdown welcome)")

        row = QHBoxLayout()
        row.addWidget(self.type_cb, 1)
        row.addWidget(QLabel("Priority"))
        row.addWidget(self.prio_cb, 1)
        row.addWidget(QLabel("State"))
        row.addWidget(self.state_cb, 1)
        form.addRow("Title", self.title_edit)
        form.addRow("Type", row)
        form.addRow("Parent", self.parent_cb)
        form.addRow("Labels", self.labels_edit)
        form.addRow("Body", self.body_edit)
        lay.addLayout(form, 3)

        # initial values
        if self.issue:
            i = self.issue
            self.title_edit.setText(i["title"])
            self.type_cb.setCurrentIndex(self.type_cb.findData(i["type"]))
            self.prio_cb.setCurrentIndex(self.prio_cb.findData(i["priority"]))
            self.state_cb.setCurrentIndex(max(0, self.state_cb.findText(i["state"], Qt.MatchFlag.MatchFixedString)))
            self.labels_edit.setText(", ".join(i["labels"]))
            self.body_edit.setPlainText(i["body"])
            self._fill_parents(i["parent_id"])
        else:
            default_type = CHILD_TYPE.get(parent_issue["type"], "story") if parent_issue else "story"
            self.type_cb.setCurrentIndex(self.type_cb.findData(default_type))
            self.prio_cb.setCurrentIndex(self.prio_cb.findData(2))
            if state:
                self.state_cb.setCurrentIndex(max(0, self.state_cb.findData(state)))
            self._fill_parents(parent_issue["id"] if parent_issue else None)
        self.type_cb.currentIndexChanged.connect(lambda _: self._fill_parents())

        # comments (existing issues only)
        if self.issue:
            lay.addWidget(_label("Comments", "colTitle"))
            self.comments_view = QTextBrowser()
            self.comments_view.setOpenExternalLinks(True)
            lay.addWidget(self.comments_view, 2)
            crow = QHBoxLayout()
            self.comment_edit = QPlainTextEdit()
            self.comment_edit.setPlaceholderText("Add a comment…")
            self.comment_edit.setFixedHeight(56)
            add_btn = QPushButton("Add comment")
            add_btn.clicked.connect(self._add_comment)
            crow.addWidget(self.comment_edit, 1)
            crow.addWidget(add_btn, 0, Qt.AlignmentFlag.AlignBottom)
            lay.addLayout(crow)
            self._render_comments()

        buttons = QHBoxLayout()
        if self.issue:
            delete = QPushButton("Delete…")
            delete.setObjectName("danger")
            delete.clicked.connect(self._delete)
            buttons.addWidget(delete)
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save" if self.issue else "Create")
        save.setObjectName("primary")
        save.setDefault(True)
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        lay.addLayout(buttons)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self._save)
        self.title_edit.setFocus()

    def _fill_parents(self, keep: int | None | bool = False) -> None:
        current = self.parent_cb.currentData() if keep is False else keep
        itype = self.type_cb.currentData()
        allowed = PARENT_TYPES[itype]
        own = self.issue["id"] if self.issue else None
        self.parent_cb.clear()
        self.parent_cb.addItem("(none)", None)
        for i in self.all_issues:
            if i["type"] in allowed and i["id"] != own:
                self.parent_cb.addItem(f"#{i['id']} [{i['type']}] {i['title']}", i["id"])
        idx = self.parent_cb.findData(current) if current is not None else 0
        self.parent_cb.setCurrentIndex(max(idx, 0))
        self.parent_cb.setEnabled(bool(allowed))

    def _render_comments(self) -> None:
        comments = self.issue.get("comments") or []
        if not comments:
            self.comments_view.setHtml("<p style='color:#9ca3af'><i>No comments yet.</i></p>")
            return
        parts = []
        for c in comments:
            body = html.escape(c["body"]).replace("\n", "<br>")
            parts.append(f"<p><b>{html.escape(c['author'])}</b> "
                         f"<span style='color:#6b7280'>{html.escape(c['created_at'])}</span><br>{body}</p>")
        self.comments_view.setHtml("".join(parts))
        bar = self.comments_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _add_comment(self) -> None:
        text = self.comment_edit.toPlainText().strip()
        if not text:
            return
        try:
            tracker.add_comment(self.conn, self.issue["id"], text)
            self.issue["comments"] = tracker.get_issue(self.conn, self.issue["id"])["comments"]
        except tracker.TrackerError as e:
            warn(self, e)
            return
        self.comment_edit.clear()
        self._render_comments()

    def _save(self) -> None:
        title = self.title_edit.text().strip()
        if not title:
            warn(self, "Title must not be empty.")
            return
        values = {
            "title": title,
            "type": self.type_cb.currentData(),
            "priority": self.prio_cb.currentData(),
            "parent_id": self.parent_cb.currentData(),
            "body": self.body_edit.toPlainText(),
        }
        state = self.state_cb.currentData()
        labels = parse_labels(self.labels_edit.text())
        try:
            if self.issue is None:
                self.created_id = tracker.create_issue(self.conn, title, type=values["type"],
                                                       priority=values["priority"], parent_id=values["parent_id"],
                                                       state=state, body=values["body"], labels=labels)
            else:
                iid = self.issue["id"]
                changed = {k: v for k, v in values.items() if v != self.issue[k]}
                if changed:
                    tracker.update_issue(self.conn, iid, **changed)
                if state and state.lower() != self.issue["state"].lower():
                    tracker.move_issue(self.conn, iid, state)
                old = {n.lower(): n for n in self.issue["labels"]}
                new = {n.lower(): n for n in labels}
                add = [new[k] for k in new if k not in old]
                remove = [old[k] for k in old if k not in new]
                if add:
                    tracker.add_labels(self.conn, iid, add)
                if remove:
                    tracker.remove_labels(self.conn, iid, remove)
        except tracker.TrackerError as e:
            if self.issue is not None:  # re-baseline so a retry diffs against what was saved
                try:
                    self.issue = tracker.get_issue(self.conn, self.issue["id"])
                except tracker.TrackerError:
                    pass
            warn(self, e)
            return
        self.accept()

    def _delete(self) -> None:
        i = self.issue
        answer = QMessageBox.question(
            self, "Delete issue",
            f"Delete #{i['id']} “{i['title']}”?\nChild issues are kept but lose their parent. This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            tracker.delete_issue(self.conn, i["id"])
        except tracker.TrackerError as e:
            warn(self, e)
            return
        self.accept()


class ColumnsDialog(QDialog):
    """Add / rename / delete / reorder states."""

    def __init__(self, conn, parent: QWidget | None = None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Manage columns")
        self.resize(420, 380)
        lay = QHBoxLayout(self)
        self.list = QListWidget()
        lay.addWidget(self.list, 1)
        side = QVBoxLayout()
        for text, slot in (("Add…", self._add), ("Rename…", self._rename), ("Delete…", self._delete),
                           ("Move up", lambda: self._shift(-1)), ("Move down", lambda: self._shift(1))):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            side.addWidget(btn)
        side.addStretch()
        close = QPushButton("Close")
        close.setObjectName("primary")
        close.clicked.connect(self.accept)
        side.addWidget(close)
        lay.addLayout(side)
        self.list.itemDoubleClicked.connect(lambda _: self._rename())
        self.reload()

    def reload(self, select: str | None = None) -> None:
        if select is None and self.list.currentItem():
            select = self.list.currentItem().data(Qt.ItemDataRole.UserRole)
        self.counts = {s["name"]: len(issues) for s, issues in tracker.board(self.conn)}
        self.list.clear()
        for s in tracker.list_states(self.conn):
            item = QListWidgetItem(f"{s['name']}   ({self.counts.get(s['name'], 0)})")
            item.setData(Qt.ItemDataRole.UserRole, s["name"])
            self.list.addItem(item)
            if select and s["name"].lower() == select.lower():
                self.list.setCurrentItem(item)

    def _selected(self) -> str | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _run(self, fn, *args, select: str | None = None, **kwargs) -> None:
        try:
            fn(self.conn, *args, **kwargs)
        except tracker.TrackerError as e:
            warn(self, e)
        self.reload(select)

    def _add(self) -> None:
        name, ok = QInputDialog.getText(self, "Add column", "Column name:")
        if ok and name.strip():
            self._run(tracker.add_state, name.strip(), select=name.strip())

    def _rename(self) -> None:
        old = self._selected()
        if not old:
            return
        new, ok = QInputDialog.getText(self, "Rename column", f"New name for “{old}”:", text=old)
        if ok and new.strip() and new.strip() != old:
            self._run(tracker.rename_state, old, new.strip(), select=new.strip())

    def _delete(self) -> None:
        name = self._selected()
        if not name:
            return
        count = self.counts.get(name, 0)
        if count:
            others = [self.list.item(r).data(Qt.ItemDataRole.UserRole) for r in range(self.list.count())]
            others = [o for o in others if o != name]
            if not others:
                warn(self, "Cannot delete the last column.")
                return
            target, ok = QInputDialog.getItem(
                self, "Delete column", f"“{name}” contains {count} issue(s).\nMove them to:", others, 0, False)
            if ok:
                self._run(tracker.remove_state, name, move_to=target)
        elif QMessageBox.question(self, "Delete column", f"Delete empty column “{name}”?") \
                == QMessageBox.StandardButton.Yes:
            self._run(tracker.remove_state, name)

    def _shift(self, delta: int) -> None:
        name, row = self._selected(), self.list.currentRow()
        if name and 0 <= row + delta < self.list.count():
            self._run(tracker.move_state, name, row + delta, select=name)


# --------------------------------------------------------------------------- main window

class BoardWindow(QMainWindow):
    def __init__(self, conn, db_path: str | os.PathLike):
        super().__init__()
        self.conn = conn
        self.db_path = Path(db_path)
        self.columns: dict[int, Column] = {}
        self.issue_types: dict[int, dict] = {}
        self._dragging = False
        self.setWindowTitle(f"Issues — {self.db_path}")
        self.resize(1400, 820)
        self._build_toolbar()

        self.board_scroll = QScrollArea()
        self.board_scroll.setWidgetResizable(True)
        self.board_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.board_scroll.viewport().setObjectName("vp")
        inner = QWidget()
        inner.setObjectName("boardInner")
        self.board_layout = QHBoxLayout(inner)
        self.board_layout.setContentsMargins(14, 14, 14, 14)
        self.board_layout.setSpacing(12)
        self.board_layout.addStretch()
        # make the scroll area honour the columns' fixed widths (scroll horizontally instead of overlapping)
        self.board_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.board_scroll.setWidget(inner)
        self.setCentralWidget(self.board_scroll)

        # live refresh: file watcher (debounced) + polling fallback
        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(lambda _p: self._debounce.start())
        self._debounce = QTimer(self, singleShot=True, interval=150)
        self._debounce.timeout.connect(self.refresh)
        self._poll = QTimer(self, interval=3000)
        self._poll.timeout.connect(self.refresh)
        self._poll.start()
        self.refresh()

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)
        for text, shortcut, slot in (("＋  New Issue", "Ctrl+N", lambda: self.new_issue()),
                                     ("⟳  Refresh", "F5", lambda: self.refresh(force=True)),
                                     ("☰  Manage Columns", None, self.manage_columns)):
            act = QAction(text, self)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            act.triggered.connect(slot)
            tb.addAction(act)
        tb.addSeparator()
        self.type_cb = QComboBox()
        self.type_cb.addItem("All types", None)
        for t in tracker.TYPES:
            self.type_cb.addItem(t.capitalize(), t)
        self.label_cb = QComboBox()
        self.label_cb.addItem("All labels", None)
        self.parent_cb = QComboBox()
        self.parent_cb.addItem("Any parent", None)
        self.parent_cb.setMinimumContentsLength(18)
        self.parent_cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title / body…")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(220)
        self._search_timer = QTimer(self, singleShot=True, interval=250)
        self._search_timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda _: self._search_timer.start())
        for cb in (self.type_cb, self.label_cb, self.parent_cb):
            cb.currentIndexChanged.connect(lambda _: self.refresh())
            tb.addWidget(cb)
        tb.addWidget(self.search)

    # ---- data

    def filters(self) -> dict:
        f = {}
        if self.type_cb.currentData():
            f["type"] = self.type_cb.currentData()
        if self.label_cb.currentData():
            f["label"] = self.label_cb.currentData()
        if self.parent_cb.currentData() is not None:
            f["parent_id"] = self.parent_cb.currentData()
        if self.search.text().strip():
            f["search"] = self.search.text().strip()
        return f

    def _busy(self) -> bool:
        return self._dragging or QApplication.activeModalWidget() is not None

    def _ensure_watch(self) -> None:
        watched = {os.path.normcase(os.path.abspath(p)) for p in self.watcher.files()}
        for p in (self.db_path, self.db_path.with_name(self.db_path.name + "-wal")):
            if p.exists() and os.path.normcase(os.path.abspath(p)) not in watched:
                self.watcher.addPath(str(p))

    @staticmethod
    def _sync_combo(combo: QComboBox, items: list[tuple[str, object]]) -> bool:
        """Replace all but the first ("All…") entry; keep selection. Returns True if selection was lost."""
        existing = [(combo.itemText(i), combo.itemData(i)) for i in range(1, combo.count())]
        if existing == items:
            return False
        current = combo.currentData()
        combo.blockSignals(True)
        while combo.count() > 1:
            combo.removeItem(1)
        for text, data in items:
            combo.addItem(text, data)
        idx = combo.findData(current) if current is not None else 0
        combo.setCurrentIndex(max(idx, 0))
        combo.blockSignals(False)
        return current is not None and idx < 0

    def refresh(self, force: bool = False) -> None:
        if self._busy():
            return  # retried by the poll timer / after the dialog or drag ends
        self._ensure_watch()
        try:
            all_issues = tracker.list_issues(self.conn, sort="id")
            labels = tracker.list_labels(self.conn)
            self.issue_types = {i["id"]: i for i in all_issues}
            lost = self._sync_combo(self.label_cb, [(f"{l['name']} ({l['count']})", l["name"]) for l in labels])
            lost |= self._sync_combo(self.parent_cb, [
                (f"#{i['id']} [{i['type']}] {i['title']}", i["id"])
                for i in all_issues if i["type"] in ("saga", "epic")])
            data = tracker.board(self.conn, **self.filters())
        except (tracker.TrackerError, sqlite3.Error) as e:
            self.statusBar().showMessage(f"Refresh failed: {e}")
            return
        titles = {i["id"]: i["title"] for i in all_issues}

        board = self.board_scroll.widget()
        board.setUpdatesEnabled(False)
        try:
            ids = [s["id"] for s, _ in data]
            for sid in [s for s in self.columns if s not in ids]:
                col = self.columns.pop(sid)
                self.board_layout.removeWidget(col)
                col.hide()
                col.deleteLater()
            for idx, (state, issues) in enumerate(data):
                col = self.columns.get(state["id"])
                if col is None:
                    col = self.columns[state["id"]] = Column(self)
                if self.board_layout.indexOf(col) != idx:
                    self.board_layout.removeWidget(col)
                    self.board_layout.insertWidget(idx, col)
                if force:
                    col.invalidate()
                col.set_data(state, issues, titles)
        finally:
            board.setUpdatesEnabled(True)
        shown = sum(len(issues) for _, issues in data)
        self.statusBar().showMessage(
            f"{shown} of {len(all_issues)} issues shown  ·  {self.db_path}  ·  synced {time.strftime('%H:%M:%S')}")
        if lost:  # a selected filter value disappeared -> re-query with the reset filter
            QTimer.singleShot(0, self.refresh)

    # ---- actions

    def begin_drag(self) -> None:
        self._dragging = True

    def end_drag(self) -> None:
        self._dragging = False
        QTimer.singleShot(0, self.refresh)

    def move_issue(self, issue_id: int, state: str) -> None:
        """Code path used by drag-and-drop."""
        try:
            tracker.move_issue(self.conn, issue_id, state)
        except tracker.TrackerError as e:
            warn(self, e)
        self.refresh()

    def _run_dialog(self, dlg: QDialog) -> int:
        result = dlg.exec()
        self.refresh()
        return result

    def new_issue(self, state: str | None = None) -> None:
        parent_issue = self.issue_types.get(self.parent_cb.currentData())
        self._run_dialog(IssueDialog(self.conn, None, self, state=state, parent_issue=parent_issue))

    def edit_issue(self, issue_id: int) -> None:
        try:
            dlg = IssueDialog(self.conn, issue_id, self)
        except tracker.TrackerError as e:
            warn(self, e)
            self.refresh()
            return
        self._run_dialog(dlg)

    def manage_columns(self) -> None:
        self._run_dialog(ColumnsDialog(self.conn, self))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="issues-board", description="Kanban board for tiny-issue-tracker")
    parser.add_argument("--db", metavar="PATH", help="database file (default: tracker.find_db())")
    args, _unknown = parser.parse_known_args(argv)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Issues")
    hints = QGuiApplication.styleHints()
    if hasattr(hints, "setColorScheme"):  # Qt >= 6.8: force light palette to match the stylesheet
        hints.setColorScheme(Qt.ColorScheme.Light)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    try:
        path = tracker.find_db(args.db)
        conn = tracker.connect(path)
    except (tracker.TrackerError, sqlite3.Error, OSError) as e:
        QMessageBox.critical(None, "Issues", f"Cannot open database:\n{e}")
        return 1
    win = BoardWindow(conn, path)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
