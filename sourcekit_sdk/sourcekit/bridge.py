"""Native plugin factory, not a second downloader."""
import os,re
from pathlib import Path
from urllib.parse import urlsplit
from novel_downloader import __version__
from novel_downloader.plugins.registry import registrar
from novel_downloader.plugins.rule.model import parse_rule,apply_limits
from novel_downloader.plugins.rule.fetcher import RuleFetcher
from novel_downloader.plugins.rule.parser import RuleParser
from novel_downloader.plugins.rule.searcher import RuleSearcher
from novel_downloader.plugins.common.client import CommonClient
from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from .compiler import read_json,digest,ENGINE_COMMIT
HOST52="www.52shuku.net"
PATH52=re.compile(r"/(?:[A-Za-z0-9_-]+/)+[A-Za-z0-9_-]+\.html")
def encode52(url):
    u=urlsplit(url)
    if u.scheme!="https" or u.hostname not in (HOST52,"52shuku.net") or u.port not in (None,443) or u.username or u.password or not PATH52.fullmatch(u.path):
        raise ValueError("invalid 52 URL")
    book_id="b"+u.path.encode("ascii").hex()
    if len(book_id)>127:raise ValueError("52 ID exceeds upstream API limit")
    return book_id
def decode52(book_id):
    if not re.fullmatch(r"b[0-9a-f]{2,126}",book_id): raise ValueError("invalid 52 ID")
    path=bytes.fromhex(book_id[1:]).decode("ascii")
    if not PATH52.fullmatch(path): raise ValueError("invalid 52 path")
    return path
def page52(book_id,chapter_id):
    path=decode52(book_id)
    if not re.fullmatch(r"[0-9]{1,6}",chapter_id) or int(chapter_id)<2: raise ValueError("invalid reading page")
    return "https://"+HOST52+path[:-5]+"_"+chapter_id+".html"
def split52(value):
    value=re.sub(r"^\d+\.\s*","",value.strip()); status=""
    if m:=re.search(r"【([^】]+)】\s*$",value):
        status=m[1];value=value[:m.start()]
    title,sep,author=value.rpartition("_")
    return (title.strip(),author.strip(),status) if sep else (value.strip(),"",status)
class ReadingPageClient(CommonClient):
    async def download_book(self,book,**kwargs):
        kwargs["strict"]=True
        return await super().download_book(book,**kwargs)
    async def get_book_info(self,book_id,**kwargs):
        decode52(book_id)
        info=await super().get_book_info(book_id,**kwargs)
        for volume in info["volumes"]:
            for ch in volume["chapters"]:
                expected=urlsplit(page52(book_id,ch["chapterId"]))
                actual=urlsplit(ch["url"])
                if actual.hostname not in (HOST52,"52shuku.net") or actual.scheme!="https" or actual.path!=expected.path:
                    raise ValueError("directory link does not belong to requested book")
        return info
    def export_book(self,book,cfg=None,*,formats=None,**kwargs):
        decode52(book.book_id)
        stage=kwargs.get("stage") or self._detect_latest_stage(book.book_id)
        info=self._load_book_info(book.book_id,stage=stage)
        vols=self._filter_volumes(info["volumes"],book.start_id,book.end_id,book.ignore_ids)
        ids=[ch["chapterId"] for v in vols for ch in v["chapters"]]
        if not ids:raise ValueError("empty export plan")
        raw=self._book_dir(book.book_id)
        if not (raw/f"chapter.{stage}.sqlite").is_file():raise ValueError("missing chapter database")
        with ChapterStorage(raw,filename=f"chapter.{stage}.sqlite") as storage:
            rows=storage.get_chapters(ids)
            missing=[cid for cid in ids if not rows.get(cid) or not rows[cid].get("content","").strip() or storage.need_refetch(cid)]
        if missing:raise ValueError(f"incomplete reading-page export: {len(missing)} missing")
        return super().export_book(book,cfg,formats=formats or ["txt"],**kwargs)
    def _xp_txt_header(self,book_info,name,author): return ""
    def _xp_txt_volume_heading(self,title,volume): return ""
    def _xp_txt_chapter(self,title,chapter): return chapter["content"].strip()+"\n"
class ReadingPageFetcher(RuleFetcher):
    async def fetch_book_info(self,book_id,**kwargs):
        return [await self._get("https://"+HOST52+decode52(book_id),**kwargs)]
    async def fetch_chapter_content(self,book_id,chapter_id,**kwargs):
        return [await self._get(page52(book_id,chapter_id),**kwargs)]
class ReadingPageSearcher(RuleSearcher):
    def _parse_html(self,html_str,limit=None):
        out=[]
        for item in super()._parse_html(html_str,limit):
            try: item["book_id"]=encode52(item["book_url"])
            except ValueError: continue
            item["title"],item["author"],_=split52(item["title"])
            if item["title"]:out.append(item)
        return out
class ReadingPageParser(RuleParser):
    def parse_book_info(self,raw_pages,**kwargs):
        info=super().parse_book_info(raw_pages,**kwargs)
        if info:
            info["book_name"],info["author"],info["serial_status"]=split52(info["book_name"])
            if not info["book_name"]:return None
            for volume in info["volumes"]:
                seen=set()
                for ch in volume["chapters"]:
                    cid=ch["chapterId"]
                    if not re.fullmatch(r"[0-9]{1,6}",cid) or int(cid)<2 or cid in seen:raise ValueError("invalid/duplicate page ID")
                    u=urlsplit(ch["url"])
                    if u.scheme!="https" or u.hostname not in (HOST52,"52shuku.net") or not PATH52.fullmatch(u.path):raise ValueError("foreign reading page")
                    seen.add(cid)
                ids=[int(ch["chapterId"]) for ch in volume["chapters"]]
                if ids!=list(range(2,2+len(ids))):raise ValueError("missing or unordered reading pages")
        return info
class CzbooksSearcher(RuleSearcher):
    @property
    def nsfw(self):return True
    async def _fetch_html(self,keyword):
        from urllib.parse import quote
        url="https://czbooks.net/s/"+quote(keyword,safe="")
        self._request_url=url
        self._final_url=""
        async with self.session.get(url,params={"q":keyword},allow_redirects=False) as response:
            if response.status==403 and response.headers.get("cf-mitigated","").lower()=="challenge":
                raise RuntimeError("CZBOOKS_CHALLENGE: original site requires browser verification")
            response.raise_for_status()
            if response.status!=200:
                raise RuntimeError(f"CZBOOKS_HTTP_{response.status}")
            return await self._text_of(response)

def plugin_classes(item):
    rule=parse_rule(item["rule"],source=item["id"]); key=item["id"];ext=item["extension"]
    fb=ReadingPageFetcher if ext=="reading_pages" else RuleFetcher
    pb=ReadingPageParser if ext=="reading_pages" else RuleParser
    sb={"reading_pages":ReadingPageSearcher,"czbooks":CzbooksSearcher}[ext]
    def init(self,config,cookies=None,**kwargs):
        fb.__init__(self,apply_limits(config,rule.limits) if rule.limits else config,cookies,**kwargs)
    return dict(fetcher=type(key+"Fetcher",(fb,),dict(_rule=rule,site_name=key,DEFAULT_BASE_URL=rule.base_url,__init__=init)),
                parser=type(key+"Parser",(pb,),dict(_rule=rule,site_name=key)),
                searcher=type(key+"Searcher",(sb,),dict(_rule=rule,site_name=key,BASE_URL=rule.base_url,priority=rule.search.priority)),
                client=ReadingPageClient if ext=="reading_pages" else None)
def register_sources(build):
    if __version__!="1.41.1":raise RuntimeError("unverified upstream version "+__version__)
    build=Path(build).resolve();m=read_json(build/"manifest.json")
    if m["engine_commit"]!=ENGINE_COMMIT or digest({k:v for k,v in m.items() if k!="sha256"})!=m["sha256"]:raise ValueError("manifest mismatch")
    for x in m["sources"]:
        if x["backend"]=="native" and read_json(build/"rules"/(x["id"]+".json"))!=x["rule"]:raise ValueError("rule mismatch")
    os.environ["NOVEL_RULES_DIR"]=str(build/"rules")
    from novel_downloader.plugins.rule import register_all
    register_all()
    for x in m["sources"]:
        if x["backend"]=="plugin":
            if any(registrar.get_registered(x["id"],k) is not None for k in ("fetcher","parser","searcher","client")):raise RuntimeError("registration collision "+x["id"])
            registrar.register_site(x["id"],**plugin_classes(x))
    return m
