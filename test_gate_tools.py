"""gate's read-only check against the formatters themselves.

For each formatter gate knows, a tree with a formatted file, an unformatted one
and one in a subdirectory: `gate check --format` must name exactly the files
`gate fmt` then changes, and pass afterwards. A file the tool cannot parse is a
formatter error, never "needs formatting".

Needs a built gate's tools: run with GATE_BUILD_INFO naming its gate-build.json
(see `gate which --all`), e.g.

    GATE_BUILD_INFO=$(grep -o '/nix/store/[^"]*gate-build.json' "$(readlink -f "$(which gate)")") \
      python3 -m unittest test_gate_tools.py

Skipped without it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import gate

BUILD = os.environ.get("GATE_BUILD_INFO", "")

# formatter -> (formatted, unformatted, unparsable) sample, by file name.
SAMPLES = {
    "rustfmt": ("ok.rs", "fn main() {}\n", "fn main(){let x=1;}\n", "fn main( {\n"),
    "dprint": ("ok.json", '{ "a": 1 }\n', '{"a":1,\n"b":2}\n', '{"a":\n'),
    "dprint-md": ("ok.md", "# T\n\n*x*\n", "#  T\n", None),
    "nixfmt": ("ok.nix", "{ a = 1; }\n", "{a=1;}\n", "{a=\n"),
    "ruff": ("ok.py", "x = 1\n", "x=1;y=2\n", "def (\n"),
    "biome": ("ok.css", "a {\n\tcolor: red;\n}\n", "a  {color:red}\n", "a {{{\n"),
    "kdlfmt": (
        "ok.kdl",
        "node 1 {\n    child a\n}\n",
        'node   1 {\nchild "a"\n}\n',
        "node {{\n",
    ),
    "just": ("ok.just", "a:\n    echo hi\n", "a:\n  echo hi\nb :\n  echo b\n", None),
}


@unittest.skipUnless(BUILD, "set GATE_BUILD_INFO to a built gate's gate-build.json")
class Tools(unittest.TestCase):
    def setUp(self) -> None:
        with open(BUILD, encoding="utf-8") as f:
            self.build = json.load(f)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {**os.environ, "XDG_RUNTIME_DIR": str(self.tmp / "run"), "NO_COLOR": "1"}

    def gate(self, cwd: Path, *argv: str) -> tuple[int, str]:
        g = gate.Gate(self.build, env=self.env, cwd=str(cwd))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                rc = g.main(list(argv))
            except gate.Exit as e:
                rc = e.code
            finally:
                g.cleanup()
        return rc, err.getvalue()

    def listed(self, text: str) -> list[str]:
        out, on = [], False
        for line in text.splitlines():
            if "needs formatting" in line:
                on = True
            elif on and line.startswith("  "):
                out.append(line.strip())
            else:
                on = False
        return sorted(out)

    def test_check_names_exactly_what_fmt_changes(self) -> None:
        for tool, (name, good, bad, _broken) in SAMPLES.items():
            with self.subTest(tool=tool):
                if tool == "rustfmt" and not shutil.which("rustfmt"):
                    self.skipTest("rustfmt not on PATH")
                repo = self.tmp / tool
                (repo / "sub").mkdir(parents=True)
                ext = name.rsplit(".", 1)[1]
                files = {name: good, f"bad.{ext}": bad, f"sub/x.{ext}": bad}
                for rel, text in files.items():
                    (repo / rel).write_text(text, encoding="utf-8")
                subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
                before = {rel: (repo / rel).read_bytes() for rel in files}
                rc, err = self.gate(repo, "check", "--format")
                self.assertEqual(rc, 1, err)
                self.assertNotIn("formatter error", err)
                self.assertEqual(
                    before, {rel: (repo / rel).read_bytes() for rel in files}, "check wrote"
                )
                listed = self.listed(err)
                rc, err = self.gate(repo, "fmt")
                self.assertEqual(rc, 0, err)
                changed = sorted(
                    rel for rel in files if (repo / rel).read_bytes() != before[rel]
                )
                self.assertEqual(listed, changed)
                self.assertEqual(len(changed), len(files) - 1)
                rc, err = self.gate(repo, "check", "--format")
                self.assertEqual(rc, 0, err)

    def test_an_unparsable_file_is_a_formatter_error(self) -> None:
        for tool, (name, _good, _bad, broken) in SAMPLES.items():
            if broken is None:
                continue
            with self.subTest(tool=tool):
                if tool == "rustfmt" and not shutil.which("rustfmt"):
                    self.skipTest("rustfmt not on PATH")
                repo = self.tmp / f"broken-{tool}"
                repo.mkdir()
                (repo / name).write_text(broken, encoding="utf-8")
                rc, err = self.gate(repo, "check", "--format")
                self.assertEqual(rc, 1, err)
                self.assertIn("formatter error", err.splitlines()[-1])
                self.assertEqual(self.listed(err), [])

    def test_pipe_formats_kdl_and_justfiles(self) -> None:
        for name, raw, want in (
            ("x.kdl", 'node   1 {\nchild "a"\n}\n', "node 1 {\n    child a\n}\n"),
            ("justfile", "a:\n  echo hi\n", "a:\n    echo hi\n"),
            ("dir/b.just", "b :\n  echo b\n", "b:\n    echo b\n"),
        ):
            with self.subTest(name=name):
                out = subprocess.run(
                    ["python3", "-I", gate.__file__, "pipe", name],
                    input=raw.encode(),
                    capture_output=True,
                    env={**self.env, "GATE_BUILD_INFO": BUILD},
                    cwd=self.tmp,
                    check=False,
                )
                self.assertEqual((out.returncode, out.stdout.decode()), (0, want), out.stderr)


if __name__ == "__main__":
    unittest.main()
