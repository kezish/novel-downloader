"""Explicit startup integration for pinned upstream; no source-code edits."""
import os
from .bridge import register_sources
manifest=register_sources(os.environ["SOURCEKIT_BUILD"])
from novel_downloader.apps.constants import SEARCH_SUPPORT_SITES,DOWNLOAD_SUPPORT_SITES
for source in manifest["sources"]:
    if source["backend"]=="direct":
        continue
    key=source["id"]
    if key in SEARCH_SUPPORT_SITES or key in DOWNLOAD_SUPPORT_SITES:
        raise RuntimeError("duplicate API source key: "+key)
    SEARCH_SUPPORT_SITES[key]=source["rule"]["site_name"]
    DOWNLOAD_SUPPORT_SITES[key]=source["rule"]["site_name"]
from novel_downloader.apps.api.app import create_app
app=create_app()
