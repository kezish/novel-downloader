{ lib, stdenvNoCC, python3, makeWrapper, source ? ../. }:
let
  python = python3.withPackages (p: with p; [
    rich requests pycryptodome aiohttp lxml cssselect platformdirs
  ]);
in stdenvNoCC.mkDerivation {
  pname = "novel-sourcekit-cli";
  version = "1.42.0-prototype";
  src = source;
  nativeBuildInputs = [ makeWrapper ];
  dontBuild = true;
  installPhase = ''
    runHook preInstall
    mkdir -p "$out/share/novel-sourcekit" "$out/bin"
    cp -r src "$out/share/novel-sourcekit/engine"
    cp -r sourcekit_sdk/{sourcekit,sources,templates} "$out/share/novel-sourcekit/"
    export PYTHONPATH="$out/share/novel-sourcekit:$out/share/novel-sourcekit/engine"
    export PYTHONDONTWRITEBYTECODE=1
    export XDG_CACHE_HOME="$TMPDIR/cache"
    export XDG_CONFIG_HOME="$TMPDIR/config"
    ${python}/bin/python -m sourcekit compile --build "$out/share/novel-sourcekit/build"
    ${python}/bin/python -m sourcekit check --build "$out/share/novel-sourcekit/build"
    makeWrapper ${python}/bin/python "$out/bin/novel-sourcekit" \
      --add-flags "-m sourcekit.webcli" \
      --add-flags "--build $out/share/novel-sourcekit/build" \
      --set PYTHONPATH "$PYTHONPATH" \
      --set PYTHONDONTWRITEBYTECODE 1
    runHook postInstall
  '';
  meta.mainProgram = "novel-sourcekit";
}
