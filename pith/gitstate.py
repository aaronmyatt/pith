"""Uncommitted-change state: which files changed, and which lines.

Feeds the git badges in the skeleton tree and the ``pith --diff`` file-set
filter.  The diff base is always "working tree + index vs HEAD", i.e. every
change that would vanish after ``git commit -a`` plus untracked files.

Doc refs:
  porcelain v1 format  https://git-scm.com/docs/git-status#_porcelain_format_version_1
  unified hunk headers https://git-scm.com/docs/git-diff#generate_patch_text_with_p
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Sentinel hunk end meaning "every line of the file changed" (untracked or
# newly added files).  Large enough to overlap any real def's line range.
WHOLE_FILE = 1_000_000_000


@dataclass
class GitState:
    """Snapshot of uncommitted changes; empty/available=False outside a repo."""
    available: bool = False
    # rel path -> one-letter code: M(odified) A(dded) ?(untracked) R(enamed) D(eleted)
    codes: dict[str, str] = field(default_factory=dict)
    # rel path -> sorted, merged (start, end) changed-line intervals,
    # 1-based inclusive, on the new-file side of the diff
    hunks: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    def code(self, rel: str) -> str | None:
        return self.codes.get(rel)

    def is_changed(self, rel: str) -> bool:
        """True for files worth exploring — deleted files can't be opened."""
        return self.codes.get(rel, "D") != "D"

    def touches(self, rel: str, start: int, end: int) -> bool:
        """Does any changed line fall inside [start, end] (1-based inclusive)?"""
        return any(a <= end and b >= start for a, b in self.hunks.get(rel, ()))


def _run_git(root: Path, args: list[str], timeout: float) -> subprocess.CompletedProcess:
    # --no-optional-locks: a viewer must not write .git/index just by looking.
    # Ref: https://git-scm.com/docs/git#Documentation/git.txt---no-optional-locks
    return subprocess.run(["git", "--no-optional-locks", *args],
                          cwd=root, capture_output=True, timeout=timeout)


def _parse_porcelain_z(out: bytes) -> dict[str, str]:
    """``git status --porcelain=v1 -z`` -> {rel path: collapsed code}.

    -z entries are "XY path" NUL-terminated; renames/copies are followed by a
    second NUL-terminated token holding the *old* path, which we discard.
    Paths are NOT quoted in -z mode, so non-ASCII names come through verbatim.
    """
    codes: dict[str, str] = {}
    tokens = out.split(b"\0")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if len(tok) < 4:  # trailing empty token after the final NUL
            continue
        xy, path = tok[:2].decode(errors="replace"), tok[3:].decode(errors="replace")
        if "R" in xy or "C" in xy:
            i += 1  # skip the old-path token
        if xy == "??":
            code = "?"
        elif "R" in xy or "C" in xy:
            code = "R"
        elif "A" in xy:
            code = "A"
        elif xy[1] == "D" or xy == "D ":
            # gone from the working tree ("␣D", "MD", "AD", "DD") or a staged
            # delete with nothing re-created ("D␣") — either way, not openable
            code = "D"
        else:
            code = "M"
        codes[path] = code
    return codes


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _parse_hunks(out: str) -> dict[str, list[tuple[int, int]]]:
    """``git diff -U0`` -> {rel path: merged new-side (start, end) intervals}.

    Hunk headers look like ``@@ -a,b +c,d @@``; with -U0 there is no context,
    so c..c+d-1 is exactly the changed lines.  d defaults to 1 when omitted;
    d == 0 means a pure deletion — we mark the seam line so the enclosing def
    still shows a change.  Binary files emit no @@ lines at all.
    """
    hunks: dict[str, list[tuple[int, int]]] = {}
    path: str | None = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            # "+++ b/<path>" on the new side; "+++ /dev/null" for deletions.
            target = line[4:]
            path = target[2:] if target.startswith("b/") else None
        elif path and (m := _HUNK_RE.match(line)):
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            if count == 0:
                iv = (max(start, 1), max(start, 1))
            else:
                iv = (start, start + count - 1)
            hunks.setdefault(path, []).append(iv)
    # merge overlapping/adjacent intervals so touches() stays a short scan
    for p, ivs in hunks.items():
        ivs.sort()
        merged = [ivs[0]]
        for a, b in ivs[1:]:
            la, lb = merged[-1]
            if a <= lb + 1:
                merged[-1] = (la, max(lb, b))
            else:
                merged.append((a, b))
        hunks[p] = merged
    return hunks


def collect(root: Path, timeout: float = 5.0) -> GitState:
    """Gather uncommitted-change state for the repo at *root*.

    Never raises: any git failure (no repo, no git binary, timeout) yields
    GitState(available=False) and pith degrades to badge-less behaviour —
    same defensive stance as Indexer.discover_files().
    """
    try:
        st = _run_git(root, ["status", "--porcelain=v1", "-z"], timeout)
        if st.returncode != 0:
            return GitState()
        codes = _parse_porcelain_z(st.stdout)
        state = GitState(available=True, codes=codes)

        # A repo before its first commit has no HEAD to diff against.  Don't
        # hardcode the empty-tree hash (it differs under SHA-256 repos) —
        # just treat every tracked change as whole-file.
        # Ref: https://git-scm.com/docs/git-rev-parse#Documentation/git-rev-parse.txt---verify
        head = _run_git(root, ["rev-parse", "--verify", "-q", "HEAD"], timeout)
        if head.returncode == 0:
            # One diff against HEAD covers staged AND unstaged changes.
            # -M keeps rename hunks under the new path; -U0 = no context lines.
            diff = _run_git(root, ["diff", "HEAD", "-U0", "--no-color",
                                   "--no-ext-diff", "-M"], timeout)
            if diff.returncode in (0, 1):  # 1 just means "differences found"
                state.hunks = _parse_hunks(diff.stdout.decode(errors="replace"))
        else:
            for rel, code in codes.items():
                if code != "?":
                    state.hunks[rel] = [(1, WHOLE_FILE)]

        # Untracked files have no diff — the whole file is new.
        for rel, code in codes.items():
            if code == "?":
                state.hunks[rel] = [(1, WHOLE_FILE)]
        return state
    except Exception:
        return GitState()
