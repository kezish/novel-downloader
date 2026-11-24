import asyncio
import pytest
from sourcekit.webcli import resolve,query
from sourcekit.bridge import CzbooksSearcher

def test_resolve():
    assert resolve("https://czbooks.net/n/abc")==("sk_czbooks","abc")
    assert resolve("https://www.biquge5.com/123/")==("sk_biquge5","123")
    assert resolve("https://www.52shuku.net/gl/a.html")[0]=="sk_shuku52"
    for url in ("https://czbooks.net/n/abc/def","https://evil.example/n/a","https://www.52shuku.net/gl/a_99.html","https://www.biquge5.com/1/2.html","https://czbooks.net@evil.example/n/a"):
        with pytest.raises(ValueError):resolve(url)

def test_cz_challenge():
    class Response:
        status=403
        headers={"cf-mitigated":"challenge"}
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class Session:
        def get(self,*args,**kwargs):return Response()
    with pytest.raises(RuntimeError,match="CZBOOKS_CHALLENGE"):
        asyncio.run(CzbooksSearcher(Session())._fetch_html("測試"))

@pytest.mark.parametrize("site,url,href",[
 ("sk_shuku52","https://www.52shuku.net/so/search.php?q=%E9%87%8D%E7%94%9F","/so/search.php?q=%E9%87%8D%E7%94%9F&m=no&f=_all&syn=no&p=2"),
 ("sk_biquge5","https://www.biquge5.com/search.php?q=%E9%87%8D%E7%94%9F","/search.php?q=%E9%87%8D%E7%94%9F&p=2"),
 ("sk_czbooks","https://czbooks.net/s/重生","/s/重生/2")
])
def test_pager(site,url,href):
    from sourcekit.paged_search import linked_page,pagination,page_url
    assert linked_page(site,"重生",href,url)==2
    assert linked_page(site,"different",href,url) is None
    assert linked_page(site,"重生","https://evil.example"+href,url) is None
    fragment=f'<div class="xs-pagination pages"><nav class="paginate"><a href="{href}">2</a></nav></div>'
    assert pagination(site,"重生",fragment,1,url)==2
    assert pagination(site,"重生",fragment,2,url) is None
    assert linked_page(site,"重生",page_url(site,"重生",2),url)==2

def test_search_more_than_15_and_next_page(monkeypatch):
    import sourcekit.paged_search as m
    class Parser:
        def __init__(self,session):pass
        def _parse_html(self,raw,limit):
            assert limit is None
            page=int(raw)
            return [{"book_url":f"https://www.biquge5.com/{page*100+i}/","title":str(i),"author":"作者"} for i in range(20)]
    async def get(session,site,keyword,page):return str(page),"https://www.biquge5.com/search.php"
    async def sleep(seconds):pass
    monkeypatch.setattr(m.registrar,"get_searcher_class",lambda site:Parser)
    monkeypatch.setattr(m,"fetch",get)
    monkeypatch.setattr(m,"pagination",lambda site,keyword,raw,page,url:page+1 if page<4 else None)
    monkeypatch.setattr(m.asyncio,"sleep",sleep)
    rows,status=asyncio.run(m.search_site(None,"sk_biquge5","重生",1,resolve))
    assert len(rows)==60 and status["next_page"]==4 and status["pages"]==3
    rows,status=asyncio.run(m.search_site(None,"sk_biquge5","重生",4,resolve))
    assert len(rows)==20 and status["next_page"] is None

def test_partial_pages_retained(monkeypatch):
    import sourcekit.paged_search as m
    class Parser:
        def __init__(self,s):pass
        def _parse_html(self,raw,limit):return [{"book_url":"https://czbooks.net/n/abc","title":"合成","author":"作者"}]
    async def get(session,site,keyword,page):
        if page==2:raise RuntimeError("HTTP 503")
        return "ok","https://czbooks.net/s/test"
    async def sleep(seconds):pass
    monkeypatch.setattr(m.registrar,"get_searcher_class",lambda s:Parser)
    monkeypatch.setattr(m,"fetch",get)
    monkeypatch.setattr(m,"pagination",lambda *a:2)
    monkeypatch.setattr(m.asyncio,"sleep",sleep)
    rows,status=asyncio.run(m.search_site(None,"sk_czbooks","test",1,resolve))
    assert len(rows)==1 and status["state"]=="failed" and status["next_page"]==2

def test_cli_cursor_arguments(monkeypatch,capsys):
    import sys,json
    import sourcekit.webcli as cli
    monkeypatch.setattr(cli,"register_sources",lambda path:None)
    async def query(keyword,sites,cursors):
        assert keyword=="重生" and sites==["sk_shuku52"] and cursors=={"sk_shuku52":4}
        return {"results":[],"statuses":{}}
    monkeypatch.setattr(cli,"query",query)
    monkeypatch.setattr(sys,"argv",["webcli","search","--sites","sk_shuku52","--cursors",'{"sk_shuku52":4}',"--","重生"])
    assert cli.main()==0
    assert json.loads(capsys.readouterr().out)["results"]==[]
