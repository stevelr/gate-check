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
  kdlfmt ? pkgs.kdlfmt,
  libxml2 ? pkgs.libxml2, # xmllint
  nixfmt ? pkgs.nixfmt,
  python3 ? pkgs.python3, # runs gate itself and the portability check
  ruff ? pkgs.ruff,
  # null (default): not bundled; rustfmt comes from the caller's PATH. See gatePath.
  rustfmt ? null,
  shellcheck ? pkgs.shellcheck,
  typos ? pkgs.typos,
  ...
}:

let
  package_name = "gate-check";
  default_line_length = 95;

  # JSON templates, indented like pkgs.formats.json writes them (which needs
  # jq); gateConfigs then puts them in dprint's style.
  prettyJSON =
    name: value:
    pkgs.runCommand name {
      value = builtins.toJSON value;
      passAsFile = [ "value" ];
    } "${python3}/bin/python3 -m json.tool --indent 2 $valuePath > $out";

  tomlFormat = pkgs.formats.toml { };

  raw_dprint_json = prettyJSON "dprint.json" dprint_json_src;
  raw_treefmt_toml = tomlFormat.generate "treefmt.toml" treefmt_toml_src;
  raw_biome_json = prettyJSON "biome.json" biome_json_src;
  raw_ruff_toml = tomlFormat.generate "ruff.toml" ruff_toml_src;
  raw_clippy_toml = tomlFormat.generate "clippy.toml" clippy_toml_src;

  shellcheck_bin = "${shellcheck}/bin/shellcheck";
  typos_bin = "${typos}/bin/typos";
  ruff_bin = "${ruff}/bin/ruff";
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

  # Tools gate puts on its OWN PATH before running the formatters, so the bare
  # command names in treefmt.toml resolve to gate's pinned versions
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
      kdlfmt
      libxml2
      nixfmt
      ruff
      shellcheck
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
        # drops every file.
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

    # kdl: no dprint or biome plugin formats it.
    formatter.kdlfmt = {
      command = "kdlfmt";
      options = [ "format" ];
      includes = [ "*.kdl" ];
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
    # The schema of the biome this gate runs, so a regenerated config never
    # trails it (biome notes a schema that differs from its own version).
    "$schema" = "https://biomejs.dev/schemas/${biome.version}/schema.json";
    plugins = [ ];
    # No VCS integration: biome resolves its VCS root to the folder holding this
    # config (./.gate, or the nix store path for the built-in), and with
    # useIgnoreFile it hard-errors there with "couldn't find an ignore file".
    # gate does its own gitignore-aware enumeration (fd, see candidates()) and
    # hands each formatter explicit paths, so biome has nothing
    # to discover anyway.
    vcs.enabled = false;
    files.ignoreUnknown = false;
    files.maxSize = 1024 * 1024 * 5; # 5 MiB
    formatter.enabled = true;
    formatter.indentStyle = "space";
    linter.enabled = true;
    # biome 2.6 deprecates rules.recommended = true for this (`biome migrate`).
    linter.rules.preset = "recommended";
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

  # What the installed gate needs to know about this build: the built-in
  # configs, the pinned tools and the dprint plugins. gate.py reads it from the
  # path substituted for its BUILD_INFO token below.
  buildInfo = pkgs.writeText "gate-build.json" (
    builtins.toJSON {
      package = package_name;
      inherit version;
      cfg_version = cfgVersion;
      path = gatePath;
      builtin = {
        dprint = dprint_json;
        treefmt = treefmt_toml;
        biome = biome_json;
        ruff = ruff_toml;
        clippy = clippy_toml;
      };
      plugins = builtins.listToAttrs (
        map (p: {
          inherit (p) name;
          value = "${p.pkg}/plugin.wasm";
        }) dprintPlugins
      );
      shellcheck = shellcheck_bin;
      typos = typos_bin;
      ruff = ruff_bin;
      xmllint = "${libxml2}/bin/xmllint";
      python3 = "${python3}/bin/python3";
      portability = "${./portability.py}";
    }
  );

  # gate.py installed as bin/gate, run by gate's python3 in isolated mode.
  gateScript = pkgs.runCommand "gate" { } ''
    mkdir -p $out/bin
    ${sed_bin} -e '1s|^#!.*|#!${python3}/bin/python3 -I|' \
      -e 's|@gate_build_info@|${buildInfo}|' ${./gate.py} > $out/bin/gate
    chmod +x $out/bin/gate
  '';
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
