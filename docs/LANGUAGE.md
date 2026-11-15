# The Medha language reference

This document describes Medha 0.1. Source files use the `.mdh` extension and are UTF-8 text.

- [Lexical structure](#lexical-structure)
- [Values](#values)
- [Bindings and assignment](#bindings-and-assignment)
- [Expressions](#expressions)
- [Control flow](#control-flow)
- [Functions](#functions)
- [Types](#types)
- [Models](#models)
- [Prompts](#prompts)
- [Agents](#agents)
- [Tests and mocks](#tests-and-mocks)
- [Budgets](#budgets)
- [Errors](#errors)
- [Modules](#modules)
- [Standard library](#standard-library)

## Lexical structure

```rust
// line comment
/* block comment */
/// doc comment: attaches to the next type, field, function, prompt or agent
```

Newlines usually do not matter and semicolons are optional. Three rules keep that unambiguous:

1. `(` and `[` continue an expression only on the **same line** (so a new line starting with `[` is a new statement).
2. A line starting with `-` starts a new statement.
3. The word operators `in`, `is`, `as` and `not in` must be on the same line as their left operand.

Other operators (`|>`, `+`, `and`, `.` …) may start a continuation line.

**Reserved words:** `let var fn return if else while for in match import as is and or not true false null try catch finally throw break continue assert`.

**Contextual words** are special only where they are used and are otherwise ordinary names:
`type model prompt agent test mock budget using with where`, and inside prompt bodies
`system user assistant example tools max_steps`.

### Strings

```rust
let name = "Medha"
let a = "hello {name}, 2 + 2 = {2 + 2}"      // {expression} interpolates
let b = "literal \{braces\} and a \"quote\""  // escapes: \n \t \r \" \\ \{ \}
let c = r"raw: no {interpolation}, no \escapes"
let d = """
    Triple-quoted strings span lines.
    Their common indentation is removed,
      so this line keeps two spaces.
    """
```

## Values

| Kind | Examples | Notes |
| --- | --- | --- |
| `null` | `null` | |
| `bool` | `true`, `false` | |
| `int` | `42`, `1_000_000` | arbitrary precision |
| `float` | `3.14`, `1e-3` | |
| `string` | `"text"` | immutable |
| list | `[1, 2, 3]`, `[...xs, 4]` | mutable, ordered |
| record | `{name: "Asha", "odd key": 1, [computed]: 2, ...other}` | mutable, ordered, string keys; `{name}` is shorthand for `{name: name}` |
| function | `fn(x) { x }`, `x => x` | closures |
| type | `int`, `Ticket`, `[string]` | types are values |
| model, prompt, agent, chat | | see below |

**Truthiness:** `null`, `false`, `0`, `""`, `[]` and `{}` are falsy. Everything else is truthy.

**Equality** (`==`) is structural for lists and records. `1 == 1.0` is true; `true == 1` is false.

## Bindings and assignment

```rust
let x = 1                 // immutable
var y = 2                 // mutable
let z: float = 3          // annotated: validated when bound (an int is accepted as a float)
let {a, b: {c}} = {a: 1, b: {c: 2}}   // record destructuring
let [head, ...tail] = [1, 2, 3]       // list destructuring

y = y + 1
y += 1                    // also -= *= /=
```

Assigning to a `let` binding is an error, caught before the program runs. Fields and items of a
record or list can always be assigned: `user.age = 31`, `xs[0] = 9`, `user["city"] = "Pune"`.

Blocks (`{ ... }`) introduce a scope. Functions, prompts, agents and types declared in a block are
visible throughout that block, so they can be used before their definition and be mutually recursive.

## Expressions

From lowest to highest precedence:

| Operators | Meaning |
| --- | --- |
| `x => e`, `(a, b) => e` | lambda |
| `a \|> f`, `a \|> f(b)` | pipeline: `f(a)`, `f(a, b)` |
| `a ?? b` | `b` if `a` is null |
| `or` | short-circuit, returns an operand |
| `and` | short-circuit, returns an operand |
| `not` | |
| `== != < <= > >=`, `in`, `not in`, `is`, `is not`, `as` | comparison, membership, type test, coercion |
| `a..b` | list of ints from `a` up to but not including `b` |
| `+ -` | numbers; `+` also joins two strings or two lists |
| `* / %` | `/` always produces a float; `"ab" * 3` repeats |
| `-x` | negation |
| `**` | power, right-associative |
| `f(x)`, `xs[i]`, `xs[a:b]`, `r.field`, `r?.field` | call, index, slice, field, null-safe field |

Notes:

- Adding a string to a non-string is an error; use interpolation.
- Indexing a list out of range throws `IndexError`; negative indexes count from the end. Indexing a
  record with a missing key gives `null`; `r.missing` throws `FieldError` while `r?.missing` gives `null`.
- `x is T` tests a value against a type. `x as T` validates with coercion and returns the result.
- Calls take positional arguments, named arguments (`f(1, verbose: true)`) and spreads (`f(...list)`).

### Pipelines and uniform calls

Every method is also a free function taking the receiver first, and every function can be called
as a method. These are equivalent:

```rust
let a = [3, 1, 2].sort().map(x => x * 2)
let b = map(sort([3, 1, 2]), x => x * 2)
let c = [3, 1, 2] |> sort() |> map(x => x * 2)
```

`|>` binds more loosely than comparison, so parenthesise a pipeline you compare: `(xs |> len()) == 3`.

## Control flow

```rust
if score > 3 { print("good") } else if score == 3 { print("ok") } else { print("bad") }

let label = if score > 3 { "good" } else { "bad" }   // the last expression of a block is its value

while n > 0 { n -= 1 }

for x in [1, 2, 3] { print(x) }
for i, x in ["a", "b"] { print(i, x) }       // index and item
for key, value in {a: 1} { print(key, value) }
for ch in "abc" { print(ch) }
for i in 0..10 { if i == 5 { break }; if i % 2 == 0 { continue } }
```

### match

```rust
fn describe(v) {
  match v {
    0 -> "zero"                               // literal
    1 | 2 | 3 -> "small"                      // alternatives
    is string -> "text"                       // type test
    [first, ...rest] -> "list of {rest.len() + 1}"
    {kind: "point", x, y} -> "point {x},{y}"  // record shape, binding x and y
    n if n is int and n < 0 -> "negative"     // binding with a guard
    _ -> {                                    // a block body
      let s = str(v)
      "other: {s}"
    }
  }
}
```

Arms are tried in order. If none matches, `MatchError` is thrown. An arm body that starts with `{`
is a block unless it clearly looks like a record literal (`{}` or `{key: ...}`).

## Functions

```rust
/// Doc comments describe a function. For tools, the model reads them.
fn greet(name: string, greeting: string = "Hello") -> string {
  "{greeting}, {name}!"          // implicit return of the last expression
}

fn square(x: float) -> float => x * x     // expression body

let add = (a, b) => a + b                  // lambdas
let shout = s => s.upper() + "!"
let block = x => {                         // a lambda with a block body
  let y = x * 2
  y + 1
}
let also = fn(x: int) -> int { x + 1 }     // anonymous fn with annotations
```

Parameter and return annotations are optional. When present they are validated on every call;
a mismatch throws `TypeError`. `return` exits early.

## Types

```rust
type Id = int where it > 0                       // alias with a refinement
type Status = "open" | "closed"                  // union of literals
type Tags = [string]                             // list
type Scores = map<float>                         // record with arbitrary keys

/// A doc comment on a type becomes the schema description.
type Issue {
  id: Id
  /// A doc comment on a field becomes that property's description.
  title: string where it.len() > 0
  status: Status = "open"                        // default
  labels: Tags = []
  assignee: string?                              // may be null or absent
  estimate?: float                               // same thing, written on the name
  parent: Issue?                                 // types may refer to themselves
}
```

| Form | Meaning |
| --- | --- |
| `string int float bool any` | primitives (`number` is an alias of `float`); `float` accepts ints |
| `"x"`, `42`, `true`, `null` | literal types |
| `[T]` | list of `T` |
| `map<T>` | record whose values are all `T` |
| `{ a: T, b?: U, c: V = default }` | record with those fields |
| `A \| B` | union |
| `T?` | `T \| null` |
| `T where <expr>` | refinement; `it` is the value being checked |
| `Name` | a declared type |

### Using types

```rust
let ok = value is Issue                 // bool
let issue = Issue.validate(value)       // strict; fills defaults; throws ValidationError
let loose = value as Issue              // lenient coercion; drops unknown fields
let problem = Issue.errors(value)       // error message or null
let parsed = Issue.parse(model_text)    // find JSON in text, then coerce
let schema = Issue.schema()             // JSON Schema as a record
let fake = Issue.sample()               // a placeholder value that satisfies the type
let n = int("42")                       // calling a type converts one value
```

**Strict validation** (annotations, `is`, `.validate`) requires exact types, except that an int is a
valid float. Extra record fields are kept.

**Lenient validation** (`as`, `.parse`, model output, tool arguments) additionally accepts numeric
strings for numbers, `"true"`/`"false"` for bools, numbers for strings, and case-insensitive matches
for literal strings. Extra record fields are dropped.

Types are values, so inline types work wherever a type is expected: `claude("...", [string])`,
`schema({name: string, age: int})`.

## Models

```rust
model claude = "anthropic:claude-opus-5-5" { effort: "high" }
model small = "ollama:llama3.2"
let m = model("mock", cache: true)       // the same thing as an expression
```

A model spec is `provider:model-id`. Providers: `anthropic`, `openai`, `ollama`, `openrouter`,
`groq`, `gemini`, and `mock`.

| Option | Meaning |
| --- | --- |
| `effort` | reasoning effort (`"low"` … `"max"`) |
| `max_tokens` | output cap (Anthropic default: 16000) |
| `temperature`, `top_p`, `seed`, `stop` | sampling controls; not sent to Anthropic, which rejects them |
| `timeout` | seconds per request (default 600) |
| `base_url`, `api_key_env` | point a provider at another server or key variable |
| `fallbacks` | Anthropic server-side refusal fallbacks, e.g. `"default"` |
| `retries` | repair attempts for typed output (default 2) |
| `cache` | `true` to answer identical requests once per run |
| `max_steps` | default tool-step limit for agents (default 10) |

A model is callable: `claude("question")` returns text, `claude("question", Type)` returns a typed
value, and `system:`, `tools:` or any option may be passed by name. `m.name`, `m.spec`, `m.provider`,
`m.id` and `m.options` are readable.

Requests that fail with HTTP 408, 409, 429 or 5xx, or with a connection error, are retried twice
with backoff. Other failures throw `ModelError`.

## Prompts

```rust
prompt name(params) -> ReturnType using a_model with { retries: 3 } {
  system "…"
  "…"
}
```

`-> ReturnType`, `using` and `with` are each optional. Without a return type the prompt returns the
reply text. Without `using` it uses the first model declared in the program (or `MEDHA_MODEL`).

The body runs like a function body, with these additions:

| Statement | Effect |
| --- | --- |
| an expression statement that evaluates to a string | appended to the current user message (joined with newlines) |
| `system <expr>` | appended to the system prompt (paragraphs joined by blank lines) |
| `user <expr>` | same as a string statement, for non-string values |
| `assistant <expr>` | adds an assistant turn |
| `example <input> -> <output>` | adds a user turn and the matching assistant turn |

Non-string values in `system`, `user`, `assistant` and `example` are sent as JSON.

When the return type is not `string`, the system prompt gains an instruction to reply with JSON
conforming to the type's schema. The reply is searched for JSON (whole reply, fenced blocks, first
`{` or `[`), coerced leniently, and validated. On failure the error is sent back and the model is
asked again, up to `retries` times, after which `ModelOutputError` is thrown with the last reply in `raw`.

`p.render(args)` returns `{system, messages, tools, model}` without calling the model.
`p.schema()` returns the JSON Schema of the return type.

## Agents

An agent is a prompt that can call tools.

```rust
agent researcher(question: string) -> Report using claude {
  system "Use the tools. Cite what you find."
  tools [search, read_page]
  max_steps 8
  question
}
```

- `tools` takes a list of functions, prompts or agents. Each needs a name; `///` doc comments are
  the descriptions; typed parameters become the input schema; parameters with defaults are optional.
- Each round, the model may request several tool calls. Arguments are coerced to the parameter
  types, the function runs, and the result (text, or JSON for other values) is returned to the model.
- A tool that throws returns `Error: [Kind] message` to the model. `BudgetExceeded` is the exception:
  it always propagates.
- After `max_steps` rounds (default 10) without a final answer, `StepLimit` is thrown.
- The final answer is typed and repaired like a prompt's.

`tools` and `max_steps` are rejected inside a `prompt`.

## Tests and mocks

```rust
test "name" {
  mock claude -> "a scripted reply"
  assert summarize("text") == "a scripted reply"
}
```

`test` blocks are registered when a file is run by `medha test` and ignored by `medha run`. Each
test starts with no mocks, an empty cache and zeroed usage. In tests every model is answered by the
mock provider unless `--live` is passed.

`mock <target> -> <value>` where the target is:

- **a model** — the value scripts its next reply:
  - a string: the reply text
  - any other value: sent as JSON text
  - `{tool: "name", args: {…}}` or a list of those: tool calls
  - a function: called with `{model, system, messages, tools}`, its result interpreted as above
- **a prompt or agent** — the value is returned from the next call without contacting any model
  (a function is called with the prompt's arguments). It is still validated against the return type.

Several mocks for one target form a queue; the last one keeps answering.

Assertions: `assert cond`, `assert cond, "message"`, `assert_eq(actual, expected)`,
`assert_throws(fn, kind)` (returns the error record).

Outside tests, `--dry-run` forces the mock provider for a whole run, and `--record` / `--replay`
save and reuse real replies in order.

## Budgets

```rust
budget(tokens: 20000, calls: 12) {
  run_pipeline()
}
```

Either limit may be omitted. `calls` is checked before each model call, `tokens` (input + output)
after it. Exceeding a limit throws `BudgetExceeded` with `limit` set to `"calls"` or `"tokens"`.
Budgets nest, and each counts only what happens inside its block. `embed` counts as a call.

## Errors

```rust
try {
  risky()
} catch e {
  print(e.kind, e.message)
} finally {
  cleanup()
}

throw "something went wrong"                       // kind "Error"
throw error("NotFound", "no such user", id: 7)     // custom kind and extra fields
```

A caught error is a record with `kind`, `message`, usually `line`, and any extra fields.
`throw` accepts a string, an error record, or any value (available as `e.value`).

| Kind | Raised when |
| --- | --- |
| `SyntaxError`, `CheckError`, `NameError` | the program is malformed (reported before it runs) |
| `TypeError` | an operation or annotation got the wrong kind of value |
| `ValidationError` | `as`, `.validate`, `.parse` or a mock value did not match a type |
| `FieldError`, `IndexError`, `MatchError`, `MathError` | missing field, bad index, no match arm, division by zero |
| `ModelError` | a provider request failed or credentials are missing |
| `ModelRefusal` | the model declined the request |
| `ModelOutputError` | typed output still invalid after all retries (`raw` holds the reply) |
| `StepLimit` | an agent exceeded `max_steps` |
| `BudgetExceeded` | a `budget` block overspent |
| `PromptError` | a prompt body produced no message |
| `AssertionError` | `assert` failed |
| `IOError`, `ImportError`, `StackOverflow`, `ReplayError`, `MockError` | as named |

## Modules

```rust
import "lib/text.mdh"            // brings its top-level names into scope
import "lib/geometry" as geo     // or binds them under one name; ".mdh" is optional
```

Paths are relative to the importing file. A module runs once. If a file defines `fn main`, it is
called after the top-level code when the file is run directly; if `main` takes a parameter it
receives the command-line arguments as a list of strings.

## Standard library

### Global functions

| Function | Description |
| --- | --- |
| `print(...)`, `eprint(...)` | write values to stdout / stderr, space-separated |
| `str(v)` | text form of a value |
| `type_of(v)` | `"int"`, `"string"`, `"list"`, `"record"`, `"function"`, … |
| `range(n)`, `range(a, b, step)` | list of ints |
| `to_json(v, pretty)`, `from_json(text)` | JSON |
| `read_file(path)`, `write_file(path, text)`, `append_file(path, text)`, `file_exists(path)`, `list_dir(path)` | files |
| `fetch(url, method:, headers:, body:, timeout:)` | HTTP; returns `{status, body, headers}` |
| `env(name, default)`, `args()`, `input(prompt)`, `exit(code)` | process |
| `now()`, `timestamp()`, `sleep(seconds)` | time |
| `abs floor ceil round sqrt min max` | math; `min`/`max` take a list or several values, and `key:` |
| `random()`, `random_int(a, b)`, `random_choice(list)`, `seed(n)`, `uuid()`, `hash(text)` | randomness and hashing |
| `error(kind, message, ...fields)` | build an error record for `throw` |
| `assert_eq(a, b)`, `assert_throws(fn, kind)` | test helpers |

### AI functions

| Function | Description |
| --- | --- |
| `usage()` | `{calls, input_tokens, output_tokens, total_tokens}` so far |
| `pmap(list, f, workers: 8)` | map concurrently, preserving order |
| `chat(model, system:)` | a conversation; `.say(text, Type, tools)`, `.history`, `.reset()` |
| `embed(text_or_list, model)` | embedding vector(s) from an OpenAI-compatible model (deterministic offline vectors under the mock provider) |
| `cosine(a, b)` | cosine similarity |
| `tokens(text)` | rough token estimate (about four characters per token) |
| `model(spec, ...options)` | create a model value |
| `schema(type)` | JSON Schema of any type expression |

### String methods

`len upper lower trim split(sep) lines words chars contains starts_with ends_with replace(a, b)
find slice(a, b) repeat(n) is_empty truncate(n) matches(regex) find_all(regex) chunk(size, overlap)
tokens pad_left(n) pad_right(n)`

### List methods

`len map filter reduce(f, init) each join(sep) push pop contains first last slice take drop
sort(key:, desc:) reverse sum min max find any all flat flat_map zip enumerate unique chunk(n)
group_by count index_of concat is_empty`

`push` and `pop` change the list; every other method returns a new value.

### Record methods

`len keys values entries has(key) get(key, default) merge(other) remove(...keys) pick(...keys)
map_values(f) is_empty`

A record field with the same name as a method takes precedence over the method.
