# gate-check.nix package builder for gate: ci formatter and linter
{ pkgs, version, ... }:

let
  package_name = "gate-check";
  default_line_length = 95;

  jsonFormat = pkgs.formats.json { };
  tomlFormat = pkgs.formats.toml { };

  raw_dprint_json = jsonFormat.generate "dprint.json" dprint_json_src;
  raw_treefmt_toml = tomlFormat.generate "treefmt.toml" treefmt_toml_src;
  raw_biome_json = jsonFormat.generate "biome.json" biome_json_src;
  raw_ruff_toml = tomlFormat.generate "ruff.toml" ruff_toml_src;

  shellcheck_bin = "${pkgs.shellcheck}/bin/shellcheck";
  typos_bin = "${pkgs.typos}/bin/typos";
  ruff_bin = "${pkgs.ruff}/bin/ruff";
  sed_bin = "${pkgs.gnused}/bin/sed";

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
  gateConfigs = pkgs.runCommand "gate-configs" { nativeBuildInputs = [ pkgs.dprint ]; } ''
    mkdir -p $out
    cp ${raw_dprint_json}  $out/dprint.json
    cp ${raw_treefmt_toml} $out/treefmt.toml
    cp ${raw_biome_json}   $out/biome.json
    cp ${raw_ruff_toml}    $out/ruff.toml
    chmod +w $out/*
    export DPRINT_CACHE_DIR="$TMPDIR/dprint-cache"
    cd $out
    dprint fmt --config ${buildTimeDprintConfig} --config-discovery=false \
      dprint.json treefmt.toml biome.json ruff.toml
  '';

  dprint_json = "${gateConfigs}/dprint.json";
  treefmt_toml = "${gateConfigs}/treefmt.toml";
  biome_json = "${gateConfigs}/biome.json";
  ruff_toml = "${gateConfigs}/ruff.toml";

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
    )
  );

  # Tools gate puts on its OWN PATH before invoking treefmt / stdin formatters,
  # so the bare command names in treefmt.toml resolve to gate's pinned versions
  # regardless of the caller's PATH. Includes the coreutils/git plumbing the
  # script's bare calls need.
  gatePath = pkgs.lib.makeBinPath [
    pkgs.biome
    pkgs.coreutils
    pkgs.diffutils
    pkgs.dprint
    pkgs.findutils
    pkgs.git
    pkgs.gnugrep
    pkgs.gnused
    pkgs.just
    pkgs.libxml2
    pkgs.nixfmt
    pkgs.ruff
    pkgs.rustfmt
    pkgs.shellcheck
    pkgs.treefmt
    pkgs.typos
  ];

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
    ruff = {
      quoteStyle = "double";
      lineWidth = default_line_length;
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
        "*.html"
        "*.htm"
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

  biome_json_src = {
    "$schema" = "https://biomejs.dev/schemas/2.4.15/schema.json";
    plugins = [ ];
    # No VCS integration: biome resolves its VCS root to the folder holding this
    # config (./.gate, or the nix store path for the built-in), and with
    # useIgnoreFile it hard-errors there with "couldn't find an ignore file".
    # gate does its own gitignore-aware enumeration (git ls-files -co
    # --exclude-standard) and hands treefmt explicit paths in a non-git mirror
    # tree, so biome has nothing to discover anyway.
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
    html.formatter.enabled = true;
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
    #   The effective dprint.json, treefmt.toml, biome.json and ruff.toml are each
    #   resolved independently, in this order:
    #    1. -d/--dir DIR         (must exist, else error)
    #    2. $GATE_CONFIG         (must exist, else error)
    #    3. nearest ./.gate walking up from $PWD
    #    4. $XDG_CONFIG_HOME/gate
    #    5. the built-in defaults baked into this gate
    # A file absent from the chosen dir falls back to the built-in default.
    # 
    # Performance:
    #   The resolved config is COMPILED once (plugin tokens + config paths expanded)
    #   into a per-(config,gate)-version cache dir and reused across runs.
    #   `pipe` bypasses treefmt entirely, dispatching to each formatter's stdin mode.

    BUILTIN_DPRINT="${dprint_json}"
    BUILTIN_TREEFMT="${treefmt_toml}"
    BUILTIN_BIOME="${biome_json}"
    BUILTIN_RUFF="${ruff_toml}"
    TREEFMT_BIN="${pkgs.treefmt}/bin/treefmt"
    CFG_VERSION="${cfgVersion}"

    die()  { printf 'gate: %s\n' "$*" >&2; exit 2; }
    note() { printf 'gate: %s\n' "$*" >&2; }
    have() { command -v "$1" >/dev/null 2>&1; }

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
    compile_config() {
      local cfgdir dprint biome ruff treefmt keyinput key root cachedir tmp leftover f
      cfgdir="$(resolve_cfg_dir)" || exit 2
      dprint="$(resolve_file  "$cfgdir" dprint.json  "$BUILTIN_DPRINT")"
      biome="$(resolve_file   "$cfgdir" biome.json   "$BUILTIN_BIOME")"
      ruff="$(resolve_file    "$cfgdir" ruff.toml    "$BUILTIN_RUFF")"
      treefmt="$(resolve_file "$cfgdir" treefmt.toml "$BUILTIN_TREEFMT")"

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

    treefmt_run() {   # <extra flags and paths...>
      "$TREEFMT_BIN" --config-file "$EFFECTIVE_TREEFMT" --allow-missing-formatter "$@"
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
        *.json|*.jsonc|*.json5|*.md|*.markdown|*.toml|*.yaml|*.yml|*.kdl)
          fmt=(dprint fmt --config "$EFFECTIVE_DPRINT" --config-discovery=ignore-descendants --stdin "$base") ;;
        *.css|*.html|*.htm|*.graphql|*.gql|*.js|*.mjs|*.cjs|*.jsx|*.ts|*.mts|*.cts|*.tsx|*.vue)
          fmt=(biome format "--config-path=$EFFECTIVE_BIOME" "--stdin-file-path=$base") ;;
        *.rs)       fmt=(rustfmt --edition 2024) ;;
        *.nix)      fmt=(nixfmt) ;;
        *.py|*.pyi) fmt=(ruff format --config "$EFFECTIVE_RUFF" --stdin-filename "$base" -) ;;
      esac
      if [ "''${#fmt[@]}" -eq 0 ]; then cat; return 0; fi   # passthrough
      tmpd="$(mk_tmpd)"; cat > "$tmpd/buf"
      if "''${fmt[@]}" < "$tmpd/buf" > "$tmpd/out" 2>/dev/null; then cat "$tmpd/out"; else cat "$tmpd/buf"; return 1; fi
    }

    # ---- candidate enumeration -------------------------------------------------
    candidates() {
      if [ "$#" -gt 0 ]; then
        local p
        for p in "$@"; do
          if   [ -d "$p" ]; then find "$p" -type f
          elif [ -e "$p" ]; then printf '%s\n' "$p"
          fi
        done
      elif git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git ls-files
      else
        find . -type f
      fi
    }

    # ---- read-only formatting check --------------------------------------------
    # treefmt has no dry-run, so mirror candidates into a temp tree, format the
    # mirror, and compare. Work tree is unmodified. Enumeration is
    # gitignore-aware to match treefmt's discovery.
    check_fmt() {   # [paths...]
      local rc=0 f tmpd
      local -a cand changed
      if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        mapfile -t cand < <(git ls-files -co --exclude-standard -- "$@")
      else
        mapfile -t cand < <(candidates "$@")
      fi
      [ "''${#cand[@]}" -gt 0 ] || return 0
      tmpd="$(mk_tmpd)"
      for f in "''${cand[@]}"; do
        [ -f "$f" ] && [ ! -L "$f" ] || continue
        case $f in */*) mkdir -p "$tmpd/''${f%/*}" ;; esac
        cp -p -- "$f" "$tmpd/$f"
      done
      # Formatter configs the mirror needs because their tools discover them
      # relative to the tree being formatted. dprint/biome/ruff get configs
      # via --config with absolute path outside the mirror.
      for f in rustfmt.toml .rustfmt.toml .editorconfig; do
        if [ -f "$f" ] && [ ! -e "$tmpd/$f" ]; then cp -p -- "$f" "$tmpd/$f"; fi
      done
      treefmt_run --tree-root "$tmpd" -C "$tmpd" --no-cache >&2 || rc=1
      changed=()
      for f in "''${cand[@]}"; do
        [ -f "$tmpd/$f" ] || continue
        cmp -s -- "$f" "$tmpd/$f" || changed+=("$f")
      done
      if [ "''${#changed[@]}" -gt 0 ]; then
        note "needs formatting (''${#changed[@]}):"
        printf '  %s\n' "''${changed[@]}" >&2
        rc=1
      fi
      return "$rc"
    }

    # Classify an extensionless file by shebang: prints "sh" | "py" | "".
    shebang_class() {
      local first
      IFS= read -rn 256 first < "$1" 2>/dev/null
      [[ $first == '#!'* ]] || return 0
      LC_ALL=C grep -qIe '^#!' -- "$1" 2>/dev/null || return 0   # skip binaries
      if   [[ $first =~ (^|/|[[:space:]])(bash|sh|dash)([[:space:]]|$) ]]; then echo sh
      elif [[ $first =~ (^|/|[[:space:]])python[0-9.]*([[:space:]]|$) ]]; then echo py
      fi
    }

    lint() {   # [paths...]
      local rc=0 f
      local -a cand sh_files py_files xml_files rs_files
      mapfile -t cand < <(candidates "$@")
      sh_files=(); py_files=(); xml_files=(); rs_files=()

      for f in "''${cand[@]}"; do
        case $f in
          *.sh|*.bash)  sh_files+=("$f");  continue ;;
          *.py|*.pyi)   py_files+=("$f");  continue ;;
          *.xml|*.svg)  xml_files+=("$f"); continue ;;
          *.rs)         rs_files+=("$f");  continue ;;
        esac
        if [[ ''${f##*/} != *.* ]]; then
          case $(shebang_class "$f") in
            sh) sh_files+=("$f") ;;
            py) py_files+=("$f") ;;
          esac
        fi
      done

      if [ "''${#sh_files[@]}" -gt 0 ]; then
        note "shellcheck (''${#sh_files[@]})"
        "${shellcheck_bin}" "''${sh_files[@]}" || rc=1
      fi

      if [ "''${#py_files[@]}" -gt 0 ] && have ruff; then
        note "ruff check (''${#py_files[@]})"
        "${ruff_bin}" check --config "$EFFECTIVE_RUFF" "''${py_files[@]}" || rc=1
      fi

      if [ "''${#xml_files[@]}" -gt 0 ] && have xmllint; then
        note "xmllint (''${#xml_files[@]})"
        "${pkgs.libxml2}/bin/xmllint" --noout "''${xml_files[@]}" || rc=1
      fi

      note "typos"
      if [ "$#" -gt 0 ]; then ${typos_bin} "$@" || rc=1; else ${typos_bin} || rc=1; fi

      if [ "''${#rs_files[@]}" -gt 0 ] || { [ "$#" -eq 0 ] && [ -f Cargo.toml ]; }; then
        if have cargo; then
          note "cargo clippy"
          cargo clippy --all-targets -- -D warnings || rc=1
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
        treefmt_run "''${POS[@]}"
        ;;
      pipe)
        parse_opts "$@"; compile_config
        pipe "''${POS[@]}"
        ;;
      check)
        parse_opts "$@"; compile_config
        rc=0
        check_fmt "''${POS[@]}" || rc=1
        lint "''${POS[@]}" || rc=1
        exit "$rc"
        ;;
      lint)
        parse_opts "$@"; compile_config   # lint needs EFFECTIVE_RUFF for `ruff check`
        lint "''${POS[@]}"
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

  toolPackages = [
    pkgs.biome
    pkgs.dprint
    pkgs.libxml2 # xmllint
    pkgs.nixfmt
    pkgs.shellcheck
    pkgs.treefmt
    pkgs.typos
  ];
in
pkgs.symlinkJoin {
  name = package_name;
  inherit version;
  paths = [ gateScript ] ++ toolPackages;
  meta = {
    description = "ci formatter and linter";
    license = [ pkgs.lib.licenses.mit ];
    homepage = "https://github.com/stevelr/gate-check";
    mainProgram = "gate";
    inherit version;
  };
}
