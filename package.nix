# gate-check.nix package builder for gate: ci formatter and linter
#
# Each tool gate runs is an argument, so a consumer can swap one with
# `.override` (the flake wraps this file in lib.makeOverridable), e.g.
#   gate-check.override { rustfmt = rust-bin.stable.latest.rustfmt; }
{
  pkgs,
  version,
  biome ? pkgs.biome,
  dprint ? pkgs.dprint,
  just ? pkgs.just,
  libxml2 ? pkgs.libxml2, # xmllint
  nixfmt ? pkgs.nixfmt,
  python3 ? pkgs.python3, # portability check
  ruff ? pkgs.ruff,
  # null (default): not bundled; rustfmt comes from the caller's PATH. See gatePath.
  rustfmt ? null,
  shellcheck ? pkgs.shellcheck,
  treefmt ? pkgs.treefmt,
  typos ? pkgs.typos,
  ...
}:

let
  package_name = "gate-check";
  default_line_length = 95;

  jsonFormat = pkgs.formats.json { };
  tomlFormat = pkgs.formats.toml { };

  raw_dprint_json = jsonFormat.generate "dprint.json" dprint_json_src;
  raw_treefmt_toml = tomlFormat.generate "treefmt.toml" treefmt_toml_src;
  raw_biome_json = jsonFormat.generate "biome.json" biome_json_src;
  raw_ruff_toml = tomlFormat.generate "ruff.toml" ruff_toml_src;
  raw_clippy_toml = tomlFormat.generate "clippy.toml" clippy_toml_src;

  shellcheck_bin = "${shellcheck}/bin/shellcheck";
  typos_bin = "${typos}/bin/typos";
  ruff_bin = "${ruff}/bin/ruff";
  sed_bin = "${pkgs.gnused}/bin/sed";
  yq_bin = "${pkgs.yq-go}/bin/yq";
  jq_bin = "${pkgs.jq}/bin/jq";

  # BUILD-TIME ONLY: the dprint config with its plugin tokens already expanded.
  # Never written to a repo. It's needed so the templates below
  # can be formatted by the same dprint they configure.
  buildTimeDprintConfig = pkgs.runCommand "gate-dprint-resolved.json" { } ''
    ${sed_bin} ${pluginSedExpr} ${raw_dprint_json} > $out
  '';

  # The Nix json/toml generators emit valid but non-canonical formatting (long
  # single-line arrays, expanded one-element arrays). Run each template through
  # dprint ONCE here, so a repo's committed ./.gate is already in gate's own
  # canonical style and `gate check` passes on it with no exclude needed.
  #
  # --config-discovery=false is required: the directory being
  # formatted contains a file named dprint.json, which dprint would otherwise
  # discover and prefer over --config.
  gateConfigs = pkgs.runCommand "gate-configs" { nativeBuildInputs = [ dprint ]; } ''
    mkdir -p $out
    cp ${raw_dprint_json}  $out/dprint.json
    cp ${raw_treefmt_toml} $out/treefmt.toml
    cp ${raw_biome_json}   $out/biome.json
    cp ${raw_ruff_toml}    $out/ruff.toml
    cp ${raw_clippy_toml}  $out/clippy.toml
    chmod +w $out/*
    export DPRINT_CACHE_DIR="$TMPDIR/dprint-cache"
    cd $out
    dprint fmt --config ${buildTimeDprintConfig} --config-discovery=false \
      dprint.json treefmt.toml biome.json ruff.toml clippy.toml
  '';

  dprint_json = "${gateConfigs}/dprint.json";
  treefmt_toml = "${gateConfigs}/treefmt.toml";
  biome_json = "${gateConfigs}/biome.json";
  ruff_toml = "${gateConfigs}/ruff.toml";
  clippy_toml = "${gateConfigs}/clippy.toml";

  # dprint plugins, as an ordered list of {name, pkg}.
  # dprint picks the first plugin based on extension.
  # `name` is the logical token key used in the config.
  # `pkg` the current wasm path used at run time.
  dprintPlugins = with pkgs.dprint-plugins; [
    {
      name = "dprint-plugin-biome";
      pkg = dprint-plugin-biome;
    } # js, ts, json, jsonc, json5
    {
      name = "dprint-plugin-dockerfile";
      pkg = dprint-plugin-dockerfile;
    }
    {
      name = "g-plane-malva";
      pkg = g-plane-malva;
    } # css, scss, sass, less
    {
      name = "dprint-plugin-markdown";
      pkg = dprint-plugin-markdown;
    }
    {
      name = "g-plane-markup_fmt";
      pkg = g-plane-markup_fmt;
    } # html, vue, svelte, xml…
    {
      name = "dprint-plugin-ruff";
      pkg = dprint-plugin-ruff;
    } # py
    {
      name = "dprint-plugin-toml";
      pkg = dprint-plugin-toml;
    }
    {
      name = "dprint-plugin-typescript";
      pkg = dprint-plugin-typescript;
    }
    {
      name = "g-plane-pretty_yaml";
      pkg = g-plane-pretty_yaml;
    }
  ];

  # sed clauses that expand each @GATE_PLUGIN:<name>@ token to its wasm path.
  # Store paths contain no '|', so it is a safe delimiter.
  pluginSedExpr = pkgs.lib.concatMapStringsSep " " (
    p: "-e 's|@GATE_PLUGIN:${p.name}@|${p.pkg}/plugin.wasm|g'"
  ) dprintPlugins;

  # Config placeholders substituted at run time with the resolved dprint / biome
  # config paths. Present in the built-in AND any generated treefmt.toml, so
  # per-file config resolution works in all combinations.
  dprintPlaceholder = "@GATE_DPRINT_CONFIG@";
  biomePlaceholder = "@GATE_BIOME_CONFIG@";
  ruffPlaceholder = "@GATE_RUFF_CONFIG@";

  # Cache-key version: changes whenever the tool/plugin closure or the built-in
  # templates change (nixpkgs bump), invalidating stale compiled caches.
  cfgVersion = builtins.substring 0 16 (
    builtins.hashString "sha256" (
      "${version}"
      + pluginSedExpr
      + "|"
      + "${treefmt_toml}"
      + "|"
      + "${dprint_json}"
      + "|"
      + "${biome_json}"
      + "|"
      + "${ruff_toml}"
      + "|"
      + "${clippy_toml}"
    )
  );

  # Tools gate puts on its OWN PATH before invoking treefmt / stdin formatters,
  # so the bare command names in treefmt.toml resolve to gate's pinned versions
  # regardless of the caller's PATH. Includes the coreutils/fd plumbing the
  # script's bare calls need.
  gatePath = pkgs.lib.makeBinPath (
    [
      biome
      pkgs.coreutils
      pkgs.diffutils
      dprint
      pkgs.fd
      pkgs.findutils
      pkgs.gnugrep
      pkgs.gnused
      just
      libxml2
      nixfmt
      ruff
      shellcheck
      treefmt
      typos
    ]
    # rustfmt is not bundled by default: it links librustc_driver, dragging
    # rustc + llvm (~1.7 GB) into the closure. Unless overridden, it comes from
    # the caller's Rust toolchain, the same way `cargo clippy` does, so it also
    # matches the project's toolchain.
    ++ pkgs.lib.optional (rustfmt != null) rustfmt
  );

  # dprint settings (formerly nix/dprint.nix, inlined). .json/.jsonc/.json5 are
  # handled by the biome plugin (it precedes the others), matching helix.nix.
  # Plugins are logical-name tokens; gate expands them at run time.
  dprint_json_src = {
    useTabs = false;
    indentWidth = 2;

    biome = {
      indentStyle = "space";
      indentWidth = 2;
      lineWidth = default_line_length;
    };
    dockerfile = { };
    markdown = {
      textWrap = "maintain";
      emphasisKind = "asterisks";
      strongKind = "asterisks";
      lineWidth = default_line_length;
    };
    # markup_fmt handles html/htm (and vue/svelte/xml). Its width key is
    # printWidth; `lineWidth` is rejected by the plugin.
    markup = {
      printWidth = default_line_length;
    };
    # The ruff plugin's width key is lineLength, NOT lineWidth: an unknown
    # property fails plugin init outright ("Error initializing from
    # configuration file"), silently disabling it.
    ruff = {
      quoteStyle = "double";
      lineLength = default_line_length;
    };
    toml = {
      lineWidth = default_line_length;
    };
    typescript = { };
    yaml = {
      associations = [ ".yml" ];
      bracketSpacing = true;
    };

    plugins = map (p: "@GATE_PLUGIN:${p.name}@") dprintPlugins;

    excludes = [
      "*.bak"
      "*.tmp"
      "*backup*gz"
      ".*[_-]history"
      ".env"
      ".envrc"
      ".direnv"
      ".DS_Store"
      "target/"
      "dist/"
      "node_modules/"
      ".venv"
      "*.py[cod]"
      "__pycache__"
      ".zcompcache*"
      ".zcompdump*"
      "zig-bin"
      "zig-cache"
      ".vscode"
      ".idea/"
      "*.mp[34]"
      ".m4b"
      ".mkv"
      "*.tiff"
      "*.avi"
      "*.flv"
      "*.mov"
      "*.wmv"
      "nohup.out"
      ".ssh/"
      ".gnupg/"
      ".*.sw[pon]"
      ".*cache"
      "*.log"
      "*.rdb"
      "*.db"
      "**/*-lock.json"
      "**/flake.lock"
    ];
  };

  # treefmt.toml formatter map. Commands are bare names resolved via gate's
  # injected PATH (avoiding store paths). dprint/biome config paths are placeholders
  # resolved at run time. Linters are in the gate script.
  treefmt_toml_src = {
    on-unmatched = "debug";
    excludes = [
      "**/*-lock.json"
      "*-lock.json"
      "*.lock"
      "**/*.lock"
    ];

    formatter.nixfmt = {
      command = "nixfmt";
      includes = [ "*.nix" ];
    };

    formatter.rustfmt = {
      command = "rustfmt";
      options = [
        "--edition"
        "2024"
      ];
      includes = [ "*.rs" ];
    };

    formatter.dprint = {
      command = "dprint";
      options = [
        "fmt"
        "--config"
        dprintPlaceholder
        # use only explicit config. Without this setting,
        # any found dprint.json would override --config.
        "--config-discovery=ignore-descendants"
        # gate picks the file set itself (fd, see candidates()) and passes it
        # explicitly. dprint's own .gitignore filtering must stay off: with the
        # config outside the repo, dprint scans .gitignore files from / down,
        # so a parent .gitignore (e.g. ~/.gitignore listing `project`) silently
        # drops every file in fmt, while check's /tmp mirror is unaffected.
        "--no-gitignore"
      ];
      includes = [
        "*.json"
        "*.jsonc"
        "*.json5"
        "*.md"
        "*.markdown"
        "*.toml"
        "*.yaml"
        "*.yml"
        "*.kdl"
        # html/htm go to dprint's g-plane-markup_fmt, NOT biome: biome 2.x's
        # html formatter is not idempotent (it rewraps prose, then the new line
        # lengths push inline <code> spans onto their own lines on the next
        # pass), so a formatted tree never reaches a fixed point and `check`
        # reports files `fmt` has already written. markup_fmt converges in one
        # pass and leaves <pre> content byte-identical.
        "*.html"
        "*.htm"
      ];
    };

    formatter.biome = {
      command = "biome";
      options = [
        "format"
        "--write"
        "--config-path=${biomePlaceholder}"
      ];
      includes = [
        "*.css"
        "*.graphql"
        "*.gql"
        "*.js"
        "*.mjs"
        "*.cjs"
        "*.jsx"
        "*.ts"
        "*.mts"
        "*.cts"
        "*.tsx"
        "*.vue"
      ];
    };

    formatter.ruff = {
      command = "ruff";
      options = [
        "format"
        "--config"
        ruffPlaceholder
      ];
      includes = [
        "*.py"
        "*.pyi"
      ];
    };

    formatter.just = {
      command = "just";
      options = [
        "--unstable"
        "--fmt"
        "-f"
      ];
      includes = [
        "justfile"
        ".justfile"
        "*.just"
      ];
    };
  };

  # ruff settings for `ruff format` AND `ruff check`. Passed by absolute path via
  # --config, which disables ruff's internal config discovery
  # (including pyproject.toml settings).
  # This is needed to make config discovery independent of the process cwd,
  # so editor (e.g. helix) format-on-save uses formatting consistent
  # with the project format rules.
  ruff_toml_src = {
    line-length = default_line_length;
    format = {
      # Accept either quote style rather than rewriting everything to double.
      quote-style = "preserve";
    };
  };

  # `cargo clippy` invocations for lint. Each [[run]] is one invocation and
  # `args` follow `cargo clippy`. The default lints every target and feature;
  # a workspace whose features are mutually exclusive (for example per-platform
  # dioxus features) lists the feature sets it really builds instead.
  # $GATE_CLIPPY_ARGS, when set, replaces the file with a single run.
  clippy_toml_src = {
    run = [
      {
        args = [
          "--all-targets"
          "--all-features"
          "--"
          "-D"
          "warnings"
        ];
      }
    ];
  };

  biome_json_src = {
    "$schema" = "https://biomejs.dev/schemas/2.4.15/schema.json";
    plugins = [ ];
    # No VCS integration: biome resolves its VCS root to the folder holding this
    # config (./.gate, or the nix store path for the built-in), and with
    # useIgnoreFile it hard-errors there with "couldn't find an ignore file".
    # gate does its own gitignore-aware enumeration (fd, see candidates()) and
    # hands treefmt explicit paths in a non-git mirror tree, so biome has nothing
    # to discover anyway.
    vcs.enabled = false;
    files.ignoreUnknown = false;
    files.maxSize = 1024 * 1024 * 5; # 5 MiB
    formatter.enabled = true;
    formatter.indentStyle = "space";
    linter.enabled = true;
    linter.rules.recommended = true;
    javascript.formatter.quoteStyle = "double";
    json.formatter.enabled = true;
    json.formatter.indentStyle = "space";
    css.formatter.enabled = true;
    css.formatter.indentStyle = "tab";
    # Off deliberately: html/htm are routed to dprint's markup_fmt because
    # biome's html formatter is not idempotent (see the dprint includes above).
    html.formatter.enabled = false;
    assist.enabled = true;
    assist.actions.source.organizeImports = "on";
  };

  gate_script_src = ''
    #!/usr/bin/env bash
    set -uo pipefail
    export PATH="${gatePath}:$PATH"

    # gate - ci formatter and linter
    #
    #   gate fmt    [-d DIR] [paths...]   format in place (default: whole repo)
    #   gate check  [-d DIR] [paths...]   check formatting + lint (read-only)
    #   gate lint   [-d DIR] [paths...]   linters only
    #   gate format ...                   alias for fmt
    #   gate pipe   [-d DIR] <name>       stdin -> stdout; <name> picks formatter
    #   gate gen    [-f] [-d DIR]         write portable default configs to ./.gate
    #
    # At run time (fmt | check | pipe) gate compiles the resolved config into an
    # "effective" config under a per-(config,gate-version) CACHE dir, so repeated
    # invocations reuse it instead of regenerating.
    #
    # Config resolution (check | fmt | lint | pipe):
    #   The effective dprint.json, treefmt.toml, biome.json, ruff.toml and
    #   clippy.toml are each resolved independently, in this order:
    #    1. -d/--dir DIR         (must exist, else error)
    #    2. $GATE_CONFIG         (must exist, else error)
    #    3. nearest ./.gate walking up from $PWD
    #    4. $XDG_CONFIG_HOME/gate
    #    5. the built-in defaults baked into this gate
    # A file absent from the chosen dir falls back to the built-in default.
    #
    # clippy (check | lint):
    #   clippy.toml lists the `cargo clippy` invocations; each [[run]] has an
    #   `args` array of the arguments after `cargo clippy`:
    #     [[run]]
    #     args = ["--workspace", "--all-targets", "--", "-D", "warnings"]
    #   $GATE_CLIPPY_ARGS (whitespace-separated, no quoting) replaces clippy.toml
    #   with one run. The built-in default is
    #   `--all-targets --all-features -- -D warnings`.
    #
    # portability (check | lint):
    #   Flags source files that name machine-local paths: an absolute path below
    #   /tmp, /home, /Users, /mnt or /var/tmp, or a ~/, $HOME/ or ''${HOME}/ path.
    #   Helps catch portability problems (tied to specific environment), and references
    #   to files that might not be committed to the repo.
    #   The check is textual only. Exceptions, extra roots and extra sources are configured
    #   in the repo's .gate/portability.toml:
    #     # add path roots to be flagged
    #     [roots]
    #     add = ["/media"]
    #     # add source files to be checked (override .gitignore)
    #     [sources]
    #     add = ["templates/*.tmpl"]
    #     # paths that should not be flagged
    #     [[allowedPaths]]
    #     path = "~/.secrets/**"
    #     reason = "secrets never live in the repo"
    #     # allowed src/path combinations
    #     [[allowed]]
    #     src = "test/stress/*.py"
    #     path = "/mnt/scratch/**"
    #     reason = "stress tests use the scratch volume"
    #     optional = true           # no error when it matches nothing
    # 
    #   Every allowed or allowedPaths entry needs a reason. In a whole-repo run,
    #   an entry that fails a match generates an error (to prevent stale rules
    #   from accumulating). Set `optional=true` to all a rule with no matches.
    # 
    #   If a (suspected) portability error is found, and no .gate/porability.toml
    #   exists, a draft .gate/portability.toml.TO_REVIEW is created.
    #   Review, edit, and rename to .gate/portability.toml.
    # 
    # Status (fmt | check | lint):
    #   Each run ends with exactly one status line on stderr, and it is the only
    #   verdict worth reading:
    #     gate: check ok                                   (green, exit 0)
    #     gate: check FAILED: formatting (3 files), typos  (red,   exit 1)
    #   Green is printed only when EVERY step passed. Per-tool success chatter is
    #   suppressed where the tool allows it so no sub-command can flash green while
    #   another is failing. Colour is disabled when stderr is not a tty or when
    #   $NO_COLOR is set.
    #
    # Performance:
    #   The resolved config is COMPILED once (plugin tokens + config paths expanded)
    #   into a per-(config,gate)-version cache dir and reused across runs.
    #   `pipe` bypasses treefmt entirely, dispatching to each formatter's stdin mode.

    BUILTIN_DPRINT="${dprint_json}"
    BUILTIN_TREEFMT="${treefmt_toml}"
    BUILTIN_BIOME="${biome_json}"
    BUILTIN_RUFF="${ruff_toml}"
    BUILTIN_CLIPPY="${clippy_toml}"
    TREEFMT_BIN="${treefmt}/bin/treefmt"
    CFG_VERSION="${cfgVersion}"

    # ---- status reporting ------------------------------------------------------
    # Every sub-tool reports success differently: ruff prints a green
    # "All checks passed!", treefmt prints a summary line, shellcheck/xmllint/typos
    # print nothing. Left alone one tool's green success outranks another tool's
    # plain-text failure list, so a run with formatting errors still LOOKS green.
    # gate therefore silences per-tool success chatter where the tool allows it
    # (ruff -q, treefmt --quiet in check) and always ends with exactly ONE status
    # line: red when any step failed, green only when every step passed.
    if [ -t 2 ] && [ -z "''${NO_COLOR:-}" ]; then
      C_RED=$'\033[1;31m'; C_GREEN=$'\033[1;32m'; C_OFF=$'\033[0m'
    else
      C_RED=""; C_GREEN=""; C_OFF=""
    fi

    die()  { printf '%sgate: %s%s\n' "$C_RED" "$*" "$C_OFF" >&2; exit 2; }
    note() { printf 'gate: %s\n' "$*" >&2; }
    warn() { printf '%sgate: %s%s\n' "$C_RED" "$*" "$C_OFF" >&2; }
    have() { command -v "$1" >/dev/null 2>&1; }

    # Steps that failed this run, named for the final status line.
    GATE_FAILED=()
    fail() { GATE_FAILED+=("$1"); }

    plural() {   # <count> <noun> -> "1 file" | "3 files"
      if [ "$1" -eq 1 ]; then printf '%s %s' "$1" "$2"; else printf '%s %ss' "$1" "$2"; fi
    }

    # Single exit point for fmt | check | lint. Never prints green unless
    # GATE_FAILED is empty, so a green line always means the whole run is clean.
    finish() {   # <command label>
      local joined
      if [ "''${#GATE_FAILED[@]}" -eq 0 ]; then
        printf '%sgate: %s ok%s\n' "$C_GREEN" "$1" "$C_OFF" >&2
        exit 0
      fi
      joined="$(printf '%s, ' "''${GATE_FAILED[@]}")"
      printf '%sgate: %s FAILED: %s%s\n' "$C_RED" "$1" "''${joined%, }" "$C_OFF" >&2
      exit 1
    }

    # Print the contiguous leading help comment, stripping '# '.
    usage() {
      ${sed_bin} -n '/^# gate /,$ { /^#/!q; s/^# \?//; p }' "$0"
    }

    # Single EXIT trap over a list of temp dirs shared by all helpers.
    GATE_TMPDIRS=()
    gate_cleanup() { local d; for d in "''${GATE_TMPDIRS[@]:-}"; do [ -n "$d" ] && rm -rf "$d"; done; }
    trap gate_cleanup EXIT
    mk_tmpd() { local d; d="$(mktemp -d)" || die "mktemp failed"; GATE_TMPDIRS+=("$d"); printf '%s\n' "$d"; }

    # ---- option parsing --------------------------------------------------------
    DIR_OPT=""; FORCE=0; POS=()
    parse_opts() {
      DIR_OPT=""; FORCE=0; POS=()
      while [ "$#" -gt 0 ]; do
        case "$1" in
          -d|--dir) shift; [ "$#" -gt 0 ] || die "-d/--dir requires an argument"; DIR_OPT="$1" ;;
          --dir=*)  DIR_OPT="''${1#--dir=}" ;;
          -f|--force) FORCE=1 ;;
          --) shift; while [ "$#" -gt 0 ]; do POS+=("$1"); shift; done; break ;;
          -*) die "unknown option: $1" ;;
          *)  POS+=("$1") ;;
        esac
        shift
      done
    }

    # ---- config resolution -----------------------------------------------------
    resolve_cfg_dir() {
      if [ -n "$DIR_OPT" ]; then
        [ -d "$DIR_OPT" ] || die "config dir not found: $DIR_OPT"
        printf '%s\n' "$DIR_OPT"; return
      fi
      if [ -n "''${GATE_CONFIG:-}" ]; then
        [ -d "$GATE_CONFIG" ] || die "GATE_CONFIG dir not found: $GATE_CONFIG"
        printf '%s\n' "$GATE_CONFIG"; return
      fi
      local d="$PWD"
      while [ -n "$d" ]; do
        [ -d "$d/.gate" ] && { printf '%s\n' "$d/.gate"; return; }
        [ "$d" = "/" ] && break
        d="$(dirname "$d")"
      done
      local xdg="''${XDG_CONFIG_HOME:-$HOME/.config}/gate"
      [ -d "$xdg" ] && { printf '%s\n' "$xdg"; return; }
      printf '%s\n' ""
    }

    resolve_file() {   # <cfgdir> <basename> <builtin>
      local dir="$1" name="$2" builtin="$3"
      if [ -n "$dir" ] && [ -f "$dir/$name" ]; then printf '%s\n' "$dir/$name"
      else printf '%s\n' "$builtin"; fi
    }

    # Resolve + COMPILE the effective config into a per-(config,gate)-version
    # cache dir (plugin tokens expanded in dprint.json; config paths substituted
    # in treefmt.toml). Idempotent: a warm cache is reused with no work. Sets
    # EFFECTIVE_TREEFMT, EFFECTIVE_DPRINT, EFFECTIVE_BIOME.
    EFFECTIVE_TREEFMT=""; EFFECTIVE_DPRINT=""; EFFECTIVE_BIOME=""; EFFECTIVE_RUFF=""
    EFFECTIVE_CLIPPY=""
    compile_config() {
      local cfgdir dprint biome ruff treefmt keyinput key root cachedir tmp leftover f
      cfgdir="$(resolve_cfg_dir)" || exit 2
      dprint="$(resolve_file  "$cfgdir" dprint.json  "$BUILTIN_DPRINT")"
      biome="$(resolve_file   "$cfgdir" biome.json   "$BUILTIN_BIOME")"
      ruff="$(resolve_file    "$cfgdir" ruff.toml    "$BUILTIN_RUFF")"
      treefmt="$(resolve_file "$cfgdir" treefmt.toml "$BUILTIN_TREEFMT")"
      # Like ruff.toml, clippy.toml is read as-is and never compiled.
      EFFECTIVE_CLIPPY="$(resolve_file "$cfgdir" clippy.toml "$BUILTIN_CLIPPY")"

      # Key on the gate config version and the resolved sources; for non-store
      # (repo/user) sources add the mtime so edits invalidate the cache. Use
      # nanosecond mtime (%y) so two edits within the same
      # second (has happened with format-on-save) get distinct keys.
      keyinput="v=$CFG_VERSION|d=$dprint|b=$biome|r=$ruff|t=$treefmt"
      for f in "$dprint" "$biome" "$ruff" "$treefmt"; do
        case "$f" in /nix/store/*) ;; *) keyinput="$keyinput|m:$f=$(stat -c '%y' "$f" 2>/dev/null || echo 0)" ;; esac
      done
      key="$(printf '%s' "$keyinput" | sha256sum | cut -c1-32)"
      root="''${XDG_RUNTIME_DIR:-''${XDG_CACHE_HOME:-$HOME/.cache}}/gate"
      cachedir="$root/$key"
      EFFECTIVE_TREEFMT="$cachedir/treefmt.toml"
      EFFECTIVE_DPRINT="$cachedir/dprint.json"
      EFFECTIVE_BIOME="$biome"
      # Like biome.json, ruff.toml carries no tokens, so the resolved source is
      # used as-is rather than compiled into the cache dir.
      EFFECTIVE_RUFF="$ruff"

      # Warm cache: nothing to do (the fast path for format-on-save).
      [ -f "$EFFECTIVE_TREEFMT" ] && [ -f "$EFFECTIVE_DPRINT" ] && return 0

      mkdir -p "$root" || die "cannot create cache root $root"
      tmp="$cachedir.tmp.$$"; rm -rf "$tmp"; mkdir -p "$tmp" || die "cannot create $tmp"

      # Expand dprint plugin tokens; a surviving token is an unknown plugin.
      ${sed_bin} ${pluginSedExpr} "$dprint" > "$tmp/dprint.json" || { rm -rf "$tmp"; die "compiling dprint.json failed"; }
      leftover="$(grep -o '@GATE_PLUGIN:[^@]*@' "$tmp/dprint.json" | sort -u | tr '\n' ' ')"
      [ -z "$leftover" ] || { rm -rf "$tmp"; die "unknown dprint plugin token(s): ''${leftover}(not provided by this gate)"; }

      # Point treefmt.toml at the compiled dprint.json (final cache path) and the
      # resolved biome.json / ruff.toml.
      ${sed_bin} \
        -e "s|${dprintPlaceholder}|$cachedir/dprint.json|g" \
        -e "s|${biomePlaceholder}|$biome|g" \
        -e "s|${ruffPlaceholder}|$ruff|g" \
        "$treefmt" > "$tmp/treefmt.toml" || { rm -rf "$tmp"; die "compiling treefmt.toml failed"; }

      # Publish atomically; a concurrent compile produces identical bytes, so if
      # the rename loses the race the existing dir is equally valid.
      if ! mv -T "$tmp" "$cachedir" 2>/dev/null; then
        rm -rf "$tmp"
        [ -f "$EFFECTIVE_TREEFMT" ] || die "failed to publish cache $cachedir"
      fi
      note "compiled config -> $cachedir (from ''${cfgdir:-built-in defaults})"
    }

    # ---- shared treefmt invocation ---------------------------------------------
    # fmt and check both hand treefmt an EXPLICIT file list (from candidates())
    # instead of letting it walk. treefmt's walker defaults to `auto`, which picks
    # the git/jujutsu walker in a repo but the filesystem walker in check's
    # non-git mirror — two different file sets for what is meant to be the same
    # question. Explicit paths bypass the walker in both cases while config
    # `excludes` still apply, so fmt and check agree by construction.
    #
    # --no-cache in BOTH for the same reason: treefmt's eval cache keys on
    # size+mtime and is stamped right after a format, so any formatter that is not
    # idempotent leaves fmt reporting "0 files changed" on files check still
    # flags, with no way to converge. check was already --no-cache; fmt was not.
    #
    # The list is piped through xargs so a tree larger than ARG_MAX still works.
    treefmt_run() {   # <listfile> <root> [extra flags...]
      local list="$1" root="$2"; shift 2
      [ -s "$list" ] || return 0
      xargs -a "$list" -d '\n' -r \
        "$TREEFMT_BIN" --config-file "$EFFECTIVE_TREEFMT" --allow-missing-formatter \
                       --tree-root "$root" -C "$root" --no-cache "$@" --
    }

    # ---- gen -------------------------------------------------------------------
    gen_one() {   # <src> <dst>
      local src="$1" dst="$2"
      if [ -e "$dst" ] && [ "$FORCE" != 1 ]; then
        note "refusing to overwrite $dst (use -f/--force)"; return 1
      fi
      cp -- "$src" "$dst" && chmod 0644 "$dst" || die "failed writing $dst"
      note "wrote $dst"
    }
    gen() {
      local outdir="''${DIR_OPT:-./.gate}" rc=0
      mkdir -p "$outdir" || die "cannot create $outdir"
      gen_one "$BUILTIN_DPRINT"  "$outdir/dprint.json"  || rc=1
      gen_one "$BUILTIN_TREEFMT" "$outdir/treefmt.toml" || rc=1
      gen_one "$BUILTIN_BIOME"   "$outdir/biome.json"   || rc=1
      gen_one "$BUILTIN_RUFF"    "$outdir/ruff.toml"    || rc=1
      gen_one "$BUILTIN_CLIPPY"  "$outdir/clippy.toml"  || rc=1
      [ "$rc" = 0 ] && note "generated portable gate config in $outdir (no store paths; refresh-free)"
      return "$rc"
    }

    # ---- pipe (helix formatter contract, treefmt-free) -------------------------
    # The buffer arrives on stdin and stdout REPLACES it. Dispatch to the
    # matching formatter's stdin mode by extension. Skips treefmt startup
    # and config scan. If format fails, echoes the original buffer
    # with a non-zero exit so the buffer is never lost.
    # Unmatched extensions pass through verbatim.
    pipe() {   # <buffer_name>
      [ "$#" -eq 1 ] || die "pipe: expected exactly one buffer name"
      local base tmpd; base="''${1##*/}"
      [ -n "$base" ] || die "pipe: empty buffer name"
      local -a fmt=()
      case "$base" in
        *.json|*.jsonc|*.json5|*.md|*.markdown|*.toml|*.yaml|*.yml|*.kdl|*.html|*.htm)
          fmt=(dprint fmt --config "$EFFECTIVE_DPRINT" --config-discovery=ignore-descendants --stdin "$base") ;;
        *.css|*.graphql|*.gql|*.js|*.mjs|*.cjs|*.jsx|*.ts|*.mts|*.cts|*.tsx|*.vue)
          fmt=(biome format "--config-path=$EFFECTIVE_BIOME" "--stdin-file-path=$base") ;;
        *.rs)       have rustfmt || warn "rustfmt not on PATH; buffer not formatted"
                    fmt=(rustfmt --edition 2024) ;;
        *.nix)      fmt=(nixfmt) ;;
        *.py|*.pyi) fmt=(ruff format --config "$EFFECTIVE_RUFF" --stdin-filename "$base" -) ;;
      esac
      if [ "''${#fmt[@]}" -eq 0 ]; then cat; return 0; fi   # passthrough
      tmpd="$(mk_tmpd)"; cat > "$tmpd/buf"
      if "''${fmt[@]}" < "$tmpd/buf" > "$tmpd/out" 2>/dev/null; then cat "$tmpd/out"; else cat "$tmpd/buf"; return 1; fi
    }

    # ---- candidate enumeration -------------------------------------------------
    # ONE enumerator, shared by fmt, check and lint, so all three agree on the
    # file set. fd replaces the old `git ls-files` / `find` split because it:
    #   - honours .gitignore/.ignore/.fdignore with no git dependency, so it
    #     behaves identically in a repo and in check's mirror,
    #   - includes dotfiles (--hidden) the way `git ls-files -co` did,
    #   - emits regular files only (--type f), so a symlink is never followed by
    #     fmt or by check. treefmt's git walker used to hand symlinks to fmt while
    #     check skipped them explicitly.
    # Paths are relative to the search root, matching treefmt's -C.
    #
    # Difference from the old `git ls-files -co --exclude-standard`: a file that is
    # BOTH tracked and gitignored was listed by git and is skipped by fd. Ignored
    # now means ignored, whatever its index state.
    FD_ARGS=(--type f --hidden --no-require-git --exclude .git --exclude .jj)
    candidates() {   # [paths...]
      if [ "$#" -eq 0 ]; then
        fd "''${FD_ARGS[@]}" .
        return
      fi
      local p
      for p in "$@"; do
        if   [ -L "$p" ]; then continue                      # never follow symlinks
        elif [ -d "$p" ]; then fd "''${FD_ARGS[@]}" . "$p"
        elif [ -f "$p" ]; then printf '%s\n' "$p"
        fi
      done
    }

    # ---- in-place formatting ---------------------------------------------------
    # Named fmt_tree, not fmt: pipe() already uses a local array named `fmt`.
    fmt_tree() {   # [paths...]
      local work list
      work="$(mk_tmpd)"; list="$work/files"
      candidates "$@" > "$list"
      warn_no_rustfmt "$list"
      treefmt_run "$list" "$PWD"
    }

    # rustfmt is not bundled by default (see gatePath); treefmt runs with
    # --allow-missing-formatter, so without this note .rs files would be
    # skipped silently.
    warn_no_rustfmt() {   # <listfile>
      if ! have rustfmt && grep -q '\.rs$' "$1"; then
        warn "rustfmt not on PATH; .rs files not formatted"
      fi
    }

    # ---- read-only formatting check --------------------------------------------
    # treefmt has no dry-run, so mirror candidates into a temp tree, format the
    # mirror, and compare. Work tree is unmodified. The mirror is populated from
    # the SAME candidates() list fmt uses, and treefmt is given that list
    # explicitly, so check can only disagree with fmt about a file's contents,
    # never about whether the file is in scope.
    check_fmt() {   # [paths...]
      local rc=0 f work list mirror
      local -a changed
      work="$(mk_tmpd)"; list="$work/files"
      candidates "$@" > "$list"
      [ -s "$list" ] || return 0
      warn_no_rustfmt "$list"
      mirror="$(mk_tmpd)"
      while IFS= read -r f; do
        case $f in */*) mkdir -p "$mirror/''${f%/*}" ;; esac
        cp -p -- "$f" "$mirror/$f"
      done < "$list"
      # Formatter configs the mirror needs because their tools discover them
      # relative to the tree being formatted. dprint/biome/ruff get configs
      # via --config with absolute path outside the mirror.
      for f in rustfmt.toml .rustfmt.toml .editorconfig; do
        if [ -f "$f" ] && [ ! -e "$mirror/$f" ]; then cp -p -- "$f" "$mirror/$f"; fi
      done
      # --quiet here, not in fmt: treefmt's own summary reads "formatted 402 files
      # (34 changed)", which is actively wrong for a read-only check (it formatted
      # the MIRROR). gate prints the authoritative list below. --quiet still lets
      # real formatter errors through.
      treefmt_run "$list" "$mirror" --quiet >&2 || { rc=1; fail "formatter error"; }
      changed=()
      while IFS= read -r f; do
        [ -f "$mirror/$f" ] || continue
        cmp -s -- "$f" "$mirror/$f" || changed+=("$f")
      done < "$list"
      if [ "''${#changed[@]}" -gt 0 ]; then
        warn "needs formatting (''${#changed[@]}):"
        printf '  %s\n' "''${changed[@]}" >&2
        fail "formatting ($(plural "''${#changed[@]}" file))"
        rc=1
      fi
      return "$rc"
    }

    # Classify an extensionless file by shebang: prints "sh" | "py" | "".
    # A nix-shell shebang names nix-shell, not the real interpreter -- that
    # lives on a following '#! nix-shell -i INTERP' line -- so look there.
    shebang_class() {
      local first interp line n=0
      IFS= read -rn 256 first < "$1" 2>/dev/null
      [[ $first == '#!'* ]] || return 0
      LC_ALL=C grep -qIe '^#!' -- "$1" 2>/dev/null || return 0   # skip binaries
      interp=$first
      if [[ $first =~ (^|/|[[:space:]])nix-shell([[:space:]]|$) ]]; then
        interp=""
        while IFS= read -r line && [ "$((n += 1))" -le 20 ]; do
          [[ $line == '#!'*nix-shell* ]] || continue
          if [[ $line =~ [[:space:]]-i[[:space:]]+([^[:space:]]+) ]]; then
            interp=''${BASH_REMATCH[1]}
            break
          fi
        done < "$1"
      fi
      if   [[ $interp =~ (^|/|[[:space:]])(bash|sh|dash)([[:space:]]|$) ]]; then echo sh
      elif [[ $interp =~ (^|/|[[:space:]])python[0-9.]*([[:space:]]|$) ]]; then echo py
      fi
    }

    # ---- portability -----------------------------------------------------------
    # The repo root: the nearest directory holding .gate, .git or .jj.
    repo_root() {
      local d="$PWD"
      while :; do
        if [ -d "$d/.gate" ] || [ -e "$d/.git" ] || [ -d "$d/.jj" ]; then
          printf '%s\n' "$d"; return
        fi
        [ "$d" = "/" ] && break
        d="$(dirname "$d")"
      done
      printf '%s\n' "$PWD"
    }

    # Stale allow entries are reported only when the whole repo was scanned.
    portability() {   # <listfile> [paths...]
      local list="$1" root; shift
      local -a partial=()
      root="$(repo_root)"
      if [ "$#" -gt 0 ] || [ "$PWD" != "$root" ]; then partial=(--partial); fi
      note "portability"
      "${python3}/bin/python3" -I ${./portability.py} \
        --root "$root" --files "$list" "''${partial[@]}" || { fail portability; return 1; }
    }

    # Run every configured `cargo clippy` invocation (see "clippy" in the help).
    # Each run's args are read as a NUL-separated list, so arguments containing
    # spaces survive. A malformed clippy.toml fails the step rather than
    # silently falling back to the default.
    clippy_runs() {
      local -a args
      local n i json rc=0
      if [ -n "''${GATE_CLIPPY_ARGS:-}" ]; then
        read -r -a args <<< "$GATE_CLIPPY_ARGS"
        note "cargo clippy ''${args[*]} (GATE_CLIPPY_ARGS)"
        cargo clippy "''${args[@]}" || { fail clippy; return 1; }
        return 0
      fi
      json="$("${yq_bin}" -p toml -o json '.' "$EFFECTIVE_CLIPPY")" \
        || { warn "cannot parse $EFFECTIVE_CLIPPY"; fail clippy; return 1; }
      n="$(printf '%s' "$json" | "${jq_bin}" -e '
        if (.run | type) == "array" and (.run | length) > 0
           and all(.run[]; (.args | type) == "array" and all(.args[]; type == "string"))
        then .run | length else error("bad clippy.toml") end')" \
        || { warn "invalid $EFFECTIVE_CLIPPY: expected [[run]] tables with args = [strings]"; fail clippy; return 1; }
      for ((i = 0; i < n; i++)); do
        mapfile -d "" -t args < <(printf '%s' "$json" | "${jq_bin}" -j --argjson i "$i" '.run[$i].args[] | ., "\u0000"')
        note "cargo clippy ''${args[*]}"
        cargo clippy "''${args[@]}" || rc=1
      done
      [ "$rc" -eq 0 ] || fail clippy
      return "$rc"
    }

    lint() {   # [paths...]
      local rc=0 f sb port_list
      local -a cand sh_files py_files xml_files rs_files
      mapfile -t cand < <(candidates "$@")
      sh_files=(); py_files=(); xml_files=(); rs_files=()
      port_list="$(mk_tmpd)/portability"
      : > "$port_list"

      for f in "''${cand[@]}"; do
        sb=""
        [[ ''${f##*/} != *.* ]] && sb="$(shebang_class "$f")"
        # portability.py picks the files it scans; it needs the shebang class
        # only for extensionless scripts.
        printf '%s\t%s\n' "$sb" "$f" >> "$port_list"
        case $f in
          *.sh|*.bash)  sh_files+=("$f");  continue ;;
          *.py|*.pyi)   py_files+=("$f");  continue ;;
          *.xml|*.svg)  xml_files+=("$f"); continue ;;
          *.rs)         rs_files+=("$f");  continue ;;
        esac
        case $sb in
          sh) sh_files+=("$f") ;;
          py) py_files+=("$f") ;;
        esac
      done

      if [ "''${#sh_files[@]}" -gt 0 ]; then
        note "shellcheck (''${#sh_files[@]})"
        "${shellcheck_bin}" "''${sh_files[@]}" || { rc=1; fail shellcheck; }
      fi

      if [ "''${#py_files[@]}" -gt 0 ] && have ruff; then
        note "ruff check (''${#py_files[@]})"
        # -q prints diagnostics but suppresses ruff's own green "All checks
        # passed!", which otherwise reads as an overall pass next to another
        # step's failures. gate's final status line is the verdict.
        "${ruff_bin}" check -q --config "$EFFECTIVE_RUFF" "''${py_files[@]}" || { rc=1; fail "ruff check"; }
      fi

      if [ "''${#xml_files[@]}" -gt 0 ] && have xmllint; then
        note "xmllint (''${#xml_files[@]})"
        "${libxml2}/bin/xmllint" --noout "''${xml_files[@]}" || { rc=1; fail xmllint; }
      fi

      note "typos"
      if [ "$#" -gt 0 ]; then ${typos_bin} "$@" || { rc=1; fail typos; }
      else ${typos_bin} || { rc=1; fail typos; }; fi

      portability "$port_list" "$@" || rc=1

      if [ "''${#rs_files[@]}" -gt 0 ] || { [ "$#" -eq 0 ] && [ -f Cargo.toml ]; }; then
        if have cargo; then
          clippy_runs || rc=1
        else
          note "clippy skipped (no cargo on PATH)"
        fi
      fi

      return "$rc"
    }

    # ---- dispatch --------------------------------------------------------------
    cmd="''${1:-}"; [ "$#" -gt 0 ] && shift
    case "$cmd" in
      fmt|format)
        parse_opts "$@"; compile_config
        fmt_tree "''${POS[@]}" || fail "formatter error"
        finish fmt
        ;;
      pipe)
        parse_opts "$@"; compile_config
        # No status line: stdout replaces an editor buffer, and a per-keystroke
        # "ok" on stderr is noise in that contract.
        pipe "''${POS[@]}"
        ;;
      check)
        parse_opts "$@"; compile_config
        check_fmt "''${POS[@]}" || true
        lint "''${POS[@]}" || true
        finish check
        ;;
      lint)
        parse_opts "$@"; compile_config   # lint needs EFFECTIVE_RUFF for `ruff check`
        lint "''${POS[@]}" || true
        finish lint
        ;;
      gen)
        parse_opts "$@"
        gen
        ;;
      version|-V|--version)
        echo "${package_name}-v${version}"
        ;;
      help|-h|--help|"")
        usage
        ;;
      *)
        die "unknown command '$cmd' (expected: fmt | check | lint | pipe | gen | version)"
        ;;
    esac
  '';

  gateScript = pkgs.writeShellScriptBin "gate" gate_script_src;
in
# Only bin/gate is exported. The tools gate runs are reached through gatePath,
# which gate prepends to its own PATH, so they are not added to the user's PATH.
pkgs.symlinkJoin {
  pname = package_name;
  inherit version;
  paths = [ gateScript ];
  meta = {
    description = "ci formatter and linter";
    license = [ pkgs.lib.licenses.mit ];
    homepage = "https://github.com/stevelr/gate-check";
    mainProgram = "gate";
    inherit version;
  };
}
