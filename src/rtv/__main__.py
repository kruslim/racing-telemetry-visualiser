"""``python -m rtv`` entry point — runs the API server with uvicorn."""

from __future__ import annotations

import uvicorn

from rtv.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "rtv.main:app",
        host=settings.host,
        port=settings.port,
        workers=1,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
