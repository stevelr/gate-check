"""gate portability: flag source files that name machine-local paths.

A path such as /home/alice/tmp/x.json, ~/scratch/y or /mnt/data/z exists on
one machine only. Committed code that reads one works only there, and breaks
silently when the file is lost. This check is textual: it never looks at the
filesystem to decide whether a named path exists.

A finding is an absolute path under a flagged root (built-in roots plus
roots.add), or a ~/, $HOME/ or ${HOME}/ path. A root on its own (/tmp) is not
a finding; a path below it (/tmp/x) is. Lines that start with a comment are
skipped.

Scanned: source and config files by extension or name (see EXTENSIONS and
NAMES), extensionless sh/python scripts by shebang, and sources.add. Markdown
is not scanned unless sources.add names it.

Config: <repo>/.gate/portability.toml (the repo's .gate only, never a user
config, so every checkout gets the same verdict):

    [roots]
    add = ["/media"]                  # more flagged root directories

    [sources]
    add = ["templates/*.tmpl"]        # more files to scan (may be gitignored)

    [[allowedPaths]]                  # allowed in every source file
    path = "~/.secrets/**"
    reason = "secrets never live in the repo"

    [[allowed]]                       # allowed in matching source files only
    src = "test/stress/*.py"
    path = "/mnt/scratch/**"
    reason = "stress tests use the scratch volume"
    optional = true                   # no error when the entry matches nothing

Globs: `**` crosses directories, `*` and `?` stay within one, [...] is a
class. `src` and `sources.add` are relative to the repo root. `path` matches
the path as written, or its home-normalised form: /home/<user>/x,
/Users/<user>/x, $HOME/x and ${HOME}/x all become ~/x. `dir/**` also matches
`dir` itself.

Every entry needs a reason; a reason starting with TODO is rejected. In a
whole-repo run an entry that matches nothing is an error unless optional.

When there are findings and no portability.toml, a proposed config is written
to .gate/portability.toml.TO_REVIEW.

Exit status: 0 clean, 1 findings or stale entries, 2 usage or config error.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import tomllib

DEFAULT_ROOTS = ("/tmp", "/home", "/Users", "/mnt", "/var/tmp")
CONFIG_NAME = "portability.toml"

# Line-leading comment markers, per comment syntax.
COMMENTS = {
    "hash": ("#",),
    "c": ("//", "/*", "*"),
    "nix": ("#", "/*", "*"),
    "hcl": ("#", "//", "/*", "*"),
    "conf": ("#", ";"),
    "config": ("#", ";", "<!--"),
    "lua": ("--",),
    "none": (),
}

# The files gate scans, by extension and by exact file name, with their comment
# syntax. Extensionless scripts are classified by gate's shebang_class.
EXTENSIONS = {
    **dict.fromkeys(("py", "pyi", "sh", "bash", "toml", "yaml", "yml", "just"), "hash"),
    **dict.fromkeys(("mk", "cmake", "env"), "hash"),
    **dict.fromkeys(("rs", "go", "c", "cc", "cpp", "cxx", "h"), "c"),
    **dict.fromkeys(("js", "mjs", "cjs", "jsx", "ts", "mts", "cts", "tsx"), "c"),
    **dict.fromkeys(("json", "jsonc", "jsonl"), "none"),
    "nix": "nix",
    "hcl": "hcl",
    "conf": "conf",
    "config": "config",
    "lua": "lua",
}
NAMES = {
    **dict.fromkeys(("justfile", "Justfile", ".justfile"), "hash"),
    **dict.fromkeys(("Makefile", "makefile", "GNUmakefile", "CMakeLists.txt"), "hash"),
    **dict.fromkeys((".envrc", ".env"), "hash"),
    "go.mod": "c",
    "go.sum": "none",
}
SHEBANGS = {"sh": "hash", "py": "hash"}


def classify(rel: str, shebang: str = "") -> str | None:
    """The comment syntax of a file gate scans, or None for a file it skips."""
    name = rel.rsplit("/", 1)[-1]
    if name in NAMES:
        return NAMES[name]
    if name.startswith(".env."):  # .env.example, .env.local
        return "hash"
    if "." in name.lstrip("."):
        return EXTENSIONS.get(name.rsplit(".", 1)[-1])
    return SHEBANGS.get(shebang)


# A path runs until whitespace, a quote, a bracket, or a list separator.
TAIL = r"""[^\s"'`()\[\]{}<>,;:|\\]*"""
# An absolute path must not continue a word, URL host, or variable expansion
# (${out}/tmp/x is relative to $out).
ABS_PATH = re.compile(r"(?<![\w.~$}/-])/[A-Za-z0-9._-]" + TAIL)
HOME_PATH = re.compile(r"(?<![\w.~$/-])(?:~|\$HOME|\$\{HOME\})/" + TAIL)
USER_HOME = re.compile(r"^/(?:home|Users)/[^/]+(?=/|$)")
HOME_VAR = re.compile(r"^(?:~|\$HOME|\$\{HOME\})(?=/|$)")


class ConfigError(Exception):
    pass


def glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob to a regex: ** crosses '/', * and ? do not."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if pattern.startswith("**", i):
            i += 2
            if pattern.startswith("/", i):
                out.append("(?:.*/)?")  # **/ also matches zero directories
                i += 1
            elif i == n and out and out[-1] == "/":
                out[-1] = "(?:/.*)?"  # dir/** also matches dir
            else:
                out.append(".*")
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j < 0:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1 : j].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def normalise_home(path: str) -> str:
    """Rewrite /home/<user>/x, /Users/<user>/x, $HOME/x and ${HOME}/x as ~/x."""
    path = USER_HOME.sub("~", path, count=1)
    return HOME_VAR.sub("~", path, count=1)


@dataclass
class Rule:
    kind: str  # "allowedPaths" or "allowed"
    index: int
    path: str
    reason: str
    src: str | None = None
    optional: bool = False
    path_re: re.Pattern[str] = field(init=False)
    src_re: re.Pattern[str] | None = field(init=False)
    used: bool = False

    def __post_init__(self) -> None:
        self.path_re = glob_regex(self.path)
        self.src_re = glob_regex(self.src) if self.src is not None else None

    def label(self) -> str:
        where = f"src={self.src!r} " if self.src is not None else ""
        return f"{self.kind}[{self.index}] {where}path={self.path!r}"

    def matches(self, src: str, path: str, normalised: str) -> bool:
        if self.src_re is not None and not self.src_re.match(src):
            return False
        return bool(self.path_re.match(path) or self.path_re.match(normalised))


@dataclass
class Config:
    roots: list[str]
    sources: list[str]
    rules: list[Rule]


def _strings(table: dict, key: str, where: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"{where}.{key} must be a list of non-empty strings")
    return value


def _check_keys(table: object, allowed: set[str], where: str) -> dict:
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    return table


def load_config(path: Path) -> Config:
    if not path.exists():
        return Config(list(DEFAULT_ROOTS), [], [])
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    _check_keys(data, {"roots", "sources", "allowedPaths", "allowed"}, CONFIG_NAME)
    roots_t = _check_keys(data.get("roots", {}), {"add"}, "roots")
    sources_t = _check_keys(data.get("sources", {}), {"add"}, "sources")
    roots = list(DEFAULT_ROOTS)
    for root in _strings(roots_t, "add", "roots"):
        if not root.startswith("/"):
            raise ConfigError(f"roots.add: {root!r} is not an absolute directory")
        root = root.rstrip("/") or "/"
        if root not in roots:
            roots.append(root)

    rules: list[Rule] = []
    errors: list[str] = []
    for kind, required in (("allowedPaths", {"path"}), ("allowed", {"src", "path"})):
        entries = data.get(kind, [])
        if not isinstance(entries, list):
            raise ConfigError(f"{kind} must be an array of tables")
        for i, entry in enumerate(entries):
            where = f"{kind}[{i}]"
            _check_keys(entry, required | {"reason", "optional"}, where)
            missing = [k for k in sorted(required | {"reason"}) if k not in entry]
            if missing:
                errors.append(f"{where}: missing {', '.join(missing)}")
                continue
            if not all(isinstance(entry[k], str) and entry[k] for k in required):
                errors.append(
                    f"{where}: {' and '.join(sorted(required))} must be non-empty strings"
                )
                continue
            reason = entry["reason"]
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"{where}: reason must be a non-empty string")
                continue
            if reason.strip().upper().startswith("TODO"):
                errors.append(f"{where}: reason is still TODO; say why this path is portable")
                continue
            optional = entry.get("optional", False)
            if not isinstance(optional, bool):
                errors.append(f"{where}: optional must be true or false")
                continue
            rules.append(Rule(kind, i, entry["path"], reason, entry.get("src"), optional))
    if errors:
        raise ConfigError("\n".join(f"{path.name}: {e}" for e in errors))
    return Config(roots, _strings(sources_t, "add", "sources"), rules)


def under_root(path: str, roots: list[str]) -> bool:
    """True for a path strictly below a flagged root (the root alone is not a finding)."""
    return any(path.startswith(r + "/") and len(path) > len(r) + 1 for r in roots)


def scan_line(line: str, roots: list[str]) -> list[str]:
    found = [m.group(0) for m in ABS_PATH.finditer(line) if under_root(m.group(0), roots)]
    found += [m.group(0) for m in HOME_PATH.finditer(line) if not m.group(0).endswith("/")]
    return found


def literal_prefix(pattern: str) -> str:
    """The leading directories of a glob that contain no wildcard."""
    parts = pattern.split("/")
    prefix = []
    for part in parts[:-1]:
        if any(c in part for c in "*?["):
            break
        prefix.append(part)
    return "/".join(prefix)


def expand_sources(root: Path, patterns: list[str]) -> list[str]:
    """Files under root matching sources.add globs, gitignored or not."""
    found: set[str] = set()
    for pattern in patterns:
        rx = glob_regex(pattern)
        start = root / literal_prefix(pattern)
        if start.is_file():
            rel = start.relative_to(root).as_posix()
            if rx.match(rel):
                found.add(rel)
            continue
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = [d for d in dirnames if d not in (".git", ".jj")]
            for name in filenames:
                full = Path(dirpath, name)
                if full.is_symlink():
                    continue
                rel = full.relative_to(root).as_posix()
                if rx.match(rel):
                    found.add(rel)
    return sorted(found)


@dataclass
class Finding:
    src: str
    line: int
    path: str


def scan(root: Path, files: list[tuple[str, str]], config: Config) -> list[Finding]:
    findings = []
    for cls, rel in files:
        markers = COMMENTS[cls]
        try:
            text = (root / rel).read_text(errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if markers and stripped.startswith(markers):
                continue
            for path in scan_line(line, config.roots):
                normalised = normalise_home(path)
                rule = next(
                    (r for r in config.rules if r.matches(rel, path, normalised)), None
                )
                if rule is not None:
                    rule.used = True
                else:
                    findings.append(Finding(rel, n, path))
    return findings


def proposal(findings: list[Finding]) -> str:
    lines = [
        "# Proposed by gate: one [[allowed]] entry per machine-local path it found.",
        "# Review each one. Prefer fixing the code: track the input in the repo,",
        "# or have a recipe produce it. Keep an entry only when the path is truly",
        "# machine-local by design (a secret, a mounted volume), replace its TODO",
        "# reason, and rename this file to .gate/portability.toml.",
        "",
    ]
    seen = set()
    for f in findings:
        key = (f.src, normalise_home(f.path))
        if key in seen:
            continue
        seen.add(key)
        path = normalise_home(f.path)
        lines += [
            "[[allowed]]",
            f"src = {toml_str(f.src)}",
            f"path = {toml_str(path)}",
            f"reason = {toml_str(f'TODO: why is {path} not an untracked input? (line {f.line})')}",
            "",
        ]
    return "\n".join(lines)


def toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="flag machine-local paths in source files")
    ap.add_argument("--root", type=Path, required=True, help="repo root (holds .gate)")
    ap.add_argument(
        "--files",
        type=Path,
        required=True,
        help="file of '<shebang class>\\t<path>' lines; paths relative to the current directory",
    )
    ap.add_argument(
        "--partial",
        action="store_true",
        help="a subset of the repo: skip the stale-entry check",
    )
    args = ap.parse_args(argv)

    root = args.root.resolve()
    gate_dir = root / ".gate"
    config_path = gate_dir / CONFIG_NAME
    try:
        config = load_config(config_path)
    except ConfigError as e:
        print(f"portability: {e}", file=sys.stderr)
        return 2

    cwd = Path.cwd()
    files: dict[str, str] = {}
    for raw in args.files.read_text().splitlines():
        if not raw.strip():
            continue
        shebang, _, p = raw.partition("\t")
        rel = Path(os.path.relpath(cwd / p, root)).as_posix()
        cls = classify(rel, shebang)
        if cls is not None and not rel.startswith(".."):
            files[rel] = cls
    # sources.add files are scanned whatever their type; '#' comments by default.
    for rel in expand_sources(root, config.sources):
        files.setdefault(rel, classify(rel) or "hash")
    files.pop(f".gate/{CONFIG_NAME}", None)
    files.pop(f".gate/{CONFIG_NAME}.TO_REVIEW", None)

    findings = scan(root, sorted((c, r) for r, c in files.items()), config)
    stale = [] if args.partial else [r for r in config.rules if not r.used and not r.optional]

    for f in findings:
        print(f"{f.src}:{f.line}: machine-local path {f.path}", file=sys.stderr)
    for r in stale:
        print(
            f"{CONFIG_NAME}: {r.label()} matches nothing (remove it, or set optional = true)",
            file=sys.stderr,
        )
    if findings:
        print(
            "portability: committed code must not depend on files outside the repo. Track the "
            "input, have a recipe produce it, or allow the path in .gate/portability.toml "
            "with a reason.",
            file=sys.stderr,
        )
        if not config_path.exists():
            gate_dir.mkdir(exist_ok=True)
            review = gate_dir / f"{CONFIG_NAME}.TO_REVIEW"
            review.write_text(proposal(findings))
            print(
                f"portability: wrote {review.relative_to(root)}; review it, then rename it "
                f"to .gate/{CONFIG_NAME}",
                file=sys.stderr,
            )
    return 1 if findings or stale else 0


if __name__ == "__main__":
    sys.exit(main())
