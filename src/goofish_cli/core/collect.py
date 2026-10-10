"""被动采集 —— CDP Network 域纯监听常驻窗口的 mtop 响应。

方案：`docs/goofish-cli-被动采集方案.md`（§3.2 协议红线 / §4.3 目标发现 / §4.4 事件循环）。

红线（只读监听，页面 JS 上下文零改动）——本模块只允许发送：
  Target.setDiscoverTargets / Target.getTargets / Target.attachToTarget /
  Network.enable / Network.getResponseBody
明确不发送：Runtime.enable、Fetch.enable、Input.*、Page.navigate/reload、
Network.setCacheDisabled（会改变缓存行为）、Network.getCookies 等一切"驱动/干预"消息。

当前阶段（骨架）：连接 + 白名单过滤 + 原文落盘（采样）+ stdout JSONL 事件流。
解析器与 SQLite 入库为下一步（方案 §5 步骤 3）。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import requests
from loguru import logger
from websockets.asyncio.client import connect

from goofish_cli.core.browser import _cdp_candidates
from goofish_cli.core.errors import GoofishError

# ── 过滤与缓冲参数（方案 §4.4 / §4.5）────────────────────────────────────

GOOFISH_API_HOST = "h5api.m.goofish.com"
GOOFISH_PAGE_PREFIX = "https://www.goofish.com"

#: 内置采集白名单（方案 §4.5）。`GOOFISH_COLLECT_APIS="*"` 时可放开为全部（deny 恒生效）。
DEFAULT_CAPTURE_APIS = frozenset(
    {
        "mtop.taobao.idlemtopsearch.pc.search",
        "mtop.taobao.idle.pc.detail",
        "mtop.taobao.idle.item.web.recommend.list",
        "mtop.taobao.idlehome.home.webpc.feed",
        "mtop.idle.web.xyh.item.list",
    }
)

#: 永久排除前缀：聊天/订单通信（他人隐私）、风控遥测（不碰也不分析）。
DENY_API_PREFIXES = ("mtop.taobao.idlemessage.", "mtop.gaia.")

MAX_BODY_BYTES = 10 * 1024 * 1024
NETWORK_BUFFER_TOTAL = 50 * 1024 * 1024
NETWORK_BUFFER_RESOURCE = 25 * 1024 * 1024
NETWORK_BUFFER_POSTDATA = 64 * 1024
CDP_WS_MAX_SIZE = 32 * 1024 * 1024
RECONNECT_INTERVAL = 5.0
GET_BODY_CONCURRENCY = 4

_QUERY_API_RE = re.compile(r"[?&]api=([^&]+)")
_PATH_API_RE = re.compile(r"/h5/(mtop\.[^/]+)/")


# ── 配置读取（调用时读取，方便测试与运行时切换）──────────────────────────

def _debug_dump_enabled() -> bool:
    return os.environ.get("GOOFISH_COLLECT_DEBUG_DUMP") == "1"


def _dump_dir() -> Path:
    raw = os.environ.get("GOOFISH_COLLECT_DUMP_DIR")
    if raw:
        return Path(os.path.expanduser(raw))
    return Path.home() / ".goofish-cli" / "corpus" / "raw"


def _capture_apis() -> frozenset[str] | None:
    """采集 api 白名单。None = 全部（仍受 DENY 前缀约束）；缺省 = 内置白名单。"""
    raw = (os.environ.get("GOOFISH_COLLECT_APIS") or "").strip()
    if raw == "*":
        return None
    if raw:
        return frozenset(part.strip() for part in raw.split(",") if part.strip())
    return DEFAULT_CAPTURE_APIS


# ── 纯函数（可单测）──────────────────────────────────────────────────────

def _extract_api(url: str) -> str:
    """从 mtop URL 提取 api 名：优先 query 的 api=，兜底路径 /h5/<api>/。"""
    match = _QUERY_API_RE.search(url or "")
    if match:
        return match.group(1)
    match = _PATH_API_RE.search(url or "")
    return match.group(1) if match else ""


def _is_denied(api: str) -> bool:
    return api.startswith(DENY_API_PREFIXES)


def _should_capture(
    url: str,
    mime_type: str,
    status: int,
    size: int,
    allowed: frozenset[str] | None,
) -> bool:
    if not url or GOOFISH_API_HOST not in url:
        return False
    if status != 200 or "json" not in (mime_type or "").lower():
        return False
    if size > MAX_BODY_BYTES:
        return False
    api = _extract_api(url)
    if not api or _is_denied(api):
        return False
    return allowed is None or api in allowed


def _extract_search_query(post_data: str) -> str:
    """从 mtop 请求体提取搜索词 q（`data=<urlencoded JSON>` 形态）。"""
    if not post_data:
        return ""
    try:
        form = parse_qs(post_data, keep_blank_values=False)
        payload = (form.get("data") or [""])[0]
        if not payload:
            return ""
        parsed = json.loads(payload)
        query = parsed.get("q")
        return query if isinstance(query, str) else ""
    except (ValueError, TypeError):
        return ""


def _sanitize_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name or "").strip("_")[:120]


def _default_event_sink(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _fetch_browser_ws_endpoint(timeout: float = 1.5) -> str | None:
    """解析常驻窗口 CDP 浏览器级 WS 端点（双栈；要求响应含 Browser 标识）。"""
    for base in _cdp_candidates():
        try:
            resp = requests.get(f"{base}/json/version", timeout=timeout)
            if not resp.ok:
                continue
            data = resp.json() or {}
            if data.get("Browser") and data.get("webSocketDebuggerUrl"):
                return data["webSocketDebuggerUrl"]
        except Exception:  # noqa: BLE001 — 未启动 / 端口被占 / 超时都算不可用
            continue
    return None


# ── CDP 消息分发 ─────────────────────────────────────────────────────────

class CdpClient:
    """一条 CDP WebSocket 上的请求/事件分发器。

    - `send()` 挂起等待对应 id 的回包；回包由 `run()` 的读循环解析。
    - 事件（有 method 无 id）不阻塞读循环：派发为独立任务，
      因此事件处理里可以安全地 `await send()`（如 getResponseBody）。
    """

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._tasks: set[asyncio.Task] = set()

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        self._next_id += 1
        message_id = self._next_id
        message: dict[str, Any] = {"id": message_id, "method": method}
        if params is not None:
            message["params"] = params
        if session_id:
            message["sessionId"] = session_id

        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await self._ws.send(json.dumps(message))
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            raise GoofishError(f"CDP 调用超时：{method}") from None
        finally:
            self._pending.pop(message_id, None)

    async def run(self, handler: Callable[[dict[str, Any]], Any]) -> None:
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if message.get("id") is not None:
                    future = self._pending.pop(message["id"], None)
                    if future is None or future.done():
                        continue
                    error = message.get("error")
                    if error:
                        future.set_exception(
                            GoofishError(f"CDP 错误：{error.get('message') or error}")
                        )
                    else:
                        future.set_result(message.get("result") or {})
                elif message.get("method"):
                    task = asyncio.create_task(handler(message))
                    self._tasks.add(task)
                    task.add_done_callback(self._on_task_done)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("CDP 连接已关闭"))
            self._pending.clear()
            for task in self._tasks:
                task.cancel()

    @staticmethod
    def _on_task_done(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.debug(f"[collect] 事件处理异常：{task.exception()}")


# ── 采集器 ───────────────────────────────────────────────────────────────

class Collector:
    """只读监听常驻窗口：发现 goofish 页面 → Network 域收响应 → 取体 → 落盘/输出。

    `allowed_apis=None` 表示全部（仍受 DENY 前缀约束）；缺省生产用法走
    `Collector.from_env()`（默认内置白名单，`GOOFISH_COLLECT_APIS="*"` 放开）。
    """

    def __init__(
        self,
        *,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        dump_enabled: bool = False,
        dump_dir: Path | None = None,
        allowed_apis: frozenset[str] | None = None,
    ) -> None:
        self._sink = event_sink or _default_event_sink
        self.dump_enabled = dump_enabled
        self.dump_dir = dump_dir or _dump_dir()
        self.allowed_apis = allowed_apis
        self.captured = 0
        self._pending: dict[str, dict[str, Any]] = {}
        self._req_meta: dict[str, dict[str, Any]] = {}
        self._sessions: dict[str, str] = {}
        self._targets: set[str] = set()
        self._sem = asyncio.Semaphore(GET_BODY_CONCURRENCY)

    @classmethod
    def from_env(cls) -> Collector:
        return cls(
            dump_enabled=_debug_dump_enabled(),
            dump_dir=_dump_dir(),
            allowed_apis=_capture_apis(),
        )

    # ── 连接与重连 ───────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        """连常驻窗口监听；窗口不可达/断开时按固定间隔重连（日志限频）。"""
        last_warn = 0.0
        while True:
            endpoint = await asyncio.to_thread(_fetch_browser_ws_endpoint)
            if endpoint:
                try:
                    async with connect(
                        endpoint,
                        open_timeout=5,
                        max_size=CDP_WS_MAX_SIZE,
                        ping_interval=None,
                    ) as ws:
                        await self._session_loop(ws)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — 断开/协议异常都走重连
                    logger.warning(f"[collect] 连接中断：{e}（{RECONNECT_INTERVAL:.0f}s 后重连）")
            else:
                now = time.monotonic()
                if now - last_warn > 60:
                    logger.warning("[collect] 常驻窗口不可达（等待中；可先跑 goofish browser open）")
                    last_warn = now
            await asyncio.sleep(RECONNECT_INTERVAL)

    async def _session_loop(self, ws: Any) -> None:
        client = CdpClient(ws)
        self._pending.clear()
        self._req_meta.clear()
        self._sessions.clear()
        self._targets.clear()
        runner = asyncio.create_task(client.run(lambda message: self._handle_event(client, message)))
        try:
            await client.send("Target.setDiscoverTargets", {"discover": True})
            targets = await client.send("Target.getTargets")
            for info in targets.get("targetInfos", []):
                await self._maybe_attach(client, info)
            self._sink(
                {
                    "event": "listening",
                    "attached": len(self._targets),
                    "dump": self.dump_enabled,
                    "apis": "all" if self.allowed_apis is None else sorted(self.allowed_apis),
                }
            )
            await runner
        finally:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await runner

    # ── 目标发现与挂载（方案 §4.3）───────────────────────────────────────

    async def _maybe_attach(self, client: CdpClient, info: dict[str, Any]) -> None:
        target_id = info.get("targetId") or ""
        if not target_id or target_id in self._targets:
            return
        if info.get("type") != "page":
            return
        url = info.get("url") or ""
        if not url.startswith(GOOFISH_PAGE_PREFIX):
            return
        # 先占位再 await：setDiscoverTargets 的 targetCreated 与 getTargets 扫描会把
        # 同一 target 并发投递进来；占位防止重复挂载（重复会话会导致双份采集）。
        self._targets.add(target_id)
        try:
            result = await client.send(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True}
            )
        except GoofishError as e:
            self._targets.discard(target_id)
            logger.debug(f"[collect] attach 失败 {target_id}：{e}")
            return
        session_id = result.get("sessionId") or ""
        if not session_id:
            self._targets.discard(target_id)
            return
        self._sessions[session_id] = target_id
        await client.send(
            "Network.enable",
            {
                "maxTotalBufferSize": NETWORK_BUFFER_TOTAL,
                "maxResourceBufferSize": NETWORK_BUFFER_RESOURCE,
                "maxPostDataSize": NETWORK_BUFFER_POSTDATA,
            },
            session_id=session_id,
        )
        self._sink({"event": "attached", "url": url, "targetId": target_id})

    def _forget_session(self, session_id: str) -> None:
        target_id = self._sessions.pop(session_id, None)
        if target_id:
            self._targets.discard(target_id)
        stale = [rid for rid, meta in self._pending.items() if meta.get("session") == session_id]
        for rid in stale:
            self._pending.pop(rid, None)

    # ── Network 事件（方案 §4.4）─────────────────────────────────────────

    async def _handle_event(self, client: CdpClient, message: dict[str, Any]) -> None:
        method = message.get("method") or ""
        params = message.get("params") or {}
        session_id = message.get("sessionId") or ""
        try:
            if method in ("Target.targetCreated", "Target.targetInfoChanged"):
                await self._maybe_attach(client, params.get("targetInfo") or {})
            elif method == "Target.detachedFromTarget":
                self._forget_session(params.get("sessionId") or "")
            elif method == "Network.requestWillBeSent":
                self._on_request(params)
            elif method == "Network.responseReceived":
                self._on_response(session_id, params)
            elif method == "Network.loadingFinished":
                await self._on_finished(client, session_id, params)
            elif method == "Network.loadingFailed":
                request_id = params.get("requestId") or ""
                self._pending.pop(request_id, None)
                self._req_meta.pop(request_id, None)
        except Exception as e:  # noqa: BLE001 — 单事件失败不允许中断监听
            logger.debug(f"[collect] 事件处理失败（{method}）：{e}")

    def _on_request(self, params: dict[str, Any]) -> None:
        request = params.get("request") or {}
        url = request.get("url") or ""
        if GOOFISH_API_HOST not in url:
            return
        request_id = params.get("requestId") or ""
        post_data = request.get("postData") or ""
        query = _extract_search_query(post_data) if post_data else ""
        if request_id and query:
            self._req_meta[request_id] = {"q": query}

    def _on_response(self, session_id: str, params: dict[str, Any]) -> None:
        request_id = params.get("requestId") or ""
        response = params.get("response") or {}
        url = response.get("url") or ""
        status = int(response.get("status") or 0)
        mime_type = response.get("mimeType") or ""
        size = int(response.get("encodedDataLength") or 0)
        if not request_id or not _should_capture(url, mime_type, status, size, self.allowed_apis):
            return
        self._pending[request_id] = {
            "api": _extract_api(url),
            "url": url,
            "size": size,
            "session": session_id,
            "q": (self._req_meta.get(request_id) or {}).get("q", ""),
        }

    async def _on_finished(
        self, client: CdpClient, session_id: str, params: dict[str, Any]
    ) -> None:
        request_id = params.get("requestId") or ""
        meta = self._pending.pop(request_id, None)
        self._req_meta.pop(request_id, None)
        if not meta:
            return
        async with self._sem:
            try:
                result = await client.send(
                    "Network.getResponseBody", {"requestId": request_id}, session_id=session_id
                )
            except GoofishError as e:
                logger.debug(f"[collect] 取响应体失败（{meta['api']}）：{e}")
                return
        if result.get("base64Encoded"):
            logger.debug(f"[collect] 响应体为 base64（{meta['api']}），跳过")
            return
        body = result.get("body") or ""
        self.captured += 1
        event: dict[str, Any] = {
            "event": "captured",
            "api": meta["api"],
            "bytes": len(body),
            "url": meta["url"],
        }
        if meta.get("q"):
            event["q"] = meta["q"]
        if self.dump_enabled:
            event["file"] = str(self._dump(meta["api"], body))
        self._sink(event)

    def _dump(self, api: str, body: str) -> Path:
        self.dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        millis = int(time.time() * 1000) % 1000
        digest = hashlib.sha1(f"{api}:{time.time_ns()}".encode()).hexdigest()[:8]
        name = f"{stamp}-{millis:03d}-{_sanitize_filename(api) or 'unknown'}-{digest}.json"
        path = self.dump_dir / name
        path.write_text(body, encoding="utf-8")
        return path
