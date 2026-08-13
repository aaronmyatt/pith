; The skeleton shows page structure, not div soup: capture semantic/landmark
; tags plus any element carrying an id, and skip anonymous div/span wrappers.
; Skipped wrappers don't break the tree — pith nests by byte containment, so
; an id'd element still lands under its nearest *captured* ancestor even when
; its direct parent was an uncaptured div (mirrors the DOM hierarchy).
; <script>/<style> are distinct node types in this grammar (script_element /
; style_element with raw_text bodies), not `element`, so they need their own
; patterns.
; Grammar: https://github.com/tree-sitter/tree-sitter-html
; Predicates (#any-of? / #eq?), evaluated natively by py-tree-sitter:
; https://tree-sitter.github.io/tree-sitter/using-parsers/queries/3-predicates-and-directives.html

(element
  (start_tag
    (tag_name) @name.definition.element
    (#any-of? @name.definition.element
      "html" "head" "body" "title" "header" "nav" "main" "section"
      "article" "aside" "footer" "form" "fieldset" "table" "template"
      "dialog" "details" "figure" "iframe" "canvas" "svg" "video" "audio"))) @definition.element

; Any element with an id is an addressable anchor — always show it. An element
; that is both semantic and id'd matches twice; file_view() dedupes by byte
; span, so it still yields a single definition.
(element
  (start_tag
    (tag_name) @name.definition.element
    (attribute
      (attribute_name) @_attr
      (#eq? @_attr "id")))) @definition.element

(script_element (start_tag (tag_name) @name.definition.element)) @definition.element
(style_element (start_tag (tag_name) @name.definition.element)) @definition.element
