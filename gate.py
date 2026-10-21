#!/usr/bin/env python3
"""gate - ci formatter and linter

  gate fmt    [-d DIR] [paths...]   format in place (default: whole repo)
  gate check  [-d DIR] [--format] [--lint] [--portability] [paths...]
                                    check formatting, lints and portability
                                    (read-only); the flags pick parts (default: all)
  gate lint   [-d DIR] [paths...]   linters only
  gate format ...                   alias for fmt
  gate pipe   [-d DIR] <name>       stdin -> stdout; <name> picks formatter
  gate gen    [-f] [-d DIR]         write portable default configs to ./.gate
  gate which  TOOL... | --all       path of each tool gate runs (--all: every tool)

At run time (fmt | check | pipe) gate compiles the resolved config into an
"effective" config under a per-(config,gate-version) CACHE dir, so repeated
invocations reuse it instead of regenerating.

Config resolution (check | fmt | lint | pipe):
  The effective dprint.json, treefmt.toml, biome.json, ruff.toml and
  clippy.toml are each resolved independently, in this order:
   1. -d/--dir DIR         (must exist, else error)
   2. $GATE_CONFIG         (must exist, else error)
   3. nearest ./.gate walking up from $PWD
   4. $XDG_CONFIG_HOME/gate
   5. the built-in defaults baked into this gate
A file absent from the chosen dir falls back to the built-in default.

clippy (check | lint):
  clippy.toml lists the `cargo clippy` invocations; each [[run]] has an
  `args` array of the arguments after `cargo clippy`:
    [[run]]
    args = ["--workspace", "--all-targets", "--", "-D", "warnings"]
  $GATE_CLIPPY_ARGS (whitespace-separated, no quoting) replaces clippy.toml
  with one run. The built-in default is
  `--all-targets --all-features -- -D warnings`.

portability (check | lint):
  Flags source files that name machine-local paths: an absolute path below
  /tmp, /home, /Users, /mnt or /var/tmp, or a ~/, $HOME/ or ${HOME}/ path.
  Helps catch portability problems (tied to specific environment), and references
  to files that might not be committed to the repo.
  The check is textual only. Exceptions, extra roots and extra sources are configured
  in the repo's .gate/portability.toml:
    # add path roots to be flagged
    [roots]
    add = ["/media"]
    # add source files to be checked (override .gitignore)
    [sources]
    add = ["templates/*.tmpl"]
    # paths that should not be flagged
    [[allowedPaths]]
    path = "~/.secrets/**"
    reason = "secrets never live in the repo"
    # allowed src/path combinations
    [[allowed]]
    src = "test/stress/*.py"
    path = "/mnt/scratch/**"
    reason = "stress tests use the scratch volume"
    optional = true           # no error when it matches nothing

  Built in: .beads/*.jsonl (issue tracker exports) may cite any path.
  Every allowed or allowedPaths entry needs a reason. In a whole-repo run,
  an entry that fails a match generates an error (to prevent stale rules
  from accumulating). Set `optional=true` to allow a rule with no matches.

  If a (suspected) portability error is found, and no .gate/porability.toml
  exists, a draft .gate/portability.toml.TO_REVIEW is created.
  Review, edit, and rename to .gate/portability.toml.

Status (fmt | check | lint):
  Each run ends with exactly one status line on stderr, and it is the only
  verdict worth reading:
    gate: check ok                                   (green, exit 0)
    gate: check FAILED: formatting (3 files), typos  (red,   exit 1)
  Green is printed only when EVERY step passed. Per-tool success chatter is
  suppressed where the tool allows it so no sub-command can flash green while
  another is failing. Colour is disabled when stderr is not a tty or when
  $NO_COLOR is set.

Performance:
  The resolved config is COMPILED once (plugin tokens + config paths expanded)
  into a per-(config,gate)-version cache dir and reused across runs.
  `pipe` dispatches to each formatter's stdin mode by extension.

Formatters (fmt | check):
  treefmt.toml (the file name and format come from treefmt, which gate no longer
  runs) maps files to formatters: top-level `excludes` and `on-unmatched`, and
  per [formatter.NAME] `command`, `options`, `includes` and `excludes`. Globs
  match the path from the working directory and `*` crosses `/`. fmt runs each
  formatter with its options; check runs its read-only mode on the files where
  they are and lists the files it would change.
"""

# The installed `gate` is this file with BUILD_INFO pointing at the build's
# JSON (store paths of the built-in configs and tools; see package.nix). Run
# from a checkout, $GATE_BUILD_INFO names that file instead.

from __future__ import annotations

import atexit
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BUILD_INFO = "@gate_build_info@"

# Placeholders in treefmt.toml, substituted with the resolved config paths.
DPRINT_PLACEHOLDER = "@GATE_DPRINT_CONFIG@"
BIOME_PLACEHOLDER = "@GATE_BIOME_CONFIG@"
RUFF_PLACEHOLDER = "@GATE_RUFF_CONFIG@"
PLUGIN_TOKEN = re.compile(r"@GATE_PLUGIN:[^@]*@")

# Longest argument list handed to one formatter run (bytes, like xargs' default
# budget), so a tree larger than ARG_MAX still works.
ARG_BUDGET = 100_000

# Every tool gate runs (`gate which --all`), and those the build pins to a
# store path rather than finding on gate's PATH.
TOOLS = (
    "biome",
    "dprint",
    "just",
    "kdlfmt",
    "nixfmt",
    "ruff",
    "rustfmt",
    "shellcheck",
    "typos",
    "xmllint",
    "fd",
    "python3",
    "cargo",
)
PINNED_TOOLS = ("shellcheck", "typos", "ruff", "xmllint", "python3")

# fd arguments for candidate enumeration (see candidates()).
FD_ARGS = [
    "--type",
    "f",
    "--hidden",
    "--no-require-git",
    "--exclude",
    ".git",
    "--exclude",
    ".jj",
]


class Exit(Exception):
    """Ends the run with ``code`` (die() and finish() raise it)."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def load_build() -> dict:
    path = (
        BUILD_INFO if not BUILD_INFO.startswith("@") else os.environ.get("GATE_BUILD_INFO", "")
    )
    if not path:
        sys.stderr.write(
            "gate: no build info (set GATE_BUILD_INFO when running gate.py from a checkout)\n"
        )
        raise Exit(2)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Gate:
    def __init__(self, build: dict, env: dict | None = None, cwd: str | None = None) -> None:
        self.build = build
        self.env = dict(os.environ if env is None else env)
        self.env["PATH"] = build["path"] + (
            ":" + self.env["PATH"] if self.env.get("PATH") else ""
        )
        self.cwd = cwd or os.getcwd()
        colour = sys.stderr.isatty() and not self.env.get("NO_COLOR")
        self.red, self.green, self.off = (
            ("\033[1;31m", "\033[1;32m", "\033[0m") if colour else ("", "", "")
        )
        self.failed: list[str] = []
        self.tmpdirs: list[str] = []
        self.dir_opt = ""
        self.force = False
        self.pos: list[str] = []
        self.only: set[str] = set()  # check --format | --lint | --portability
        self.effective: dict[str, str] = {}

    # ---- status reporting --------------------------------------------------
    # Every sub-tool reports success differently: ruff prints a green
    # "All checks passed!", a formatter prints its own summary, shellcheck/xmllint/typos
    # print nothing. Left alone one tool's green success outranks another tool's
    # plain-text failure list, so a run with formatting errors still LOOKS green.
    # gate therefore silences per-tool success chatter where the tool allows it
    # (ruff -q; check captures the formatters' output) and always ends with exactly ONE status
    # line: red when any step failed, green only when every step passed.

    def die(self, msg: str) -> None:
        sys.stderr.write(f"{self.red}gate: {msg}{self.off}\n")
        raise Exit(2)

    def note(self, msg: str) -> None:
        sys.stderr.write(f"gate: {msg}\n")

    def warn(self, msg: str) -> None:
        sys.stderr.write(f"{self.red}gate: {msg}{self.off}\n")

    def fail(self, step: str) -> None:
        self.failed.append(step)

    def have(self, cmd: str) -> bool:
        return shutil.which(cmd, path=self.env["PATH"]) is not None

    def finish(self, label: str) -> None:
        """Single exit point for fmt | check | lint: green only when nothing failed."""
        sys.stderr.flush()
        if not self.failed:
            sys.stderr.write(f"{self.green}gate: {label} ok{self.off}\n")
            raise Exit(0)
        sys.stderr.write(
            f"{self.red}gate: {label} FAILED: {', '.join(self.failed)}{self.off}\n"
        )
        raise Exit(1)

    def mk_tmpd(self) -> str:
        d = tempfile.mkdtemp()
        self.tmpdirs.append(d)
        return d

    def cleanup(self) -> None:
        for d in self.tmpdirs:
            shutil.rmtree(d, ignore_errors=True)

    def run(self, argv: list[str], **kw) -> int:
        """Run a tool with gate's PATH in the current directory; its output goes through."""
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            return subprocess.run(
                argv, env=self.env, cwd=self.cwd, check=False, **kw
            ).returncode
        except FileNotFoundError:
            sys.stderr.write(f"gate: {argv[0]}: command not found\n")
            return 127

    # ---- option parsing ----------------------------------------------------

    def parse_opts(self, args: list[str]) -> None:
        self.dir_opt, self.force, self.pos, self.only = "", False, [], set()
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-d", "--dir"):
                i += 1
                if i >= len(args):
                    self.die("-d/--dir requires an argument")
                self.dir_opt = args[i]
            elif a.startswith("--dir="):
                self.dir_opt = a[len("--dir=") :]
            elif a in ("-f", "--force"):
                self.force = True
            elif a in ("--format", "--lint", "--portability"):
                self.only.add(a[2:])
            elif a == "--":
                self.pos.extend(args[i + 1 :])
                break
            elif a.startswith("-"):
                self.die(f"unknown option: {a}")
            else:
                self.pos.append(a)
            i += 1

    # ---- config resolution -------------------------------------------------

    def path_of(self, p: str) -> str:
        return p if os.path.isabs(p) else os.path.join(self.cwd, p)

    def resolve_cfg_dir(self) -> str:
        if self.dir_opt:
            if not os.path.isdir(self.path_of(self.dir_opt)):
                self.die(f"config dir not found: {self.dir_opt}")
            return self.dir_opt
        gc = self.env.get("GATE_CONFIG", "")
        if gc:
            if not os.path.isdir(self.path_of(gc)):
                self.die(f"GATE_CONFIG dir not found: {gc}")
            return gc
        d = self.cwd
        while d:
            if os.path.isdir(os.path.join(d, ".gate")):
                return os.path.join(d, ".gate")
            if d == "/":
                break
            d = os.path.dirname(d)
        xdg = os.path.join(
            self.env.get("XDG_CONFIG_HOME")
            or os.path.join(self.env.get("HOME", ""), ".config"),
            "gate",
        )
        return xdg if os.path.isdir(xdg) else ""

    def resolve_file(self, cfgdir: str, name: str, builtin: str) -> str:
        if cfgdir and os.path.isfile(self.path_of(os.path.join(cfgdir, name))):
            return os.path.join(cfgdir, name)
        return builtin

    def compile_config(self) -> None:
        """Resolve + COMPILE the effective config into a per-(config,gate)-version
        cache dir (plugin tokens expanded in dprint.json; config paths substituted
        in treefmt.toml). Idempotent: a warm cache is reused with no work."""
        b = self.build["builtin"]
        cfgdir = self.resolve_cfg_dir()
        dprint = self.resolve_file(cfgdir, "dprint.json", b["dprint"])
        biome = self.resolve_file(cfgdir, "biome.json", b["biome"])
        ruff = self.resolve_file(cfgdir, "ruff.toml", b["ruff"])
        treefmt = self.resolve_file(cfgdir, "treefmt.toml", b["treefmt"])
        # Like ruff.toml, clippy.toml is read as-is and never compiled.
        self.effective["clippy"] = self.resolve_file(cfgdir, "clippy.toml", b["clippy"])

        # Key on the gate config version and the resolved sources; for non-store
        # (repo/user) sources add the nanosecond mtime so edits invalidate the
        # cache (two edits in one second happen with format-on-save).
        keyinput = f"v={self.build['cfg_version']}|d={dprint}|b={biome}|r={ruff}|t={treefmt}"
        for f in (dprint, biome, ruff, treefmt):
            if not f.startswith("/nix/store/"):
                try:
                    mtime = os.stat(self.path_of(f)).st_mtime_ns
                except OSError:
                    mtime = 0
                keyinput += f"|m:{f}={mtime}"
        key = hashlib.sha256(keyinput.encode()).hexdigest()[:32]
        root = os.path.join(
            self.env.get("XDG_RUNTIME_DIR")
            or self.env.get("XDG_CACHE_HOME")
            or os.path.join(self.env.get("HOME", ""), ".cache"),
            "gate",
        )
        cachedir = os.path.join(root, key)
        self.effective.update(
            treefmt=os.path.join(cachedir, "treefmt.toml"),
            dprint=os.path.join(cachedir, "dprint.json"),
            biome=biome,
            # Like biome.json, ruff.toml carries no tokens, so the resolved source
            # is used as-is rather than compiled into the cache dir.
            ruff=ruff,
        )

        # Warm cache: nothing to do (the fast path for format-on-save).
        if os.path.isfile(self.effective["treefmt"]) and os.path.isfile(
            self.effective["dprint"]
        ):
            return

        try:
            os.makedirs(root, exist_ok=True)
        except OSError:
            self.die(f"cannot create cache root {root}")
        tmp = f"{cachedir}.tmp.{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            os.makedirs(tmp)
        except OSError:
            self.die(f"cannot create {tmp}")
        try:
            # Expand dprint plugin tokens; a surviving token is an unknown plugin.
            text = Path(self.path_of(dprint)).read_text(encoding="utf-8")
            for name, wasm in self.build["plugins"].items():
                text = text.replace(f"@GATE_PLUGIN:{name}@", wasm)
            leftover = sorted(set(PLUGIN_TOKEN.findall(text)))
            if leftover:
                shutil.rmtree(tmp, ignore_errors=True)
                self.die(
                    f"unknown dprint plugin token(s): {' '.join(leftover)} (not provided by this gate)"
                )
            Path(tmp, "dprint.json").write_text(text, encoding="utf-8")
            # Point treefmt.toml at the compiled dprint.json (final cache path) and
            # the resolved biome.json / ruff.toml.
            text = Path(self.path_of(treefmt)).read_text(encoding="utf-8")
            text = (
                text.replace(DPRINT_PLACEHOLDER, os.path.join(cachedir, "dprint.json"))
                .replace(BIOME_PLACEHOLDER, biome)
                .replace(RUFF_PLACEHOLDER, ruff)
            )
            Path(tmp, "treefmt.toml").write_text(text, encoding="utf-8")
        except OSError as error:
            shutil.rmtree(tmp, ignore_errors=True)
            self.die(f"compiling config failed: {error}")

        # Publish atomically; a concurrent compile produces identical bytes, so if
        # the rename loses the race the existing dir is equally valid.
        try:
            os.rename(tmp, cachedir)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            if not os.path.isfile(self.effective["treefmt"]):
                self.die(f"failed to publish cache {cachedir}")
        self.note(f"compiled config -> {cachedir} (from {cfgdir or 'built-in defaults'})")

    # ---- gen ---------------------------------------------------------------

    def gen_one(self, src: str, dst: str) -> bool:
        if os.path.exists(self.path_of(dst)) and not self.force:
            self.note(f"refusing to overwrite {dst} (use -f/--force)")
            return False
        try:
            shutil.copyfile(src, self.path_of(dst))
            os.chmod(self.path_of(dst), 0o644)
        except OSError:
            self.die(f"failed writing {dst}")
        self.note(f"wrote {dst}")
        return True

    def gen(self) -> int:
        outdir = self.dir_opt or "./.gate"
        try:
            os.makedirs(self.path_of(outdir), exist_ok=True)
        except OSError:
            self.die(f"cannot create {outdir}")
        b = self.build["builtin"]
        ok = True
        for name, key in (
            ("dprint.json", "dprint"),
            ("treefmt.toml", "treefmt"),
            ("biome.json", "biome"),
            ("ruff.toml", "ruff"),
            ("clippy.toml", "clippy"),
        ):
            ok = self.gen_one(b[key], os.path.join(outdir, name)) and ok
        if ok:
            self.note(
                f"generated portable gate config in {outdir} (no store paths; refresh-free)"
            )
        return 0 if ok else 1

    # ---- pipe (helix formatter contract) ------------------------------------
    # The buffer arrives on stdin and stdout REPLACES it. Dispatch to the
    # matching formatter's stdin mode by extension. Skips routing
    # and config scan. If format fails, echoes the original buffer
    # with a non-zero exit so the buffer is never lost.
    # Unmatched extensions pass through verbatim.

    def pipe_argv(self, base: str) -> list[str] | None:
        ext = base.rsplit(".", 1)[1] if "." in base else ""
        # Through a temp file (pipe_through_file): just formats files only, and
        # kdlfmt's stdin mode adds a blank line its file mode does not.
        if ext == "kdl":
            return ["kdlfmt", "format"]
        if is_justfile(base):
            return ["just", "--unstable", "--fmt", "-f"]
        if ext in (
            "json",
            "jsonc",
            "json5",
            "md",
            "markdown",
            "toml",
            "yaml",
            "yml",
            "html",
            "htm",
        ):
            return [
                "dprint",
                "fmt",
                "--config",
                self.effective["dprint"],
                "--config-discovery=ignore-descendants",
                "--stdin",
                base,
            ]
        if ext in (
            "css",
            "graphql",
            "gql",
            "js",
            "mjs",
            "cjs",
            "jsx",
            "ts",
            "mts",
            "cts",
            "tsx",
            "vue",
        ):
            return [
                "biome",
                "format",
                f"--config-path={self.effective['biome']}",
                f"--stdin-file-path={base}",
            ]
        if ext == "rs":
            if not self.have("rustfmt"):
                self.warn("rustfmt not on PATH; buffer not formatted")
            return ["rustfmt", "--edition", "2024"]
        if ext == "nix":
            return ["nixfmt"]
        if ext in ("py", "pyi"):
            return [
                "ruff",
                "format",
                "--config",
                self.effective["ruff"],
                "--stdin-filename",
                base,
                "-",
            ]
        return None

    def pipe(self, names: list[str]) -> int:
        if len(names) != 1:
            self.die("pipe: expected exactly one buffer name")
        base = names[0].rsplit("/", 1)[-1]
        if not base:
            self.die("pipe: empty buffer name")
        argv = self.pipe_argv(base)
        buf = sys.stdin.buffer.read()
        if argv is None:
            sys.stdout.buffer.write(buf)  # passthrough
            return 0
        if is_justfile(base) or base.endswith(".kdl"):
            result = self.pipe_through_file(argv, base, buf)
        else:
            try:
                out = subprocess.run(
                    argv,
                    input=buf,
                    capture_output=True,
                    env=self.env,
                    cwd=self.cwd,
                    check=False,
                )
                result = out.stdout if out.returncode == 0 else None
            except FileNotFoundError:
                result = None
        sys.stdout.buffer.write(buf if result is None else result)
        return 1 if result is None else 0

    def pipe_through_file(self, argv: list[str], base: str, buf: bytes) -> bytes | None:
        """Format ``buf`` with a tool that formats files in place: write it to a temp
        file named ``base``, run ``argv`` on it, read it back. None on failure."""
        path = os.path.join(self.mk_tmpd(), base)
        Path(path).write_bytes(buf)
        try:
            out = subprocess.run(
                [*argv, path], capture_output=True, env=self.env, cwd=self.cwd, check=False
            )
        except FileNotFoundError:
            return None
        return Path(path).read_bytes() if out.returncode == 0 else None

    # ---- candidate enumeration ---------------------------------------------
    # ONE enumerator, shared by fmt, check and lint, so all three agree on the
    # file set. fd:
    #   - honours .gitignore/.ignore/.fdignore with no git dependency, so it
    #     behaves identically in and out of a repo,
    #   - includes dotfiles (--hidden),
    #   - emits regular files only (--type f), so a symlink is never followed by
    #     fmt or by check.
    # Paths are relative to the search root. A file that
    # is BOTH tracked and gitignored is skipped: ignored means ignored.

    def fd(self, *where: str) -> list[str]:
        out = subprocess.run(
            ["fd", *FD_ARGS, ".", *where],
            env=self.env,
            cwd=self.cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if out.stderr:
            sys.stderr.write(out.stderr)
        return [line for line in out.stdout.splitlines() if line]

    def candidates(self, paths: list[str]) -> list[str]:
        if not paths:
            return self.fd()
        found: list[str] = []
        for p in paths:
            full = self.path_of(p)
            if os.path.islink(full):
                continue  # never follow symlinks
            if os.path.isdir(full):
                found.extend(self.fd(p))
            elif os.path.isfile(full):
                found.append(p)
        return found

    # ---- routing ------------------------------------------------------------
    # treefmt.toml says which formatter handles which files: top-level
    # `excludes`, `on-unmatched`, and per [formatter.NAME] `command`, `options`,
    # `includes` and `excludes`. gate routes the candidate files itself and runs
    # each formatter on its own files: `fmt` with the configured options (they
    # write), `check` with that formatter's read-only mode (see CHECKS), so check
    # reads the files where they are and nothing is copied.
    #
    # Globs match the path relative to the working directory, and `*` crosses
    # `/` (as in treefmt): "*.rs" matches "src/main.rs", "justfile" only the
    # top-level one. A file two formatters claim is a config error.

    def formatters(self) -> tuple[list[dict], list[str], str]:
        """The formatters, the global excludes and the on-unmatched level from the
        effective treefmt.toml (config paths already substituted)."""
        import tomllib

        path = self.effective["treefmt"]
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as error:
            self.die(f"cannot read {path}: {error}")
        formatters = []
        for name, table in sorted((data.get("formatter") or {}).items()):
            if not isinstance(table, dict) or not isinstance(table.get("command"), str):
                self.die(f"{path}: [formatter.{name}] needs a command")
            formatters.append(
                {
                    "name": name,
                    "command": table["command"],
                    "options": [str(o) for o in table.get("options") or []],
                    "includes": [str(g) for g in table.get("includes") or []],
                    "excludes": [str(g) for g in table.get("excludes") or []],
                }
            )
        return (
            formatters,
            [str(g) for g in data.get("excludes") or []],
            str(data.get("on-unmatched") or "info"),
        )

    def route(self, files: list[str]) -> list[tuple[dict, list[str]]]:
        """Each formatter with the candidate files it handles (paths relative to
        the working directory). Unmatched files are reported per on-unmatched."""
        formatters, excludes, unmatched_level = self.formatters()
        claimed: dict[str, list[str]] = {f["name"]: [] for f in formatters}
        unmatched = []
        for f in files:
            rel = os.path.relpath(self.path_of(f), self.cwd)
            if globs_match(excludes, rel):
                continue
            owners = [
                fm
                for fm in formatters
                if globs_match(fm["includes"], rel) and not globs_match(fm["excludes"], rel)
            ]
            if len(owners) > 1:
                names = ", ".join(fm["name"] for fm in owners)
                self.die(
                    f"{rel}: claimed by more than one formatter ({names}); narrow their includes"
                )
            if owners:
                claimed[owners[0]["name"]].append(rel)
            else:
                unmatched.append(rel)
        if unmatched and unmatched_level in ("warn", "error", "fatal"):
            for rel in unmatched:
                self.warn(f"no formatter for {rel}")
            if unmatched_level != "warn":
                self.fail(f"unmatched ({plural(len(unmatched), 'file')})")
        return [(fm, claimed[fm["name"]]) for fm in formatters if claimed[fm["name"]]]

    def runnable(self, fm: dict, files: list[str]) -> bool:
        """False, with a warning, when the formatter's command is not on gate's PATH
        (rustfmt comes from the caller's toolchain)."""
        if self.have(fm["command"]):
            return True
        if fm["command"] == "rustfmt":
            self.warn("rustfmt not on PATH; .rs files not formatted")
        else:
            self.warn(
                f"{fm['command']} not on PATH; {plural(len(files), 'file')} not formatted"
            )
        return False

    # ---- in-place formatting -------------------------------------------------

    def fmt_tree(self, paths: list[str]) -> int:
        rc = 0
        total = changed = 0
        for fm, files in self.route(self.candidates(paths)):
            if not self.runnable(fm, files):
                continue
            before = {f: digest(self.path_of(f)) for f in files}
            per_file = FORMATTERS.get(os.path.basename(fm["command"]), {}).get(
                "per_file", False
            )
            for batch in [[f] for f in files] if per_file else batches(files, ARG_BUDGET):
                if self.run([fm["command"], *fm["options"], *batch]) != 0:
                    rc = 1
            total += len(files)
            changed += sum(1 for f in files if digest(self.path_of(f)) != before[f])
        self.note(f"formatted {plural(total, 'file')} ({changed} changed)")
        return rc

    # ---- read-only formatting check ------------------------------------------
    # Each formatter runs in its read-only mode on the files where they are, and
    # its output names the files it would change (CHECKS). Output that names no
    # such file, from a run that did not pass, is a formatter error (a file it
    # cannot parse, a bad config) and is printed as the tool wrote it.

    def check_fmt(self, paths: list[str]) -> int:
        rc = 0
        needs: list[str] = []
        for fm, files in self.route(self.candidates(paths)):
            if not self.runnable(fm, files):
                continue
            tool = FORMATTERS.get(os.path.basename(fm["command"]))
            if tool is None:
                self.warn(
                    f"formatter {fm['name']} ({fm['command']}): gate has no read-only check for it"
                )
                self.fail("formatter error")
                rc = 1
                continue
            known = {os.path.realpath(self.path_of(f)): f for f in files}
            groups = (
                [[f] for f in files] if tool.get("per_file") else batches(files, ARG_BUDGET)
            )
            for batch in groups:
                argv = [fm["command"], *tool["argv"](fm["options"]), *batch]
                sys.stdout.flush()
                sys.stderr.flush()
                out = subprocess.run(
                    argv,
                    env=self.env,
                    cwd=self.cwd,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                named, error = tool["parse"](out, batch)
                for printed in named:
                    # A tool may name a file it reached on its own (rustfmt follows
                    # `mod` declarations); only the candidates count.
                    real = os.path.realpath(self.path_of(printed))
                    if real in known:
                        needs.append(known[real])
                if error:
                    sys.stderr.write(error if error.endswith("\n") else error + "\n")
                    if "formatter error" not in self.failed:
                        self.fail("formatter error")
                    rc = 1
        if needs:
            needs = sorted(set(needs))
            self.warn(f"needs formatting ({len(needs)}):")
            sys.stderr.write("".join(f"  {f}\n" for f in needs))
            self.fail(f"formatting ({plural(len(needs), 'file')})")
            rc = 1
        return rc

    # ---- lint --------------------------------------------------------------

    def repo_root(self) -> str:
        """The nearest directory holding .gate, .git or .jj."""
        d = self.cwd
        while True:
            if (
                os.path.isdir(os.path.join(d, ".gate"))
                or os.path.exists(os.path.join(d, ".git"))
                or os.path.isdir(os.path.join(d, ".jj"))
            ):
                return d
            if d == "/":
                return self.cwd
            d = os.path.dirname(d)

    def portability(self, port_list: str, paths: list[str]) -> int:
        """Stale allow entries are reported only when the whole repo was scanned."""
        root = self.repo_root()
        partial = ["--partial"] if paths or self.cwd != root else []
        self.note("portability")
        if (
            self.run(
                [
                    self.build["python3"],
                    "-I",
                    self.build["portability"],
                    "--root",
                    root,
                    "--files",
                    port_list,
                    *partial,
                ]
            )
            != 0
        ):
            self.fail("portability")
            return 1
        return 0

    def clippy_runs(self) -> int:
        """Run every configured `cargo clippy` invocation (see "clippy" in the help).
        A malformed clippy.toml fails the step rather than falling back silently."""
        args_env = self.env.get("GATE_CLIPPY_ARGS", "")
        if args_env:
            args = args_env.split()
            self.note(f"cargo clippy {' '.join(args)} (GATE_CLIPPY_ARGS)")
            if self.run(["cargo", "clippy", *args]) != 0:
                self.fail("clippy")
                return 1
            return 0
        import tomllib

        path = self.effective["clippy"]
        try:
            with open(self.path_of(path), "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            self.warn(f"cannot parse {path}")
            self.fail("clippy")
            return 1
        runs = data.get("run")
        if not (
            isinstance(runs, list)
            and runs
            and all(
                isinstance(r, dict)
                and isinstance(r.get("args"), list)
                and all(isinstance(a, str) for a in r["args"])
                for r in runs
            )
        ):
            self.warn(f"invalid {path}: expected [[run]] tables with args = [strings]")
            self.fail("clippy")
            return 1
        rc = 0
        for r in runs:
            self.note(f"cargo clippy {' '.join(r['args'])}")
            if self.run(["cargo", "clippy", *r["args"]]) != 0:
                rc = 1
        if rc:
            self.fail("clippy")
        return rc

    def lint(self, paths: list[str], linters: bool = True, port: bool = True) -> int:
        """The linters (shellcheck, ruff check, xmllint, typos, clippy) and the
        portability check; ``linters`` or ``port`` false skips that part."""
        rc = 0
        files = self.candidates(paths)
        sh_files: list[str] = []
        py_files: list[str] = []
        xml_files: list[str] = []
        rs_files: list[str] = []
        lines: list[str] = []
        for f in files:
            sb = shebang_class(self.path_of(f)) if "." not in f.rsplit("/", 1)[-1] else ""
            # portability.py picks the files it scans; it needs the shebang class
            # only for extensionless scripts.
            lines.append(f"{sb}\t{f}\n")
            if f.endswith((".sh", ".bash")):
                sh_files.append(f)
            elif f.endswith((".py", ".pyi")):
                py_files.append(f)
            elif f.endswith((".xml", ".svg")):
                xml_files.append(f)
            elif f.endswith(".rs"):
                rs_files.append(f)
            elif sb == "sh":
                sh_files.append(f)
            elif sb == "py":
                py_files.append(f)
        port_list = os.path.join(self.mk_tmpd(), "portability")
        Path(port_list).write_text("".join(lines), encoding="utf-8")

        if linters and sh_files:
            self.note(f"shellcheck ({len(sh_files)})")
            if self.run([self.build["shellcheck"], *sh_files]) != 0:
                rc = 1
                self.fail("shellcheck")

        if linters and py_files and self.have("ruff"):
            self.note(f"ruff check ({len(py_files)})")
            # -q prints diagnostics but suppresses ruff's own green "All checks
            # passed!", which otherwise reads as an overall pass next to another
            # step's failures. gate's final status line is the verdict.
            if (
                self.run(
                    [
                        self.build["ruff"],
                        "check",
                        "-q",
                        "--config",
                        self.effective["ruff"],
                        *py_files,
                    ]
                )
                != 0
            ):
                rc = 1
                self.fail("ruff check")

        if linters and xml_files and self.have("xmllint"):
            self.note(f"xmllint ({len(xml_files)})")
            if self.run([self.build["xmllint"], "--noout", *xml_files]) != 0:
                rc = 1
                self.fail("xmllint")

        if linters:
            self.note("typos")
            if self.run([self.build["typos"], *paths]) != 0:
                rc = 1
                self.fail("typos")

        if port and self.portability(port_list, paths) != 0:
            rc = 1

        if linters and (
            rs_files or (not paths and os.path.isfile(self.path_of("Cargo.toml")))
        ):
            if self.have("cargo"):
                if self.clippy_runs() != 0:
                    rc = 1
            else:
                self.note("clippy skipped (no cargo on PATH)")
        return rc

    # ---- which -------------------------------------------------------------

    def tool_paths(self) -> dict[str, str | None]:
        """Every tool gate runs, with the path it would run (None: not found).
        The pinned ones come from the build; the others from gate's PATH."""
        pinned = {name: self.build[name] for name in PINNED_TOOLS if name in self.build}
        return {
            name: pinned.get(name) or shutil.which(name, path=self.env["PATH"])
            for name in TOOLS
        }

    def which(self, args: list[str]) -> int:
        """`gate which TOOL...` prints the path gate runs for each tool;
        `--all` lists every tool gate uses. Exit 1 when one is not found."""
        if not args:
            self.die("which: name a tool, or --all")
        tools = self.tool_paths()
        if args == ["--all"]:
            width = max(len(name) for name in tools)
            for name, path in tools.items():
                print(f"{name:<{width}}  {path or '(not found)'}")
            return 0 if all(tools.values()) else 1
        rc = 0
        for name in args:
            if name.startswith("-"):
                self.die(f"which: unknown option: {name}")
            path = tools.get(name) or shutil.which(name, path=self.env["PATH"])
            if path:
                print(path)
            else:
                self.warn(f"which: {name}: not found")
                rc = 1
        return rc

    # ---- dispatch ----------------------------------------------------------

    def main(self, argv: list[str]) -> int:
        cmd = argv[0] if argv else ""
        args = argv[1:]
        if cmd in ("fmt", "format", "pipe", "check", "lint", "gen"):
            self.parse_opts(args)
            if self.only and cmd != "check":
                self.die(
                    f"--{', --'.join(sorted(self.only))}: only `gate check` takes these flags"
                )
        if cmd in ("fmt", "format"):
            self.compile_config()
            if self.fmt_tree(self.pos) != 0:
                self.fail("formatter error")
            self.finish("fmt")
        elif cmd == "pipe":
            self.compile_config()
            # No status line: stdout replaces an editor buffer, and a per-keystroke
            # "ok" on stderr is noise in that contract.
            return self.pipe(self.pos)
        elif cmd == "check":
            self.compile_config()
            # --format, --lint and --portability pick parts; none picks all three.
            parts = self.only or {"format", "lint", "portability"}
            if "format" in parts:
                self.check_fmt(self.pos)
            if parts & {"lint", "portability"}:
                self.lint(self.pos, linters="lint" in parts, port="portability" in parts)
            self.finish("check")
        elif cmd == "lint":
            self.compile_config()  # lint needs the effective ruff.toml for `ruff check`
            self.lint(self.pos)
            self.finish("lint")
        elif cmd == "gen":
            return self.gen()
        elif cmd == "which":
            return self.which(args)
        elif cmd in ("version", "-V", "--version"):
            print(f"{self.build['package']}-v{self.build['version']}")
        elif cmd in ("help", "-h", "--help", ""):
            print(__doc__.rstrip("\n"))
        else:
            self.die(
                f"unknown command '{cmd}' (expected: fmt | check | lint | pipe | gen | which | version)"
            )
        return 0


def batches(items: list[str], budget: int) -> list[list[str]]:
    """``items`` split into lists whose summed lengths stay under ``budget``."""
    out: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for item in items:
        n = len(os.fsencode(item)) + 1
        if cur and size + n > budget:
            out.append(cur)
            cur, size = [], 0
        cur.append(item)
        size += n
    if cur:
        out.append(cur)
    return out


def digest(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).digest()
    except OSError:
        return None


def globs_match(globs: list[str], rel: str) -> bool:
    """treefmt-style glob match: `*` and `**` cross `/`."""
    return any(fnmatch.fnmatchcase(rel, g) for g in globs)


# ---- read-only modes -------------------------------------------------------
# For each formatter command gate knows: how its configured (writing) options
# become a read-only check, and how to read that check's output. ``parse`` gets
# the finished process and the files it was given, and returns the files it
# would change (as the tool printed them) and the error text to show, if any.
# ``per_file``: the tool takes one file per run (just -f FILE).


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _rustfmt(out, batch):
    # -l lists the files that would change; a parse error is reported on stderr
    # as "error: ..." (warnings there are not failures).
    named = _lines(out.stdout)
    if out.returncode == 0:
        return named, ""
    if "error" in out.stderr or not named:
        return named, (out.stderr + out.stdout).strip() or f"exit {out.returncode}"
    return named, ""


def _dprint(out, batch):
    named = _lines(out.stdout)
    # 20: files would change; anything else non-zero is an error.
    bad = out.stderr.strip() if out.returncode not in (0, 20) else ""
    return named, bad


NIX_NOT_FORMATTED = re.compile(r"^(.*): not formatted$")


def _nixfmt(out, batch):
    named, rest = [], []
    for line in (out.stderr + out.stdout).splitlines():
        m = NIX_NOT_FORMATTED.match(line)
        if m:
            named.append(m.group(1))
        elif line.strip():
            rest.append(line)
    return named, "\n".join(rest) if rest and out.returncode else ""


RUFF_LINE = re.compile(r"^(.*?):\d+:\d+: (\S+):")


def _ruff(out, batch):
    named, rest = [], []
    for line in (out.stdout + out.stderr).splitlines():
        m = RUFF_LINE.match(line)
        if m and m.group(2) == "unformatted":
            named.append(m.group(1))
        elif m:
            rest.append(line)
    if out.returncode not in (0, 1) and not rest:
        rest = [(out.stderr + out.stdout).strip() or f"exit {out.returncode}"]
    return named, "\n".join(rest)


def _biome(out, batch):
    try:
        report = json.loads(out.stdout)
    except ValueError:
        text = (out.stderr + out.stdout).strip()
        return [], text if out.returncode else ""
    named, rest, broken = [], [], set()
    for d in report.get("diagnostics") or []:
        path = ((d.get("location") or {}).get("path")) or ""
        if d.get("category") == "format":
            named.append(path)
        elif d.get("severity") in ("error", "fatal"):
            broken.add(path)
            rest.append(f"{path}: {d.get('category')}: {d.get('message') or ''}".rstrip())
    # A file biome cannot parse also gets a "format" diagnostic: it is an error,
    # not a file to format.
    named = [p for p in named if p not in broken]
    if out.returncode and not named and not rest:
        rest = [out.stderr.strip() or f"exit {out.returncode}"]
    return named, "\n".join(rest)


KDL_NOT_FORMATTED = re.compile(r"^(.*) is not formatted$")


def _kdlfmt(out, batch):
    named, rest = [], []
    for line in (out.stderr + out.stdout).splitlines():
        m = KDL_NOT_FORMATTED.match(line)
        if m:
            named.append(m.group(1))
        elif line.strip() and not line.startswith("Found changes while running in check mode"):
            rest.append(line)
    return named, "\n".join(rest) if out.returncode else ""


def _kdlfmt_argv(options: list[str]) -> list[str]:
    """`kdlfmt format ...` becomes `kdlfmt check ...`."""
    opts = list(options)
    if "format" in opts:
        opts[opts.index("format")] = "check"
    return opts


def _just(out, batch):
    if out.returncode == 0:
        return [], ""
    if out.returncode == 1:
        return list(batch), ""
    return [], (out.stderr + out.stdout).strip() or f"exit {out.returncode}"


def _dprint_argv(options: list[str]) -> list[str]:
    """`dprint fmt ...` becomes `dprint check ... --list-different`."""
    opts = list(options)
    if "fmt" in opts:
        opts[opts.index("fmt")] = "check"
    return [*opts, "--list-different"]


def _just_argv(options: list[str]) -> list[str]:
    opts = [o for o in options if o != "-f"]
    return [*opts, "--check", "-f"]


FORMATTERS: dict[str, dict] = {
    "rustfmt": {"argv": lambda o: [*o, "--check", "-l"], "parse": _rustfmt},
    "dprint": {"argv": _dprint_argv, "parse": _dprint},
    "nixfmt": {"argv": lambda o: [*o, "--check"], "parse": _nixfmt},
    "ruff": {"argv": lambda o: [*o, "--check", "--output-format", "concise"], "parse": _ruff},
    "biome": {
        "argv": lambda o: [
            *(x for x in o if x != "--write"),
            "--reporter=json",
            "--max-diagnostics=none",
        ],
        "parse": _biome,
    },
    "just": {"argv": _just_argv, "parse": _just, "per_file": True},
    "kdlfmt": {"argv": _kdlfmt_argv, "parse": _kdlfmt},
}


def is_justfile(base: str) -> bool:
    return base in ("justfile", ".justfile", "Justfile") or base.endswith(".just")


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


INTERP_SH = re.compile(r"(^|/|\s)(bash|sh|dash)(\s|$)")
INTERP_PY = re.compile(r"(^|/|\s)python[0-9.]*(\s|$)")
NIX_SHELL = re.compile(r"(^|/|\s)nix-shell(\s|$)")
NIX_SHELL_I = re.compile(r"\s-i\s+(\S+)")


def shebang_class(path: str) -> str:
    """Classify an extensionless file by shebang: "sh" | "py" | "".

    A nix-shell shebang names nix-shell, not the real interpreter -- that lives
    on a following '#! nix-shell -i INTERP' line (within the first 20 lines)."""
    try:
        with open(path, "rb") as f:
            head = f.read(32768)
    except OSError:
        return ""
    if not head.startswith(b"#!") or b"\0" in head:  # not a script, or binary
        return ""
    lines = head.decode("utf-8", "replace").split("\n")
    interp = lines[0][:256]
    if NIX_SHELL.search(interp):
        interp = ""
        for line in lines[:20]:
            if line.startswith("#!") and "nix-shell" in line:
                m = NIX_SHELL_I.search(line)
                if m:
                    interp = m.group(1)
                    break
    if INTERP_SH.search(interp):
        return "sh"
    if INTERP_PY.search(interp):
        return "py"
    return ""


def main(argv: list[str] | None = None) -> int:
    try:
        gate = Gate(load_build())
    except Exit as e:
        return e.code
    atexit.register(gate.cleanup)
    try:
        return gate.main(sys.argv[1:] if argv is None else argv)
    except Exit as e:
        return e.code
    finally:
        gate.cleanup()


if __name__ == "__main__":
    sys.exit(main())
