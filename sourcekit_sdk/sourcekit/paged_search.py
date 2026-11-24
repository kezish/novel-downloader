"""Bounded pagination for observed search contracts, reusing native parsers."""
import asyncio
import re
from datetime import date
from urllib.parse import urlsplit, urljoin, parse_qs, unquote, quote, urlencode
import aiohttp
from lxml import html
from novel_downloader.infra.http_defaults import DEFAULT_USER_HEADERS
from novel_downloader.plugins.registry import registrar

# These paths and pager selectors are verified against live site responses.
SPECS={
    "sk_shuku52":("www.52shuku.net","/so/search.php",".xs-pagination a[href]"),
    "sk_biquge5":("www.biquge5.com","/search.php",".pages a[href]"),
    "sk_czbooks":("czbooks.net","/s/",".paginate a[href]"),
}
PAGES_PER_BATCH=3
MAX_PAGE=10000
MAX_BYTES=3*1024*1024
CATEGORIES={"romance":"10","bl":"1,2,3","gl":"5","fanfic":"4","male":"15,16,18,19,20,23"}

def search_metadata(site,raw):
    if site!="sk_shuku52":return {}
    tree=html.fromstring(raw)
    result={}
    for card in tree.cssselect("article.excerpt"):
        links=card.cssselect("header h2 a[href]")
        if not links:continue
        url=urljoin("https://www.52shuku.net",links[0].get("href"))
        cats=card.cssselect(".cat a")
        times=card.cssselect(".time")
        category=cats[0].text_content().strip() if cats else ""
        m=re.search(r"\d{4}-\d{2}-\d{2}",times[0].text_content()) if times else None
        published=""
        if m:
            try:published=date.fromisoformat(m.group()).isoformat()
            except ValueError:pass
        result[url]={"category":category,"published":published}
    return result

def page_url(site,keyword,page,category="",order="relevance"):
    host,path,_=SPECS[site]
    if site=="sk_czbooks":
        return "https://"+host+path+quote(keyword,safe="")+("/"+str(page) if page>1 else "")+"?"+urlencode({"q":keyword})
    params={"q":keyword}
    if site=="sk_shuku52":
        if page>1 or category or order!="relevance":params.update(m="no",f="_all",syn="no")
        if category:params["c"]=CATEGORIES[category]
        if order!="relevance":params["s"]={"newest":"time","oldest":"time_asc"}[order]
    if page>1:
        params["p"]=page
    return "https://"+host+path+"?"+urlencode(params)

def linked_page(site,keyword,href,base,category="",order="relevance"):
    host,path,_=SPECS[site]
    u=urlsplit(urljoin(base,href))
    if u.scheme!="https" or u.hostname!=host or u.username or u.password or u.port not in (None,443):
        return None
    if site=="sk_czbooks":
        prefix="/s/"+keyword+"/"
        decoded=unquote(u.path)
        if not decoded.startswith(prefix):return None
        raw=decoded[len(prefix):]
    else:
        q=parse_qs(u.query)
        if u.path!=path or q.get("q")!=[keyword]:return None
        if site=="sk_shuku52":
            if category and q.get("c")!=[CATEGORIES[category]]:return None
            if order!="relevance" and q.get("s")!=[{"newest":"time","oldest":"time_asc"}[order]]:return None
        raw=q.get("p",["1"])[0]
    if re.fullmatch(r"[0-9]{1,5}",raw) and 1<=int(raw)<=MAX_PAGE:return int(raw)
    return None

def pagination(site,keyword,raw,page,base,category="",order="relevance"):
    tree=html.fromstring(raw)
    links=tree.cssselect(SPECS[site][2])
    found={p for a in links if (p:=linked_page(site,keyword,a.get("href",""),base,category,order)) is not None}
    # Only continue when the site's pager actually advertises a later page.
    return page+1 if any(p>page for p in found) and page<MAX_PAGE else None

async def fetch(session,site,keyword,page,category="",order="relevance"):
    url=page_url(site,keyword,page,category,order)
    async with session.get(url,allow_redirects=False) as r:
        if r.status==403 and r.headers.get("cf-mitigated","").lower()=="challenge":
            raise RuntimeError("site requires browser verification")
        if r.status!=200:
            raise RuntimeError(f"HTTP {r.status}")
        raw=bytearray()
        async for chunk in r.content.iter_chunked(65536):
            raw.extend(chunk)
            if len(raw)>MAX_BYTES:raise ValueError("search response too large")
        return raw.decode("utf-8","replace"),str(r.url)

async def search_site(session,site,keyword,start,resolve,category="",order="relevance"):
    cls=registrar.get_searcher_class(site)
    parser=cls(session)
    results=[];seen=set();page=start;pages=0
    state="completed";message=""
    try:
        for _ in range(PAGES_PER_BATCH):
            options=(category,order) if site=="sk_shuku52" and (category or order!="relevance") else ()
            raw,url=await fetch(session,site,keyword,page,*options)
            parsed=parser._parse_html(raw,None)
            metadata=search_metadata(site,raw)
            next_page=pagination(site,keyword,raw,page,url,*options)
            rows=[]
            for row in parsed:
                try:
                    actual,bid=resolve(row["book_url"])
                    if actual!=site:continue
                except (ValueError,KeyError,TypeError):continue
                key=(actual,bid)
                if key in seen:continue
                seen.add(key)
                rows.append(dict(site=actual,book_id=bid,book_url=row["book_url"],title=row["title"],author=row["author"],**metadata.get(row["book_url"],{})))
            if (parsed and not rows) or (not parsed and (page>1 or next_page is not None)):
                raise ValueError("repeated or invalid search page")
            results.extend(rows);pages+=1;page=next_page
            if page is None:break
            if pages<PAGES_PER_BATCH:await asyncio.sleep(1)
    except Exception as exc:
        state="blocked" if "browser verification" in str(exc) else "failed"
        if state=="blocked":page=None  # do not retry access-control challenges in the next batch
        message=str(exc)[:200] or type(exc).__name__
    return results,dict(state=state,count=len(results),message=message,pages=pages,next_page=page)

async def paged_query(keyword,sites,cursors,resolve,category="",order="relevance"):
    async with aiohttp.ClientSession(headers=DEFAULT_USER_HEADERS,timeout=aiohttp.ClientTimeout(total=7)) as session:
        tasks=await asyncio.gather(*(search_site(session,s,keyword,cursors.get(s,1),resolve,category,order) for s in sites))
    results=[];statuses={}
    for site,(rows,status) in zip(sites,tasks):
        results.extend(rows);statuses[site]=status
    return dict(results=results,statuses=statuses)
