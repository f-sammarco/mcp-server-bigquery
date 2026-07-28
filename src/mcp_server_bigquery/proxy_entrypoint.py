"""Run mcp-proxy with an extra ``/healthz`` endpoint for container probes.

The upstream BigQuery server speaks stdio, so the image fronts it with
mcp-proxy. mcp-proxy only routes ``/status``, ``/sse``, ``/mcp`` and
``/messages/``; orchestrators that probe the conventional ``/healthz`` get a
404, fail readiness/liveness and restart the container in a loop.

Rather than reimplement mcp-proxy's stdio/session lifecycle just to register
one route, wrap the ASGI application it hands to uvicorn. Everything else -
CLI flags, transports, named servers - stays upstream behaviour.
"""

from __future__ import annotations

import json
import sys
from typing import Any

# Paths answered locally instead of being forwarded to mcp-proxy. A trailing
# slash is tolerated; probes are not always consistent about it.
HEALTH_PATHS = frozenset({"/healthz", "/health", "/livez", "/readyz"})

_HEALTH_BODY = json.dumps({"status": "ok"}).encode()


class HealthEndpoint:
    """ASGI middleware answering the health paths, delegating the rest."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and self._is_health_request(scope):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(_HEALTH_BODY)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _HEALTH_BODY})
            return
        await self._app(scope, receive, send)

    @staticmethod
    def _is_health_request(scope: Any) -> bool:
        if scope.get("method") not in {"GET", "HEAD"}:
            return False
        path = str(scope.get("path") or "")
        normalized = path.rstrip("/") or "/"
        return normalized in HEALTH_PATHS


def _install_health_endpoint() -> None:
    """Make every uvicorn app built in this process serve the health paths.

    mcp-proxy resolves ``uvicorn.Config`` at call time, so patching the
    attribute on the uvicorn module is enough - and it is the only upstream
    detail this shim depends on. Only this process is affected.
    """
    import uvicorn

    original_config = uvicorn.Config
    if getattr(original_config, "_wraps_health_endpoint", False):
        return

    def config_with_health(*args: Any, **kwargs: Any) -> Any:
        if "app" in kwargs:
            kwargs["app"] = HealthEndpoint(kwargs["app"])
        elif args:
            args = (HealthEndpoint(args[0]), *args[1:])
        return original_config(*args, **kwargs)

    config_with_health._wraps_health_endpoint = True  # type: ignore[attr-defined]
    uvicorn.Config = config_with_health  # type: ignore[assignment]


def main() -> None:
    """Install the health endpoint, then hand over to mcp-proxy's CLI."""
    _install_health_endpoint()

    from mcp_proxy.__main__ import main as proxy_main

    proxy_main()


if __name__ == "__main__":
    sys.exit(main())
