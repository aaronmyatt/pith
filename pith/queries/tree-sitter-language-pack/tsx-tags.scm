; TSX tags query: typescript-tags.scm base plus JSX component references.
; Node names come from the tree-sitter-typescript grammar; the same file works
; for the `typescript` and `tsx` grammars since tsx is a node-type superset.
; Ref: https://github.com/tree-sitter/tree-sitter-typescript/blob/master/queries/tags.scm

(
  (comment)* @doc
  .
  (method_definition
    name: (property_identifier) @name.definition.method) @definition.method
  (#not-eq? @name.definition.method "constructor")
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.method)
)

(
  (comment)* @doc
  .
  [
    (class
      name: (_) @name.definition.class)
    (class_declaration
      name: (_) @name.definition.class)
    (abstract_class_declaration
      name: (type_identifier) @name.definition.class)
  ] @definition.class
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.class)
)

(
  (comment)* @doc
  .
  [
    (function_expression
      name: (identifier) @name.definition.function)
    (function_declaration
      name: (identifier) @name.definition.function)
    (generator_function
      name: (identifier) @name.definition.function)
    (generator_function_declaration
      name: (identifier) @name.definition.function)
    (function_signature
      name: (identifier) @name.definition.function)
  ] @definition.function
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.function)
)

(
  (comment)* @doc
  .
  (lexical_declaration
    (variable_declarator
      name: (identifier) @name.definition.function
      value: [(arrow_function) (function_expression)]) @definition.function)
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.function)
)

(
  (comment)* @doc
  .
  (variable_declaration
    (variable_declarator
      name: (identifier) @name.definition.function
      value: [(arrow_function) (function_expression)]) @definition.function)
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.function)
)

(assignment_expression
  left: [
    (identifier) @name.definition.function
    (member_expression
      property: (property_identifier) @name.definition.function)
  ]
  right: [(arrow_function) (function_expression)]
) @definition.function

(pair
  key: (property_identifier) @name.definition.function
  value: [(arrow_function) (function_expression)]) @definition.function

; -- TypeScript-only declarations ------------------------------------------

(
  (comment)* @doc
  .
  (interface_declaration
    name: (type_identifier) @name.definition.interface) @definition.interface
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.interface)
)

(
  (comment)* @doc
  .
  (type_alias_declaration
    name: (type_identifier) @name.definition.type) @definition.type
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.type)
)

(
  (comment)* @doc
  .
  (enum_declaration
    name: (identifier) @name.definition.enum) @definition.enum
  (#strip! @doc "^[\\s\\*/]+|^[\\s\\*/]$")
  (#select-adjacent! @doc @definition.enum)
)

; declaration-file / ambient constructs (.d.ts)
(method_signature
  name: (property_identifier) @name.definition.method) @definition.method
(abstract_method_signature
  name: (property_identifier) @name.definition.method) @definition.method
(module
  name: (identifier) @name.definition.module) @definition.module

; -- references -------------------------------------------------------------

(
  (call_expression
    function: (identifier) @name.reference.call) @reference.call
  (#not-match? @name.reference.call "^(require)$")
)

(call_expression
  function: (member_expression
    property: (property_identifier) @name.reference.call)
  arguments: (_) @reference.call)

(new_expression
  constructor: (_) @name.reference.class) @reference.class

; type usage in annotations, extends clauses, generics, etc.
(type_annotation
  (type_identifier) @name.reference.type) @reference.type

; -- JSX (React) ------------------------------------------------------------
; <Button /> and <Button>...</Button> link component usage to its definition.
; The uppercase filter excludes intrinsic elements like <div>: JSX treats
; lowercase tag names as host elements, capitalized ones as components.
; Ref: https://react.dev/learn/your-first-component#recap
(
  (jsx_opening_element
    name: (identifier) @name.reference.class) @reference.class
  (#match? @name.reference.class "^[A-Z]")
)
(
  (jsx_self_closing_element
    name: (identifier) @name.reference.class) @reference.class
  (#match? @name.reference.class "^[A-Z]")
)
