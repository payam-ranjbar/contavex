"""Command line: `python -m content_fleet serve | healthcheck | openapi`."""

from __future__ import annotations

import json
import sys
import urllib.request

from content_fleet.config import ConfigError, Settings
from content_fleet.observability import configure_logging, log
from content_fleet.secrets import SecretError


def serve() -> int:
    import uvicorn

    from content_fleet.api.app import create_app
    from content_fleet.services import build_services

    try:
        settings = Settings.from_env()
        configure_logging(settings.log_level)
        services = build_services(settings)
    except (ConfigError, SecretError) as exc:
        configure_logging()
        log.error("startup_failed", error=str(exc))
        return 78  # EX_CONFIG
    uvicorn.run(
        create_app(services),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
    )
    return 0


def healthcheck() -> int:
    """Readiness probe for Docker's HEALTHCHECK; the image has no curl."""
    port = Settings.from_env().port
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=3) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


def openapi() -> int:
    """Print the OpenAPI document without starting the service."""
    from fastapi import FastAPI

    from content_fleet.api.app import create_app

    # Route definitions do not touch services until a request arrives.
    app: FastAPI = create_app(None, start_worker=False)  # type: ignore[arg-type]
    json.dump(app.openapi(), sys.stdout, indent=2)
    return 0


def main(argv: list[str]) -> int:
    commands = {"serve": serve, "healthcheck": healthcheck, "openapi": openapi}
    if len(argv) != 2 or argv[1] not in commands:
        print(f"usage: python -m content_fleet {{{'|'.join(commands)}}}", file=sys.stderr)
        return 64  # EX_USAGE
    return commands[argv[1]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
