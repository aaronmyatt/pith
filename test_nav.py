"""Headless regression tests for pith nav: / live narrow + h/l drill & walk."""
import asyncio, json, os, sys, tempfile
from pathlib import Path

sys.path.insert(0, "/Users/oya/Development/pith")
from textual.widgets import Tree, Input, Static
from pith.app import PithApp, PickScreen, _fzf_pick_file

A = '''\
import b

def render(theme):
    """Render the scene."""
    return b.parse("x")

class Renderer:
    """A renderer."""

    def paint(self):
        return render("dark")
'''

B = '''\
def parse(source):
    """Parse source."""
    return source
'''


def top_names(tree):
    return [n.data.get("name") for n in tree.root.children if isinstance(n.data, dict)]


def dump(tree):
    out = []
    def walk(n, depth=0):
        for ch in n.children:
            d = ch.data if isinstance(ch.data, dict) else {}
            out.append("  " * depth + (ch.label.plain if ch.label else "")[:70]
                       + "  |" + str(d.get("kind")) + " " + str(d.get("name")))
            walk(ch, depth + 1)
    walk(tree.root)
    return "\n".join(out)


async def main():
    with tempfile.TemporaryDirectory() as td:
        os.environ["XDG_CONFIG_HOME"] = td  # keep test bookmarks out of ~/.config
        root = Path(td)
        (root / "a.py").write_text(A)
        (root / "b.py").write_text(B)
        # user-command config: global registers t (+ a reserved key, skipped);
        # the project file overrides t's description
        (root / "pith").mkdir()
        (root / "pith" / "config.json").write_text(json.dumps({"commands": {
            "t": {"run": "sink.sh", "desc": "global sink"},
            "h": "never.sh"}}))
        (root / ".pith.json").write_text(json.dumps({"commands": {
            "t": {"run": "sink.sh", "desc": "project sink"}}}))
        (root / "sink.sh").write_text(
            '#!/bin/sh\n'
            'printf \'%s\\n\' "$PITH_REL:$PITH_LINE-$PITH_END_LINE" "$PITH_KIND"'
            ' "$PITH_SYMBOL" "$PITH_TARGET" "$#" > "$PITH_ROOT/cmd_out.txt"\n'
            'cat > "$PITH_ROOT/stdin_copy.txt"\n')
        (root / "sink.sh").chmod(0o755)
        app = PithApp(root, start_file="a.py")
        async with app.run_test() as pilot:
            for _ in range(300):
                if app.ready:
                    break
                await pilot.pause(0.05)
            assert app.ready, "app never became ready"
            assert app.current and app.current.path == "a.py"
            tree = app.query_one(Tree)
            await pilot.pause()
            print("initial tree:\n" + dump(tree))

            # ---- 1. '/' narrows the current screen ------------------------
            await pilot.press("slash")
            await pilot.pause()
            inp = app.query_one("#search", Input)
            assert inp.display, "search input should appear on /"
            assert app.search_active

            # narrow to a nested method: ancestors kept, siblings pruned
            await pilot.press("p", "a", "i", "n", "t")
            await pilot.pause()
            assert app.needle == "paint"
            t = app.query_one(Tree)
            cur = t.cursor_node
            print("after 'paint':\n" + dump(t))
            assert cur.data.get("kind") == "def" and cur.data.get("name") == "paint", cur.data
            assert "render" not in top_names(t), "non-matching def pruned"

            # enter commits the narrow (input hides, needle persists)
            await pilot.press("enter")
            await pilot.pause()
            assert not app.search_active and not inp.display
            assert app.needle == "paint"

            # escape clears it back to the full screen
            await pilot.press("escape")
            await pilot.pause()
            assert app.needle == ""
            t = app.query_one(Tree)
            assert "render" in top_names(t), "escape restores the full screen"

            # narrow to a *call* (reference line): def kept as ancestor
            await pilot.press("slash")
            await pilot.pause()
            await pilot.press("p", "a", "r", "s", "e")
            await pilot.pause()
            t = app.query_one(Tree)
            cur = t.cursor_node
            print("after 'parse':\n" + dump(t))
            assert cur.data.get("kind") == "ref" and cur.data.get("name") == "parse", cur.data
            assert "render" in top_names(t), "def kept as ancestor of matching call"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert app.needle == ""

            # narrow to a docstring line (anything visible on screen)
            await pilot.press("slash")
            await pilot.pause()
            await pilot.press("R", "e", "n", "d", "e", "r", "space", "t", "h", "e",
                              "space", "s", "c", "e", "n", "e")
            await pilot.pause()
            t = app.query_one(Tree)
            cur = t.cursor_node
            print("after doc search:\n" + dump(t))
            assert cur.data.get("kind") == "doc", cur.data
            await pilot.press("escape")
            await pilot.pause()
            assert app.needle == ""

            # narrow persists across a drill: follow a call while narrowed
            await pilot.press("slash")
            await pilot.pause()
            await pilot.press("p", "a", "r", "s", "e")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            t = app.query_one(Tree)
            cur = t.cursor_node
            assert cur.data.get("kind") == "ref" and cur.data.get("name") == "parse", cur.data
            await pilot.press("l")   # follow the call into b.py while narrowed
            await pilot.pause()
            assert app.current.path == "b.py", app.current.path
            assert app.needle == "parse", "narrow follows the drill"
            t = app.query_one(Tree)
            assert t.cursor_node.data.get("kind") == "def" \
                and t.cursor_node.data.get("name") == "parse"
            await pilot.press("escape")
            await pilot.pause()
            assert app.needle == ""
            await pilot.press("h")
            await pilot.pause()
            assert app.current.path == "a.py", "h walks back up the navtree"

            # ---- 2. l / h drill & walk ------------------------------------
            t = app.query_one(Tree)
            cur = t.cursor_node
            # after the h-back, cursor is on the render def (focus_line placement)
            assert cur.data.get("kind") == "def" and cur.data.get("name") == "render", cur.data
            await pilot.press("l")          # drill: expand + descend to first call
            await pilot.pause()
            cur = t.cursor_node
            assert cur.data.get("kind") == "ref", "l descends into first call"
            await pilot.press("l")          # follow the call -> b.py
            await pilot.pause()
            assert app.current.path == "b.py", app.current.path
            await pilot.press("h")          # collapse-first: shrink the expanded landing def
            await pilot.pause()
            assert app.current.path == "b.py" and not t.cursor_node.is_expanded,                 "h collapses the expanded selection before popping the navtree"
            await pilot.press("h")          # now walk back up the navtree
            await pilot.pause()
            assert app.current.path == "a.py", "h pops back to previous file"

            # collapse-first also applies to an expanded def at the top of a file
            cur = t.cursor_node
            assert cur.data.get("kind") == "def" and cur.data.get("name") == "render"                 and cur.is_expanded, cur.data
            await pilot.press("h")
            await pilot.pause()
            assert app.current.path == "a.py" and not t.cursor_node.is_expanded,                 "h collapses the top-level def instead of leaving the file"

            # h walks up the visual tree inside a file (mid-file nodes)
            await pilot.press("z")          # collapse all so j/k walk top level
            await pilot.pause()
            await pilot.press("j")          # down to class Renderer
            await pilot.pause()
            cur = t.cursor_node
            assert cur.data.get("kind") == "def" and cur.data.get("name") == "Renderer", cur.data
            await pilot.press("l")          # expand Renderer, descend to method paint
            await pilot.pause()
            cur = t.cursor_node
            assert cur.data.get("kind") == "def" and cur.data.get("name") == "paint", cur.data
            await pilot.press("l")          # expand paint, descend to its call
            await pilot.pause()
            cur = t.cursor_node
            assert cur.data.get("kind") == "ref", cur.data
            await pilot.press("h")          # move up to parent paint (still expanded)
            await pilot.pause()
            cur = t.cursor_node
            assert cur.data.get("kind") == "def" and cur.data.get("name") == "paint"                 and cur.is_expanded, cur.data
            await pilot.press("h")          # collapse paint (mid-file, expanded)
            await pilot.pause()
            cur = t.cursor_node
            assert cur.data.get("kind") == "def" and cur.data.get("name") == "paint"                 and not cur.is_expanded, "h collapses a mid-file def"
            await pilot.press("h")          # move up to parent Renderer
            await pilot.pause()
            cur = t.cursor_node
            assert cur.data.get("kind") == "def" and cur.data.get("name") == "Renderer", cur.data

            # l on a group header drills into it
            await pilot.press("k", "k")    # up to the imports group
            await pilot.pause()
            cur = t.cursor_node
            assert cur.data.get("kind") == "plain", cur.data
            await pilot.press("l")
            await pilot.pause()
            cur = t.cursor_node
            assert cur.data.get("kind") == "ref" and "import b" in cur.data.get("name", ""), cur.data
            await pilot.press("l")          # follow import -> b.py
            await pilot.pause()
            assert app.current.path == "b.py", app.current.path
            await pilot.press("h")          # collapse the expanded landing def
            await pilot.pause()
            await pilot.press("h")          # then pop the navtree
            await pilot.pause()
            assert app.current.path == "a.py", "h walks back up again"

            # enter behaves exactly like l (drill down)
            await pilot.press("z"); await pilot.pause()   # collapse all
            await pilot.press("j"); await pilot.pause()   # -> render def
            cur = t.cursor_node
            assert cur.data.get("kind") == "def" and cur.data.get("name") == "render", cur.data
            await pilot.press("enter"); await pilot.pause()
            cur = t.cursor_node
            assert cur.data.get("kind") == "ref", "enter descends into first call (same as l)"
            await pilot.press("h"); await pilot.pause()   # back to parent render (still expanded)
            await pilot.press("enter"); await pilot.pause()
            cur = t.cursor_node
            assert cur.data.get("kind") == "ref", "enter on an expanded def drills again (same as l)"
            await pilot.press("enter"); await pilot.pause()   # on the ref -> follow to b.py
            assert app.current.path == "b.py", "enter on a ref follows like l"
            await pilot.press("h"); await pilot.pause()       # collapse the expanded landing def
            await pilot.press("h"); await pilot.pause()       # then pop the navtree
            assert app.current.path == "a.py", "h back after enter-follow"

            # ---- 3. bookmarks, repo symbols, breadcrumb, highlight ---------
            # breadcrumb tracks the cursor deep in the tree
            await pilot.press("z"); await pilot.pause()      # collapse all
            await pilot.press("j"); await pilot.pause()      # -> render
            await pilot.press("j"); await pilot.pause()      # -> Renderer
            await pilot.press("l"); await pilot.pause()      # expand Renderer -> paint
            cur = t.cursor_node
            assert cur.data.get("kind") == "def" and cur.data.get("name") == "paint", cur.data
            bar = app.query_one("#status", Static)
            assert "Renderer" in bar.content.plain and "paint" in bar.content.plain,                 bar.content.plain

            # bookmark the paint method: star + status count + persistence
            await pilot.press("m"); await pilot.pause()
            assert len(app.bookmarks) == 1, app.bookmarks
            assert t.cursor_node.label.plain.startswith("★"), t.cursor_node.label.plain
            bar = app.query_one("#status", Static)
            assert "★ 1" in bar.content.plain, bar.content.plain
            bm_files = list((Path(td) / "pith" / "bookmarks").glob("*.json"))
            assert bm_files and "paint" in bm_files[0].read_text(), "bookmark persisted"

            # m toggles off, then back on
            await pilot.press("m"); await pilot.pause()
            assert not app.bookmarks, "m removes the bookmark"
            await pilot.press("m"); await pilot.pause()
            assert len(app.bookmarks) == 1

            # M: bookmarks picker -> filter -> jump
            await pilot.press("M"); await pilot.pause()
            assert isinstance(app.screen, PickScreen), "M opens the bookmarks picker"
            await pilot.press("p", "a", "i", "n", "t"); await pilot.pause()
            await pilot.press("enter"); await pilot.pause()
            assert app.current.path == "a.py"
            assert t.cursor_node.data.get("name") == "paint"

            # g: repo-wide symbol jump -> parse lives in b.py
            await pilot.press("g"); await pilot.pause()
            assert isinstance(app.screen, PickScreen), "g opens the symbol picker"
            await pilot.press("p", "a", "r", "s", "e"); await pilot.pause()
            await pilot.press("enter"); await pilot.pause()
            assert app.current.path == "b.py", app.current.path
            await pilot.press("h"); await pilot.pause()       # collapse the expanded landing def
            await pilot.press("h"); await pilot.pause()       # then pop the navtree
            assert app.current.path == "a.py", "h back after symbol jump"

            # highlight: narrowed search underlines the matched substring
            await pilot.press("slash"); await pilot.pause()
            await pilot.press("p", "a", "i", "n", "t"); await pilot.pause()
            cur = t.cursor_node
            assert any("underline" in str(sp.style) for sp in cur.label.spans if sp.style),                 "needle is underlined in the visible label"
            await pilot.press("escape"); await pilot.pause()
            assert app.needle == ""

            # ---- 4. backspace = h; double-press at the root opens the picker
            app.history.clear()                      # simulate being at the navtree root
            await pilot.press("z"); await pilot.pause()
            await pilot.press("k", "k", "k", "k"); await pilot.pause()  # cursor to a top-level row
            await pilot.press("backspace"); await pilot.pause()
            assert app._root_confirm and not isinstance(app.screen, PickScreen),                 "first press at the root only arms the confirmation"
            await pilot.press("j"); await pilot.pause()
            assert not app._root_confirm, "any other key disarms the confirmation"
            await pilot.press("h"); await pilot.pause()      # re-arm (h and backspace interchangeable)
            await pilot.press("backspace"); await pilot.pause()
            assert isinstance(app.screen, PickScreen),                 "second consecutive press opens the fuzzy file search"
            await pilot.press("escape"); await pilot.pause()

            # ---- 5. user-configured commands ------------------------------
            assert "t" in app.user_cmds and app.user_cmds["t"].desc == "project sink",                 "project config overrides global"
            assert "h" not in app.user_cmds, "reserved keys are skipped"
            # cursor is on def render (collapsed) after section 4
            cur = t.cursor_node
            assert cur.data.get("kind") == "def" and cur.data.get("name") == "render", cur.data
            out, stdin_copy = root / "cmd_out.txt", root / "stdin_copy.txt"

            async def run_cmd():
                for p in (out, stdin_copy):
                    p.unlink(missing_ok=True)
                await pilot.press("t")
                for _ in range(100):             # background worker: poll for the files
                    if out.exists() and stdin_copy.exists():
                        break
                    await pilot.pause(0.05)
                assert out.exists(), "user command ran"
                return out.read_text().splitlines()

            got = await run_cmd()
            assert got[0] == "a.py:3-5", got[0]                    # def block bounds
            assert got[1] == "def", got[1]
            assert "render" in got[2] and "def render(theme):" in got[2]                 and "Render the scene." in got[2], got[2]           # symbol + doc line
            assert got[3] == "", "a def has no resolve target"
            assert got[4] == "0", "no positional args — env vars only"
            assert stdin_copy.read_text() == "\n".join(A.splitlines()[2:5]),                 "def snippet is piped to stdin"

            # on a ref node the command also gets the resolved target
            await pilot.press("l"); await pilot.pause()   # expand render, descend to ref
            assert t.cursor_node.data.get("kind") == "ref", t.cursor_node.data
            got = await run_cmd()
            assert got[0] == "a.py:5-5" and got[1] == "ref", got[:2]
            assert got[3] == "b.py:1", "PITH_TARGET carries the resolved definition"
            assert stdin_copy.read_text() == A.splitlines()[4],                 "ref snippet is its single source line"

            print("ALL TESTS PASSED")


asyncio.run(main())


FAKE_FZF = r'''#!/usr/bin/env bash
# pith test stand-in for fzf: echo the first candidate containing $FAKE_PICK,
# else the first candidate.
pick="${FAKE_PICK:-}"
first=""
while IFS= read -r line; do
    if [ -z "$first" ]; then first="$line"; fi
    if [ -n "$pick" ] && [[ "$line" == *"$pick"* ]]; then echo "$line"; exit 0; fi
done
echo "$first"
'''


class _TTY:
    """Fake terminal object: isatty() returns the configured value."""
    def __init__(self, tty=True):
        self._tty = tty

    def isatty(self):
        return self._tty


def test_fzf_pick():
    """_fzf_pick_file: picks via fzf when available + tty, None otherwise."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "a.py").write_text("def a():\n    pass\n")
        (root / "b.py").write_text("def b():\n    pass\n")
        bin_dir = Path(td) / "bin"
        bin_dir.mkdir()
        (bin_dir / "fzf").write_text(FAKE_FZF)
        (bin_dir / "fzf").chmod(0o755)

        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bin_dir}:{old_path}"
        old_stdin, old_stdout = sys.stdin, sys.stdout
        try:
            # not a tty -> fall back, even though fzf is on PATH
            sys.stdin, sys.stdout = _TTY(False), _TTY(False)
            assert _fzf_pick_file(root) is None, "no fzf without a tty"

            # tty + fzf -> picks the FAKE_PICK candidate
            sys.stdin, sys.stdout = _TTY(True), _TTY(True)
            os.environ["FAKE_PICK"] = "b.py"
            assert _fzf_pick_file(root) == "b.py", "fzf returns the FAKE_PICK file"

            # no FAKE_PICK -> first candidate
            os.environ.pop("FAKE_PICK", None)
            assert _fzf_pick_file(root) == "a.py", "defaults to first candidate"
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout
            os.environ["PATH"] = old_path
            os.environ.pop("FAKE_PICK", None)
    print("fzf pick OK")


test_fzf_pick()
