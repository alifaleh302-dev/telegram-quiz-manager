#!/usr/bin/env python3
"""Development entry point: ``python run.py``.

For production prefer: ``uvicorn app.main:app --host 0.0.0.0 --port 8000``.
"""

from __future__ import annotations

import uvicorn

from app.core.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_debug,
        log_config=None,
    )


if __name__ == "__main__":
    main()
