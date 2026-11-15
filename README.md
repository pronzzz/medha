<p align="center">
  <img src="assets/logo.svg" alt="Medha" width="420">
</p>

<p align="center">
  <a href="https://github.com/pronzzz/medha/actions/workflows/ci.yml"><img src="https://github.com/pronzzz/medha/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/pronzzz/medha/releases/latest"><img src="https://img.shields.io/github/v/release/pronzzz/medha" alt="Latest release"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/dependencies-none-brightgreen" alt="No dependencies">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="MIT license"></a>
</p>

**Medha** (मेधा, Sanskrit for *intelligence*) is a small programming language for writing programs that
call language models. Model calls are part of the language instead of a library bolted onto one:

- **Types are schemas.** A `type` validates values, becomes the JSON Schema the model is shown, and
  generates sample data for offline runs.
- **Prompts are functions.** A `prompt` has parameters and a return type. Calling it returns a
  validated value, never a string you have to parse.
- **Wrong output repairs itself.** When a reply does not match the type, Medha tells the model
  exactly which field was wrong and asks again.
- **Agents are loops you don't write.** An `agent` is a prompt with `tools`. Every function is a
  tool; its doc comment and parameter types are the tool definition.
- **Everything runs offline.** `--dry-run`, `mock`, and record/replay tapes make model code
  testable in CI with no API key and no network.

```rust
model claude = "anthropic:claude-opus-5-5"

type Review {
  sentiment: "positive" | "negative" | "neutral"
  /// 1 is furious, 5 is delighted.
  score: int where it >= 1 and it <= 5
  topics: [string]
  follow_up: string?
}

prompt analyze(text: string) -> Review using claude {
  system "You are a careful analyst of customer reviews."
  "Review: {text}"
}

let r = analyze("The strap snapped within a week. I want a refund.")
print(r.sentiment, r.score, r.topics)
```

`analyze` returns a record that is guaranteed to have a `score` between 1 and 5 — or it throws.
There is no JSON parsing, no schema file, and no retry loop in your code.

---

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [A tour of the language](#a-tour-of-the-language)
  - [Types are schemas](#types-are-schemas)
  - [Prompts are functions](#prompts-are-functions)
  - [Self-repairing output](#self-repairing-output)
  - [Agents and tools](#agents-and-tools)
  - [Models and providers](#models-and-providers)
  - [Testing without a model](#testing-without-a-model)
  - [Budgets, caching and concurrency](#budgets-caching-and-concurrency)
  - [Chat, embeddings and direct calls](#chat-embeddings-and-direct-calls)
  - [The rest of the language](#the-rest-of-the-language)
- [Command line](#command-line)
- [Examples](#examples)
- [Editor support](#editor-support)
- [How it works](#how-it-works)
- [Status and limitations](#status-and-limitations)
- [Contributing](#contributing)

## Install

Medha needs Python 3.9 or newer and has **no dependencies**.

```bash
pip install git+https://github.com/pronzzz/medha
```

Or install a pinned release wheel:

```bash
pip install https://github.com/pronzzz/medha/releases/download/v0.1.0/medha-0.1.0-py3-none-any.whl
```

With [uv](https://docs.astral.sh/uv/) or pipx, as an isolated command-line tool:

```bash
uv tool install git+https://github.com/pronzzz/medha
```

```bash
pipx install git+https://github.com/pronzzz/medha
```

Check that it works:

```bash
medha version
```

## Quick start

Save this as `hello.mdh`:

```rust
model claude = "anthropic:claude-opus-5-5"

type Greeting {
  /// One friendly sentence addressed to the person.
  text: string
  language: "english" | "hindi" | "spanish"
}

prompt greet(name: string) -> Greeting using claude {
  "Greet {name} warmly, in a language of your choice."
}

fn main(args) {
  let g = greet(args.first() ?? "world")
  print("[{g.language}] {g.text}")
}
```

Run it without an API key. The mock provider answers every call with a value that satisfies the
return type, so you can exercise the whole program first:

```bash
medha run hello.mdh --dry-run
```

```
[english] <text>
```

Then run it for real:

```bash
export ANTHROPIC_API_KEY=...
medha run hello.mdh Ada
```

Add `--trace` to see every model call, tool call and token count on stderr.

## A tour of the language

### Types are schemas

```rust
type Priority = "low" | "medium" | "high"

/// A support ticket.
type Ticket {
  /// Short headline.
  title: string where it.len() > 0
  priority: Priority
  tags: [string] = []
  assignee: string?
}
```

One declaration does four jobs:

| Use | Code | Result |
| --- | --- | --- |
| Check a value | `v is Ticket` | `true` / `false` |
| Validate strictly | `Ticket.validate(v)` | the value with defaults filled in, or a `ValidationError` naming the exact path (`$.tags[2]: expected string, got int 7`) |
| Coerce loose data | `v as Ticket` | accepts `"HIGH"` for `"high"`, `"3"` for `3`, drops unknown fields |
| Describe to a model | `Ticket.schema()` | JSON Schema, with `///` comments as descriptions |

Building blocks: `string` `int` `float` `bool` `any`, literal types (`"low"`, `42`), lists `[T]`,
maps `map<T>`, records `{ a: T, b?: U }`, unions `A | B`, optionals `T?`, defaults `= value`, and
refinements `T where <condition on it>`. Types may be recursive.

`where` conditions are ordinary Medha expressions. They run during validation, and their source text
is included in the schema description so the model sees the rule too.

Print any schema from the command line:

```bash
medha schema examples/sentiment.mdh Review
```

### Prompts are functions

```rust
prompt summarize(text: string, words: int = 50) using claude {
  system "You write plain, precise summaries."
  "Summarize in at most {words} words:"
  text
}

prompt classify(text: string) -> Priority using claude {
  example "The site is down for everyone" -> "high"
  example "Typo on the pricing page" -> "low"
  text
}
```

Inside a prompt body:

- any **string statement** becomes part of the user message (consecutive ones are joined by newlines),
- `system <expr>` adds to the system prompt,
- `example <input> -> <output>` adds a few-shot turn,
- `user` and `assistant` add explicit turns,
- and everything else is normal code — `let`, `if`, `for`, function calls.

```rust
prompt answer(question: string, docs: [string]) -> string using claude {
  system "Answer only from the numbered documents."
  for i, doc in docs { "[{i}] {doc}" }
  if docs.len() == 0 { "(no documents were found)" }
  "Question: {question}"
}
```

With no return type a prompt returns the reply text. With one, Medha appends the type's JSON Schema
to the system prompt, parses the reply (tolerating code fences and surrounding chatter), validates
it, and returns the value.

`summarize.render("some text")` shows exactly what would be sent — system prompt, messages, model —
without calling anything.

### Self-repairing output

If the reply fails validation, the conversation continues with the precise error, up to `retries`
times (default 2). Here is the `--trace` log of a run where the (scripted) model first replies with
prose, then with an out-of-range score, then correctly:

```
[medha] analyze -> anthropic:claude-opus-5-5 (mock)  in=146 out=8  0.00s
[medha] analyze -> anthropic:claude-opus-5-5 (mock)  in=202 out=13  0.00s
[medha] analyze -> anthropic:claude-opus-5-5 (mock)  in=247 out=13  0.00s
{"sentiment": "negative", "score": 2, "topics": []}
```

The third request ended with the message
``That reply is not valid: $.score: int 9 does not satisfy `it >= 1 and it <= 5` ``.
When retries run out you get a `ModelOutputError` whose `raw` field holds the last reply:

```rust
try {
  let r = analyze(text)
  print(r.score)
} catch e {
  if e.kind == "ModelOutputError" { eprint("model would not comply: {e.raw}") }
}
```

### Agents and tools

```rust
/// Look up one order by its numeric id. Returns null when there is no such order.
fn find_order(id: int) -> Order? {
  orders.find(o => o.id == id)
}

/// Start a refund for a delivered order.
fn refund(id: int, reason: string) -> string {
  let order = find_order(id)
  if order == null or order.status != "delivered" {
    throw error("NotRefundable", "order {id} cannot be refunded")
  }
  "refund of {order.total} started for order {id}"
}

type Resolution {
  reply: string
  action: "answered" | "refunded" | "escalated"
}

agent support(message: string) -> Resolution using claude {
  system "You are a support agent. Look orders up before answering."
  tools [find_order, refund]
  max_steps 6
  message
}
```

- The `///` comment is the tool description; parameter types become its input schema.
- Arguments from the model are validated and coerced before your function runs.
- A tool that throws does not crash the agent: the error text goes back to the model, which can
  recover or explain. (Here, a refund on an undelivered order returns `Error: [NotRefundable] ...`.)
- `max_steps` bounds the loop; exceeding it throws `StepLimit`.
- The final answer is typed and repaired exactly like a prompt's.
- Prompts and agents can be tools of other agents, which is all multi-agent delegation needs.

### Models and providers

```rust
model claude = "anthropic:claude-opus-5-5" { effort: "high", max_tokens: 8000 }
model local = "ollama:llama3.2" { temperature: 0 }
model fast = "groq:llama-3.3-70b-versatile"
model custom = "openai:my-model" { base_url: "http://localhost:8000/v1" }
```

| Prefix | Wire format | Credentials |
| --- | --- | --- |
| `anthropic:` | Anthropic Messages API | `ANTHROPIC_API_KEY` |
| `openai:` | OpenAI-compatible chat completions | `OPENAI_API_KEY` |
| `ollama:` | OpenAI-compatible, `http://localhost:11434/v1` | none |
| `openrouter:` | OpenAI-compatible | `OPENROUTER_API_KEY` |
| `groq:` | OpenAI-compatible | `GROQ_API_KEY` |
| `gemini:` | OpenAI-compatible endpoint | `GEMINI_API_KEY` |
| `mock` | no network; scripted or schema-shaped replies | none |

Options: `effort`, `max_tokens`, `temperature`, `top_p`, `stop`, `seed`, `timeout`, `base_url`,
`api_key_env`, `fallbacks`, plus Medha's own `retries`, `cache` and `max_steps`. Set them on the
model or per prompt with `with { ... }`. A prompt without `using` uses the first model declared.

Notes on Anthropic: the current API rejects `temperature`, `top_p` and `top_k`, so Medha drops them
with a warning — control depth with `effort` (`"low"` to `"max"`). A refusal surfaces as a
`ModelRefusal` error; `fallbacks: "default"` opts in to server-side fallback models.

`--model <spec>` on the command line overrides every model in the program, which is the easy way to
try the same program against another provider.

### Testing without a model

`test` blocks run with `medha test`. Inside them the network is never touched: models are mocked.

```rust
test "refunds are escalated to a human when the order is missing" {
  mock claude -> {tool: "find_order", args: {id: 9}}
  mock claude -> {reply: "I could not find order 9.", action: "escalated"}

  let r = support("Refund order 9 please")
  assert r.action == "escalated"
  assert usage().calls == 2
}

test "analyze rejects scores outside 1..5" {
  mock analyze -> {sentiment: "positive", score: 9, topics: []}
  assert_throws(() => analyze("x"), "ValidationError")
}
```

- `mock <model> -> <reply>` queues a reply: a string, a value (sent as JSON), a tool call
  `{tool: "name", args: {...}}`, or a function of the request for assertions on what was sent.
- `mock <prompt> -> <value>` replaces a whole prompt or agent with a value (or a function of its
  arguments), still validated against the return type.
- With nothing scripted, the mock provider invents a value from the return type.
- `assert` prints both sides of a failed comparison.

```
tests/ai_test.mdh
  ✓ a typed prompt returns a validated value (0ms)
  ✓ invalid output is repaired by telling the model what was wrong (0ms)
  ✓ an agent calls tools until it has an answer (0ms)
```

For whole-program reproducibility, record a real run once and replay it forever:

```bash
medha run pipeline.mdh --record run.tape.json
```

```bash
medha run pipeline.mdh --replay run.tape.json
```

### Budgets, caching and concurrency

```rust
fn write_article(topic: string) -> string {
  let plan = outline(topic)
  var body = plan.sections
    |> pmap(s => draft(plan.title, s))
    |> join("\n\n")

  try {
    budget(calls: 2) {
      let verdict = review(body)
      if verdict.score < 8 { body = revise(body, verdict.problems) }
    }
  } catch e {
    eprint("skipping revision: {e.message}")
  }
  body
}

budget(tokens: 60000) {
  print(write_article("how vaccines train the immune system"))
}
```

- `budget(tokens: N, calls: M) { ... }` throws a catchable `BudgetExceeded` the moment a block
  overspends. Budgets nest.
- `pmap(list, f)` runs model calls concurrently and keeps the order.
- `with { cache: true }` on a prompt (or `cache: true` on a model) answers identical requests once
  per run.
- `usage()` returns `{calls, input_tokens, output_tokens, total_tokens}` at any point.

### Chat, embeddings and direct calls

```rust
// A model is callable: text in, text (or a typed value) out.
let haiku = claude("Write a haiku about compilers.")
let cities = claude("Three large cities in India.", [string])

// A chat keeps its history.
let bot = chat(claude, system: "You are concise.")
print(bot.say("What is a monad?"))
print(bot.say("Now explain it to a five-year-old."))

// Embeddings (OpenAI-compatible models) and similarity.
model embedder = "openai:text-embedding-3-small"
let vectors = embed(["refund policy", "shipping times"], embedder)
print(cosine(vectors[0], embed("how do I return an item", embedder)))
```

### The rest of the language

Medha is a complete small language, so the code around your prompts needs no second language.

```rust
// Bindings: let is immutable, var is mutable. Annotations are optional and checked at run time.
let limit: int = 10
var total = 0

// Functions, defaults, named arguments, lambdas, closures.
fn area(w: float, h: float = 1.0) -> float => w * h
let double = x => x * 2
area(h: 2.0, w: 3.0)

// if and match are expressions.
let size = if total > limit { "big" } else { "small" }

fn describe(v) {
  match v {
    0 -> "zero"
    1 | 2 | 3 -> "small"
    is string -> "text"
    [first, ...rest] -> "a list starting with {first}"
    {kind: "point", x, y} -> "point at {x},{y}"
    n if n is int and n < 0 -> "negative"
    _ -> "something else"
  }
}

// Pipelines, and methods that are also plain functions.
let top = [5, 3, 8, 1] |> sort(desc: true) |> take(2) |> map(double)
let same = [5, 3, 8, 1].sort(desc: true).take(2).map(double)

// Records, spread, destructuring, null-safety.
let user = {name: "Asha", langs: ["hi", "en"]}
let {name, ...other} = user
let city = user?.address?.city ?? "unknown"

// Errors are records.
try {
  throw error("NotFound", "no such user", id: 7)
} catch e {
  print(e.kind, e.message, e.id)
}

// Loops and ranges.
for i in 0..3 { total += i }
for key, value in user { print("{key} = {value}") }
```

Also built in: string interpolation with any expression, triple-quoted strings that strip their
indentation, raw strings (`r"\d+"`), `import "file.mdh"`, JSON, files, `fetch`, `env`, text
chunking for RAG (`text.chunk(800, 100)`), and around 65 list, string and record methods.

The full reference is in **[docs/LANGUAGE.md](docs/LANGUAGE.md)**.

## Command line

```
medha run <file.mdh> [args...]     run a program (also: medha <file.mdh>)
medha test [paths...]              run test blocks (offline, models are mocked)
medha check <file.mdh>...          parse and statically check without running
medha schema <file.mdh> <Type>     print the JSON Schema of a declared type
medha repl                         interactive session
medha version
```

| Option | Applies to | Meaning |
| --- | --- | --- |
| `--dry-run` | run, repl | answer every model call with the mock provider |
| `--trace` | run, repl, test | log each model call, tool call and token count |
| `--model <spec>` | run, repl | use this model for every call |
| `--record <file>` | run | save every model reply to a tape |
| `--replay <file>` | run | answer model calls from a tape |
| `--no-check` | run | skip the static check |
| `--filter <text>` | test | only run tests whose name contains the text |
| `--live` | test | let tests reach real models |

Before running, `medha` checks the program and refuses to start if it finds undefined names,
unknown types, assignment to a `let`, wrong argument counts, or a stray `return`/`break`:

```
error[NameError]: 'totl' is not defined — did you mean 'total'?
  --> report.mdh:4:7
    |
  4 | print(totl)
    |       ^
```

`medha test` with no path looks for `*_test.mdh` and `test_*.mdh` under the current directory.

## Examples

Every file in [`examples/`](examples) runs with `--dry-run`.

| File | Shows |
| --- | --- |
| [`hello.mdh`](examples/hello.mdh) | one model, one typed prompt |
| [`sentiment.mdh`](examples/sentiment.mdh) | batch classification with `pmap`, few-shot examples, caching |
| [`support_agent.mdh`](examples/support_agent.mdh) | an agent with tools and a typed resolution |
| [`extract.mdh`](examples/extract.mdh) | nested extraction with refinements, then arithmetic checks in code |
| [`rag.mdh`](examples/rag.mdh) | embeddings, similarity search, grounded answers |
| [`pipeline.mdh`](examples/pipeline.mdh) | outline → draft → critique → revise, under a budget |
| [`chat.mdh`](examples/chat.mdh) | a terminal chatbot with memory |

## Editor support

[`editors/vscode`](editors/vscode) contains a TextMate grammar for `.mdh` files. To use it in
VS Code, copy the folder into your extensions directory and reload:

```bash
cp -r editors/vscode ~/.vscode/extensions/medha-language
```

## How it works

```
source ─▶ lexer ─▶ parser ─▶ checker ─▶ tree-walking evaluator
                                              │
                         types (validate · schema · sample)
                                              │
              AI runtime: generate ─▶ tool loop ─▶ parse ─▶ validate ─▶ repair
                                              │
                 providers: Anthropic · OpenAI-compatible · mock · tape replay
```

| Module | Role |
| --- | --- |
| `lexer.py`, `parser.py` | hand-written lexer and recursive-descent parser |
| `checker.py` | conservative static checks before anything runs |
| `interp.py`, `builtins.py` | evaluator and standard library |
| `types.py` | the type system: validation, JSON Schema, sample generation |
| `ai.py` | prompt execution, repair loop, tool loop, budgets, mocks, tapes, cache |
| `providers.py` | HTTP clients for the two wire formats (standard library only) |

Structured output is provider-independent by design: the schema travels in the system prompt and
the reply is validated locally. That is why the same program runs unchanged on any provider, and
why validation can enforce rules (`where` clauses) that no provider's constrained decoding can.

The interpreter can also be embedded:

```python
import medha

interp = medha.run('model m = "mock"\nlet reply = m("hi")', dry_run=True)
print(interp.last_env.vars["reply"])
```

## Status and limitations

Medha is **v0.1.0** — usable, tested, and young. Know these before you rely on it:

- **Speed.** It is a tree-walking interpreter written in Python. That is irrelevant next to model
  latency, but it is not the language for CPU-heavy loops.
- **Provider testing.** The Anthropic and OpenAI-compatible clients are tested against local servers
  that implement those wire formats (see `tests/test_providers.py`), not against the live services.
  Please open an issue if a real provider disagrees.
- **No streaming yet.** Replies arrive whole. Requests default to `max_tokens: 16000` on Anthropic.
- **Gradual typing.** Type annotations are checked at run time, not at compile time. The static
  checker catches names, arity and control-flow mistakes only.
- **Token counts** come from the provider; the mock provider estimates (about four characters per token).
- **Mock values are placeholders** (`"<summary>"`, `0`), shaped by the type but not meaningful.
- **String braces.** `{` starts interpolation in every string, so literal braces are written `\{`
  (or use a raw string, `r"..."`).

Planned next: streaming, provider-native structured output where available, a formatter, a language
server, and image inputs.

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). To work on Medha:

```bash
git clone https://github.com/pronzzz/medha && cd medha && pip install -e .
```

```bash
python -m unittest discover -s tests -p "test_*.py" && medha test tests
```

## License

[MIT](LICENSE) © 2026 Pranav Dwivedi
