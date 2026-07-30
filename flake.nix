{
  description = "ci formatter and linter";
  inputs = {
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    { nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        version = "0.2.0";
        pkgs = nixpkgs.legacyPackages.${system};
        gate-check = import ./package.nix { inherit pkgs version; };
      in
      {
        packages.default = gate-check;
      }
    );
}
