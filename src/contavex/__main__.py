"""Command line: `python -m contavex serve | healthcheck | openapi`."""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from typing import Any

from contavex.config import ConfigError, Settings
from contavex.observability import configure_logging, log
from contavex.secrets import SecretError


def serve() -> int:
    import uvicorn

    from contavex.api.app import create_app
    from contavex.services import build_services
    from contavex.staging.self_hosted import SelfHostedStager

    try:
        settings = Settings.from_env()
        configure_logging(settings.log_level)
        services = build_services(settings)
    except (ConfigError, SecretError) as exc:
        configure_logging()
        log.error("startup_failed", error=str(exc))
        return 78  # EX_CONFIG
    apps: list[tuple[Any, int]] = [(create_app(services), settings.port)]
    if isinstance(services.stager, SelfHostedStager):
        # The public media listener: a separate app on a separate port, so the
        # consumer API can never be exposed by publishing it.
        apps.append((services.stager.app(), settings.public_port))
        log.info("public_media_listener", port=settings.public_port)
    servers = [
        uvicorn.Server(
            uvicorn.Config(app, host=settings.host, port=port, log_config=None, access_log=False)
        )
        for app, port in apps
    ]

    async def run() -> None:
        await asyncio.gather(*(server.serve() for server in servers))

    asyncio.run(run())
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

    from contavex.api.app import create_app

    # Route definitions do not touch services until a request arrives.
    app: FastAPI = create_app(None, start_worker=False)  # type: ignore[arg-type]
    json.dump(app.openapi(), sys.stdout, indent=2)
    return 0


def main(argv: list[str]) -> int:
    commands = {"serve": serve, "healthcheck": healthcheck, "openapi": openapi}
    if len(argv) != 2 or argv[1] not in commands:
        print(f"usage: python -m contavex {{{'|'.join(commands)}}}", file=sys.stderr)
        return 64  # EX_USAGE
    return commands[argv[1]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
