# Changelog

## 0.1.0 — 2026-11-15

First public release.

### Language
- Core language: `let`/`var`, functions, lambdas, closures, records, lists, `if` and `match`
  expressions, pattern matching, destructuring, pipelines (`|>`), uniform call syntax,
  string interpolation, `try`/`catch`/`finally`, modules.
- Types as schemas: literals, unions, optionals, lists, maps, records with defaults, recursive
  types and `where` refinements. Every type validates, coerces, emits JSON Schema and generates samples.
- `model` declarations for Anthropic, OpenAI-compatible servers (OpenAI, Ollama, OpenRouter, Groq,
  Gemini, custom `base_url`) and a built-in mock provider.
- `prompt` declarations with typed return values, `system`/`example` directives and automatic
  repair of invalid output.
- `agent` declarations with a tool loop; any function, prompt or agent can be a tool.
- `test` blocks, `mock` for models and prompts, `budget` blocks, `pmap`, `chat`, `embed`, caching.

### Tooling
- `medha run`, `test`, `check`, `schema`, `repl`.
- `--dry-run`, `--trace`, `--model`, `--record` / `--replay` tapes.
- Static checker for undefined names, unknown types, arity and control-flow mistakes.
- TextMate grammar for VS Code.
