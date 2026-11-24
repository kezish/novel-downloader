"""Local, bounded CLI bridge for the existing ks-novels job queue."""
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit
from .bridge import register_sources,encode52
from .compiler import ROOT
from novel_downloader.plugins.registry import registrar
from novel_downloader.plugins.search import search_stream
from novel_downloader.plugins.mixins.download import IncompleteDownloadError
from novel_downloader.schemas import BookConfig,ClientConfig,FetcherConfig,ExporterConfig

SITES={"sk_biquge5":"笔趣阁5","sk_czbooks":"小说狂人","sk_shuku52":"52书库"}
def resolve(url):
    u=urlsplit(url)
    if u.scheme!="https" or u.username or u.password or u.port not in (None,443) or u.query:
        raise ValueError("invalid book URL")
    host=u.hostname
    if host in ("www.52shuku.net","52shuku.net"):
        if re.search(r"_\d+\.html$",u.path):raise ValueError("reading page, not book homepage")
        return "sk_shuku52",encode52(url)
    if host=="www.biquge5.com" and (m:=re.fullmatch(r"/([\d_]+)/?",u.path)):
        return "sk_biquge5",m[1]
    if host=="czbooks.net" and (m:=re.fullmatch(r"/n/([A-Za-z0-9_-]{1,100})/?",u.path)):
        return "sk_czbooks",m[1]
    raise ValueError("unsupported book URL")

async def query(keyword,sites,cursors=None,category="",order="relevance"):
    from .paged_search import paged_query,MAX_PAGE,CATEGORIES
    cursors=cursors or {}
    if not keyword.strip() or len(keyword)>100 or not sites or any(s not in SITES for s in sites):
        raise ValueError("invalid search request")
    if any(s not in sites or type(n) is not int or not 1<=n<=MAX_PAGE for s,n in cursors.items()):
        raise ValueError("invalid search page")
    if category and (category not in CATEGORIES or any(s!="sk_shuku52" for s in sites)):
        raise ValueError("invalid or unsupported category")
    if order not in ("relevance","newest","oldest"):raise ValueError("invalid sort")
    return await paged_query(keyword,list(dict.fromkeys(sites)),cursors,resolve,category,order)

class Progress:
    async def on_start(self,book):pass
    async def on_progress(self,done,total):print(f"downloading: {done}/{total} completed, 0 failed",file=sys.stderr,flush=True)
    async def on_complete(self,book,*,failed=0):pass

async def download(url,output,cache):
    site,bid=resolve(url)
    out=Path(output).resolve();out.mkdir(parents=True,exist_ok=True)
    client=registrar.get_client(site,ClientConfig(output_dir=str(out),raw_data_dir=str(Path(cache).resolve()),workers=2,request_interval=1,fetcher_cfg=FetcherConfig(max_rps=1,request_interval=1,timeout=20)))
    book=BookConfig(bid)
    try:
        async with client:
            summary=await client.download_book(book,strict=True,ui=Progress())
        if not summary["total"] or summary["failed"]:raise RuntimeError("incomplete or empty download")
    except IncompleteDownloadError as e:
        s=e.summary
        print(f"completed: {s['succeeded']}/{s['total']} completed, {s['failed']} failed",file=sys.stderr,flush=True)
        return 3
    print(f"exporting: {summary['succeeded']}/{summary['total']} completed, 0 failed",file=sys.stderr,flush=True)
    exported=client.export_book(book,ExporterConfig(append_timestamp=True,render_missing_chapter=False),formats=["txt"],stage="raw")
    paths=exported.get("txt",[])
    if len(paths)!=1 or paths[0].resolve().parent!=out or not paths[0].is_file() or paths[0].stat().st_size==0:raise RuntimeError("TXT export failed")
    print(paths[0].resolve(),flush=True)
    return 0
def main():
    p=argparse.ArgumentParser();p.add_argument("--build",default=str(ROOT/"build"))
    sub=p.add_subparsers(dest="command",required=True)
    s=sub.add_parser("search");s.add_argument("keyword");s.add_argument("--sites",nargs="+",choices=list(SITES),required=True);s.add_argument("--cursors",default="{}");s.add_argument("--category",default="",choices=["","romance","bl","gl","fanfic","male"]);s.add_argument("--order",default="relevance",choices=["relevance","newest","oldest"])
    m=sub.add_parser("metadata");m.add_argument("url")
    d=sub.add_parser("download");d.add_argument("url");d.add_argument("--output-dir",required=True);d.add_argument("--cache-dir",required=True)
    a=p.parse_args()
    register_sources(a.build)
    if a.command=="search":print(json.dumps(asyncio.run(query(a.keyword,a.sites,json.loads(a.cursors),a.category,a.order)),ensure_ascii=False));return 0
    if a.command=="metadata":
        from .metadata import metadata
        print(json.dumps(asyncio.run(metadata(a.url)),ensure_ascii=False));return 0
    return asyncio.run(download(a.url,a.output_dir,a.cache_dir))
if __name__=="__main__":
    try:raise SystemExit(main())
    except Exception as e:
        print(str(e),file=sys.stderr);raise SystemExit(1)
