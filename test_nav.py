"""Headless regression tests for pith nav: / live narrow + h/l drill & walk."""
import asyncio, json, os, sys, tempfile
from pathlib import Path

sys.path.insert(0, "/Users/oya/Development/pith")
from textual.widgets import Tree, Input, Static
from pith.app import PithApp, PickScreen, _fzf_pick_file, MAX_DOC_LINE_CHARS

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

C = '''\
def alpha():
    """Line one.

    Line two.
    Line three.
    """
    return beta()


def beta():
    return 1
'''

D = '''\
def gamma():
    """This docstring line is deliberately very long, well past a hundred characters, so it must collapse instead of overflowing the screen."""
    return 1
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
        (root / "c.py").write_text(C)
        (root / "d.py").write_text(D)
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

            # ---- 6. j/k treat a multi-line doc block as one cursor stop ----
            app.open_file("c.py")
            await pilot.pause()
            await pilot.press("x"); await pilot.pause()   # expand all
            t = app.query_one(Tree)
            t.move_cursor(t.get_node_at_line(0)); await pilot.pause()
            cur = t.cursor_node
            assert cur.data.get("kind") == "def" and cur.data.get("name") == "alpha", cur.data
            await pilot.press("j"); await pilot.pause()
            assert t.cursor_node.data.get("kind") == "doc" and t.cursor_line == 1,                 "j enters the doc block on its first line"
            await pilot.press("j"); await pilot.pause()
            assert t.cursor_node.data.get("kind") == "ref",                 "j hops the rest of the block in one press"
            await pilot.press("j"); await pilot.pause()
            cur = t.cursor_node
            assert cur.data.get("kind") == "def" and cur.data.get("name") == "beta", cur.data
            await pilot.press("k"); await pilot.pause()
            assert t.cursor_node.data.get("kind") == "ref", "k back onto the call line"
            await pilot.press("k"); await pilot.pause()
            assert t.cursor_node.data.get("kind") == "doc" and t.cursor_line == 1,                 "k snaps to the block's first line from below"
            await pilot.press("k"); await pilot.pause()
            cur = t.cursor_node
            assert cur.data.get("kind") == "def" and cur.data.get("name") == "alpha",                 "k exits the block upward in one press"
            # arrow keys go through the same block-aware cursor actions
            await pilot.press("down"); await pilot.pause()
            assert t.cursor_node.data.get("kind") == "doc" and t.cursor_line == 1
            await pilot.press("down"); await pilot.pause()
            assert t.cursor_node.data.get("kind") == "ref",                 "arrow down hops the block like j"

            # ---- 7. left/right mirror h/l (drill in / walk back) -----------
            app.open_file("a.py"); await pilot.pause()
            t = app.query_one(Tree)
            await pilot.press("z"); await pilot.pause()      # collapse all
            await pilot.press("j"); await pilot.pause()      # imports -> render def
            cur = t.cursor_node
            assert cur.data.get("kind") == "def" and cur.data.get("name") == "render", cur.data
            await pilot.press("right"); await pilot.pause()  # right == l: drill into first call
            cur = t.cursor_node
            assert cur.data.get("kind") == "ref", "right descends like l"
            await pilot.press("right"); await pilot.pause()  # right == l: follow the call -> b.py
            assert app.current.path == "b.py", app.current.path
            await pilot.press("left"); await pilot.pause()   # left == h: collapse the landing def
            assert app.current.path == "b.py" and not t.cursor_node.is_expanded,                 "left collapses the expanded selection like h"
            await pilot.press("left"); await pilot.pause()   # left == h: pop the navtree
            assert app.current.path == "a.py", "left walks back up like h"

            # ---- 8. long doc lines collapse; drill in to read the rest -----
            app.open_file("d.py"); await pilot.pause()
            t = app.query_one(Tree)
            await pilot.press("x"); await pilot.pause()      # expand all
            t.move_cursor(t.get_node_at_line(0)); await pilot.pause()
            cur = t.cursor_node
            assert cur.data.get("kind") == "def" and cur.data.get("name") == "gamma", cur.data
            await pilot.press("j"); await pilot.pause()      # onto the long doc line
            cur = t.cursor_node
            assert cur.data.get("kind") == "doc", cur.data
            full_text = cur.data.get("text")
            assert len(full_text) > MAX_DOC_LINE_CHARS,                 "fixture line must actually exceed the truncation threshold"
            label = cur.label.plain
            assert len(label) <= MAX_DOC_LINE_CHARS + 5 and label.rstrip().endswith("…"),                 "long doc line renders collapsed with an ellipsis"
            assert cur.children, "collapsed doc line is expandable to read the rest"
            await pilot.press("right"); await pilot.pause()  # drill in / expand to read the rest
            cur = t.cursor_node
            assert cur.data.get("kind") == "doc" and cur.data.get("text") == full_text,                 "drilling into a truncated doc line lands on its full, untruncated text"
            assert full_text in cur.label.plain,                 "expanded child shows the full line, not just the truncated summary"

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


def test_react_tsx():
    """Indexer React/Next.js support: tsx grammar routing, TS-only defs,
    JSX component refs, and @/ path-alias import resolution."""
    from pith.indexer import Indexer, _lang_for

    # .tsx must use the `tsx` grammar — plain `typescript` cannot parse JSX.
    # Ref: https://github.com/tree-sitter/tree-sitter-typescript#typescript-and-tsx
    assert _lang_for("app/page.tsx") == "tsx"
    assert _lang_for("lib/format.ts") == "typescript"
    assert _lang_for("components/Legacy.jsx") == "javascript"

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "components").mkdir()
        (root / "app").mkdir()
        (root / "components" / "Button.tsx").write_text(
            "interface ButtonProps { label: string }\n"
            "/** Primary action button. */\n"
            "export default function Button({ label }: ButtonProps) {\n"
            "  return <button>{label}</button>;\n"
            "}\n")
        (root / "app" / "page.tsx").write_text(
            "import Button from '@/components/Button';\n"
            "export default function Home() {\n"
            "  return <main><Button label='add' /><div /></main>;\n"
            "}\n")
        ix = Indexer(root)
        ix.discover_files()
        ix.build_symbol_table()

        btn = ix.file_view("components/Button.tsx")
        assert not btn.error, btn.error
        kinds = {d.name: d.kind for d in btn.defs}
        assert kinds == {"ButtonProps": "interface", "Button": "function"}, kinds
        assert next(d for d in btn.defs if d.name == "Button").docstring == \
            "Primary action button."

        page = ix.file_view("app/page.tsx")
        # "@/x" alias resolves against the project root (Next.js convention)
        assert [t.path for t in page.imports[0].targets] == ["components/Button.tsx"]
        home = page.defs[0]
        refs = {r.name: [t.path for t in r.targets] for r in home.refs}
        assert refs.get("Button") == ["components/Button.tsx"], refs
        assert "div" not in refs, "lowercase JSX elements are host tags, not refs"
    print("react/tsx OK")


test_react_tsx()


def test_nextjs():
    """tsconfig "paths" alias resolution + Next.js route-role annotation."""
    from pith.indexer import Indexer
    from pith.app import _nextjs_role

    # -- route roles (pure path logic) -----------------------------------
    assert _nextjs_role("app/page.tsx") == "/ · page"
    assert _nextjs_role("src/app/(shop)/cart/[id]/page.tsx") == "/cart/[id] · page"
    assert _nextjs_role("app/api/users/route.ts") == "/api/users · API route"
    assert _nextjs_role("app/dashboard/layout.tsx") == "/dashboard · layout"
    assert _nextjs_role("pages/blog/[slug].tsx") == "/blog/[slug] · page"
    assert _nextjs_role("pages/index.tsx") == "/ · page"
    assert _nextjs_role("pages/api/hello.ts") == "/api/hello · API route"
    assert _nextjs_role("pages/_app.tsx") == "custom app"
    assert _nextjs_role("middleware.ts") == "middleware"
    assert _nextjs_role("app/utils.ts") is None, "non-convention files unlabeled"
    assert _nextjs_role("lib/app/page.tsx") is None, "only root/src app dirs count"
    assert _nextjs_role("app/page.css") is None

    # -- tsconfig paths aliases ------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "src" / "ui").mkdir(parents=True)
        (root / "src" / "app").mkdir()
        # JSONC on purpose: comments + trailing comma must not break parsing
        (root / "tsconfig.base.json").write_text(
            '{\n'
            '  // shared compiler options\n'
            '  "compilerOptions": {\n'
            '    "paths": { "~ui/*": ["./src/ui/*"], },\n'
            '  },\n'
            '}\n')
        (root / "tsconfig.json").write_text(
            '{ "extends": "./tsconfig.base.json",\n'
            '  "compilerOptions": { "paths": { "@/*": ["./src/*"] } } }\n')
        (root / "src" / "ui" / "Card.tsx").write_text(
            "export const Card = () => <div />;\n")
        (root / "src" / "app" / "page.tsx").write_text(
            "import { Card } from '~ui/Card';\n"
            "import { Card as C2 } from '@/ui/Card';\n"
            "export default function Home() { return <Card />; }\n")
        ix = Indexer(root)
        ix.discover_files()
        ix.build_symbol_table()
        page = ix.file_view("src/app/page.tsx")
        for imp in page.imports:
            assert [t.path for t in imp.targets] == ["src/ui/Card.tsx"], \
                (imp.text, imp.targets)
    print("nextjs OK")


test_nextjs()


def test_markdown():
    """Markdown outline: ATX headings nest by section, symbol table stays clean."""
    from pith.indexer import Indexer, _lang_for, STRUCTURAL_LANGS

    assert _lang_for("README.md") == "markdown"
    assert _lang_for("notes.markdown") == "markdown"
    assert "markdown" in STRUCTURAL_LANGS, "headings shouldn't become repo-wide symbol targets"

    MD = (
        "# Title\n"
        "\n"
        "Intro paragraph.\n"
        "\n"
        "## Section A\n"
        "\n"
        "Body A.\n"
        "\n"
        "### Sub A1\n"
        "\n"
        "Body A1.\n"
        "\n"
        "## Section B\n"
        "\n"
        "Body B.\n"
        "\n"
        "Setext, not captured\n"
        "---------------------\n"
        "\n"
        "Flat text, no grammar-level section boundary for this style.\n"
    )

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "README.md").write_text(MD)
        ix = Indexer(root)
        ix.discover_files()
        ix.build_symbol_table()

        fv = ix.file_view("README.md")
        assert not fv.error, fv.error
        assert [d.name for d in fv.defs] == ["Title"], "one top-level heading"
        title = fv.defs[0]
        assert [d.name for d in title.children] == ["Section A", "Section B"]
        section_a = title.children[0]
        assert [d.name for d in section_a.children] == ["Sub A1"], \
            "h3 nests under its enclosing h2"
        sub_a1 = section_a.children[0]
        assert sub_a1.line == 9 and sub_a1.end_line == 13
        section_b = title.children[1]
        assert section_b.children == [], \
            "setext heading has no grammar-level section, so it's left uncaptured"

        # headings must not pollute the project-wide symbol table (matches
        # the json/yaml/toml "key"/"table" precedent)
        assert not any(name in ix.symbols for name in
                       ("Title", "Section A", "Sub A1", "Section B")), ix.symbols.keys()

        # body text (paragraphs/lists/code) between a heading and its next
        # subsection loads as that section's docstring, like a comment
        assert title.docstring == "Intro paragraph.", title.docstring
        assert section_a.docstring == "Body A.", section_a.docstring
        assert sub_a1.docstring == "Body A1.", sub_a1.docstring
        assert "Body B." in section_b.docstring, section_b.docstring
        assert "Setext, not captured" in section_b.docstring,             "body text runs to the section's end when there's no nested subsection"
    print("markdown OK")


test_markdown()


def test_markdown_render():
    """Markdown section bodies render as syntax-highlighted, truncatable doc text."""
    asyncio.run(_markdown_render())


async def _markdown_render():
    from textual.widgets import Tree
    from pith.app import PithApp

    short_line = "This line has `inline code` and stays under a hundred chars total."
    really_long = ("A very long line of prose that goes on and on well past a hundred "
                    "characters so it must collapse, with `some code` inside it too.")
    assert len(really_long) > 100, "fixture must actually exceed the truncation threshold"
    MD = f"# Title\n\n{short_line}\n\n{really_long}\n"

    with tempfile.TemporaryDirectory() as td:
        os.environ["XDG_CONFIG_HOME"] = td
        root = Path(td)
        (root / "README.md").write_text(MD)
        app = PithApp(root, start_file="README.md")
        async with app.run_test() as pilot:
            for _ in range(300):
                if app.ready:
                    break
                await pilot.pause(0.05)
            assert app.ready
            t = app.query_one(Tree)
            await pilot.press("x"); await pilot.pause()
            title = t.root.children[0]
            doc_lines = [ch for ch in title.children
                        if isinstance(ch.data, dict) and ch.data.get("kind") == "doc"]
            assert len(doc_lines) == 3, [d.label.plain for d in doc_lines]  # short, blank, truncated

            short_leaf = doc_lines[0]
            assert "`inline code`" in short_leaf.label.plain
            assert any("yellow" in str(sp.style) for sp in short_leaf.label.spans),                 "inline code should pick up markdown syntax colour, not just the plain doc style"

            trunc_node = doc_lines[2]
            assert trunc_node.label.plain.rstrip().endswith("…"), "long body line collapses"
            assert trunc_node.children, "collapsed body line is expandable to read the rest"
            full_child = trunc_node.children[0]
            assert really_long in full_child.label.plain,                 "expanding reveals the full, untruncated (and still highlighted) body line"
            assert any("yellow" in str(sp.style) for sp in full_child.label.spans),                 "the expanded full line is highlighted too, not just the truncated summary"
    print("markdown render OK")


test_markdown_render()


def _git(root, *args):
    """Run git in the fixture repo, isolated from the user's config."""
    import subprocess
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "-c", "init.defaultBranch=main", *args],
                   cwd=root, env=env, check=True, capture_output=True)


def _make_git_fixture(root: Path) -> None:
    """Committed a/b/c, then: unstaged edit in paint, staged new file,
    untracked new file, rename of c.py."""
    (root / "a.py").write_text(A)
    (root / "b.py").write_text(B)
    (root / "c.py").write_text(C)
    _git(root, "init")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    (root / "a.py").write_text(A.replace('render("dark")', 'render("light")'))
    (root / "staged.py").write_text("def staged_fn():\n    return 2\n")
    _git(root, "add", "staged.py")
    (root / "new.py").write_text("def fresh():\n    return 1\n")
    _git(root, "mv", "c.py", "c2.py")


def test_gitstate():
    """collect(): codes, hunk intervals, untracked whole-file, no-HEAD, non-repo."""
    from pith.gitstate import collect, WHOLE_FILE

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_git_fixture(root)
        s = collect(root)
        assert s.available
        assert s.codes == {"a.py": "M", "staged.py": "A", "new.py": "?", "c2.py": "R"}, s.codes
        # the unstaged edit is on line 11 (inside Renderer.paint)
        assert s.touches("a.py", 11, 11) and not s.touches("a.py", 3, 5), s.hunks
        assert s.hunks["new.py"] == [(1, WHOLE_FILE)]
        assert s.is_changed("staged.py") and not s.is_changed("missing.py")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "t.py").write_text("x = 1\n")
        _git(root, "init")
        _git(root, "add", "t.py")
        s = collect(root)  # staged file but no commit yet -> no HEAD to diff
        assert s.available and s.hunks["t.py"] == [(1, WHOLE_FILE)], s.hunks

    with tempfile.TemporaryDirectory() as td:
        s = collect(Path(td))  # not a repo
        assert not s.available and not s.codes


test_gitstate()


def test_git_badges():
    asyncio.run(_git_badges())


async def _git_badges():
    def _node(parent, name):
        return next(n for n in parent.children
                    if isinstance(n.data, dict) and n.data.get("name") == name)

    with tempfile.TemporaryDirectory() as td:
        os.environ["XDG_CONFIG_HOME"] = td
        root = Path(td)
        _make_git_fixture(root)

        app = PithApp(root, start_file="a.py")
        async with app.run_test() as pilot:
            for _ in range(300):
                if app.ready:
                    break
                await pilot.pause(0.05)
            assert app.ready
            t = app.query_one(Tree)
            assert "● modified" in t.root.label.plain, t.root.label.plain
            renderer = _node(t.root, "Renderer")
            paint = _node(renderer, "paint")
            render = _node(t.root, "render")
            assert paint.label.plain.startswith("● "), "edited method gets the direct mark"
            assert renderer.label.plain.startswith("○ "), "container gets the inner mark"
            assert not render.label.plain.startswith(("● ", "○ ")), "untouched def stays clean"

            # staged-only change is still badged (diff runs against HEAD)
            app.open_file("staged.py")
            await pilot.pause()
            assert "● added" in t.root.label.plain
            assert _node(t.root, "staged_fn").label.plain.startswith("● ")

            # untracked file: whole file is new, every def marked
            app.open_file("new.py")
            await pilot.pause()
            assert "● untracked" in t.root.label.plain
            assert _node(t.root, "fresh").label.plain.startswith("● ")

            # unchanged file: no badges anywhere
            app.open_file("b.py")
            await pilot.pause()
            assert "●" not in t.root.label.plain
            assert not _node(t.root, "parse").label.plain.startswith(("● ", "○ "))
    print("git badges OK")


test_git_badges()


def test_diff_only():
    asyncio.run(_diff_only())


async def _diff_only():
    changed = {"a.py", "staged.py", "new.py", "c2.py"}
    with tempfile.TemporaryDirectory() as td:
        os.environ["XDG_CONFIG_HOME"] = td
        root = Path(td)
        _make_git_fixture(root)

        app = PithApp(root, start_file="a.py", diff_only=True)
        async with app.run_test() as pilot:
            for _ in range(300):
                if app.ready:
                    break
                await pilot.pause(0.05)
            assert app.ready
            t = app.query_one(Tree)

            app.action_pick_file()
            await pilot.pause()
            assert isinstance(app.screen, PickScreen)
            picked = {v for _, v in app.screen.items}
            assert picked == changed, picked
            await pilot.press("escape"); await pilot.pause()

            app.action_jump_symbol()
            await pilot.pause()
            assert isinstance(app.screen, PickScreen)
            sym_paths = {loc.path for _, loc in app.screen.items}
            assert sym_paths <= changed, sym_paths
            assert "b.py" not in sym_paths
            await pilot.press("escape"); await pilot.pause()

            # navigation OUT of the diff stays unrestricted: follow the
            # b.parse ref from a.py into unchanged b.py, then back
            app.open_file("b.py")
            await pilot.pause()
            assert app.current.path == "b.py"
            await pilot.press("b"); await pilot.pause()
            assert app.current.path == "a.py"
    print("diff only OK")


test_diff_only()


def test_html():
    """HTML outline: semantic + id'd elements nest like the DOM, div soup skipped."""
    from pith.indexer import Indexer, _lang_for, STRUCTURAL_LANGS

    assert _lang_for("index.html") == "html"
    assert _lang_for("page.htm") == "html"
    assert "html" in STRUCTURAL_LANGS, "elements shouldn't become repo-wide symbol targets"

    HTML = (
        "<!doctype html>\n"
        "<html>\n"
        "<head>\n"
        "  <title>T</title>\n"
        '  <script src="app.js"></script>\n'
        "</head>\n"
        "<body>\n"
        "  <header><nav>x</nav></header>\n"
        "  <main>\n"
        '    <section id="pricing" class="grid">\n'
        '      <div class="wrap">\n'
        '        <form id="signup" method="post"><span>anon</span></form>\n'
        "      </div>\n"
        "    </section>\n"
        "  </main>\n"
        "  <footer>f</footer>\n"
        "  <style>body{}</style>\n"
        "</body>\n"
        "</html>\n"
    )

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "index.html").write_text(HTML)
        ix = Indexer(root)
        ix.discover_files()
        ix.build_symbol_table()

        fv = ix.file_view("index.html")
        assert not fv.error, fv.error
        assert [d.name for d in fv.defs] == ["html"], "one top-level element"
        html = fv.defs[0]
        assert all(d.kind == "element" for d in fv.defs)
        assert [d.name for d in html.children] == ["head", "body"]
        head, body = html.children
        assert [d.name for d in head.children] == ["title", "script"]
        assert [d.name for d in body.children] == ["header", "main", "footer", "style"]
        header, main, _footer, _style = body.children
        assert [d.name for d in header.children] == ["nav"]
        section = main.children[0]
        assert section.name == "section" and 'id="pricing"' in section.signature
        # the anonymous <div class="wrap"> is uncaptured, but byte-containment
        # nesting still lands its id'd child under the nearest captured ancestor
        assert [d.name for d in section.children] == ["form"], \
            "form#signup nests under section#pricing even though its direct parent div is skipped"
        form = section.children[0]
        assert 'id="signup"' in form.signature
        assert form.children == [], "anonymous <span> stays out of the skeleton"

        # elements must not pollute the project-wide symbol table (matches the
        # markdown/json/yaml/toml structural-language precedent)
        assert not any(name in ix.symbols for name in
                       ("html", "body", "section", "form", "nav")), ix.symbols.keys()
    print("html OK")


test_html()
