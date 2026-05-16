"""Import-string entrypoint for `uvicorn --reload`.

The reloader spawns its own worker process and imports this module to get
the ASGI app, so we can't pass an already-constructed FastAPI instance.
Flags are picked up from environment variables set by `murano serve --reload`.
"""

from __future__ import annotations

import os

from .server import create_app

app = create_app(
    enable_schedule=os.environ.get("MURANO_ENABLE_SCHEDULE", "1") == "1",
    enable_watch=os.environ.get("MURANO_ENABLE_WATCH", "1") == "1",
)
