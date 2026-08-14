"""Shared skeleton styling + the static `pith --print` mode.

The label helpers here (definition/reference/doc-line styling, git badges,
Next.js route annotations) are the single source of truth for how a skeleton
looks: the Textual TUI (app.py) imports them for its live tree, and
print_skeletons() renders the same labels into a static rich Tree on stdout
so the skeleton can be piped into other tools (`pith -p file.py | less -R`)
or embedded in other programs' output. ANSI colour is emitted even when
stdout is a pipe (rich would normally strip it there); --no-colour disables
it. This module deliberately has no Textual dependency, only rich.
"""

from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console
from rich.syntax import Syntax
from rich.text import Text
# rich's Tree is the static counterpart of Textual's interactive Tree widget.
# Ref: https://rich.readthedocs.io/en/stable/tree.html
from rich.tree import Tree as RichTree

from .gitstate import GitState
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


# git badge colors follow VS Code's SCM decoration conventions:
# modified/renamed = yellow, added/untracked = green.
# Ref: https://code.visualstudio.com/api/references/theme-color#git-colors
_GIT_STYLE = {"M": "yellow", "A": "green", "?": "green", "R": "yellow"}
_GIT_WORD = {"M": "modified", "A": "added", "?": "untracked", "R": "renamed"}


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


def git_def_mark(git: GitState, rel: str, d: Definition) -> str | None:
    """Two-tier uncommitted-change mark for a definition.

    "direct" — a changed line hits d's own body (outside every child), so
    the strong badge points at the innermost def that actually changed.
    "inner" — changes only inside children; a collapsed container still
    reveals that something within it changed.  None — untouched.
    """
    if not git.touches(rel, d.line, d.end_line):
        return None
    kids = sorted((c.line, c.end_line) for c in d.children)
    for a, b in git.hunks.get(rel, ()):
        lo, hi = max(a, d.line), min(b, d.end_line)
        if lo > hi:
            continue
        # sweep lo past any child that covers it; children are disjoint
        # and sorted, so the first child starting beyond lo leaves lo
        # uncovered — that residue is a change in d's own body
        for ca, cb in kids:
            if ca > hi:
                break
            if ca <= lo <= cb:
                lo = cb + 1
        if lo <= hi:
            return "direct"
    return "inner"


# -- static tree ------------------------------------------------------------

def file_tree(fv: FileView, indexer: Indexer) -> RichTree:
    """One file's skeleton as a rich Tree — the static twin of PithApp._render.

    Same node order and styling as the TUI (imports, definitions with doc
    lines and outgoing calls, module-level refs), minus everything that only
    makes sense interactively: search narrowing, bookmarks, cursor state.
    Doc lines are printed in full rather than cut at MAX_DOC_LINE_CHARS,
    since there is no drill-in to reveal the remainder.
    """
    root_label = Text(fv.path, style="bold bright_white")
    if (role := _nextjs_role(fv.path)):
        root_label.append(f"  ▸ {role}", style="dim green")
    git_code = indexer.git.code(fv.path)
    if git_code and git_code != "D":
        root_label.append(f"  ● {_GIT_WORD[git_code]}", style=_GIT_STYLE[git_code])
    tree = RichTree(root_label, guide_style="dim")
    if fv.error:
        tree.add(Text(fv.error, style="red"))
        return tree

    if fv.imports:
        imp_node = tree.add(Text(f"⇊ imports ({len(fv.imports)})", style="blue"))
        for imp in fv.imports:
            t = Text(imp.text[:100], style="bright_blue" if imp.targets else "dim")
            if imp.targets:
                t.append(f"  {imp.targets[0].path}", style="dim")
            imp_node.add(t)

    def add_def(branch, d: Definition) -> None:
        label = _def_label(d)
        mark = git_def_mark(indexer.git, fv.path, d)
        if mark == "direct":
            label = Text("● ", style=_GIT_STYLE.get(git_code or "M", "yellow")) + label
        elif mark == "inner":
            label = Text("○ ", style="dim yellow") + label
        node = branch.add(label)
        if d.docstring:
            lines = d.docstring.splitlines()
            for ln in lines[:MAX_DOC_LINES]:
                node.add((Text("  ") + _doc_text(ln, fv.lang)) if ln else Text())
            if len(lines) > MAX_DOC_LINES:
                node.add(Text(f"  … {len(lines) - MAX_DOC_LINES} more lines", style="dim"))
        for r in d.refs:
            if r.targets:
                node.add(_ref_label(r.name, r.targets))
        unresolved = [r for r in d.refs if not r.targets]
        if unresolved:
            names = ", ".join(r.name for r in unresolved[:12])
            if len(unresolved) > 12:
                names += f", … +{len(unresolved) - 12}"
            node.add(Text(f"→ {names}", style="dim"))
        for c in d.children:
            add_def(node, c)

    for d in fv.defs:
        add_def(tree, d)

    resolved = [r for r in fv.module_refs if r.targets]
    if resolved:
        mod = tree.add(Text("▤ module level", style="blue"))
        for r in resolved:
            mod.add(_ref_label(r.name, r.targets))
    return tree


def print_skeletons(root: Path, files: list[str] | None = None, *,
                    no_colour: bool = False, diff_only: bool = False) -> int:
    """`pith --print`: index the repo, print skeleton trees to stdout.

    files=None prints every source file in the repo (restricted to files
    with uncommitted changes when diff_only); an explicit list prints just
    those. Returns a process exit code.
    """
    indexer = Indexer(root)
    indexer.discover_files()
    indexer.refresh_git()
    indexer.build_symbol_table()
    if files is None:
        files = indexer.source_files()
        if diff_only:
            files = [f for f in files if indexer.git.is_changed(f)]
    if not files:
        print("pith: --print: no source files to print", file=sys.stderr)
        return 1
    # force_terminal keeps ANSI colour when stdout is a pipe (rich strips it
    # otherwise, which would make --no-colour meaningless); no_color drops
    # colour while leaving plain text intact. A pipe has no detectable size,
    # so give non-tty output an effectively unlimited width — one node per
    # line keeps the output grep-able.
    # Ref: https://rich.readthedocs.io/en/stable/console.html#terminal-detection
    console = Console(
        force_terminal=not no_colour,
        no_color=no_colour,
        width=None if sys.stdout.isatty() else 4000,
    )
    for i, rel in enumerate(files):
        if i:
            console.print()
        console.print(file_tree(indexer.file_view(rel), indexer))
    return 0
