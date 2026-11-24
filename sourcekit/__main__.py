import argparse,json
from pathlib import Path
from .compiler import compile_all,ROOT
def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest="cmd",required=True)
    for name in ("compile","check","validate"):
        c=s.add_parser(name);c.add_argument("--build",type=Path,default=ROOT/"build")
    r=s.add_parser("resolve52");r.add_argument("url")
    d=s.add_parser("resolve-direct");d.add_argument("book_id");d.add_argument("html",type=Path,nargs="?")
    a=p.parse_args()
    if a.cmd=="compile":
        m=compile_all(a.build);print(json.dumps({"sources":[{"id":x["id"],"backend":x["backend"]} for x in m["sources"]],"sha256":m["sha256"]}))
    elif a.cmd=="validate":
        import subprocess,sys,time
        m=compile_all(a.build)
        check=subprocess.run([sys.executable,"-m","sourcekit","check","--build",str(a.build)],capture_output=True,text=True)
        test=subprocess.run([sys.executable,"-m","pytest","-q",str(ROOT/"tests")],capture_output=True,text=True)
        report={"engine_commit":m["engine_commit"],"bundle_sha256":m["sha256"],"timestamp":time.time(),"registration":{"exit":check.returncode,"stdout":check.stdout,"stderr":check.stderr},"offline_tests":{"exit":test.returncode,"stdout":test.stdout,"stderr":test.stderr},"live_status":"separate_probe_required"}
        (a.build/"validation.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
        print(test.stdout,end="")
        print("report:",a.build/"validation.json")
        if check.returncode or test.returncode:raise SystemExit(1)
    elif a.cmd=="check":
        from .bridge import register_sources
        from novel_downloader.plugins.registry import registrar
        m=register_sources(a.build);res={x["id"]:{k:registrar.get_registered(x["id"],k) is not None for k in ("fetcher","parser","searcher","client")} for x in m["sources"] if x["backend"]!="direct"}
        print(json.dumps(res))
        if not all(all(v[k] for k in ("fetcher","parser","searcher")) for v in res.values()):raise SystemExit(1)
    elif a.cmd=="resolve52":
        from .bridge import encode52
        print(encode52(a.url))
    else:
        from .direct import extract_direct_link,resolve_live
        print(extract_direct_link(a.book_id,a.html.read_bytes()) if a.html else resolve_live(a.book_id))
if __name__=="__main__":main()
