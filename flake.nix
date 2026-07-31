{
  description = "ci formatter and linter";
  inputs = {
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    { nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ] (
      system:
      let
        version = "0.2.5";
        pkgs = nixpkgs.legacyPackages.${system};
        gate-check = import ./package.nix { inherit pkgs version; };
      in
      {
        packages.default = gate-check;
      }
    );
}
