"""Resolve original TXT links; direct files are not chapter jobs."""
import re
from urllib.parse import urljoin,urlsplit,parse_qs
from lxml import html
BASE="https://www.ijjxsxzw.com"
def resolve_download_page(book_id):
    if not re.fullmatch(r"[0-9]{1,12}",book_id):raise ValueError("invalid book ID")
    return BASE+"/txt/dl-"+book_id+".html"
def extract_direct_link(book_id,raw):
    resolve_download_page(book_id)
    links=html.fromstring(raw).cssselect("a.download_1")
    if not links:raise ValueError("missing download link")
    url=urljoin(BASE,links[0].get("href",""));u=urlsplit(url);q=parse_qs(u.query)
    if (u.scheme!="https" or u.hostname!="www.ijjxsxzw.com" or u.port not in (None,443) or u.username or u.password or u.path!="/e/DownSys/doaction.php" or q.get("id")!=[book_id] or q.get("pathid")!=["0"] or q.get("enews")!=["DownSoft"]):raise ValueError("invalid direct link")
    return url

def resolve_live(book_id):
    """Fresh public download-page resolution; refuse redirects, limit response."""
    import requests
    with requests.get(resolve_download_page(book_id),timeout=(8,15),stream=True,allow_redirects=False) as r:
        if r.status_code!=200:raise RuntimeError(f"download page HTTP {r.status_code}")
        content=bytearray()
        for chunk in r.iter_content(65536):
            content.extend(chunk)
            if len(content)>2*1024*1024:raise ValueError("download page too large")
    return extract_direct_link(book_id,bytes(content))
