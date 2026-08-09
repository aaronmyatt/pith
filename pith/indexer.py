"""Tree-sitter based repo indexer for pith.

Extracts, per file: top-level definitions (classes/functions/methods/constants),
signatures, docstrings/doc-comments, imports, and call references scoped to each
definition. Builds a project-wide symbol table so references can link out to the
files that define them.

Tag queries are vendored from the aider project (Apache-2.0), which in turn
derives them from the individual tree-sitter grammar repos.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from grep_ast import filename_to_lang
from grep_ast.tsl import get_language, get_parser
from tree_sitter import Query, QueryCursor

QUERY_DIR = Path(__file__).parent / "queries"

MAX_FILE_BYTES = 400_000

IMPORT_NODE_TYPES = {
    "import_statement", "import_from_statement", "future_import_statement",
    "import_declaration", "import_spec", "use_declaration", "import_header",
    "using_directive", "require_call", "include_statement", "use_statement",
    "preproc_include", "import", "extern_crate_declaration", "load_statement",
}

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".mypy_cache", ".pytest_cache", "target", ".next", ".cache", "vendor",
    ".tox", "coverage", ".idea", ".vscode",
}


@dataclass
class Location:
    path: str  # repo-relative
    line: int  # 1-based
    kind: str = ""


@dataclass
class Reference:
    name: str
    line: int  # 1-based, in the referencing file
    targets: list[Location] = field(default_factory=list)


@dataclass
class Definition:
    name: str
    kind: str  # class / function / method / constant / ...
    line: int
    end_line: int
    signature: str
    docstring: str
    children: list["Definition"] = field(default_factory=list)
    refs: list[Reference] = field(default_factory=list)
    start_byte: int = 0
    end_byte: int = 0


@dataclass
class ImportItem:
    text: str
    line: int
    targets: list[Location] = field(default_factory=list)


@dataclass
class FileView:
    path: str  # repo-relative
    lang: str | None
    imports: list[ImportItem] = field(default_factory=list)
    defs: list[Definition] = field(default_factory=list)
    module_refs: list[Reference] = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------

_query_cache: dict[str, Query | None] = {}


def _load_query(lang: str) -> Query | None:
    if lang in _query_cache:
        return _query_cache[lang]
    q = None
    for sub in ("tree-sitter-language-pack", "tree-sitter-languages"):
        p = QUERY_DIR / sub / f"{lang}-tags.scm"
        if p.exists():
            try:
                q = Query(get_language(lang), p.read_text())
                break
            except Exception:
                q = None
    _query_cache[lang] = q
    return q


def _clean_sig(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip("{:").strip()


_COMMENT_STRIP = re.compile(r"^\s*(///?|/\*+|\*+/|\*|#+!?|--|;+|%)\s?")


def _clean_comment(text: str) -> str:
    lines = []
    for ln in text.splitlines():
        ln = _COMMENT_STRIP.sub("", ln).rstrip("*/ ").rstrip()
        lines.append(ln)
    out = "\n".join(lines).strip()
    return out


def _python_docstring(node) -> str:
    body = node.child_by_field_name("body")
    if body is None or body.named_child_count == 0:
        return ""
    first = body.named_children[0]
    s = None
    if first.type == "string":
        s = first
    elif first.type == "expression_statement" and first.named_child_count:
        if first.named_children[0].type == "string":
            s = first.named_children[0]
    if s is not None:
        if True:
            raw = s.text.decode("utf-8", "replace")
            raw = re.sub(r'^[rbuRBU]*("""|\'\'\'|"|\')', "", raw)
            raw = re.sub(r'("""|\'\'\'|"|\')$', "", raw)
            # dedent while preserving paragraph structure
            lines = raw.strip("\n").splitlines()
            if lines:
                body_lines = lines[1:]
                indents = [len(l) - len(l.lstrip()) for l in body_lines if l.strip()]
                pad = min(indents) if indents else 0
                lines = [lines[0].strip()] + [l[pad:] if len(l) >= pad else l for l in body_lines]
            return "\n".join(lines).rstrip()
    return ""


_LINE_COMMENT = re.compile(r"^\s*(///?|#|--|;+|%|\*)")
_BLOCK_END = re.compile(r"\*/\s*$")
_BLOCK_START = re.compile(r"^\s*/\*")


def _comment_docstring(def_row: int, source_lines: list[str]) -> str:
    """Doc comment block on the lines immediately preceding the definition.

    Purely textual (no tree-sitter sibling navigation, which is unstable in
    py-tree-sitter 0.26). Handles //, ///, #, --, ;, % line comments and
    /* ... */ block comments ending on the previous line. Also skips over
    decorators/annotations (@foo, #[...]) between the comment and the def.
    """
    i = def_row - 1  # row above the definition (0-based rows)
    # skip decorators / attributes between comment and definition
    while i >= 0 and re.match(r"^\s*(@\w|#\[|\[\<)", source_lines[i]):
        i -= 1
    comments: list[str] = []
    if i >= 0 and _BLOCK_END.search(source_lines[i]):
        j = i
        while j >= 0:
            comments.append(source_lines[j])
            if _BLOCK_START.match(source_lines[j]) or "/*" in source_lines[j]:
                break
            j -= 1
        comments.reverse()
    else:
        while i >= 0 and _LINE_COMMENT.match(source_lines[i]):
            comments.insert(0, source_lines[i])
            i -= 1
    if not comments:
        return ""
    return _clean_comment("\n".join(comments))


def _signature(node, source: bytes) -> str:
    body = node.child_by_field_name("body")
    if body is not None and body.start_byte > node.start_byte:
        sig = source[node.start_byte : body.start_byte].decode("utf-8", "replace")
    else:
        text = node.text.decode("utf-8", "replace")
        sig = text.splitlines()[0] if text else ""
    sig = _clean_sig(sig)
    return sig[:400]


class Indexer:
    def __init__(self, root: Path):
        self.root = root.resolve()
        # symbol name -> list of Location where it is *defined*
        self.symbols: dict[str, list[Location]] = {}
        self.files: list[str] = []
        self._file_set: set[str] = set()

    # -- repo walking -------------------------------------------------------

    def discover_files(self) -> list[str]:
        files: list[str] = []
        try:
            out = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                cwd=self.root, capture_output=True, text=True, timeout=15,
            )
            if out.returncode == 0:
                files = [f for f in out.stdout.splitlines() if f.strip()]
        except Exception:
            pass
        if not files:
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                for fn in filenames:
                    rel = os.path.relpath(os.path.join(dirpath, fn), self.root)
                    files.append(rel)
        files = [f for f in files if (self.root / f).is_file()
                 and not any(part in SKIP_DIRS for part in Path(f).parts)]
        files.sort()
        self.files = files
        self._file_set = set(files)
        return files

    def source_files(self) -> list[str]:
        return [f for f in self.files if filename_to_lang(f)]

    # -- project symbol table ----------------------------------------------

    def build_symbol_table(self, progress=None) -> None:
        self.symbols = {}
        src = self.source_files()
        for i, rel in enumerate(src):
            if progress:
                progress(i, len(src), rel)
            try:
                self._index_file_defs(rel)
            except Exception:
                continue

    def _index_file_defs(self, rel: str) -> None:
        lang = filename_to_lang(rel)
        if not lang:
            return
        query = _load_query(lang)
        if query is None:
            return
        path = self.root / rel
        try:
            data = path.read_bytes()
        except OSError:
            return
        if len(data) > MAX_FILE_BYTES:
            return
        parser = get_parser(lang)
        tree = parser.parse(data)
        captures = QueryCursor(query).captures(tree.root_node)
        for cap_name, nodes in captures.items():
            if not cap_name.startswith("name.definition."):
                continue
            kind = cap_name.rsplit(".", 1)[-1]
            for n in nodes:
                name = n.text.decode("utf-8", "replace")
                self.symbols.setdefault(name, []).append(
                    Location(rel, n.start_point.row + 1, kind)
                )

    # -- single-file view ---------------------------------------------------

    def file_view(self, rel: str) -> FileView:
        lang = filename_to_lang(rel)
        fv = FileView(path=rel, lang=lang)
        path = self.root / rel
        try:
            data = path.read_bytes()
        except OSError as e:
            fv.error = str(e)
            return fv
        if not lang:
            fv.error = "No tree-sitter grammar for this file type."
            return fv
        query = _load_query(lang)
        if query is None:
            fv.error = f"No tags query available for language: {lang}"
            return fv
        if len(data) > MAX_FILE_BYTES:
            fv.error = "File too large to parse."
            return fv

        parser = get_parser(lang)
        tree = parser.parse(data)
        root = tree.root_node
        source_lines = data.decode("utf-8", "replace").splitlines()
        all_caps = QueryCursor(query).captures(root)

        # pair @name.definition.X captures with their enclosing @definition.X
        # node by byte containment (avoids QueryCursor.matches(), which
        # segfaults in py-tree-sitter 0.26)
        def_nodes: list[tuple] = []  # (node, kind)
        name_nodes: list[tuple] = []  # (node, kind)
        for cap_name, nodes in all_caps.items():
            if cap_name.startswith("name.definition."):
                kind = cap_name.rsplit(".", 1)[-1]
                name_nodes.extend((n, kind) for n in nodes)
            elif cap_name.startswith("definition."):
                kind = cap_name.split(".", 1)[1]
                def_nodes.extend((n, kind) for n in nodes)

        defs: list[Definition] = []
        seen_spans: set[tuple[int, int, str]] = set()
        for name_node, kind in name_nodes:
            # smallest definition node of the same kind containing the name
            def_node = None
            for dn, dkind in def_nodes:
                if dkind != kind:
                    continue
                if dn.start_byte <= name_node.start_byte and name_node.end_byte <= dn.end_byte:
                    if def_node is None or (dn.end_byte - dn.start_byte) < (
                        def_node.end_byte - def_node.start_byte
                    ):
                        def_node = dn
            if def_node is None:
                def_node = name_node
            span = (def_node.start_byte, def_node.end_byte, kind or "")
            if span in seen_spans:
                continue
            seen_spans.add(span)
            name = name_node.text.decode("utf-8", "replace")
            doc = ""
            if lang == "python":
                doc = _python_docstring(def_node)
            if not doc:
                doc = _comment_docstring(def_node.start_point.row, source_lines)
            defs.append(Definition(
                name=name,
                kind=kind or "def",
                line=def_node.start_point.row + 1,
                end_line=def_node.end_point.row + 1,
                signature=_signature(def_node, data),
                docstring=doc,
                start_byte=def_node.start_byte,
                end_byte=def_node.end_byte,
            ))

        defs.sort(key=lambda d: (d.start_byte, -(d.end_byte)))

        # nest by byte containment
        top: list[Definition] = []
        stack: list[Definition] = []
        for d in defs:
            while stack and d.start_byte >= stack[-1].end_byte:
                stack.pop()
            if stack:
                if d.kind == "function":
                    d.kind = "method"
                stack[-1].children.append(d)
            else:
                top.append(d)
            stack.append(d)
        fv.defs = top

        # references, assigned to innermost containing definition
        flat = defs
        ref_nodes = []
        for cap_name, nodes in all_caps.items():
            if cap_name.startswith("name.reference."):
                ref_nodes.extend(nodes)
        ref_nodes.sort(key=lambda n: n.start_byte)
        def_names_at: dict[tuple[int, int], bool] = {}
        for n in ref_nodes:
            name = n.text.decode("utf-8", "replace")
            owner: Definition | None = None
            for d in flat:
                if d.start_byte <= n.start_byte < d.end_byte:
                    if owner is None or d.start_byte >= owner.start_byte:
                        owner = d
            ref = Reference(name=name, line=n.start_point.row + 1)
            bucket = owner.refs if owner is not None else fv.module_refs
            if any(r.name == name for r in bucket):
                continue
            bucket.append(ref)

        # imports (top-level walk, two levels deep to catch wrapped forms)
        def walk_imports(node, depth=0):
            for ch in node.named_children:
                if ch.type in IMPORT_NODE_TYPES or (
                    "import" in ch.type and "string" not in ch.type
                ):
                    text = ch.text.decode("utf-8", "replace")
                    text = re.sub(r"\s+", " ", text).strip()
                    fv.imports.append(ImportItem(text=text[:200], line=ch.start_point.row + 1))
                elif depth < 1 and ch.type in ("program", "source_file", "module",
                                               "translation_unit", "compilation_unit"):
                    walk_imports(ch, depth + 1)
        walk_imports(root)
        for imp in fv.imports:
            imp.targets = self._resolve_import(rel, imp.text)

        # resolve references against the project symbol table
        for d in flat:
            for r in d.refs:
                r.targets = self._resolve_symbol(rel, r.name)
        for r in fv.module_refs:
            r.targets = self._resolve_symbol(rel, r.name)

        return fv

    # -- resolution ---------------------------------------------------------

    def _resolve_symbol(self, from_file: str, name: str) -> list[Location]:
        locs = self.symbols.get(name, [])
        if not locs:
            return []
        same = [l for l in locs if l.path == from_file]
        if same:
            others = [l for l in locs if l.path != from_file]
            return same + others
        # prefer definitions near the referencing file in the tree
        from_parts = Path(from_file).parts

        def proximity(l: Location) -> int:
            parts = Path(l.path).parts
            common = 0
            for a, b in zip(from_parts, parts):
                if a == b:
                    common += 1
                else:
                    break
            return -common

        return sorted(locs, key=proximity)[:20]

    def _resolve_import(self, from_file: str, text: str) -> list[Location]:
        """Best-effort: map an import statement to project files."""
        candidates: list[str] = []
        # quoted module paths (js/ts/go/rust use-strings etc.)
        for m in re.findall(r"""["']([^"']+)["']""", text):
            candidates.append(m)
        # python: import a.b.c / from a.b import d
        pm = re.match(r"\s*(?:from|import)\s+([\w.]+)", text)
        if pm:
            candidates.append(pm.group(1).replace(".", "/"))
        out: list[Location] = []
        exts = [".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go", ".rs", ".rb",
                ".java", ".kt", ".swift", ".c", ".h", ".cpp", ".hpp", ".cs", ".php"]
        base = Path(from_file).parent
        for cand in candidates:
            cand = cand.lstrip("@")
            paths: list[str] = []
            if cand.startswith("."):
                joined = os.path.normpath(str(base / cand))
                paths.append(joined)
            paths.append(cand)
            for p in paths:
                for suffix in [""] + exts + ["/__init__.py", "/index.ts", "/index.js", "/mod.rs"]:
                    trial = p + suffix
                    if trial in self._file_set and trial != from_file:
                        out.append(Location(trial, 1, "file"))
        # dedupe, keep order
        seen = set()
        uniq = []
        for l in out:
            if l.path not in seen:
                seen.add(l.path)
                uniq.append(l)
        return uniq[:5]
