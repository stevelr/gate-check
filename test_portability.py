"""Tests for portability.py. Run: python3 -m unittest test_portability"""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

import portability as p


class GlobTests(unittest.TestCase):
    def test_star_stays_within_a_directory(self) -> None:
        rx = p.glob_regex("test/stress/*.py")
        self.assertTrue(rx.match("test/stress/a.py"))
        self.assertFalse(rx.match("test/stress/sub/a.py"))

    def test_double_star_crosses_directories_and_matches_the_directory(self) -> None:
        rx = p.glob_regex("/mnt/scratch/**")
        self.assertTrue(rx.match("/mnt/scratch/a/b"))
        self.assertTrue(rx.match("/mnt/scratch"))
        self.assertFalse(rx.match("/mnt/scratchpad/a"))

    def test_leading_double_star_matches_zero_directories(self) -> None:
        rx = p.glob_regex("**/contracts/*")
        self.assertTrue(rx.match("contracts/a.txt"))
        self.assertTrue(rx.match("x/y/contracts/a.txt"))


class ScanLineTests(unittest.TestCase):
    roots = p.DEFAULT_ROOTS

    def scan(self, line: str) -> list[str]:
        return p.scan_line(line, list(self.roots))

    def test_flags_paths_below_default_roots(self) -> None:
        self.assertEqual(self.scan('f = "/home/alice/tmp/x.json"'), ["/home/alice/tmp/x.json"])
        self.assertEqual(self.scan("open('/Users/bob/data')"), ["/Users/bob/data"])
        self.assertEqual(self.scan("cp /mnt/scratch/a ."), ["/mnt/scratch/a"])
        self.assertEqual(self.scan("x=/var/tmp/y"), ["/var/tmp/y"])

    def test_flags_home_forms(self) -> None:
        self.assertEqual(self.scan("cargo run ~/tmp/a.json"), ["~/tmp/a.json"])
        self.assertEqual(self.scan('d="$HOME/x"'), ["$HOME/x"])
        self.assertEqual(self.scan('d="${HOME}/x"'), ["${HOME}/x"])

    def test_a_root_alone_is_not_a_finding(self) -> None:
        self.assertEqual(self.scan('mkdtemp(dir="/tmp")'), [])
        self.assertEqual(self.scan("cd /tmp/"), [])

    def test_ignores_non_roots_urls_and_expansions(self) -> None:
        self.assertEqual(self.scan("/nix/store/abc-x/bin/y /etc/hosts"), [])
        self.assertEqual(self.scan("https://example.com/home/x"), [])
        self.assertEqual(self.scan('"${out}/tmp/x" "$d/home/y"'), [])
        self.assertEqual(self.scan("a~/b"), [])

    def test_path_lists_split_on_colon(self) -> None:
        self.assertEqual(self.scan("PATH=/usr/bin:/home/a/bin"), ["/home/a/bin"])

    def test_added_roots(self) -> None:
        self.assertEqual(
            p.scan_line("/media/usb/x", [*self.roots, "/media"]), ["/media/usb/x"]
        )


class ClassifyTests(unittest.TestCase):
    def test_extensions_and_names(self) -> None:
        cases = {
            "a/b.go": "c",
            "x.cpp": "c",
            "x.h": "c",
            "d.jsonl": "none",
            "init.lua": "lua",
            "main.tf.hcl": "hcl",
            "nginx.conf": "conf",
            "app.config": "config",
            "prod.env": "hash",
            "sub/.env": "hash",
            "sub/.env.example": "hash",
            ".envrc": "hash",
            "Makefile": "hash",
            "rules.mk": "hash",
            "CMakeLists.txt": "hash",
            "go.mod": "c",
            "go.sum": "none",
            "justfile": "hash",
        }
        for rel, cls in cases.items():
            self.assertEqual(p.classify(rel), cls, rel)

    def test_skipped_files(self) -> None:
        for rel in ("README.md", "notes.txt", "image.png", "LICENSE"):
            self.assertIsNone(p.classify(rel), rel)

    def test_extensionless_scripts_use_the_shebang_class(self) -> None:
        self.assertEqual(p.classify("bin/tool", "py"), "hash")
        self.assertEqual(p.classify("bin/tool", "sh"), "hash")
        self.assertIsNone(p.classify("bin/tool", ""))


class NormaliseTests(unittest.TestCase):
    def test_home_forms_become_tilde(self) -> None:
        for raw in (
            "/home/alice/.secrets/k",
            "/Users/bob/.secrets/k",
            "$HOME/.secrets/k",
            "${HOME}/.secrets/k",
            "~/.secrets/k",
        ):
            self.assertEqual(p.normalise_home(raw), "~/.secrets/k", raw)
        self.assertEqual(p.normalise_home("/mnt/x"), "/mnt/x")


class RepoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".gate").mkdir()
        self.cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self) -> None:
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_check(self, files: dict[str, str], *extra: str) -> tuple[int, str]:
        listing = self.root / "list"
        listing.write_text("".join(f"{cls}\t{rel}\n" for rel, cls in files.items()))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = p.main(["--root", str(self.root), "--files", str(listing), *extra])
        return rc, err.getvalue()

    def test_clean_repo_passes_without_config(self) -> None:
        self.write("a.py", 'x = "relative/path"\n')
        self.assertEqual(self.run_check({"a.py": "py"})[0], 0)
        self.assertFalse((self.root / ".gate/portability.toml.TO_REVIEW").exists())

    def test_comment_lines_are_skipped(self) -> None:
        self.write("a.py", "# see ~/tmp/notes\nx = 1\n")
        self.write("b.rs", "// ~/tmp/notes\n/* /home/a/x */\n")
        self.assertEqual(self.run_check({"a.py": "py", "b.rs": "rs"})[0], 0)

    def test_comment_syntax_follows_the_file_type(self) -> None:
        self.write("go.mod", "// local fork: /home/a/src/x\nreplace x => /home/a/src/x\n")
        self.write("init.lua", "-- ~/tmp/a\nlocal f = '~/tmp/b'\n")
        self.write("README.md", "Copy ~/tmp/c first.\n")
        rc, err = self.run_check({"go.mod": "", "init.lua": "", "README.md": ""})
        self.assertEqual(rc, 1)
        self.assertIn("go.mod:2: machine-local path /home/a/src/x", err)
        self.assertNotIn("go.mod:1", err)
        self.assertIn("init.lua:2: machine-local path ~/tmp/b", err)
        self.assertNotIn("init.lua:1", err)
        self.assertNotIn("README.md", err)

    def test_beads_exports_are_allowed_without_config(self) -> None:
        self.write(".beads/issues.jsonl", '{"comment": "lost /home/user/tmp/x"}\n')
        self.write(".beads/config.yaml", "db: /home/user/x.db\n")
        rc, err = self.run_check({".beads/issues.jsonl": "", ".beads/config.yaml": ""})
        self.assertEqual(rc, 1)
        self.assertNotIn("issues.jsonl", err)
        self.assertIn(".beads/config.yaml:1:", err)
        # Never reported stale in a repo without beads.
        self.write(".gate/portability.toml", "")
        self.write("a.py", "x = 1\n")
        self.assertEqual(self.run_check({"a.py": ""})[0], 0)

    def test_findings_without_config_write_a_proposal(self) -> None:
        self.write("a.py", 'x = "/home/alice/tmp/x.json"\n')
        rc, err = self.run_check({"a.py": "py"})
        self.assertEqual(rc, 1)
        self.assertIn("a.py:1: machine-local path /home/alice/tmp/x.json", err)
        proposal = (self.root / ".gate/portability.toml.TO_REVIEW").read_text()
        self.assertIn('path = "~/tmp/x.json"', proposal)
        # Renamed unreviewed, its TODO reasons are rejected.
        (self.root / ".gate/portability.toml.TO_REVIEW").rename(
            self.root / ".gate/portability.toml"
        )
        rc, err = self.run_check({"a.py": "py"})
        self.assertEqual(rc, 2)
        self.assertIn("reason is still TODO", err)

    def test_no_proposal_when_a_config_exists(self) -> None:
        self.write(".gate/portability.toml", "")
        self.write("a.py", 'x = "/mnt/x/y"\n')
        self.assertEqual(self.run_check({"a.py": "py"})[0], 1)
        self.assertFalse((self.root / ".gate/portability.toml.TO_REVIEW").exists())

    def test_allowed_is_scoped_to_its_sources(self) -> None:
        self.write(
            ".gate/portability.toml",
            """
[[allowed]]
src = "test/stress/*.py"
path = "/mnt/scratch/**"
reason = "stress tests use the scratch volume"
""",
        )
        self.write("test/stress/s.py", 'd = "/mnt/scratch/run"\n')
        self.write("src/m.py", 'd = "/mnt/scratch/run"\n')
        rc, err = self.run_check({"test/stress/s.py": "py", "src/m.py": "py"})
        self.assertEqual(rc, 1)
        self.assertIn("src/m.py:1:", err)
        self.assertNotIn("test/stress/s.py", err)

    def test_allowed_paths_apply_everywhere_and_match_any_home_form(self) -> None:
        self.write(
            ".gate/portability.toml",
            """
[[allowedPaths]]
path = "~/.secrets/**"
reason = "secrets never live in the repo"
""",
        )
        self.write("a.sh", 'k="$HOME/.secrets/k.env"\n')
        self.write("b.py", 'k = "/home/sandy/.secrets/k.env"\n')
        self.assertEqual(self.run_check({"a.sh": "sh", "b.py": "py"})[0], 0)

    def test_stale_entries_fail_unless_optional_or_partial(self) -> None:
        self.write(
            ".gate/portability.toml",
            """
[[allowed]]
src = "a.py"
path = "/mnt/gone/**"
reason = "was used once"

[[allowedPaths]]
path = "/mnt/sometimes/**"
reason = "present on build hosts only"
optional = true
""",
        )
        self.write("a.py", "x = 1\n")
        rc, err = self.run_check({"a.py": "py"})
        self.assertEqual(rc, 1)
        self.assertIn("allowed[0]", err)
        self.assertNotIn("allowedPaths[0]", err)
        self.assertEqual(self.run_check({"a.py": "py"}, "--partial")[0], 0)

    def test_roots_add_and_sources_add(self) -> None:
        self.write(
            ".gate/portability.toml",
            """
[roots]
add = ["/media"]

[sources]
add = ["templates/*.tmpl"]
""",
        )
        self.write("templates/x.tmpl", "src=/media/usb/x\n")
        rc, err = self.run_check({})
        self.assertEqual(rc, 1)
        self.assertIn("templates/x.tmpl:1: machine-local path /media/usb/x", err)

    def test_config_errors(self) -> None:
        cases = {
            '[[allowed]]\nsrc = "a"\npath = "/mnt/x"\n': "missing reason",
            '[[allowedPaths]]\npath = "/mnt/x"\nreason = "r"\noptional = "yes"\n': "optional",
            'allowed_paths = []\n': "unknown key",
            '[roots]\nadd = ["media"]\n': "not an absolute directory",
        }
        self.write("a.py", "x = 1\n")
        for text, expected in cases.items():
            self.write(".gate/portability.toml", text)
            rc, err = self.run_check({"a.py": "py"})
            self.assertEqual(rc, 2, text)
            self.assertIn(expected, err, text)


if __name__ == "__main__":
    unittest.main()
