"""Every Medha code block in the documentation must at least parse."""
import json
import os
import re
import unittest

from medha.errors import MedhaError
from medha.parser import parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOCK = re.compile(r"```rust\n(.*?)```", re.S)


class Docs(unittest.TestCase):
    def test_code_blocks_parse(self):
        total = 0
        for name in ("README.md", os.path.join("docs", "LANGUAGE.md")):
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                text = f.read()
            for i, m in enumerate(BLOCK.finditer(text)):
                total += 1
                line = text.count("\n", 0, m.start()) + 2
                try:
                    parse(m.group(1), "%s (block %d, line %d)" % (name, i + 1, line))
                except MedhaError as e:
                    self.fail("%s block starting at line %d does not parse: %s (block line %s)"
                              % (name, line, e.message, e.pos[1] if e.pos else "?"))
        self.assertGreater(total, 20)

    def test_editor_grammar_is_valid_json(self):
        for name in ("package.json", "language-configuration.json", os.path.join("syntaxes", "medha.tmLanguage.json")):
            with open(os.path.join(ROOT, "editors", "vscode", name), encoding="utf-8") as f:
                json.load(f)

    def test_versions_agree(self):
        import medha
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as f:
            self.assertIn('version = "%s"' % medha.__version__, f.read())
        with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
            self.assertIn("## %s" % medha.__version__, f.read())


if __name__ == "__main__":
    unittest.main()
