# Contributing to Medha

Thanks for your interest. Medha is small on purpose, and contributions that keep it small,
predictable and well tested are very welcome.

## Getting set up

```bash
git clone https://github.com/pronzzz/medha
cd medha
pip install -e .
```

There are no dependencies and no build step. Python 3.9 is the oldest supported version, so avoid
syntax newer than that (`match` statements, `X | Y` annotations evaluated at run time).

## Running the tests

```bash
python -m unittest discover -s tests -p "test_*.py"
medha test tests
```

- `tests/*_test.mdh` — the language tested in itself. Add a `test` block for any language change.
- `tests/test_providers.py` — the HTTP wire formats, against local fake servers.
- `tests/test_cli.py` — the command line end to end, including every example in `examples/`.
- `tests/test_docs.py` — every code block in the README and the reference must parse.

No test may need a network connection or an API key.

## Where things live

| Path | What |
| --- | --- |
| `src/medha/lexer.py`, `parser.py` | syntax |
| `src/medha/checker.py` | static checks |
| `src/medha/interp.py` | evaluator |
| `src/medha/builtins.py` | standard library |
| `src/medha/types.py` | types: validation, JSON Schema, samples |
| `src/medha/ai.py` | prompts, agents, repair loop, budgets, mocks, tapes |
| `src/medha/providers.py` | HTTP clients |

## Guidelines

- A new language feature needs a test, an entry in `docs/LANGUAGE.md`, and a line in `CHANGELOG.md`.
- Error messages are part of the product: say what was expected, what was found, and where.
- The checker must never reject a valid program. When unsure, do not report.
- Keep the interpreter dependency-free.

## Reporting bugs

Please include the smallest `.mdh` program that shows the problem, the output of `medha version`,
and — for provider issues — the `--trace` output with any secrets removed.
