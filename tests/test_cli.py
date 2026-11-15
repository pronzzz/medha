"""End-to-end tests of the ``medha`` command line."""
import glob
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), NO_COLOR="1")
for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MEDHA_MODEL"):
    ENV.pop(key, None)


def medha(*args, stdin=""):
    p = subprocess.run([sys.executable, "-m", "medha"] + list(args), cwd=ROOT, env=ENV, input=stdin,
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    return p.returncode, p.stdout, p.stderr


def program(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".mdh", delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    return f.name


class CLI(unittest.TestCase):
    def test_every_example_checks_and_runs_offline(self):
        examples = sorted(glob.glob(os.path.join(ROOT, "examples", "*.mdh")))
        self.assertGreaterEqual(len(examples), 6)
        for path in examples:
            code, out, err = medha("check", path)
            self.assertEqual(code, 0, "%s failed check:\n%s" % (path, err))
            code, out, err = medha("run", path, "--dry-run")
            self.assertEqual(code, 0, "%s failed to run:\n%s" % (path, err))
            self.assertTrue(out.strip(), "%s printed nothing" % path)

    def test_language_test_suite_passes(self):
        code, out, err = medha("test", "tests")
        self.assertEqual(code, 0, out + err)
        self.assertIn("0 failed", out)

    def test_runtime_error_shows_location_and_exit_code(self):
        path = program('fn f(x) {\n  x.nope\n}\nf({name: 1})\n')
        code, out, err = medha(path)
        self.assertEqual(code, 1)
        self.assertIn("error[FieldError]: record has no field 'nope'", err)
        self.assertIn("%s:2:4" % path, err)
        self.assertIn("in f", err)

    def test_checker_catches_mistakes_before_anything_runs(self):
        path = program('print("side effect")\nlet total = 1\ntotal = 2\nprint(totl)\n'
                       'fn f(a, b) { a }\nf(1)\nlet x: Missing = 1\nbreak\n')
        code, out, err = medha("run", path)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        for expected in ("cannot assign to 'total'", "'totl' is not defined — did you mean 'total'?",
                         "f() is missing argument 'b'", "unknown type 'Missing'", "'break' outside of a loop"):
            self.assertIn(expected, err)

    def test_syntax_errors_point_at_the_problem(self):
        path = program('let x = [1, 2\nprint(x)\n')
        code, out, err = medha("check", path)
        self.assertEqual(code, 1)
        self.assertIn("error[SyntaxError]", err)
        self.assertIn(":2:", err)

    def test_schema_command(self):
        code, out, err = medha("schema", os.path.join("examples", "sentiment.mdh"), "Review")
        self.assertEqual(code, 0, err)
        schema = json.loads(out)
        self.assertEqual(schema["properties"]["sentiment"]["enum"], ["positive", "negative", "neutral"])
        code, out, err = medha("schema", os.path.join("examples", "sentiment.mdh"), "Nope")
        self.assertEqual(code, 1)
        self.assertIn("Sentiment, Review", err)

    def test_record_then_replay_reproduces_a_run(self):
        path = program('model m = "mock"\ntype T { n: int, word: string }\n'
                       'prompt p(x: string) -> T using m { x }\nprint(p("a"), m("free text"))\n')
        tape = path + ".tape.json"
        code, first, err = medha("run", path, "--record", tape)
        self.assertEqual(code, 0, err)
        with open(tape, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual([c["label"] for c in data["calls"]], ["p", "m"])
        data["calls"][0]["reply"]["text"] = '{"n": 41, "word": "taped"}'
        with open(tape, "w", encoding="utf-8") as f:
            json.dump(data, f)
        code, second, err = medha("run", path, "--replay", tape, "--model", "anthropic:claude-opus-5-5")
        self.assertEqual(code, 0, err)
        self.assertIn('"n": 41, "word": "taped"', second)
        code, _, err = medha("run", program('model m = "mock"\nm("1")\nm("2")\nm("3")\n'), "--replay", tape)
        self.assertEqual(code, 1)
        self.assertIn("ReplayError", err)

    def test_program_arguments_and_exit(self):
        path = program('fn main(args) {\n  print(args)\n  exit(3)\n}\n')
        code, out, err = medha("run", path, "--dry-run", "one", "--", "--two")
        self.assertEqual(code, 3)
        self.assertEqual(out.strip(), '["one", "--two"]')

    def test_real_model_without_credentials_fails_clearly(self):
        path = program('model m = "anthropic:claude-opus-5-5"\nprint(m("hi"))\n')
        code, out, err = medha("run", path)
        self.assertEqual(code, 1)
        self.assertIn("ANTHROPIC_API_KEY", err)
        self.assertIn("--dry-run", err)

    def test_deep_recursion_is_an_error_not_a_crash(self):
        path = program('fn down(n) { down(n + 1) }\ntry { down(0) } catch e { print(e.kind) }\n'
                       'fn sum_to(n) { if n == 0 { 0 } else { n + sum_to(n - 1) } }\nprint(sum_to(800))\n')
        code, out, err = medha("run", path)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.split(), ["StackOverflow", "320400"])

    def test_imports(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "lib.mdh"), "w", encoding="utf-8") as f:
            f.write('type Point { x: int, y: int }\nfn norm1(p: Point) -> int => abs(p.x) + abs(p.y)\nlet origin = {x: 0, y: 0}\n')
        with open(os.path.join(d, "main.mdh"), "w", encoding="utf-8") as f:
            f.write('import "lib.mdh"\nimport "lib" as geo\nlet p: Point = {x: 3, y: -4}\n'
                    'print(norm1(p), geo.norm1(geo.origin))\n')
        code, out, err = medha("run", os.path.join(d, "main.mdh"))
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "7 0")

    def test_repl(self):
        code, out, err = medha("repl", "--dry-run", stdin='let x = 20\nfn twice(n) {\n  n * 2\n}\ntwice(x) + 2\n"s"\nnope\n:quit\n')
        self.assertEqual(code, 0, err)
        self.assertIn("42", out)
        self.assertIn('"s"', out)
        self.assertIn("'nope' is not defined", err)


if __name__ == "__main__":
    unittest.main()
