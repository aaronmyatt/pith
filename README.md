# pith

Skeleton-first codebase navigation, one file at a time.

`pith` shows you the *pith* of a source file — packages, classes,
functions/methods with their signatures (inputs/outputs) and docstrings —
plus the calls each definition makes, linked out to the files that define
them. Implementation details stay hidden until you deliberately jump to your
editor (or a tool like [peel](https://github.com/aaronmyatt/peel)) for the
deep dive.

Built on tree-sitter (via `tree-sitter-language-pack`), so it works across
~40 languages with no language server required. Definition/reference queries
are vendored from the [aider](https://github.com/Aider-AI/aider) project
(Apache-2.0).

## Install

```sh
pipx install ./pith        # or: pip install ./pith
```

## Use

```sh
pith                 # pick a file with fzf (instant), then open it
pith src/engine.py   # open one file directly (repo root auto-detected via .git)
pith ~/code/myrepo   # index a specific repo
pith --no-fzf        # skip fzf; use pith's built-in picker

When `fzf` is installed, `pith` (and `pith <dir>`) start with fzf over the
repo's source files — fzf launches in milliseconds, so you pick the file
*before* the slower Textual boot, and the chosen file opens directly. Esc in
fzf (or no fzf on PATH / no tty) falls back to pith's in-app picker.
```

On startup pith indexes every source file in the repo (fast — hundreds of
files per second) to build a project-wide symbol table, then shows one file's
skeleton:

```
ƒ def parse(source: bytes, grammar: str) -> Tree   :30   Parse ``source`` with the…
  │  Full docstring, shown when expanded…
  │  → get_parser
  │  → walk_tree  peel/engine.py:261
◆ class Renderer  :55   Renders the peeled view.
  ƒ def render(...)
```

- Green `→ name  path:line` = a call resolved to a definition in *this*
  project. Press enter (or click) to jump to that file's skeleton — you're
  now at step 1 again, one level deeper. `b` walks back up your trail.
- Dim `→ name` = calls into the stdlib / dependencies (not in the repo).
- The first docstring line sits inline next to each signature; expand the
  definition for the full docstring and its outgoing calls.

## Keys

| key | action |
| --- | --- |
| `j` / `k`, arrows | move |
| `l` | drill down: expand a definition, descend into its calls, follow a call/import into the next file |
| `h` | walk back up: collapse a def, move to its parent, pop back to the previous file |
| `enter` / click | drill down (same as `l`): expand + descend into calls, follow a call/import |
| `e` (or `ctrl+enter`) | open current item in `$EDITOR` at its line |
| `b` / `backspace` | back to previous file |
| `f` | file picker |
| `/` | search: narrow the current screen as you type (see below) |
| `m` / `M` | bookmark current line (toggle) / list & jump to bookmarks |
| `g` | jump to any symbol in the repo (project-wide) |
| `enter` | commit the narrowed view · `esc` cancel / clear |
| `x` / `z` | expand all / collapse all |
| `r` | re-parse current file |
| `?` | help · `q` quit |

`$EDITOR` handling: terminal editors (vim, nano, helix…) suspend the TUI and
resume when you quit; `code`/`cursor`/`subl`/`zed` open at the exact line in
the background. Override with `pith --editor 'code'`.

## Search: narrow the current screen

`/` starts an incremental search that narrows the current screen to **anything
visible on it** — definitions, calls, imports, docstring lines, module-level
references. As you type, the skeleton prunes to matching lines (ancestors of
matches are kept); `enter` commits the narrowed view, `esc` cancels the search
or clears the narrow. Typing while already narrowed searches within what's on
screen, and the narrow follows you as you drill: `/parse` + `enter` on a call
keeps trawling the same thread through every file you land in.

## Bookmarks & jumping around

`m` bookmarks the current line (def, call, import — whatever the cursor is on);
press again to remove. Bookmarked lines get a yellow ★ and the status bar shows
the count. `M` lists all bookmarks (filter as you type) and jumps to one.
Bookmarks persist per-repo in `~/.config/pith/bookmarks/<repo>.json`, so they
survive restarts.

`g` jumps to any definition in the whole repo — the project symbol table is
built at startup, so type a symbol name and land on its `file:line` directly.

## Orientation & legibility

- The status bar shows a breadcrumb of where you are — `Renderer › paint` —
  and it tracks the cursor as you move.
- While a `/` search is active, the matched text is bold-underlined in every
  visible label, so you can see exactly why each line was kept.
- Deeper tree guides make nested classes/functions read more clearly.

## How references are resolved

Resolution is syntactic (tree-sitter, not a type checker): a call name is
matched against the project-wide table of definitions, preferring same-file
definitions, then definitions in nearby directories. Ambiguous names (e.g.
`render` defined in three files) pop a picker. This is the same trade-off
aider's repo map makes — occasionally wrong, never in your way.

## Notes

- Pinned to `tree-sitter >=0.25,<0.26`: py-tree-sitter 0.26.0 has a
  use-after-free that segfaults under exactly this workload.
- `pith/queries/` is vendored from aider (Apache-2.0), which derives them
  from the individual tree-sitter grammar repos.
