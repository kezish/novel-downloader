"""Bounded public search metadata benchmark; no chapter retrieval."""
import argparse, asyncio, csv, json, re, time, unicodedata
from pathlib import Path
from datetime import datetime,timezone
import aiohttp
from novel_downloader.infra.http_defaults import DEFAULT_USER_HEADERS
from novel_downloader.plugins.registry import registrar
from sourcekit.bridge import register_sources

ROOT=Path(__file__).resolve().parent
SITES=["sk_biquge5","sk_shuku52","sk_czbooks","ttkan","ixdzs8","n23qb","n101kanshu","qimao","n8novel","hetushu","kadokado","yodu","n37yue","ciluke","i25zw","laoyaoxs","bixiange"]
PILOT=["01","02","04","05","06","07","08","47"]
def norm(x):
    x=unicodedata.normalize("NFKC",str(x)).casefold().strip()
    x=re.sub(r"^\[(都市|言情|玄幻|武侠|武俠|科幻|历史|歷史|网游|網遊|其他|耽美|百合|奇幻|修真|穿越)\]","",x)
    x=re.sub(r"(?:[\[(](?:gl|bl|完结|完結|新修版|修订版|修訂版)[\])])+$","",x,flags=re.I)
    return "".join(c for c in x if not (c.isspace() or unicodedata.category(c).startswith("P")))
def assess(case,rows):
    authors={norm(x) for x in case["author_aliases"].split("|")}
    titles={norm(x) for x in case["title_aliases"].split("|")} if case["title_aliases"] else set()
    hits=[]; title_only=[]; author_titles=set()
    for i,row in enumerate(rows,1):
        t,a=norm(row.get("title","")),norm(row.get("author",""))
        if a in authors and t:author_titles.add(t)
        if t in titles:
            if a in authors:hits.append(i)
            else:title_only.append(i)
    return {"exact_rank":min(hits,default=None),"title_only_rank":min(title_only,default=None),"author_titles":sorted(author_titles)}
async def site_run(site,cases):
    trace=aiohttp.TraceConfig()
    observed=[]
    async def end(s,c,p):
        observed.append({"status":p.response.status,"challenge":p.response.headers.get("cf-mitigated","")=="challenge"})
    trace.on_request_end.append(end)
    records=[]
    cls=registrar.get_searcher_class(site)
    if cls is None:return [{"site":site,"status":"missing_adapter"}]
    async with aiohttp.ClientSession(headers=DEFAULT_USER_HEADERS,timeout=aiohttp.ClientTimeout(total=8),trace_configs=[trace]) as session:
        parser=cls(session)
        consecutive_failures=0
        for index,case in enumerate(cases):
            observed.clear();start=time.monotonic();rows=[];error=""
            try:
                raw=await parser._fetch_html(case["query"])
                if not raw:raise ValueError("empty transport response")
                rows=parser._parse_html(raw,None)
                state="parsed" if rows else "zero_unverified"
            except Exception as e:
                state="failed";error=str(e)[:160] or type(e).__name__
            blocked=any(x["status"] in (403,429) or x["challenge"] for x in observed)
            if blocked:state="blocked"
            rec={"site":site,"case":case["id"],"genre":case["genre"],"kind":case["kind"],"query":case["query"],"status":state,"http":list(observed),"elapsed":round(time.monotonic()-start,2),"rows":len(rows),**assess(case,rows),"error":error}
            records.append(rec)
            print(json.dumps(rec,ensure_ascii=False),flush=True)
            consecutive_failures=consecutive_failures+1 if state=="failed" else 0
            if blocked or consecutive_failures>=2:break
            if index+1<len(cases):await asyncio.sleep(16 if site=="bixiange" else 3)
    return records
async def main(args):
    register_sources(ROOT.parent/"build")
    with (ROOT/"cases.tsv").open() as f:cases=list(csv.DictReader(f,delimiter="\t"))
    ids=set(args.ids.split(",")) if args.ids else set(PILOT)
    cases=[x for x in cases if x["id"] in ids]
    results=await asyncio.gather(*(site_run(s,cases) for s in args.sites),return_exceptions=True)
    records=[]
    for site,r in zip(args.sites,results):
        if isinstance(r,Exception):records.append({"site":site,"status":"runner_error","error":str(r)})
        else:records.extend(r)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps({"timestamp":datetime.now(timezone.utc).isoformat(),"cases":[x["id"] for x in cases],"sites":args.sites,"records":records},ensure_ascii=False,indent=2)+"\n")
    print("SUMMARY")
    for site in args.sites:
        r=[x for x in records if x["site"]==site and "case" in x]
        titles=[x for x in r if x["kind"]=="title"]
        hit={x["case"] for x in titles if x["exact_rank"]}
        print(site,"attempted",len(r),"exact",len(hit),"/",sum(x["kind"]=="title" for x in cases),"parsed",sum(x["status"]=="parsed" for x in r),"unverified_zero",sum(x["status"]=="zero_unverified" for x in r),"blocked_or_failed",sum(x["status"] in ("blocked","failed") for x in r),flush=True)
if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--sites",nargs="+",choices=SITES,default=SITES[:8])
    p.add_argument("--ids")
    p.add_argument("--output",type=Path,required=True)
    asyncio.run(main(p.parse_args()))
