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
