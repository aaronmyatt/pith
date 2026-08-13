; tree-sitter-markdown wraps each ATX heading (# ... ######) and everything
; under it (including nested subsections) in a `section` node, so byte-
; containment nesting alone reproduces the heading hierarchy (h1 > h2 > h3
; ...) the same way a class's body nests its methods for other languages.
; Setext headings (Title\n=====) are NOT given their own `section` by this
; grammar — they stay flat inside whatever section already encloses them —
; so they're intentionally left uncaptured rather than mis-nested.
; Ref: https://github.com/tree-sitter-grammars/tree-sitter-markdown

(section
  (atx_heading (inline) @name.definition.section)) @definition.section
