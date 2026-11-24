"""Fetch only the book homepage, extracting the registered metadata fields."""
import aiohttp
from novel_downloader.infra.http_defaults import DEFAULT_USER_HEADERS
from novel_downloader.plugins.registry import registrar
from novel_downloader.plugins.rule.engine import parse_page, extract
from novel_downloader.schemas import ParserConfig
from .paged_search import MAX_BYTES

def extract_metadata(site, raw):
    rule=registrar.get_parser(site,ParserConfig())._rule
    tree=parse_page(raw,rule.book_format)
    fields=rule.book.fields
    def get(name):
        return extract(tree,fields[name],rule.book_format).strip() if name in fields else ""
    title=get("book_name")
    if not title:raise ValueError("book detail not recognized")
    return {"title":title,"author":get("author"),"description":get("summary")[:30000]}

async def metadata(url):
    from .webcli import resolve
    site,bid=resolve(url)
    if site not in ("sk_biquge5","sk_czbooks"):
        raise ValueError("unsupported metadata source")
    # Rebuild the canonical URL, never fetch arbitrary user-provided URLs.
    target=("https://czbooks.net/n/"+bid if site=="sk_czbooks" else "https://www.biquge5.com/"+bid+"/")
    async with aiohttp.ClientSession(headers=DEFAULT_USER_HEADERS,timeout=aiohttp.ClientTimeout(total=12)) as session:
        async with session.get(target,allow_redirects=False) as resp:
            if resp.status==403 and resp.headers.get("cf-mitigated","").lower()=="challenge":
                raise RuntimeError("原站要求浏览器验证")
            if resp.status!=200:raise RuntimeError(f"原站 HTTP {resp.status}")
            raw=bytearray()
            async for chunk in resp.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw)>MAX_BYTES:raise ValueError("详情页超过大小限制")
    return extract_metadata(site,raw.decode("utf-8","replace"))
