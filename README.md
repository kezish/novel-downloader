# novel-sourcekit — thin fork prototype
In the isolated Nix shell:
    nix-shell --run 'python -m sourcekit compile && python -m sourcekit check && pytest -q'
Development API only:
    nix-shell --run 'SOURCEKIT_BUILD=$PWD/build uvicorn sourcekit.bootstrap:app --host 127.0.0.1 --port 18766'
No production changes. Biquge native JSON; CZBooks factory; 52 reading-page plugin;
Jiujiu direct-link parser and unverified search rule, not yet an upstream task.
