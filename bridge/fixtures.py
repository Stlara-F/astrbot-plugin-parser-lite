"""离线测试支持 — HTTP 响应录制/回放 (0 硬编码, 目录动态扫描).

用法:
  PARSER_LITE_RECORD_DIR=test/fixtures python run_local.py <url>   # 录制
  PARSER_LITE_REPLAY=1 python run_local.py <url>                    # 回放

实现: 通过 monkeypatch httpx.AsyncClient.send 拦截请求,
录制时落盘 {hash(url)}.json (method/url/status/headers/body),
回放时读盘返回缓存响应, 不触网.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _fixture_dir() -> Path | None:
    import os
    d = os.environ.get("PARSER_LITE_RECORD_DIR", "")
    return Path(d) if d else None


def _key(method: str, url: str) -> str:
    return hashlib.sha1(f"{method}:{url}".encode()).hexdigest()[:16]


def _load_fixture(method: str, url: str):
    d = _fixture_dir()
    if not d:
        return None
    p = d / f"{_key(method, url)}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return None


def _save_fixture(method: str, url: str, status: int, headers: dict, body: bytes) -> None:
    d = _fixture_dir()
    if not d:
        return
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "method": method,
        "url": url,
        "status": status,
        "headers": {k: v for k, v in headers.items() if k.lower() in
                    ("content-type", "content-length", "set-cookie", "location")},
        "body_b64": __import__("base64").b64encode(body).decode(),
    }
    p = d / f"{_key(method, url)}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def build_mock_transport():
    """构造 httpx.MockTransport: 回放已录制响应, 未录制的返回 404.

    仅用于离线测试 — 通过 PARSER_LITE_REPLAY=1 启用.
    """
    import os
    if os.environ.get("PARSER_LITE_REPLAY") != "1":
        return None
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        fx = _load_fixture(request.method, str(request.url))
        if fx is None:
            return httpx.Response(404, request=request, json={"error": "no fixture"})
        import base64
        body = base64.b64decode(fx["body_b64"])
        return httpx.Response(
            fx["status"],
            headers=fx.get("headers", {}),
            content=body,
            request=request,
        )

    return httpx.MockTransport(handler)


def patch_httpx_send(replay: bool):
    """monkeypatch httpx.AsyncClient.send — 录制/回放钩子.

    录制时: 真实请求后落盘; 回放时: 直接返回 fixture.
    """
    import httpx

    original = httpx.AsyncClient.send
    d = _fixture_dir()

    async def send(self, request: httpx.Request, *args, **kwargs):
        if replay:
            fx = _load_fixture(request.method, str(request.url))
            if fx is not None:
                import base64
                return httpx.Response(
                    fx["status"], headers=fx.get("headers", {}),
                    content=base64.b64decode(fx["body_b64"]), request=request)
        resp = await original(self, request, *args, **kwargs)
        if d is not None:
            try:
                await resp.aread()
                _save_fixture(request.method, str(request.url),
                              resp.status_code, resp.headers, resp.content)
            except Exception:
                pass
        return resp

    httpx.AsyncClient.send = send  # type: ignore[method-assign]
    return send


def install_httpx_hook() -> None:
    """按环境变量安装录制/回放钩子 (幂等, 无配置则不动作)."""
    import os
    d = os.environ.get("PARSER_LITE_RECORD_DIR", "")
    replay = os.environ.get("PARSER_LITE_REPLAY") == "1"
    if d or replay:
        patch_httpx_send(replay)
