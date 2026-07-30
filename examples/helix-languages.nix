# helix language settings that takes advantage of gate-check formatters
# so helix format-on-save is always consistent with project format and ci checks.
#
# for nix home-manager

{ pkgs, ... }:
let
  gateFormat = {
    command = "gate";
    args = [
      "pipe"
      "%{buffer_name}"
    ];
  };
in
{
  programs.helix.extraPackages = with pkgs; [
    biome
    bash-language-server
    docker-compose-language-service
    gopls
    lua-language-server
    markdown-oxide
    nixd # nix LSP
    nixfmt # nix formatter
    ruff
    rust-analyzer
    taplo # toml formatter
    typos-lsp
    yaml-language-server
    vscode-langservers-extracted
  ];

  programs.helix.languages = {
    language-server = {
      bash-language-server.command = "${pkgs.bash-language-server}/bin/bash-language-server";
      biome = {
        command = "${pkgs.biome}/bin/biome";
        args = [ "lsp-proxy" ];
      };
      docker-compose-language-service.command = "${pkgs.docker-compose-language-service}/bin/docker-compose-langserver";
      ruff-server = {
        command = "${pkgs.ruff}/bin/ruff";
        args = [ "server" ];
      };
      gopls = {
        command = "${pkgs.gopls}/bin/gopls";
        args = [ "serve" ];
      };
      lua-language-server.command = "${pkgs.lua-language-server}/bin/lua-language-server";
      markdown-oxide.command = "${pkgs.markdown-oxide}/bin/markdown-oxide";
      nixd.command = "${pkgs.nixd}/bin/nixd";
      taplo.command = "${pkgs.taplo}/bin/taplo";
      typos.command = "${pkgs.typos-lsp}/bin/typos-lsp";
      vscode-css-language-server.command = "${pkgs.vscode-langservers-extracted}/bin/vscode-css-language-server";
      vscode-html-language-server.command = "${pkgs.vscode-langservers-extracted}/bin/vscode-html-language-server";
      vscode-json-language-server.command = "${pkgs.vscode-langservers-extracted}/bin/vscode-json-language-server";
      rust-analyzer.command = "rust-analyzer";
      rust-analyzer.config.check.command = "clippy";
      yaml-language-server.command = "${pkgs.yaml-language-server}/bin/yaml-language-server";
    };
    language = [
      {
        name = "bash";
        auto-format = true;
        language-servers = [ "bash-language-server" ];
      }
      {
        name = "css";
        language-servers = [
          { name = "biome"; }
          {
            name = "vscode-css-language-server";
            except-features = [ "format" ];
          }
        ];
        file-types = [ "css" ];
        formatter = gateFormat;
      }
      {
        name = "docker-compose";
        language-servers = [ "docker-compose-language-service" ];
      }
      {
        name = "graphql";
        language-servers = [ { name = "biome"; } ];
        file-types = [ "graphql" ];
        formatter = gateFormat;
      }
      {
        name = "html";
        language-servers = [
          { name = "biome"; }
          {
            name = "vscode-html-language-server";
            except-features = [ "format" ];
          }
          { name = "typos"; }
        ];
        file-types = [
          "html"
          "htm"
        ];
        formatter = gateFormat;
      }
      {
        name = "javascript";
        language-servers = [ { name = "biome"; } ];
        file-types = [
          "js"
        ];
        formatter = gateFormat;
      }
      {
        name = "json";
        language-servers = [
          { name = "biome"; }
          {
            name = "vscode-json-language-server";
            except-features = [ "format" ];
          }
          { name = "typos"; }
        ];
        file-types = [
          "json"
          "jsonc"
          "json5"
        ];
        formatter = gateFormat;
      }
      {
        name = "jsx";
        language-servers = [ { name = "biome"; } ];
        file-types = [
          "jsx"
        ];
        formatter = gateFormat;
      }
      {
        name = "just";
        formatter = gateFormat;
        auto-format = false;
      }
      {
        name = "kdl";
        auto-format = true;
        formatter = gateFormat;
      }
      {
        name = "lua";
        auto-format = true;
        language-servers = [ "lua-language-server" ];
      }
      {
        name = "markdown";
        language-servers = [
          "markdown-oxide"
          "typos"
        ];
        formatter = gateFormat;
        auto-format = true;
      }
      {
        name = "nix";
        auto-format = true;
        formatter = gateFormat;
        language-servers = [ "nixd" ];
      }
      # {
      #   name = "go";
      #   auto-format = true;
      #   language-servers = [ "gopls" ];
      # }
      {

        name = "python";
        auto-format = true;
        language-servers = [ "ruff-server" ];
        formatter = gateFormat;
      }
      {
        name = "rust";
        language-servers = [
          "rust-analyzer-clippy"
          "typos"
        ];
        formatter = gateFormat;
      }
      {
        name = "toml";
        language-servers = [
          "taplo"
          "typos"
        ];
        formatter = gateFormat;
        auto-format = true;
      }
      {
        name = "tsx";
        language-servers = [ { name = "biome"; } ];
        file-types = [ "tsx" ];
        formatter = gateFormat;
      }
      {
        name = "typescript";
        language-servers = [ { name = "biome"; } ];
        file-types = [ "ts" ];
        formatter = gateFormat;
      }
      {
        name = "vue";
        language-servers = [ { name = "biome"; } ];
        file-types = [
          "vue"
        ];
        formatter = gateFormat;
      }
      {
        name = "yaml";
        language-servers = [
          "yaml-language-server"
          "typos"
        ];
        file-types = [
          "yaml"
          "yml"
        ];
        auto-format = true;
        formatter = gateFormat;
      }
    ];
  };
}
