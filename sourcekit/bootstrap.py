import os
from .bridge import register_sources
register_sources(os.environ["SOURCEKIT_BUILD"])
from novel_downloader.apps.api.app import create_app
app=create_app()
