import json,os,subprocess,sys
from pathlib import Path
from sourcekit.compiler import compile_all

def test_bootstrap_sites(tmp_path):
    root=tmp_path/"bundle"
    compile_all(root)
    env={**os.environ,"SOURCEKIT_BUILD":str(root),"XDG_CONFIG_HOME":str(tmp_path/"config"),"XDG_DATA_HOME":str(tmp_path/"data"),"XDG_CACHE_HOME":str(tmp_path/"cache")}
    script="""
import asyncio,json
from sourcekit.bootstrap import app
route=next(r for r in app.routes if r.path=="/api/v1/sites")
result=asyncio.run(route.endpoint())
for key in ("sk_biquge5","sk_czbooks","sk_shuku52"):
    assert key in result["search"] and key in result["download"]
assert "sk_jiujiu" not in result["download"]
print("site endpoint passed")
"""
    r=subprocess.run([sys.executable,"-c",script],env=env,cwd=tmp_path,capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    assert "site endpoint passed" in r.stdout
