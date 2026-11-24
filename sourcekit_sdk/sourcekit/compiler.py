"""Versioned template expansion, validated by the upstream parser."""
import copy, hashlib, json, re
from pathlib import Path
from urllib.parse import urlsplit
from novel_downloader.plugins.rule.model import parse_rule, SearchRule
ROOT=Path(__file__).resolve().parents[1]
ENGINE_COMMIT="551ae6b8dcb4dae2656def3db8bc986a509365d3"
def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
def merge(base, overrides):
    result=copy.deepcopy(base)
    for k,v in overrides.items():
        result[k]=merge(result[k],v) if isinstance(v,dict) and isinstance(result.get(k),dict) else copy.deepcopy(v)
    return result
def expand(spec):
    if set(spec)-{"schema_version","id","extends","backend","extension","rule","direct"}:
        raise ValueError("unknown source fields")
    if spec.get("schema_version")!=1 or not re.fullmatch(r"sk_[a-z0-9_]+",spec.get("id","")):
        raise ValueError("invalid source schema/id")
    backend=spec.get("backend","native"); ext=spec.get("extension")
    if backend not in {"native","plugin","direct"} or (backend=="plugin")!=(ext in {"reading_pages","czbooks"}):
        raise ValueError("invalid backend/extension")
    raw={}
    if template:=spec.get("extends"):
        if not re.fullmatch(r"[a-z0-9_-]+@[0-9]+",template): raise ValueError("invalid template")
        raw=read_json(ROOT/"templates"/(template+".json"))
    raw=merge(raw,spec["rule"]); raw["site_key"]=spec["id"]
    u=urlsplit(raw["base_url"])
    if u.scheme!="https" or not u.hostname or u.username or u.password or u.port not in (None,443): raise ValueError("invalid origin")
    if backend=="direct":
        SearchRule.from_dict(raw["search"])
        if spec.get("direct",{}).get("content_kind")!="txt":raise ValueError("invalid direct file mode")
    else:parse_rule(raw,source=spec["id"])
    return dict(id=spec["id"],backend=backend,extension=ext,rule=raw,direct=spec.get("direct"))
def digest(m):
    return hashlib.sha256(json.dumps(m,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
def compile_all(out):
    out=Path(out); rules=out/"rules"; rules.mkdir(parents=True,exist_ok=True)
    sources=[expand(read_json(p)) for p in sorted((ROOT/"sources").glob("*.json"))]
    if len({x["id"] for x in sources})!=len(sources): raise ValueError("duplicate id")
    for x in sources:
        if x["backend"]=="native":
            (rules/(x["id"]+".json")).write_text(json.dumps(x["rule"],ensure_ascii=False,indent=2)+"\n")
    if {p.name for p in rules.glob("*.json")}!={x["id"]+".json" for x in sources if x["backend"]=="native"}: raise ValueError("stale rules")
    manifest=dict(schema_version=1,engine_commit=ENGINE_COMMIT,sources=sources)
    manifest["sha256"]=digest(manifest)
    (out/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n")
    return manifest
