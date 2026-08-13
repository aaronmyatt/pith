"""pith — a keyboard-driven, file-at-a-time skeleton browser for codebases.

One file at a time: definitions + signatures + docstrings, with the calls each
definition makes linked out to the files that define them. Implementation
details stay hidden until you jump to your editor. When `fzf` is available,
startup picks the file with fzf first (instant) so the slower Textual boot
lands straight in the file; otherwise pith's built-in picker opens.

Navigation is vim-flavoured: `l` (or right arrow) drills down (expand a
definition, descend into its calls, or follow a call/import into another
file), `h`, backspace, or left arrow walks back up (collapse, move to parent,
or pop back to the previous file; at the very root, a second press reopens
the fuzzy file search). `/` starts an
incremental search that narrows the current screen to anything visible on it —
definitions, calls, imports, doc lines — and the narrow follows you as you
drill across files.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from rich.syntax import Syntax
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, LoadingIndicator, OptionList, Static, Tree
from textual.widgets.option_list import Option

from .config import UserCommand, load_user_commands
from .indexer import Definition, FileView, Indexer, Location

KIND_ICON = {
    "class": ("◆", "bright_magenta"),
    "interface": ("◇", "bright_magenta"),
    "function": ("ƒ", "bright_cyan"),
    "method": ("ƒ", "cyan"),
    "constant": ("π", "yellow"),
    "module": ("▤", "blue"),
    "type": ("τ", "bright_magenta"),
    "enum": ("∷", "yellow"),
    "key": ("∙", "grey70"),
    "table": ("▥", "bright_blue"),
    "section": ("§", "green"),
}

MAX_DOC_LINES = 14
MAX_DOC_LINE_CHARS = 100  # doc lines longer than this collapse; drill in to read the rest

# Next.js App Router special files and what they contribute to a route segment.
# Ref: https://nextjs.org/docs/app/api-reference/file-conventions
_NEXT_SPECIAL = {
    "page": "page", "layout": "layout", "route": "API route",
    "loading": "loading UI", "error": "error UI", "global-error": "global error UI",
    "not-found": "not-found UI", "template": "template",
    "default": "parallel-route fallback",
}
_NEXT_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs"}


def _nextjs_role(path: str) -> str | None:
    """Route annotation ("/dashboard · page") for Next.js convention files.

    Route groups "(group)", parallel slots "@slot" and private "_folders" are
    dropped from the displayed route, matching how Next.js maps URLs; dynamic
    "[param]" segments are kept verbatim.
    Ref: https://nextjs.org/docs/app/api-reference/file-conventions/dynamic-routes
    """
    p = Path(path)
    if p.suffix not in _NEXT_EXTS:
        return None
    parts = p.parts
    in_src = len(parts) > 1 and parts[0] == "src"
    if p.stem in ("middleware", "instrumentation") and len(parts) == (2 if in_src else 1):
        return p.stem
    for router in ("app", "pages"):
        if router not in parts:
            continue
        i = parts.index(router)
        if i != (1 if in_src else 0):
            continue
        segs = [s for s in parts[i + 1:-1]
                if not (s.startswith("(") or s.startswith("@") or s.startswith("_"))]
        if router == "app":
            role = _NEXT_SPECIAL.get(p.stem)
            if role is None:
                return None
            return f"/{'/'.join(segs)} · {role}"
        # Pages Router. Ref: https://nextjs.org/docs/pages/building-your-application/routing
        if p.stem in ("_app", "_document", "_error"):
            return f"custom {p.stem.lstrip('_')}"
        if p.stem != "index":
            segs.append(p.stem)
        role = "API route" if segs[:1] == ["api"] else "page"
        return f"/{'/'.join(segs)} · {role}".replace("// ", "/ ")
    return None


def _def_label(d: Definition) -> Text:
    icon, color = KIND_ICON.get(d.kind, ("•", "white"))
    t = Text()
    t.append(f"{icon} ", style=color)
    sig = d.signature or d.name
    idx = sig.find(d.name)
    if idx >= 0:
        t.append(sig[:idx], style="bright_white")
        t.append(d.name, style=f"bold {color}")
        t.append(sig[idx + len(d.name):], style="bright_white")
    else:
        t.append(sig, style="bright_white")
    t.append(f"  :{d.line}", style="dim")
    if d.docstring:
        first = d.docstring.splitlines()[0]
        t.append(f"  {first[:70]}", style="italic dim")
    return t


def _ref_label(name: str, targets: list[Location]) -> Text:
    t = Text()
    if targets:
        loc = targets[0]
        t.append("→ ", style="green")
        t.append(name, style="green")
        t.append(f"  {loc.path}:{loc.line}", style="dim")
        if len(targets) > 1:
            t.append(f"  (+{len(targets) - 1} more)", style="dim yellow")
    else:
        t.append("→ ", style="dim")
        t.append(name, style="dim")
    return t


def _search_text(name: str, targets: list[Location]) -> str:
    """Searchable text for a reference: name + resolved target path:line."""
    t = name
    if targets:
        t += f" {targets[0].path}:{targets[0].line}"
        if len(targets) > 1:
            t += f" (+{len(targets) - 1} more)"
    return t


_DOC_STYLE = "italic #8a8a8a"
_md_syntax: Syntax | None = None


def _doc_text(s: str, lang: str | None) -> Text:
    """Style one line of a definition's doc/comment body.

    Markdown section bodies are prose, not comments — highlight them with
    real markdown syntax colouring (headings/bold/code/links) instead of the
    plain dim-italic look every other language's docstrings/comments get.
    Ref: https://rich.readthedocs.io/en/stable/syntax.html
    """
    if lang == "markdown" and s.strip():
        global _md_syntax
        if _md_syntax is None:
            _md_syntax = Syntax("", "markdown", theme="ansi_dark", background_color="default")
        try:
            t = _md_syntax.highlight(s)
            if t.plain.endswith("\n"):
                t = t[: len(t) - 1]
            return t
        except Exception:
            pass
    return Text(s, style=_DOC_STYLE)


class PickScreen(ModalScreen):
    """Generic filterable picker (files, symbols, ambiguous targets)."""

    BINDINGS = [Binding("escape", "dismiss(None)", "cancel", show=False)]

    DEFAULT_CSS = """
    PickScreen { align: center middle; }
    PickScreen > Vertical {
        width: 80%; max-width: 100; height: 70%;
        border: round $accent; background: $surface; padding: 1;
    }
    PickScreen Input { margin-bottom: 1; }
    PickScreen OptionList { height: 1fr; }
    """

    def __init__(self, title: str, items: list[tuple[str, object]], with_input: bool = True):
        super().__init__()
        self.title_text = title
        self.items = items
        self.with_input = with_input

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(self.title_text, style="bold"))
            if self.with_input:
                yield Input(placeholder="type to filter…")
            yield OptionList()

    def on_mount(self) -> None:
        self._refill("")
        if self.with_input:
            self.query_one(Input).focus()
        else:
            self.query_one(OptionList).focus()

    def _refill(self, needle: str) -> None:
        ol = self.query_one(OptionList)
        ol.clear_options()
        needle = needle.lower()
        shown = 0
        for i, (label, _val) in enumerate(self.items):
            if needle and needle not in label.lower():
                continue
            ol.add_option(Option(label, id=str(i)))
            shown += 1
            if shown >= 400:
                break
        if shown:
            ol.highlighted = 0

    def on_input_changed(self, ev: Input.Changed) -> None:
        self._refill(ev.value)

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        ol = self.query_one(OptionList)
        if ol.highlighted is not None and ol.option_count:
            opt = ol.get_option_at_index(ol.highlighted)
            self.dismiss(self.items[int(opt.id)][1])

    def on_key(self, ev) -> None:
        if ev.key in ("down", "up") and self.with_input and isinstance(self.focused, Input):
            ol = self.query_one(OptionList)
            ol.focus()
            if ol.option_count:
                ol.highlighted = 0 if ev.key == "down" else ol.option_count - 1
            ev.stop()

    def on_option_list_option_selected(self, ev: OptionList.OptionSelected) -> None:
        self.dismiss(self.items[int(ev.option.id)][1])


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(None)", "close", show=False),
                Binding("question_mark", "dismiss(None)", "close", show=False)]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > Static {
        width: auto; padding: 1 3; border: round $accent; background: $surface;
    }
    """

    HELP = """\
[bold]pith — keys[/bold]

  j / k, ↑ / ↓      move
  l / →             drill down: expand a definition · descend into its calls
  h / backspace / ← walk up: collapse · move to parent · back a file
                    (at the root, press twice for the fuzzy file search)
  enter / click     drill down (same as l): expand · descend · follow call
  e  or  ctrl+enter open current item in $EDITOR at its line
  b                 back to previous file
  f                 file picker
  /                 search: narrow the current screen as you type
                    (defs, calls, imports, doc lines — whatever is visible)
  enter             commit the narrowed view · esc cancel / clear
  m / M             bookmark current line (toggle) / list & jump bookmarks
  g                 jump to any symbol in the repo (project-wide)
  x / z             expand all / collapse all
  r                 re-parse current file
  ?                 this help · q quit

  custom keys       register scripts in ~/.config/pith/config.json or
                    <repo>/.pith.json — selection arrives in PITH_* env vars,
                    snippet on stdin

  [yellow]★[/yellow]         bookmarked line · [cyan]A › b[/cyan]   current location
  [green]→ name  path:line[/green]   call that resolves to a project definition
  [dim]→ name[/dim]             call into stdlib / dependencies (unresolved)
"""

    def compose(self) -> ComposeResult:
        yield Static(self.HELP)

    def on_click(self) -> None:
        self.dismiss(None)


class SkeletonTree(Tree):
    """Tree whose cursor treats a run of doc-comment lines as one stop.

    Docstrings render as one leaf per line (kind "doc"), so the stock cursor
    stops on every line. Overriding the cursor actions here covers every entry
    point at once: the arrow keys (Tree's own up/down bindings) and the app's
    j/k bindings, which delegate to these same actions.
    Ref: https://textual.textualize.io/widgets/tree/#textual.widgets.Tree.action_cursor_down
    """

    def _is_doc_line(self, line: int) -> bool:
        # get_node_at_line maps a visible (expanded) line index to its node,
        # or None past either end of the tree.
        # Ref: https://textual.textualize.io/widgets/tree/#textual.widgets.Tree.get_node_at_line
        node = self.get_node_at_line(line)
        return (node is not None and isinstance(node.data, dict)
                and node.data.get("kind") == "doc")

    def _cursor_step(self, delta: int) -> None:
        line = self.cursor_line  # -1 while the tree has no cursor yet
        dest = line + delta
        if self._is_doc_line(line):
            # already on a block's first line — hop past its remaining lines
            while dest >= 0 and self._is_doc_line(dest):
                dest += delta
        elif delta < 0 and self._is_doc_line(dest):
            # entering a block from below — snap to its first line so the
            # block is one stop in both directions
            while dest > 0 and self._is_doc_line(dest - 1):
                dest -= 1
        node = self.get_node_at_line(dest) if dest >= 0 else None
        if node is not None:
            # move_cursor also scrolls the target into view.
            # Ref: https://textual.textualize.io/widgets/tree/#textual.widgets.Tree.move_cursor
            self.move_cursor(node)

    def action_cursor_down(self) -> None:
        if self.cursor_line < 0:
            super().action_cursor_down()
        else:
            self._cursor_step(1)

    def action_cursor_up(self) -> None:
        if self.cursor_line < 0:
            super().action_cursor_up()
        else:
            self._cursor_step(-1)

    # ScrollView (an ancestor class) binds left/right to horizontal scrolling,
    # which — being declared on the focused widget itself — intercepts the key
    # before it would otherwise bubble up to PithApp's own left/right bindings.
    # Repoint them at the app's h/l actions instead, so arrow keys are full
    # aliases for h/l rather than scrolling a tree that never scrolls sideways.
    # Ref: https://textual.textualize.io/guide/input/#bindings
    def action_scroll_left(self) -> None:
        self.app.action_walk_up()

    def action_scroll_right(self) -> None:
        self.app.action_drill()


class PithApp(App):
    TITLE = "pith"

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("question_mark", "help", "help"),
        Binding("f", "pick_file", "file"),
        Binding("slash", "search", "search"),
        Binding("g", "jump_symbol", show=False),
        Binding("m", "mark", show=False),
        Binding("M", "marks", show=False),
        Binding("l,right", "drill", "drill"),
        Binding("h,backspace,left", "walk_up", "up"),
        Binding("b", "back", "back"),
        Binding("e,o,ctrl+enter", "open_editor", "editor"),
        Binding("escape", "clear_search", "clear", show=False),
        Binding("x", "expand_all", "expand", show=False),
        Binding("z", "collapse_all", "collapse", show=False),
        Binding("r", "reload", "re-parse", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    CSS = """
    #status { dock: top; height: 1; background: $surface; color: $text; padding: 0 1; }
    #search { dock: top; display: none; margin: 0 1; }
    Tree { padding: 0 1; }
    """

    def __init__(self, root: Path, start_file: str | None = None, editor: str | None = None):
        super().__init__()
        self.indexer = Indexer(root)
        self.start_file = start_file
        self.editor = editor
        self.history: list[tuple[str, int]] = []  # (path, tree cursor line)
        self.current: FileView | None = None
        self.ready = False
        self.needle: str = ""          # active search narrow (persists across files)
        self.search_active: bool = False  # search input visible
        self._search_prev: str = ""    # needle before the current search started
        self.bookmarks: dict[str, dict] = {}  # "path:line" -> {path, line, label}
        self._status_extra: str = ""   # last transient status text
        self._root_confirm: bool = False  # armed by h/backspace at the root; next press opens the picker
        self.user_cmds: dict[str, UserCommand] = {}  # key -> configured shell command
        self._load_bookmarks()

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        yield Input(placeholder="search — type to narrow the current screen (enter commit · esc cancel)",
                    id="search")
        yield LoadingIndicator(id="loading")
        yield SkeletonTree("", id="skeleton")
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one(Tree)
        tree.show_root = False
        tree.guide_depth = 5
        tree.display = False
        self._register_user_commands()
        self._index_repo()

    def _register_user_commands(self) -> None:
        """Bind keys from the global/project config files to their scripts."""
        reserved = {k for b in self.BINDINGS for k in b.key.split(",")}
        reserved |= {"enter", "space", "up", "down", "left", "right", "tab"}  # tree/nav keys
        self.user_cmds, warnings = load_user_commands(self.indexer.root, reserved)
        for key, cmd in self.user_cmds.items():
            # Runtime (per-instance) key binding — the footer picks these up.
            # Ref: https://textual.textualize.io/api/app/#textual.app.App.bind
            self.bind(key, f"user_cmd('{key}')", description=cmd.desc)
        for w in warnings:
            # Toast so config mistakes are visible without fighting the status bar.
            # Ref: https://textual.textualize.io/api/app/#textual.app.App.notify
            self.notify(w, title="pith config", severity="warning")

    @work(thread=True, exclusive=True)
    def _index_repo(self) -> None:
        self.indexer.discover_files()
        self.indexer.build_symbol_table()
        self.call_from_thread(self._on_indexed)

    def _on_indexed(self) -> None:
        self.ready = True
        self.query_one("#loading").display = False
        self.query_one(Tree).display = True
        n = len(self.indexer.source_files())
        self._status(f"indexed {n} source files · {len(self.indexer.symbols)} symbols")
        if self.start_file:
            self.open_file(self.start_file)
        else:
            self.action_pick_file()

    def _breadcrumb(self) -> str:
        """Ancestor chain of the cursor node, e.g. Renderer › paint."""
        try:
            node = self.query_one(Tree).cursor_node
        except Exception:
            return ""
        parts = []
        while node is not None and not node.is_root:
            d = node.data if isinstance(node.data, dict) else None
            if d and d.get("kind") in ("def", "ref") and d.get("name"):
                parts.append(d["name"])
            node = node.parent
        return " › ".join(reversed(parts))

    def _status(self, extra: str = "") -> None:
        self._status_extra = extra
        bar = self.query_one("#status", Static)
        path = self.current.path if self.current else "—"
        crumbs = f"  ·  {len(self.history)} back" if self.history else ""
        t = Text()
        t.append(" pith ", style="bold reverse")
        t.append(f" {path}", style="bold bright_white")
        if self.needle:
            t.append(f"  🔍 “{self.needle}”", style="yellow")
        b = self._breadcrumb()
        if b:
            t.append(f"  ·  {b}", style="cyan")
        if self.bookmarks:
            t.append(f"  ·  ★ {len(self.bookmarks)}", style="yellow")
        t.append(crumbs, style="dim")
        if extra:
            t.append(f"  ·  {extra}", style="dim")
        bar.update(t)

    def on_tree_node_highlighted(self, ev) -> None:
        """Keep the status breadcrumb in sync as the cursor moves."""
        if self.ready:
            self._status(self._status_extra)

    # -- search filter ------------------------------------------------------

    def _keep(self, text: str) -> bool:
        """Does a node's search text survive the active narrow?"""
        n = self.needle.strip().lower()
        return not n or n in (text or "").lower()

    def action_search(self) -> None:
        """/ — start a search that narrows the current screen to matches."""
        if not self.ready or self.current is None:
            return
        inp = self.query_one("#search", Input)
        self._search_prev = self.needle
        inp.value = self.needle
        inp.cursor_position = len(self.needle)
        inp.display = True
        self.search_active = True
        inp.focus()
        inp.select_all()

    def on_input_changed(self, ev: Input.Changed) -> None:
        if getattr(ev.input, "id", None) != "search":
            return
        if self.current is None:
            return
        self.needle = ev.value
        self._render(self.current, needle=ev.value)

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        if getattr(ev.input, "id", None) != "search":
            return
        self.needle = ev.input.value
        ev.input.display = False
        self.search_active = False
        self._search_prev = ""
        if self.current is not None:
            self._render(self.current)
        self.query_one(Tree).focus()

    def action_clear_search(self) -> None:
        """Esc: cancel an in-progress search, or clear the active narrow."""
        inp = self.query_one("#search", Input)
        if self.search_active:
            inp.display = False
            self.search_active = False
            self.needle = self._search_prev
            self._search_prev = ""
        else:
            self.needle = ""
        if self.current is not None:
            self._render(self.current)
        self.query_one(Tree).focus()

    # -- file view ----------------------------------------------------------

    def open_file(self, rel: str, focus_line: int | None = None, push: bool = True) -> None:
        if not self.ready:
            return
        if self.current is not None and push:
            tree = self.query_one(Tree)
            cur = tree.cursor_node
            cur_line = 0
            if cur is not None and isinstance(cur.data, dict):
                cur_line = cur.data.get("line", 0)
            self.history.append((self.current.path, cur_line))
        fv = self.indexer.file_view(rel)
        self.current = fv
        self._render(fv, focus_line)
        self.query_one(Tree).focus()

    def _render(self, fv: FileView, focus_line: int | None = None, needle: str | None = None,
                keep_anchor: tuple | None = None) -> None:
        if needle is not None:
            self.needle = needle
        needle = self.needle.strip()
        narrowed = bool(needle)
        tree = self.query_one(Tree)
        tree.clear()
        root_label = Text(fv.path)
        if (role := _nextjs_role(fv.path)):
            root_label.append(f"  ▸ {role}", style="dim green")
        tree.root.set_label(root_label)
        if fv.error:
            tree.root.add_leaf(Text(fv.error, style="red"),
                               data={"kind": "plain", "line": 1, "text": fv.error})
            self._status("no structure")
            return

        # -- imports ---------------------------------------------------------
        if fv.imports:
            imp_matches = [imp for imp in fv.imports if self._keep(imp.text)]
            if imp_matches:
                if narrowed:
                    hdr_text = f"⇊ imports ({len(imp_matches)}/{len(fv.imports)})"
                else:
                    hdr_text = f"⇊ imports ({len(imp_matches)})"
                imp_node = tree.root.add(Text(hdr_text, style="blue"), expand=narrowed,
                                         data={"kind": "plain", "line": fv.imports[0].line,
                                               "text": "imports"})
                for imp in imp_matches:
                    t = Text()
                    t.append(imp.text[:100], style="bright_blue" if imp.targets else "dim")
                    if imp.targets:
                        t.append(f"  {imp.targets[0].path}", style="dim")
                    if self._bookmarked(fv.path, imp.line):
                        t = Text("★ ", style="yellow") + t
                    imp_node.add_leaf(self._highlight_matches(t),
                                      data={"kind": "ref", "line": imp.line,
                                            "targets": imp.targets, "name": imp.text,
                                            "text": imp.text})

        # -- definitions -----------------------------------------------------
        def add_def(parent, d: Definition) -> bool:
            """Add d's subtree pruned to matches; True if anything was kept."""
            label = _def_label(d)
            if self._bookmarked(fv.path, d.line):
                label = Text("★ ", style="yellow") + label
            text = f"{d.name} {d.signature}"
            node = parent.add(self._highlight_matches(label), expand=narrowed,
                              data={"kind": "def", "line": d.line, "end_line": d.end_line,
                                    "name": d.name, "text": text})
            kept = False
            if d.docstring:
                lines = d.docstring.splitlines()
                for ln in lines[:MAX_DOC_LINES]:
                    if self._keep(ln):
                        doc_data = {"kind": "doc", "line": d.line, "text": ln}
                        full = (Text("  ") + _doc_text(ln, fv.lang)) if ln else Text()
                        if len(full.plain) > MAX_DOC_LINE_CHARS:
                            # full text still lives in doc_data["text"] for search/
                            # PITH_SYMBOL context; only the label is cut short — drill
                            # in (l / right / enter) to read the rest.
                            cut = ln[:MAX_DOC_LINE_CHARS - 2].rstrip()
                            short = (Text("  ") + _doc_text(cut, fv.lang)
                                    + Text(" …", style=_DOC_STYLE))
                            doc_node = node.add(self._highlight_matches(short),
                                                expand=narrowed, data=doc_data)
                            doc_node.add_leaf(self._highlight_matches(full), data=doc_data)
                        else:
                            node.add_leaf(self._highlight_matches(full), data=doc_data)
                        kept = True
                if len(lines) > MAX_DOC_LINES and (not narrowed or self._keep(text)):
                    node.add_leaf(Text(f"  … {len(lines) - MAX_DOC_LINES} more lines", style="dim"),
                                  data={"kind": "doc", "line": d.line,
                                        "text": f"{len(lines) - MAX_DOC_LINES} more lines"})
                    kept = True
            resolved = [r for r in d.refs if r.targets]
            unresolved = [r for r in d.refs if not r.targets]
            for r in resolved:
                rt = _search_text(r.name, r.targets)
                if self._keep(rt):
                    rl = _ref_label(r.name, r.targets)
                    if self._bookmarked(fv.path, r.line):
                        rl = Text("★ ", style="yellow") + rl
                    node.add_leaf(self._highlight_matches(rl),
                                  data={"kind": "ref", "line": r.line, "targets": r.targets,
                                        "name": r.name, "text": rt})
                    kept = True
            if unresolved:
                names = ", ".join(r.name for r in unresolved[:12])
                if len(unresolved) > 12:
                    names += f", … +{len(unresolved) - 12}"
                if self._keep(names):
                    node.add_leaf(self._highlight_matches(Text(f"→ {names}", style="dim")),
                                  data={"kind": "doc", "line": d.line, "text": names})
                    kept = True
            for c in d.children:
                if add_def(node, c):
                    kept = True
            if not kept and not self._keep(text):
                node.remove()
                return False
            return True

        for d in fv.defs:
            add_def(tree.root, d)

        # -- module-level references -----------------------------------------
        if fv.module_refs:
            resolved = [r for r in fv.module_refs if r.targets]
            if narrowed:
                picked = [r for r in resolved if self._keep(_search_text(r.name, r.targets))]
            else:
                picked = resolved
            if picked:
                mod = tree.root.add(Text("▤ module level", style="blue"), expand=narrowed,
                                    data={"kind": "plain", "line": 1, "text": "module"})
                for r in picked:
                    rl = _ref_label(r.name, r.targets)
                    if self._bookmarked(fv.path, r.line):
                        rl = Text("★ ", style="yellow") + rl
                    mod.add_leaf(self._highlight_matches(rl),
                                 data={"kind": "ref", "line": r.line, "targets": r.targets,
                                       "name": r.name, "text": _search_text(r.name, r.targets)})

        # -- cursor + status -------------------------------------------------
        all_nodes = list(self._all_nodes(tree.root))
        matches = [n for n in all_nodes
                   if isinstance(n.data, dict) and self._keep(n.data.get("text", ""))]
        first_match = matches[0] if matches else None

        target_node = None
        if narrowed:
            target_node = first_match
        elif keep_anchor is not None:
            for node in all_nodes:
                d = node.data
                if isinstance(d, dict) and (d.get("kind"), d.get("line"),
                                            d.get("name"), d.get("text")) == keep_anchor:
                    target_node = node
                    break
        elif focus_line is not None:
            best = None
            for node in all_nodes:
                d = node.data
                if isinstance(d, dict) and d.get("kind") == "def":
                    dl = d.get("line", 0)
                    if dl <= focus_line and (best is None or dl > best.data["line"]):
                        best = node
                    if dl == focus_line:
                        best = node
                        break
            target_node = best
        if target_node is None and tree.root.children:
            target_node = tree.root.children[0]
        if target_node is not None:
            if narrowed or focus_line is not None or keep_anchor is not None:
                target_node.expand()
                # a target inside a collapsed ancestor is invisible (no line),
                # so move_cursor would drop it — expand the chain up to the root
                anc = target_node.parent
                while anc is not None and not anc.is_root:
                    if anc.is_collapsed:
                        anc.expand()
                    anc = anc.parent
            node = target_node

            def _place() -> None:
                tree.move_cursor(node)
                tree.scroll_to_node(node)

            self.call_after_refresh(_place)

        if narrowed:
            n = len(matches)
            self._status(f"{n} match" + ("es" if n != 1 else "") + " on screen")
        else:
            n_defs = sum(1 for _ in self._walk_defs(fv.defs))
            self._status(f"{n_defs} definitions · {fv.lang}")

    def _walk_defs(self, defs):
        for d in defs:
            yield d
            yield from self._walk_defs(d.children)

    def _all_nodes(self, node):
        for ch in node.children:
            yield ch
            yield from self._all_nodes(ch)

    # -- interactions -------------------------------------------------------

    def _follow_ref(self, data: dict) -> None:
        targets = data.get("targets") or []
        if not targets:
            return
        if len(targets) == 1:
            self._goto(targets[0])
        else:
            items = [(f"{t.path}:{t.line}  ({t.kind})", t) for t in targets]
            self.push_screen(
                PickScreen(f"definitions of “{data.get('name')}”", items, with_input=False),
                lambda t: self._goto(t) if t else None,
            )

    def on_tree_node_selected(self, ev: Tree.NodeSelected) -> None:
        # enter / click drills exactly like l: follow a call, or expand+descend
        self._drill_node(ev.node)

    def _goto(self, loc: Location) -> None:
        self.open_file(loc.path, focus_line=loc.line)

    def action_back(self) -> None:
        if not self.history:
            self._status("nothing to go back to")
            return
        path, line = self.history.pop()
        self.open_file(path, focus_line=line or None, push=False)

    def action_pick_file(self) -> None:
        if not self.ready:
            return
        files = self.indexer.source_files()
        items = [(f, f) for f in files]
        self.push_screen(PickScreen("open file", items),
                         lambda f: self.open_file(f) if f else None)

    # -- bookmarks & repo symbols ------------------------------------------

    def _bookmarks_file(self) -> Path:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        slug = re.sub(r"[^A-Za-z0-9_.-]", "_", str(self.indexer.root))
        return base / "pith" / "bookmarks" / f"{slug}.json"

    def _load_bookmarks(self) -> None:
        try:
            data = json.loads(self._bookmarks_file().read_text())
        except Exception:
            data = {}
        self.bookmarks = {k: v for k, v in data.items()
                          if isinstance(v, dict) and v.get("path")
                          and isinstance(v.get("line"), int)}

    def _save_bookmarks(self) -> None:
        try:
            p = self._bookmarks_file()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self.bookmarks, indent=1))
        except OSError:
            pass

    def _bookmarked(self, path: str, line: int) -> bool:
        return f"{path}:{line}" in self.bookmarks

    def _re_render_keep_cursor(self) -> None:
        """Re-render the current file, then restore the cursor to its node."""
        node = self.query_one(Tree).cursor_node
        d = node.data if isinstance(node.data, dict) else None
        anchor = (d.get("kind"), d.get("line"), d.get("name"), d.get("text")) if d else None
        self._render(self.current, keep_anchor=anchor)

    def action_mark(self) -> None:
        """m — toggle a bookmark on the current line."""
        tree = self.query_one(Tree)
        node = tree.cursor_node
        if node is None or not isinstance(node.data, dict) or self.current is None:
            return
        line = node.data.get("line", 0) or 1
        key = f"{self.current.path}:{line}"
        if key in self.bookmarks:
            del self.bookmarks[key]
            self._save_bookmarks()
            self._re_render_keep_cursor()
            self._status(f"removed bookmark :{line}")
            return
        label = node.data.get("name") or ""
        if not label and node.parent is not None and isinstance(node.parent.data, dict):
            label = node.parent.data.get("name", "")
        if not label:
            label = (node.label.plain if node.label else "").strip()[:60]
        self.bookmarks[key] = {"path": self.current.path, "line": line, "label": label}
        self._save_bookmarks()
        self._re_render_keep_cursor()
        self._status(f"bookmarked {label or self.current.path} :{line}")

    def action_marks(self) -> None:
        """M — list bookmarks and jump to one."""
        if not self.ready:
            return
        items = []
        for key in sorted(self.bookmarks):
            b = self.bookmarks[key]
            label = f"{b['path']}:{b['line']}"
            if b.get("label"):
                label += f"  {b['label']}"
            items.append((label, (b["path"], b["line"])))
        if not items:
            self._status("no bookmarks — press m to mark the current line")
            return
        self.push_screen(PickScreen(f"bookmarks ({len(items)})", items),
                         lambda sel: self.open_file(sel[0], focus_line=sel[1]) if sel else None)

    def action_jump_symbol(self) -> None:
        """g — jump to any definition in the repo (project symbol table)."""
        if not self.ready:
            return
        items: list[tuple[str, Location]] = []
        for name, locs in self.indexer.symbols.items():
            for loc in locs[:5]:
                items.append((f"{name}  {loc.path}:{loc.line}  ({loc.kind})", loc))
            if len(items) >= 3000:
                break
        if not items:
            self._status("no symbols indexed")
            return
        self.push_screen(PickScreen(f"symbols ({len(items)})", items),
                         lambda loc: self._goto(loc) if loc else None)

    def _highlight_matches(self, label: Text) -> Text:
        """Bold-underline the needle inside a visible label while narrowed."""
        needle = self.needle.strip()
        if not needle:
            return label
        plain = label.plain
        low = plain.lower()
        n = needle.lower()
        start = 0
        while True:
            i = low.find(n, start)
            if i < 0:
                break
            label.stylize("bold underline yellow", i, i + len(needle))
            start = i + len(needle)
        return label

    # -- vim left/right: drill down / walk back up --------------------------

    def _first_meaningful_child(self, node):
        """First child worth drilling into (a def or a call), else first child."""
        for ch in node.children:
            d = ch.data if isinstance(ch.data, dict) else {}
            if d.get("kind") in ("def", "ref"):
                return ch
        return node.children[0] if node.children else None

    def _drill_node(self, node) -> None:
        """Drill into a node (shared by enter/click and l): follow a call or
        import, or expand a def/group and descend into its first meaningful
        child (first nested def or call, skipping doc lines)."""
        data = node.data if isinstance(node.data, dict) else None
        if data and data.get("kind") == "ref":
            self._follow_ref(data)
            return
        if node.children:
            if node.is_collapsed:
                node.expand()
            first = self._first_meaningful_child(node)
            if first is not None:
                # move after refresh: expand() invalidates the tree, so freshly
                # expanded children have no line yet and move_cursor would drop
                target = first

                def _place() -> None:
                    node.tree.move_cursor(target)
                    node.tree.scroll_to_node(target)

                self.call_after_refresh(_place)

    def action_drill(self) -> None:
        """l — drill down: follow a call, or expand a def and descend into it."""
        node = self.query_one(Tree).cursor_node
        if node is not None:
            self._drill_node(node)

    def action_walk_up(self) -> None:
        """h / backspace — walk back up: collapse, move to parent, or pop the navtree."""
        tree = self.query_one(Tree)
        node = tree.cursor_node
        if node is None:
            return
        if node.is_root:
            self._walk_back_file()
            return
        # an expanded selection always collapses first, wherever it sits
        if node.children and node.is_expanded:
            self._root_confirm = False
            node.collapse()
            return
        parent = node.parent
        if parent is not None and not parent.is_root:
            self._root_confirm = False
            tree.move_cursor(parent)
            tree.scroll_to_node(parent)
        else:
            # collapsed at the top of the file: walk back up the navtree
            self._walk_back_file()

    def _walk_back_file(self) -> None:
        """Pop the navtree; at the very root, require a second press to reopen the picker."""
        if self.history:
            self._root_confirm = False
            self.action_back()
            return
        if self._root_confirm:
            self._root_confirm = False
            self.action_pick_file()
        else:
            self._root_confirm = True
            self._status("at the root — press h / backspace again for fuzzy file search")

    def on_key(self, event: events.Key) -> None:
        # Any key other than the walk-up pair disarms the pending "go to picker"
        # confirmation, so the double-press must be consecutive.
        # Ref: https://textual.textualize.io/guide/input/#key-events
        if event.key not in ("h", "backspace"):
            self._root_confirm = False

    # -- misc actions -------------------------------------------------------

    def action_expand_all(self) -> None:
        self.query_one(Tree).root.expand_all()

    def action_collapse_all(self) -> None:
        tree = self.query_one(Tree)
        for ch in tree.root.children:
            ch.collapse_all()

    def action_reload(self) -> None:
        if self.current:
            self.open_file(self.current.path, push=False)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_cursor_down(self) -> None:
        self.query_one(Tree).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(Tree).action_cursor_up()

    # -- editor hand-off ----------------------------------------------------

    def action_open_editor(self) -> None:
        tree = self.query_one(Tree)
        node = tree.cursor_node
        if node is None or not isinstance(node.data, dict) or self.current is None:
            return
        data = node.data
        if data.get("kind") == "ref" and data.get("targets"):
            loc = data["targets"][0]
            path, line = loc.path, loc.line
        else:
            path, line = self.current.path, data.get("line", 1)
        self._open_editor(path, line)

    def _open_editor(self, rel: str, line: int) -> None:
        full = str(self.indexer.root / rel)
        editor = self.editor or os.environ.get("EDITOR") or os.environ.get("VISUAL") or ""
        parts = shlex.split(editor) if editor else []
        base = os.path.basename(parts[0]) if parts else ""
        gui_g = {"code", "code-insiders", "codium", "cursor", "windsurf"}
        try:
            if base in gui_g:
                subprocess.Popen(parts + ["-g", f"{full}:{line}"])
                self._status(f"opened {rel}:{line} in {base}")
            elif base in {"subl", "zed", "sublime_text"}:
                subprocess.Popen(parts + [f"{full}:{line}"])
                self._status(f"opened {rel}:{line} in {base}")
            elif parts:
                with self.suspend():
                    subprocess.run(parts + [f"+{line}", full])
                self._sync_after_edit(rel)
            else:
                for cand in ("vi", "nano"):
                    from shutil import which
                    if which(cand):
                        with self.suspend():
                            subprocess.run([cand, f"+{line}", full])
                        self._sync_after_edit(rel)
                        return
                self._status("set $EDITOR to enable editor hand-off")
        except Exception as e:
            self._status(f"editor failed: {e}")

    def _sync_after_edit(self, rel: str) -> None:
        """Recompute rel's definitions/references after a blocking editor exits.

        The editor may have changed line numbers, added/removed/renamed
        definitions, or edited a file other than the one currently on screen
        (e.g. after jumping to a ref's definition). Refresh rel's entries in
        the project symbol table, then re-parse and re-render the file
        currently on screen so its line numbers and any refs resolving into
        rel pick up the change; self.refresh() alone only repaints the
        existing (now possibly stale) tree.
        """
        self.indexer.reindex_file(rel)
        if self.current is not None:
            tree = self.query_one(Tree)
            node = tree.cursor_node
            d = node.data if isinstance(node.data, dict) else None
            anchor = (d.get("kind"), d.get("line"), d.get("name"),
                      d.get("text")) if d else None
            self.current = self.indexer.file_view(self.current.path)
            self._render(self.current, keep_anchor=anchor)
        self.refresh()

    # -- user-configured commands -------------------------------------------

    def _cmd_context(self) -> tuple[dict[str, str], str] | None:
        """(PITH_* env vars, snippet) for the cursor node, or None."""
        tree = self.query_one(Tree)
        node = tree.cursor_node
        if node is None or not isinstance(node.data, dict) or self.current is None:
            return None
        data = node.data
        line = data.get("line")
        if not isinstance(line, int) or line < 1:
            return None
        rel = self.current.path
        try:
            src = (self.indexer.root / rel).read_text(encoding="utf-8",
                                                      errors="replace").splitlines()
        except OSError:
            return None
        # a def's snippet is its whole block (tree-sitter end_line); anything
        # else (call, import, doc line) is just its own source line
        end = max(data.get("end_line", line), line)
        snippet = "\n".join(src[line - 1:end])
        line_text = src[line - 1].strip() if line <= len(src) else ""
        crumb = self._breadcrumb()  # ancestor chain, e.g. "Renderer › paint"
        symbol = f"{crumb} — {line_text}" if crumb else line_text
        doc = next((ch.data.get("text", "") for ch in node.children
                    if isinstance(ch.data, dict) and ch.data.get("kind") == "doc"), "")
        if doc:
            symbol += f" · {doc}"
        target = ""
        if data.get("kind") == "ref" and data.get("targets"):
            loc = data["targets"][0]  # where the call/import resolves to
            target = f"{loc.path}:{loc.line}"
        env = {"PITH_ROOT": str(self.indexer.root),
               "PITH_FILE": str(self.indexer.root / rel),
               "PITH_REL": rel,
               "PITH_LINE": str(line),
               "PITH_END_LINE": str(end),
               "PITH_KIND": data.get("kind", ""),
               "PITH_SYMBOL": symbol,
               "PITH_TARGET": target}
        return env, snippet

    def action_user_cmd(self, key: str) -> None:
        cmd = self.user_cmds.get(key)
        if cmd is None:
            return
        ctx = self._cmd_context()
        if ctx is None:
            self._status(f"{cmd.run}: nothing under the cursor to send")
            return
        pith_env, snippet = ctx
        prog = os.path.expanduser(cmd.run)
        if not os.path.isabs(prog) and (self.indexer.root / prog).is_file():
            prog = str(self.indexer.root / prog)  # project-relative script
        env = {**os.environ, **pith_env}
        if cmd.suspend:
            # interactive script: leave the TUI like the terminal-editor
            # hand-off. stdin stays the terminal here, so the snippet travels
            # in PITH_SNIPPET instead of the pipe background commands get.
            env["PITH_SNIPPET"] = snippet
            try:
                with self.suspend():
                    proc = subprocess.run([prog], cwd=self.indexer.root, env=env)
                self._sync_after_edit(pith_env["PITH_REL"])
                self._status(f"{cmd.run}: exit {proc.returncode}")
            except Exception as e:
                self._status(f"{cmd.run}: {e}")
        else:
            self._run_user_cmd(cmd, [prog], env, snippet)

    @work(thread=True, exclusive=False)
    def _run_user_cmd(self, cmd: UserCommand, argv: list[str], env: dict,
                      snippet: str) -> None:
        """Run a configured script off the UI thread; report its outcome."""
        try:
            # Metadata rides in PITH_* env vars; the snippet is piped to stdin
            # so large blocks can't blow the kernel's shared argv+env budget.
            # Ref: https://docs.python.org/3/library/subprocess.html#subprocess.run
            proc = subprocess.run(argv, cwd=self.indexer.root, env=env,
                                  input=snippet, capture_output=True, text=True,
                                  timeout=cmd.timeout)
            out = (proc.stdout or proc.stderr).strip().splitlines()
            tail = f" · {out[-1][:80]}" if out else ""
            msg = f"{cmd.run}: exit {proc.returncode}{tail}"
        except FileNotFoundError:
            msg = f"{cmd.run}: not found (PATH or repo-relative)"
        except subprocess.TimeoutExpired:
            msg = f"{cmd.run}: timed out after {cmd.timeout}s"
        except Exception as e:
            msg = f"{cmd.run}: {e}"
        self.call_from_thread(self._status, msg)


def _fzf_pick_file(root: Path) -> str | None:
    """Pick a source file with fzf before pith boots (fast external picker).

    Returns the repo-relative path of the selection, or None when fzf is not
    installed, stdin/stdout aren't a terminal, the user cancelled, or there
    are no source files — in which case pith falls back to its in-app picker.
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    from shutil import which
    if which("fzf") is None:
        return None
    indexer = Indexer(root)
    indexer.discover_files()
    files = indexer.source_files()
    if not files:
        return None
    try:
        proc = subprocess.run(
            ["fzf", "--height", "40%", "--border",
             "--header", "pick a file — enter opens it in pith (esc = pith's own picker)"],
            input="\n".join(files) + "\n",
            capture_output=True, text=True,
        )
    except (OSError, ValueError):
        return None
    sel = proc.stdout.strip()
    return sel or None


def find_root(start: Path) -> Path:
    p = start if start.is_dir() else start.parent
    p = p.resolve()
    for anc in [p, *p.parents]:
        if (anc / ".git").exists():
            return anc
    return p


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(
        prog="pith",
        description="Skeleton-first codebase navigation: definitions, docstrings "
                    "and outgoing calls, one file at a time.",
        # RawDescriptionHelpFormatter keeps the epilog's hand-wrapped layout.
        # Ref: https://docs.python.org/3/library/argparse.html#formatter-class
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
config — custom keybindings:
  pith reads two optional JSON config files (project overrides global per key):

    global    $XDG_CONFIG_HOME/pith/config.json   (~/.config/pith/config.json)
    project   <repo root>/.pith.json

  each entry binds a key to a script of your own:

    {
      "commands": {
        "y":      {"run": "scripts/yank.sh", "desc": "yank snippet"},
        "ctrl+t": {"run": "~/bin/ticket.sh", "desc": "file ticket",
                   "suspend": true},
        "Y":      "scripts/quick.sh"
      }
    }

  options per command:
    run      script/executable; relative paths resolve against the repo root
    desc     footer label (default: the run value)
    suspend  true = leave the TUI so the script can be interactive
             (pager, fzf, lazygit, ...); default false = run in background
    timeout  seconds before a background command is killed (default 60)

  keys use Textual names ("y", "Y", "ctrl+t", ...); keys that shadow a
  built-in binding are skipped with a warning at startup.

  the script runs with cwd = repo root, no positional args; the current
  selection arrives in environment variables:

    PITH_ROOT      absolute repo root (== the cwd)
    PITH_FILE      absolute path of the current file
    PITH_REL       repo-relative path of the current file
    PITH_LINE      1-based line of the selection
    PITH_END_LINE  last line of the selection (a def's block end)
    PITH_KIND      def / ref / doc / plain
    PITH_SYMBOL    ancestor chain + source line (+ first doc line)
    PITH_TARGET    path:line a call/import resolves to (refs only)

  the snippet (PITH_LINE..PITH_END_LINE) is piped to stdin; with
  "suspend": true stdin stays the terminal, so it arrives in PITH_SNIPPET.
""",
    )
    ap.add_argument("path", nargs="?", default=".",
                    help="repo root, subdirectory, or a file to open")
    ap.add_argument("--editor", help="editor command (default: $EDITOR)")
    ap.add_argument("--no-fzf", action="store_true",
                    help="skip the fzf file picker and use pith's in-app picker")
    args = ap.parse_args(argv)

    target = Path(args.path).expanduser()
    if not target.exists():
        raise SystemExit(f"pith: no such path: {target}")
    root = find_root(target)
    start_file = None
    if target.is_file():
        start_file = os.path.relpath(target.resolve(), root)
    elif not args.no_fzf:
        # pick the file with fzf (instant) before the slower Textual boot
        start_file = _fzf_pick_file(root)

    PithApp(root, start_file=start_file, editor=args.editor).run()


if __name__ == "__main__":
    main()
