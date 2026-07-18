from __future__ import annotations

import argparse
import asyncio
import importlib
import os
from pathlib import Path
from typing import Any

import uvicorn

from .api import create_app
from .config import Settings
from .engine import OpsEngine


async def serve(settings: Settings, *, enable_uds: bool = True) -> None:
    settings.ensure_directories()
    engine = OpsEngine(settings)
    app = create_app(settings, engine)
    await engine.start()

    web_config = uvicorn.Config(
        app,
        host=settings.web_host,
        port=settings.web_port,
        log_level="info",
        lifespan="off",
        access_log=False,
    )
    servers = [uvicorn.Server(web_config)]
    socket_permission_task: asyncio.Task[None] | None = None
    if enable_uds and os.name != "nt":
        if settings.socket_path.exists():
            settings.socket_path.unlink()
        uds_config = uvicorn.Config(
            app, uds=str(settings.socket_path), log_level="warning", lifespan="off", access_log=False
        )
        servers.append(uvicorn.Server(uds_config))
        socket_permission_task = asyncio.create_task(_set_socket_permissions(settings.socket_path))

    async def serve_one(server: uvicorn.Server) -> None:
        try:
            await server.serve()
        finally:
            # Either Uvicorn instance may receive the process signal. Ensure the
            # TCP and Unix-socket listeners always leave together.
            for peer in servers:
                peer.should_exit = True

    try:
        await asyncio.gather(*(serve_one(server) for server in servers))
    finally:
        await engine.stop()
        if socket_permission_task:
            socket_permission_task.cancel()
        engine.store.close()


async def _set_socket_permissions(socket_path: Path) -> None:
    for _ in range(100):
        if socket_path.exists():
            socket_path.chmod(0o660)
            try:
                grp_module = importlib.import_module("grp")
                os_module: Any = os
                os_module.chown(socket_path, -1, grp_module.getgrnam("aiops-operators").gr_gid)
            except (AttributeError, ImportError, KeyError, PermissionError):
                pass
            return
        await asyncio.sleep(0.05)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Local AI Ops Agent")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--profile", choices=["live", "test", "demo"])
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-uds", action="store_true")
    args = parser.parse_args()
    settings = Settings.load(args.config, profile=args.profile)
    if args.host or args.port:
        from dataclasses import replace

        settings = replace(
            settings, web_host=args.host or settings.web_host, web_port=args.port or settings.web_port
        ).validate()
    asyncio.run(serve(settings, enable_uds=not args.no_uds))


if __name__ == "__main__":
    main()
