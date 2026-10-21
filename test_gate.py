"""Tests for gate.py: run with `python3 -m unittest test_gate.py`.

The tools gate runs are replaced by small scripts in a temporary bin dir (the
build's PATH), and `Gate.run` is recorded where only the argv matters.
"""

from __future__ import annotations

import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path

import gate


def write(path: Path, text: str, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    return path


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.bin = self.tmp / "bin"
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        builtin = self.tmp / "builtin"
        self.builtin = {
            "dprint": str(
                write(builtin / "dprint.json", '{"plugins": ["@GATE_PLUGIN:md@"]}\n')
            ),
            "treefmt": str(
                write(
                    builtin / "treefmt.toml",
                    'options = ["@GATE_DPRINT_CONFIG@", "@GATE_BIOME_CONFIG@", "@GATE_RUFF_CONFIG@"]\n',
                )
            ),
            "biome": str(write(builtin / "biome.json", "{}\n")),
            "ruff": str(write(builtin / "ruff.toml", "\n")),
            "clippy": str(
                write(builtin / "clippy.toml", '[[run]]\nargs = ["--all-targets"]\n')
            ),
        }
        self.build = {
            "package": "gate-check",
            "version": "9.9.9",
            "cfg_version": "v1",
            "path": str(self.bin),
            "builtin": self.builtin,
            "plugins": {"md": "/store/md/plugin.wasm"},
            "treefmt": "treefmt",
            "shellcheck": "shellcheck",
            "typos": "typos",
            "ruff": "ruff",
            "xmllint": "xmllint",
            "python3": "python3",
            "portability": "portability.py",
        }
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.tmp / "home"),
            "XDG_RUNTIME_DIR": str(self.tmp / "run"),
        }

    def tool(self, name: str, body: str = "exit 0") -> None:
        write(self.bin / name, f"#!/bin/sh\n{body}\n", 0o755)

    def gate(self, **env: str) -> gate.Gate:
        g = gate.Gate(self.build, env={**self.env, **env}, cwd=str(self.repo))
        self.addCleanup(g.cleanup)
        return g

    def exit_code(self, fn, *args) -> int:
        with self.assertRaises(gate.Exit) as caught, contextlib.redirect_stderr(io.StringIO()):
            fn(*args)
        return caught.exception.code


class Options(Base):
    def test_dir_force_and_positionals(self) -> None:
        g = self.gate()
        g.parse_opts(["-d", "cfg", "-f", "a", "--", "-b", "c"])
        self.assertEqual((g.dir_opt, g.force, g.pos), ("cfg", True, ["a", "-b", "c"]))
        g.parse_opts(["--dir=x", "y"])
        self.assertEqual((g.dir_opt, g.force, g.pos), ("x", False, ["y"]))

    def test_unknown_option_and_missing_dir_argument_exit_2(self) -> None:
        self.assertEqual(self.exit_code(self.gate().parse_opts, ["--nope"]), 2)
        self.assertEqual(self.exit_code(self.gate().parse_opts, ["-d"]), 2)


class ConfigResolution(Base):
    def test_order_dir_env_nearest_xdg_builtin(self) -> None:
        (self.repo / "sub").mkdir()
        g = self.gate()
        g.cwd = str(self.repo / "sub")
        self.assertEqual(g.resolve_cfg_dir(), "")
        xdg = self.tmp / "home" / ".config" / "gate"
        xdg.mkdir(parents=True)
        self.assertEqual(g.resolve_cfg_dir(), str(xdg))
        (self.repo / ".gate").mkdir()
        self.assertEqual(g.resolve_cfg_dir(), str(self.repo / ".gate"))
        envdir = self.tmp / "envcfg"
        envdir.mkdir()
        g = self.gate(GATE_CONFIG=str(envdir))
        self.assertEqual(g.resolve_cfg_dir(), str(envdir))
        g.dir_opt = str(self.tmp)
        self.assertEqual(g.resolve_cfg_dir(), str(self.tmp))

    def test_a_missing_named_dir_is_an_error(self) -> None:
        g = self.gate(GATE_CONFIG=str(self.tmp / "nope"))
        self.assertEqual(self.exit_code(g.resolve_cfg_dir), 2)
        g = self.gate()
        g.dir_opt = "nope"
        self.assertEqual(self.exit_code(g.resolve_cfg_dir), 2)

    def test_a_file_absent_from_the_dir_falls_back_to_the_builtin(self) -> None:
        write(self.repo / ".gate" / "biome.json", "{}\n")
        g = self.gate()
        cfg = g.resolve_cfg_dir()
        self.assertEqual(
            g.resolve_file(cfg, "biome.json", "B"), str(self.repo / ".gate" / "biome.json")
        )
        self.assertEqual(g.resolve_file(cfg, "ruff.toml", "R"), "R")


class Compile(Base):
    def test_tokens_and_placeholders_are_expanded_and_the_cache_is_reused(self) -> None:
        g = self.gate()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            g.compile_config()
        self.assertIn("compiled config ->", err.getvalue())
        self.assertIn("/store/md/plugin.wasm", Path(g.effective["dprint"]).read_text())
        text = Path(g.effective["treefmt"]).read_text()
        self.assertIn(g.effective["dprint"], text)
        self.assertIn(self.builtin["biome"], text)
        self.assertIn(self.builtin["ruff"], text)
        self.assertEqual(g.effective["clippy"], self.builtin["clippy"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.gate().compile_config()
        self.assertEqual(err.getvalue(), "")  # warm cache

    def test_an_edited_repo_config_gets_a_new_cache(self) -> None:
        cfg = write(self.repo / ".gate" / "ruff.toml", "a = 1\n")
        g1 = self.gate()
        with contextlib.redirect_stderr(io.StringIO()):
            g1.compile_config()
        os.utime(cfg, ns=(1, 1))
        g2 = self.gate()
        with contextlib.redirect_stderr(io.StringIO()):
            g2.compile_config()
        self.assertNotEqual(g1.effective["treefmt"], g2.effective["treefmt"])

    def test_an_unknown_plugin_token_is_an_error(self) -> None:
        write(self.repo / ".gate" / "dprint.json", '{"plugins": ["@GATE_PLUGIN:nope@"]}\n')
        self.assertEqual(self.exit_code(self.gate().compile_config), 2)


class Gen(Base):
    def test_writes_every_config_and_refuses_to_overwrite_without_force(self) -> None:
        g = self.gate()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(g.gen(), 0)
        names = sorted(p.name for p in (self.repo / ".gate").iterdir())
        self.assertEqual(
            names, ["biome.json", "clippy.toml", "dprint.json", "ruff.toml", "treefmt.toml"]
        )
        mode = stat.S_IMODE((self.repo / ".gate" / "ruff.toml").stat().st_mode)
        self.assertEqual(mode, 0o644)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(g.gen(), 1)
            g.force = True
            self.assertEqual(g.gen(), 0)


class Pipe(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g = self.gate()
        self.g.effective = {"dprint": "D", "biome": "B", "ruff": "R"}

    def test_routing_by_extension(self) -> None:
        self.assertEqual(self.g.pipe_argv("x.json")[:2], ["dprint", "fmt"])
        self.assertEqual(self.g.pipe_argv("x.json")[-2:], ["--stdin", "x.json"])
        self.assertEqual(
            self.g.pipe_argv("x.tsx"),
            ["biome", "format", "--config-path=B", "--stdin-file-path=x.tsx"],
        )
        self.assertEqual(self.g.pipe_argv("x.nix"), ["nixfmt"])
        self.assertEqual(
            self.g.pipe_argv("x.pyi"),
            ["ruff", "format", "--config", "R", "--stdin-filename", "x.pyi", "-"],
        )
        self.assertEqual(self.g.pipe_argv("x.kdl"), ["kdlfmt", "format"])
        self.assertEqual(self.g.pipe_argv("justfile"), ["just", "--unstable", "--fmt", "-f"])
        self.assertEqual(self.g.pipe_argv("a.just"), ["just", "--unstable", "--fmt", "-f"])
        self.assertIsNone(self.g.pipe_argv("Makefile"))
        self.assertIsNone(self.g.pipe_argv("x.txt"))

    def run_pipe(self, name: str, data: bytes) -> tuple[int, bytes]:
        stdin = io.TextIOWrapper(io.BytesIO(data))
        stdout = io.TextIOWrapper(io.BytesIO())
        old_in, old_out = gate.sys.stdin, gate.sys.stdout
        gate.sys.stdin, gate.sys.stdout = stdin, stdout
        try:
            rc = self.g.pipe([name])
            stdout.flush()
            return rc, stdout.buffer.getvalue()
        finally:
            gate.sys.stdin, gate.sys.stdout = old_in, old_out

    def test_formats_passes_through_and_keeps_the_buffer_on_failure(self) -> None:
        self.tool("nixfmt", "tr a-z A-Z")
        self.assertEqual(self.run_pipe("dir/x.nix", b"abc\n"), (0, b"ABC\n"))
        self.assertEqual(self.run_pipe("x.txt", b"abc\n"), (0, b"abc\n"))
        self.tool("nixfmt", "echo partial; exit 3")
        self.assertEqual(self.run_pipe("x.nix", b"abc\n"), (1, b"abc\n"))

    def test_files_only_tools_go_through_a_temp_file(self) -> None:
        self.tool("just", 'f="$4"; tr a-z A-Z < "$f" > "$f.new" && mv "$f.new" "$f"')
        self.assertEqual(self.run_pipe("justfile", b"a:\n"), (0, b"A:\n"))
        self.tool("just", "exit 1")
        self.assertEqual(self.run_pipe("x.just", b"a:\n"), (1, b"a:\n"))
        self.tool("kdlfmt", 'f="$2"; tr a-z A-Z < "$f" > "$f.new" && mv "$f.new" "$f"')
        self.assertEqual(self.run_pipe("x.kdl", b"node\n"), (0, b"NODE\n"))

    def test_a_pipe_needs_exactly_one_name(self) -> None:
        self.assertEqual(self.exit_code(self.g.pipe, []), 2)
        self.assertEqual(self.exit_code(self.g.pipe, ["dir/"]), 2)


class Shebang(Base):
    def test_classes(self) -> None:
        cases = {
            "#!/bin/sh\n": "sh",
            "#!/usr/bin/env bash\n": "sh",
            "#!/usr/bin/env python3\n": "py",
            "#!/usr/bin/python3.12 -I\n": "py",
            "#!/usr/bin/env perl\n": "",
            "echo hi\n": "",
            "#!/usr/bin/env nix-shell\n#! nix-shell -i python3 -p python3\n": "py",
            "#!/usr/bin/env nix-shell\n#! nix-shell -p hello\n": "",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(gate.shebang_class(str(write(self.tmp / "s", text))), want)
        binary = self.tmp / "b"
        binary.write_bytes(b"#!/bin/sh\n\0\1")
        self.assertEqual(gate.shebang_class(str(binary)), "")


class Lint(Base):
    def test_files_are_sent_to_their_linters(self) -> None:
        write(self.repo / "a.sh", "true\n")
        write(self.repo / "b.py", "x = 1\n")
        write(self.repo / "c.svg", "<svg/>\n")
        write(self.repo / "d.rs", "fn main() {}\n")
        write(self.repo / "tool", "#!/usr/bin/env python3\n")
        write(self.repo / "run", "#!/bin/bash\n")
        write(self.repo / "notes.md", "x\n")
        for name in ("ruff", "xmllint", "cargo"):
            self.tool(name)
        g = self.gate()
        g.effective = {"ruff": "R", "clippy": self.builtin["clippy"]}
        calls: list[list[str]] = []
        g.run = lambda argv, **kw: calls.append(argv) or 0
        g.candidates = lambda paths: [
            "a.sh",
            "b.py",
            "c.svg",
            "d.rs",
            "tool",
            "run",
            "notes.md",
        ]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(g.lint([]), 0)
        by_tool = {c[0]: c for c in calls}
        self.assertEqual(sorted(by_tool["shellcheck"][1:]), ["a.sh", "run"])
        self.assertEqual(by_tool["ruff"][:5], ["ruff", "check", "-q", "--config", "R"])
        self.assertEqual(sorted(by_tool["ruff"][5:]), ["b.py", "tool"])
        self.assertEqual(by_tool["xmllint"][1:], ["--noout", "c.svg"])
        self.assertEqual(by_tool["typos"], ["typos"])
        self.assertEqual(by_tool["cargo"], ["cargo", "clippy", "--all-targets"])
        port = by_tool["python3"]
        self.assertNotIn("--partial", port)
        lines = Path(port[port.index("--files") + 1]).read_text().splitlines()
        self.assertIn("py\ttool", lines)
        self.assertIn("\tnotes.md", lines)

    def test_failures_are_named_in_the_status(self) -> None:
        g = self.gate()
        g.effective = {"ruff": "R", "clippy": self.builtin["clippy"]}
        g.run = lambda argv, **kw: 1 if argv[0] in ("shellcheck", "typos") else 0
        g.candidates = lambda paths: ["a.sh"]
        write(self.repo / "a.sh", "true\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(g.lint(["a.sh"]), 1)
        self.assertEqual(g.failed, ["shellcheck", "typos"])


class Clippy(Base):
    def gate_with(self, toml: str, **env: str) -> tuple[gate.Gate, list[list[str]]]:
        g = self.gate(**env)
        g.effective = {"clippy": str(write(self.tmp / "clippy.toml", toml))}
        calls: list[list[str]] = []
        g.run = lambda argv, **kw: calls.append(argv) or 0
        return g, calls

    def test_each_run_and_the_env_override(self) -> None:
        g, calls = self.gate_with('[[run]]\nargs = ["-p", "a b"]\n[[run]]\nargs = []\n')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(g.clippy_runs(), 0)
        self.assertEqual(calls, [["cargo", "clippy", "-p", "a b"], ["cargo", "clippy"]])
        g, calls = self.gate_with("", GATE_CLIPPY_ARGS=" --workspace  -q ")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(g.clippy_runs(), 0)
        self.assertEqual(calls, [["cargo", "clippy", "--workspace", "-q"]])

    def test_a_malformed_file_fails_the_step(self) -> None:
        for toml in ("[[run]]\nargs = [1]\n", "run = []\n", "not toml [\n"):
            with self.subTest(toml=toml):
                g, calls = self.gate_with(toml)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(g.clippy_runs(), 1)
                self.assertEqual((calls, g.failed), ([], ["clippy"]))


class Status(Base):
    def test_one_status_line_green_only_when_nothing_failed(self) -> None:
        g = self.gate()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(gate.Exit) as caught:
            g.finish("check")
        self.assertEqual((caught.exception.code, err.getvalue()), (0, "gate: check ok\n"))
        g.fail("formatting (1 file)")
        g.fail("typos")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(gate.Exit) as caught:
            g.finish("check")
        self.assertEqual(
            (caught.exception.code, err.getvalue()),
            (1, "gate: check FAILED: formatting (1 file), typos\n"),
        )

    def test_plural_and_batches(self) -> None:
        self.assertEqual(
            (gate.plural(1, "file"), gate.plural(2, "file")), ("1 file", "2 files")
        )
        self.assertEqual(gate.batches(["aa", "bb", "cc"], 6), [["aa", "bb"], ["cc"]])
        self.assertEqual(gate.batches([], 6), [])


class Route(Base):
    def gate_with(self, toml: str) -> gate.Gate:
        g = self.gate()
        g.effective = {"treefmt": str(write(self.tmp / "treefmt.toml", toml))}
        return g

    CONFIG = """
on-unmatched = "debug"
excludes = ["*.lock", "vendor/*"]
[formatter.rustfmt]
command = "rustfmt"
includes = ["*.rs"]
[formatter.dprint]
command = "dprint"
options = ["fmt"]
includes = ["*.json", "*.md"]
excludes = ["docs/*.md"]
[formatter.just]
command = "just"
includes = ["justfile", "*.just"]
"""

    def test_globs_cross_directories_and_excludes_apply(self) -> None:
        g = self.gate_with(self.CONFIG)
        files = [
            "src/a.rs",
            "a.json",
            "docs/x.md",
            "README.md",
            "Cargo.lock",
            "vendor/v.rs",
            "justfile",
            "sub/justfile",
            "sub/x.just",
            "notes.txt",
            "./b.rs",
        ]
        routed = {fm["name"]: f for fm, f in g.route(files)}
        self.assertEqual(
            routed,
            {
                "rustfmt": ["src/a.rs", "b.rs"],
                "dprint": ["a.json", "README.md"],
                "just": ["justfile", "sub/x.just"],
            },
        )

    def test_a_file_two_formatters_claim_is_an_error(self) -> None:
        g = self.gate_with(
            self.CONFIG + '[formatter.other]\ncommand = "x"\nincludes = ["*.rs"]\n'
        )
        self.assertEqual(self.exit_code(g.route, ["a.rs"]), 2)

    def test_unmatched_files_follow_on_unmatched(self) -> None:
        g = self.gate_with(self.CONFIG.replace('"debug"', '"error"'))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            g.route(["notes.txt"])
        self.assertIn("no formatter for notes.txt", err.getvalue())
        self.assertEqual(g.failed, ["unmatched (1 file)"])


class ReadOnlyModes(unittest.TestCase):
    def test_writing_options_become_checks(self) -> None:
        f = gate.FORMATTERS
        self.assertEqual(
            f["dprint"]["argv"](["fmt", "--config", "C"]),
            ["check", "--config", "C", "--list-different"],
        )
        self.assertEqual(
            f["biome"]["argv"](["format", "--write", "--config-path=B"]),
            ["format", "--config-path=B", "--reporter=json", "--max-diagnostics=none"],
        )
        self.assertEqual(
            f["just"]["argv"](["--unstable", "--fmt", "-f"]),
            ["--unstable", "--fmt", "--check", "-f"],
        )
        self.assertEqual(
            f["ruff"]["argv"](["format"])[-3:], ["--check", "--output-format", "concise"]
        )
        self.assertEqual(f["kdlfmt"]["argv"](["format"]), ["check"])

    def test_biome_parse_errors_are_errors_not_files_to_format(self) -> None:
        import json as j
        from types import SimpleNamespace as NS

        report = {
            "diagnostics": [
                {"category": "format", "severity": "error", "location": {"path": "a.css"}},
                {
                    "category": "parse",
                    "severity": "error",
                    "message": "bad",
                    "location": {"path": "b.css"},
                },
                {"category": "format", "severity": "error", "location": {"path": "b.css"}},
                {
                    "category": "deserialize",
                    "severity": "information",
                    "location": {"path": "biome.json"},
                },
            ]
        }
        named, error = gate.FORMATTERS["biome"]["parse"](
            NS(returncode=1, stdout=j.dumps(report), stderr=""), []
        )
        self.assertEqual((named, error), (["a.css"], "b.css: parse: bad"))

    def test_ruff_nixfmt_and_dprint_output(self) -> None:
        from types import SimpleNamespace as NS

        out = NS(
            returncode=1,
            stdout="x.py:1:2: unformatted: would be reformatted\n1 file would be reformatted\n",
            stderr="",
        )
        self.assertEqual(gate.FORMATTERS["ruff"]["parse"](out, []), (["x.py"], ""))
        out = NS(
            returncode=2,
            stdout="bad.py:1:5: invalid-syntax: Expected an identifier\n",
            stderr="",
        )
        self.assertEqual(
            gate.FORMATTERS["ruff"]["parse"](out, []),
            ([], "bad.py:1:5: invalid-syntax: Expected an identifier"),
        )
        out = NS(returncode=1, stdout="", stderr="a.nix: not formatted\n")
        self.assertEqual(gate.FORMATTERS["nixfmt"]["parse"](out, []), (["a.nix"], ""))
        out = NS(returncode=20, stdout="/r/a.json\n", stderr="")
        self.assertEqual(gate.FORMATTERS["dprint"]["parse"](out, []), (["/r/a.json"], ""))
        out = NS(returncode=1, stdout="", stderr="Error formatting /r/b.json")
        self.assertEqual(
            gate.FORMATTERS["dprint"]["parse"](out, []), ([], "Error formatting /r/b.json")
        )


class Which(Base):
    def test_named_tools_and_all(self) -> None:
        self.tool("biome")
        self.build["typos"] = "/store/typos/bin/typos"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.gate().main(["which", "biome", "typos"]), 0)
        self.assertEqual(
            out.getvalue().splitlines(), [str(self.bin / "biome"), "/store/typos/bin/typos"]
        )
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(self.gate().main(["which", "no-such-tool"]), 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.gate().main(["which", "--all"])
        rows = dict(line.split(None, 1) for line in out.getvalue().splitlines())
        self.assertEqual(set(rows), set(gate.TOOLS))
        self.assertEqual(rows["biome"], str(self.bin / "biome"))
        self.assertEqual(rows["typos"], "/store/typos/bin/typos")
        self.assertEqual(self.exit_code(self.gate().main, ["which"]), 2)


class CheckParts(Base):
    def run_check(self, *flags: str) -> tuple[list, int]:
        g = self.gate()
        calls: list = []
        g.compile_config = lambda: None
        g.check_fmt = lambda paths: calls.append(("format", paths)) or 0
        g.lint = lambda paths, linters=True, port=True: (
            calls.append(("lint", paths, linters, port)) or 0
        )
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(gate.Exit) as caught:
            g.main(["check", *flags])
        return calls, caught.exception.code

    def test_flags_pick_the_parts_and_keep_paths(self) -> None:
        self.assertEqual(self.run_check(), ([("format", []), ("lint", [], True, True)], 0))
        self.assertEqual(self.run_check("--format", "a"), ([("format", ["a"])], 0))
        self.assertEqual(
            self.run_check("--lint", "a", "b"), ([("lint", ["a", "b"], True, False)], 0)
        )
        self.assertEqual(self.run_check("--portability"), ([("lint", [], False, True)], 0))
        self.assertEqual(
            self.run_check("--format", "--portability", "x"),
            ([("format", ["x"]), ("lint", ["x"], False, True)], 0),
        )

    def test_only_check_takes_them(self) -> None:
        self.assertEqual(self.exit_code(self.gate().main, ["lint", "--format"]), 2)

    def test_lint_parts_skip_the_other_tools(self) -> None:
        write(self.repo / "a.sh", "true\n")
        g = self.gate()
        g.effective = {"ruff": "R", "clippy": self.builtin["clippy"]}
        calls: list[list[str]] = []
        g.run = lambda argv, **kw: calls.append(argv) or 0
        g.candidates = lambda paths: ["a.sh"]
        with contextlib.redirect_stderr(io.StringIO()):
            g.lint([], linters=False, port=True)
        self.assertEqual([c[0] for c in calls], ["python3"])
        calls.clear()
        with contextlib.redirect_stderr(io.StringIO()):
            g.lint([], linters=True, port=False)
        self.assertEqual([c[0] for c in calls], ["shellcheck", "typos"])


class Dispatch(Base):
    def test_version_help_and_unknown_command(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.gate().main(["version"]), 0)
            self.assertEqual(self.gate().main([]), 0)
        self.assertTrue(
            out.getvalue().startswith("gate-check-v9.9.9\ngate - ci formatter and linter")
        )
        self.assertEqual(self.exit_code(self.gate().main, ["frob"]), 2)


if __name__ == "__main__":
    unittest.main()
