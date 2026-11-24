{ pkgs ? import <nixpkgs> {} }:
pkgs.mkShell {
  packages = [ (pkgs.python3.withPackages (p: with p; [
    rich requests pycryptodome aiohttp lxml cssselect platformdirs fastapi uvicorn pytest
  ])) ];
  shellHook = ''export PYTHONPATH="$PWD:$PWD/../novel-downloader/src:$PYTHONPATH"'';
}
