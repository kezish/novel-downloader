import asyncio, json
from pathlib import Path
import pytest
from sourcekit.compiler import ROOT,compile_all,merge,read_json,expand
from sourcekit.bridge import encode52,decode52,page52,split52,register_sources,ReadingPageClient
from sourcekit.direct import extract_direct_link,resolve_download_page
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import ParserConfig,ClientConfig,BookConfig,ExporterConfig
from novel_downloader.infra.persistence.chapter_storage import ChapterStorage

@pytest.fixture(scope="session",autouse=True)
def bundle(tmp_path_factory):
    root=tmp_path_factory.mktemp("bundle")
    manifest=compile_all(root)
    register_sources(root)
    return root,manifest

def test_compile(bundle,tmp_path):
    root,m=bundle
    assert compile_all(tmp_path)["sha256"]==m["sha256"]
    assert [p.name for p in (root/"rules").glob("*.json")]==["sk_biquge5.json"]
    assert merge({"a":{"x":1,"y":[1]}},{"a":{"y":[2]}})=={"a":{"x":1,"y":[2]}}
    for item in m["sources"]:
        if item["backend"]!="direct":
            for k in ("fetcher","parser","searcher"):assert registrar.get_registered(item["id"],k)
    assert registrar.get_registered("sk_jiujiu","searcher") is None

def test_bad_source():
    s=read_json(ROOT/"sources/biquge5.json");s["extends"]="../bad"
    with pytest.raises(ValueError):expand(s)

def test_identity():
    u="https://www.52shuku.net/gl/24_b/demo.html"
    bid=encode52(u)
    assert decode52(bid)=="/gl/24_b/demo.html"
    assert page52(bid,"3")==u[:-5]+"_3.html"
    for bad in ("https://evil.example/gl/demo.html","https://www.52shuku.net/a/../b.html","file:///a/b.html"):
        with pytest.raises(ValueError):encode52(bad)
    with pytest.raises(ValueError):page52(bid,"../x")
    assert split52("1. 示例_作者【完结】")==("示例","作者","完结")

def test_52_export(tmp_path):
    p=registrar.get_parser("sk_shuku52",ParserConfig())
    b=p.parse_book_info(['<h1 class="article-title">示例_作者【完结】</h1><article class="article-content"><p>小说简介：</p><p>简介</p></article><ul><li class="mulu"><a href="/gl/demo_2.html">第1页</a></li><li class="mulu"><a href="/gl/demo_3.html">第2页</a></li></ul>'])
    assert b["book_name"]=="示例" and b["author"]=="作者"
    assert [x["chapterId"] for x in b["volumes"][0]["chapters"]]==["2","3"]
    first=p.parse_chapter_content(['<article class="article-content"><p>完整简介</p><p>第1章</p><p>合成正文甲</p><p>小贴士：来52书库</p></article>'],"2")
    second=p.parse_chapter_content(['<article class="article-content"><p>合成正文乙</p></article>'],"3")
    assert "完整简介" in first["content"] and "小贴士" not in first["content"]
    client=registrar.get_client("sk_shuku52",ClientConfig(raw_data_dir=str(tmp_path/"raw"),output_dir=str(tmp_path/"out")))
    assert isinstance(client,ReadingPageClient)
    bid=encode52("https://www.52shuku.net/gl/demo.html")
    client._save_book_info(bid,b)
    with ChapterStorage(client._book_dir(bid),filename="chapter.raw.sqlite") as storage:
        storage.upsert_chapters([first,second])
    result=client.export_book(BookConfig(bid),ExporterConfig(append_timestamp=False),stage="raw")
    text=result["txt"][0].read_text()
    assert text.count("完整简介")==1 and "第1页" not in text and "第2页" not in text
    assert text.index("合成正文甲")<text.index("合成正文乙")

def test_52_gap():
    p=registrar.get_parser("sk_shuku52",ParserConfig())
    with pytest.raises(ValueError):
        p.parse_book_info(['<h1 class="article-title">示例_作者</h1><li class="mulu"><a href="/gl/demo_2.html">第1页</a></li><li class="mulu"><a href="/gl/demo_4.html">第3页</a></li>'])

def test_biquge():
    p=registrar.get_parser("sk_biquge5",ParserConfig())
    info=p.parse_book_info(['<meta property="og:novel:book_name" content="示例"><meta property="og:novel:author" content="作者"><div class="book_list2"><a href="/12/101.html">第一章</a></div>','<div class="book_list2"><a href="/12/102.html">第二章</a></div>'])
    assert info["book_name"]=="示例"
    assert [x["chapterId"] for x in info["volumes"][0]["chapters"]]==["101","102"]
    ch=p.parse_chapter_content(["<article>甲</article>","<article>乙</article>"],"101")
    assert ch["content"]=="甲\n乙"

def test_czbooks():
    p=registrar.get_parser("sk_czbooks",ParserConfig())
    info=p.parse_book_info(['<div class="info"><h1 class="title">測試</h1><span class="author"><a>作者</a></span></div><ul id="chapter-list"><li><a href="/n/abc/def">第一章</a></li><li><a href="/n/abc/def">重複</a></li></ul>'])
    assert info["book_name"]=="測試" and len(info["volumes"][0]["chapters"])==1
    assert p.parse_chapter_content(['<div class="content">測試正文</div>'],"def")["content"]=="測試正文"
    cls=registrar.get_searcher_class("sk_czbooks")
    assert cls(None).nsfw

def test_search():
    cls=registrar.get_searcher_class("sk_shuku52")
    results=cls(None)._parse_html('<article class="excerpt"><header><h2><a href="/gl/demo.html"><h4>1. 示例_作者【完结】</h4></a></h2></header></article>')
    assert len(results)==1 and results[0]["title"]=="示例" and results[0]["author"]=="作者"
    assert decode52(results[0]["book_id"])=="/gl/demo.html"

def test_direct():
    assert resolve_download_page("123").endswith("dl-123.html")
    href="/e/DownSys/doaction.php?enews=DownSoft&amp;id=123&amp;pathid=0"
    assert extract_direct_link("123",f'<a class="download_1" href="{href}">TXT</a>').startswith("https://www.ijjxsxzw.com/")
    with pytest.raises(ValueError):extract_direct_link("124",f'<a class="download_1" href="{href}">TXT</a>')
    with pytest.raises(ValueError):extract_direct_link("123",'<a class="download_1" href="https://evil.example/">TXT</a>')

def test_missing_export_refused(tmp_path):
    client=registrar.get_client("sk_shuku52",ClientConfig(raw_data_dir=str(tmp_path/"raw"),output_dir=str(tmp_path/"out")))
    bid=encode52("https://www.52shuku.net/gl/demo.html")
    client._save_book_info(bid,{"book_name":"合成","author":"作者","volumes":[{"volume_name":"正文","chapters":[{"chapterId":"2","title":"第1页","url":page52(bid,"2")},{"chapterId":"3","title":"第2页","url":page52(bid,"3")}]}]})
    with ChapterStorage(client._book_dir(bid),filename="chapter.raw.sqlite") as storage:
        storage.upsert_chapter({"id":"2","title":"","content":"合成文本","extra":{}})
    with pytest.raises(ValueError,match="incomplete"):
        client.export_book(BookConfig(bid),stage="raw")

def test_manifest_integrity(bundle,tmp_path):
    from sourcekit.bridge import register_sources
    m=compile_all(tmp_path)
    path=tmp_path/"manifest.json"
    m["engine_commit"]="bad"
    path.write_text(json.dumps(m))
    with pytest.raises(ValueError,match="manifest mismatch"):
        register_sources(tmp_path)

def test_cz_search():
    cls=registrar.get_searcher_class("sk_czbooks")
    rows=cls(None)._parse_html('<ul class="novel-list"><li class="novel-item-wrapper"><div class="novel-item-cover-wrapper"><a href="/n/abc"></a></div><div class="novel-item-title">合成小說</div><div class="novel-item-author"><a>作者</a></div></li></ul>')
    assert rows[0]["book_id"]=="abc" and rows[0]["title"]=="合成小說"

@pytest.mark.parametrize("site,url,pages,expected",[
    ("sk_biquge5","https://www.biquge5.com/123/",{
        "https://www.biquge5.com/123/":'<meta property="og:novel:book_name" content="合成"><meta property="og:novel:author" content="作者"><div class="book_list2"><a href="/123/101.html">第一章</a></div><a href="/123/index_2.html">下一页</a>',
        "https://www.biquge5.com/123/index_2.html":'<div class="book_list2"><a href="/123/102.html">第二章</a></div>',
        "https://www.biquge5.com/123/101.html":'<article>合成甲</article><a href="101_2.html">下一页</a>',
        "https://www.biquge5.com/123/101_2.html":'<article>合成乙</article>',
        "https://www.biquge5.com/123/102.html":'<article>合成丙</article>'
    },["合成甲","合成乙","合成丙"]),
    ("sk_czbooks","https://czbooks.net/n/demo",{
        "https://czbooks.net/n/demo":'<div class="info"><h1 class="title">合成</h1><span class="author"><a>作者</a></span></div><ul id="chapter-list"><li><a href="/n/demo/a">第一章</a></li><li><a href="/n/demo/b">第二章</a></li></ul>',
        "https://czbooks.net/n/demo/a":'<div class="content">合成甲</div>',
        "https://czbooks.net/n/demo/b":'<div class="content">合成乙</div>'
    },["合成甲","合成乙"]),
    ("sk_shuku52","https://www.52shuku.net/gl/demo.html",{
        "https://www.52shuku.net/gl/demo.html":'<h1 class="article-title">合成_作者</h1><li class="mulu"><a href="/gl/demo_2.html">第1页</a></li><li class="mulu"><a href="/gl/demo_3.html">第2页</a></li>',
        "https://www.52shuku.net/gl/demo_2.html":'<article class="article-content"><p>合成简介</p><p>第1章</p><p>合成甲</p></article>',
        "https://www.52shuku.net/gl/demo_3.html":'<article class="article-content"><p>合成乙</p></article>'
    },["合成简介","合成甲","合成乙"])
])
def test_full_pipeline(bundle,tmp_path,monkeypatch,site,url,pages,expected):
    from sourcekit.webcli import download
    from novel_downloader.plugins.rule.fetcher import RuleFetcher
    calls=[]
    async def get(self,url,*args,**kwargs):
        calls.append(url)
        return pages[url]
    monkeypatch.setattr(RuleFetcher,"_get",get)
    out=tmp_path/"out"
    assert asyncio.run(download(url,out,tmp_path/"cache"))==0
    paths=list(out.glob("*.txt"))
    assert len(paths)==1
    text=paths[0].read_text()
    assert all(text.count(x)==1 for x in expected)
    assert [text.index(x) for x in expected]==sorted(text.index(x) for x in expected)
    assert set(calls)==set(pages)

def test_pagination_error_is_not_completion(bundle,monkeypatch):
    from novel_downloader.schemas import FetcherConfig
    from novel_downloader.plugins.rule.fetcher import RuleFetcher
    async def get(self,url,*args,**kwargs):
        if url.endswith("101.html"):return '<article>甲</article><a href="101_2.html">下一页</a>'
        raise RuntimeError("fixture page failed")
    monkeypatch.setattr(RuleFetcher,"_get",get)
    f=registrar.get_fetcher("sk_biquge5",FetcherConfig())
    with pytest.raises(RuntimeError,match="fixture page failed"):
        asyncio.run(f.fetch_chapter_content("123","101"))


def test_metadata_without_catalog(bundle):
    from sourcekit.metadata import extract_metadata
    cz=extract_metadata("sk_czbooks",'<div class="info"><h1 class="title">測試</h1><span class="author"><a>作者</a></span></div><div class="description">简介<br>第二行</div>')
    assert cz["title"]=="測試" and "第二行" in cz["description"]
    b=extract_metadata("sk_biquge5",'<meta property="og:novel:book_name" content="测试"><meta property="og:description" content="测试简介">')
    assert b["description"]=="测试简介"
    import pytest
    with pytest.raises(ValueError):extract_metadata("sk_czbooks","<html>error</html>")
