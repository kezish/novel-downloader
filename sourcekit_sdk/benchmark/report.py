"""Merge metadata-only benchmark snapshots and compare known title matches."""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from run import ROOT,norm

BASELINE=("sk_biquge5","sk_shuku52","sk_czbooks")

def matched(case,row):
    if row.get("status")!="parsed" or case["kind"]!="title":
        return False
    aliases={norm(x) for x in case["title_aliases"].split("|")}
    # A recorded exact-author title can be re-evaluated after adding a
    # reviewed edition alias; its original result rank may be unknown.
    return bool(row.get("exact_rank") or aliases.intersection(row.get("author_titles",[])))

def summarize(records,cases):
    grouped=defaultdict(list)
    for row in records:
        if row.get("case") in cases:
            grouped[(row["site"],row["case"])].append(row)
    hits={site:{cid for (s,cid),rs in grouped.items() if s==site and any(matched(cases[cid],r) for r in rs)} for site,_ in grouped}
    baseline=set().union(*(hits.get(s,set()) for s in BASELINE))
    output=[]
    for site in sorted(hits):
        title_ids={cid for s,cid in grouped if s==site and cases[cid]["kind"]=="title"}
        author_ids={cid for s,cid in grouped if s==site and cases[cid]["kind"]=="author"}
        usable={cid for cid in title_ids if any(r["status"]=="parsed" for r in grouped[(site,cid)])}
        blocked={cid for cid in title_ids|author_ids if any(r["status"]=="blocked" for r in grouped[(site,cid)])}
        uncertain=title_ids-usable
        incremental=hits[site]-baseline if site not in BASELINE else set()
        output.append(dict(site=site,title_queries=len(title_ids),parsed_title_queries=len(usable),matched_titles=len(hits[site]),matched_ids=sorted(hits[site]),uncertain_titles=len(uncertain),blocked_queries=len(blocked),author_queries=len(author_ids),potential_increment=sorted(incremental)))
    return {"distinct_queries":len({cid for _,cid in grouped}),"attempts":sum(len(rs) for rs in grouped.values()),"baseline_union":sorted(baseline),"sites":output}

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("reports",nargs="+",type=Path)
    a=p.parse_args()
    with (ROOT/"cases.tsv").open() as f:cases={r["id"]:r for r in csv.DictReader(f,delimiter="\t")}
    rows=[r for p in a.reports for r in json.loads(p.read_text())["records"]]
    print(json.dumps(summarize(rows,cases),ensure_ascii=False,indent=2))
