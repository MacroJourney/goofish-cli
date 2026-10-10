"""验被动采集骨架的过滤/解析纯函数与 CDP 消息分发（不连真实窗口）。"""
from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from goofish_cli.core.collect import (
    CdpClient,
    _extract_api,
    _extract_search_query,
    _sanitize_filename,
    _should_capture,
)

ALL_APIS = None
DETAIL_URL = (
    "https://h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail/1.0/"
    "?jsv=2.7.2&appKey=34839810&api=mtop.taobao.idle.pc.detail&v=1.0"
)
SEARCH_URL = (
    "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
    "?jsv=2.7.2&appKey=34839810&api=mtop.taobao.idlemtopsearch.pc.search&v=1.0"
)


def test_extract_api_from_query():
    assert _extract_api(DETAIL_URL) == "mtop.taobao.idle.pc.detail"


def test_extract_api_from_path_fallback():
    url = "https://h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail/1.0/"
    assert _extract_api(url) == "mtop.taobao.idle.pc.detail"
    assert _extract_api("https://example.com/other") == ""


def test_should_capture_basic_filters():
    assert _should_capture(SEARCH_URL, "application/json", 200, 1000, ALL_APIS)
    # 非 mtop 域名 / 非 JSON / 非 200 / 超限 一律不采
    assert not _should_capture("https://www.goofish.com/x", "application/json", 200, 1, ALL_APIS)
    assert not _should_capture(SEARCH_URL, "text/html", 200, 1, ALL_APIS)
    assert not _should_capture(SEARCH_URL, "application/json", 500, 1, ALL_APIS)
    assert not _should_capture(SEARCH_URL, "application/json", 200, 20 * 1024 * 1024, ALL_APIS)


def test_should_capture_whitelist_and_deny():
    allowed = frozenset({"mtop.taobao.idle.pc.detail"})
    assert _should_capture(DETAIL_URL, "application/json", 200, 1, allowed)
    assert not _should_capture(SEARCH_URL, "application/json", 200, 1, allowed)
    # DENY 前缀恒生效（即使 "*"）
    chat_url = DETAIL_URL.replace("mtop.taobao.idle.pc.detail", "mtop.taobao.idlemessage.pc.session.sync")
    gaia_url = DETAIL_URL.replace("mtop.taobao.idle.pc.detail", "mtop.gaia.nodejs.x")
    assert not _should_capture(chat_url, "application/json", 200, 1, ALL_APIS)
    assert not _should_capture(gaia_url, "application/json", 200, 1, ALL_APIS)


def test_extract_search_query():
    post = "data=%7B%22q%22%3A%22%E6%AF%9B%E5%91%A2%E5%A4%A7%E8%A1%A3%22%7D"
    assert _extract_search_query(post) == "毛呢大衣"
    assert _extract_search_query("") == ""
    assert _extract_search_query("data=notjson") == ""
    assert _extract_search_query("data=%7B%22page%22%3A1%7D") == ""


def test_sanitize_filename():
    assert _sanitize_filename("mtop.taobao.idle.pc.detail") == "mtop.taobao.idle.pc.detail"
    assert "/" not in _sanitize_filename("a/b c")
    assert len(_sanitize_filename("x" * 500)) <= 120


class _FakeWs:
    """最小假 WebSocket：send 立即回 id 响应；可注入事件帧。"""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def send(self, raw: str) -> None:
        self.sent.append(raw)
        message = json.loads(raw)
        await self.queue.put(json.dumps({"id": message["id"], "result": {"ok": True}}))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:  # websockets 兼容
        await self.queue.put(None)


async def test_cdp_client_routes_responses_and_events():
    ws = _FakeWs()
    client = CdpClient(ws)
    events: list[dict] = []

    async def handler(message: dict) -> None:
        events.append(message)

    runner = asyncio.create_task(client.run(handler))
    result = await client.send("Target.getTargets")
    assert result == {"ok": True}

    await ws.queue.put(json.dumps({"method": "Network.loadingFinished", "params": {"requestId": "1"}}))
    await asyncio.sleep(0.05)
    assert events and events[0]["method"] == "Network.loadingFinished"

    runner.cancel()
    with suppress(asyncio.CancelledError):
        await runner


async def test_cdp_client_event_handler_can_send():
    """事件处理任务里二次 send（如 getResponseBody）不得死锁。"""
    ws = _FakeWs()
    client = CdpClient(ws)
    results: list[dict] = []

    async def handler(message: dict) -> None:
        results.append(await client.send("Network.getResponseBody", {"requestId": "1"}))

    runner = asyncio.create_task(client.run(handler))
    await ws.queue.put(json.dumps({"method": "Network.responseReceived", "params": {}}))
    await asyncio.sleep(0.05)
    assert results == [{"ok": True}]

    runner.cancel()
    with suppress(asyncio.CancelledError):
        await runner


async def test_maybe_attach_dedupes_concurrent_targets():
    """targetCreated 与 getTargets 并发投递同一 target 时只能挂载一次。"""
    from goofish_cli.core.collect import Collector

    class FakeClient:
        def __init__(self):
            self.calls: list[str] = []

        async def send(self, method, params=None, *, session_id=None, timeout=10.0):
            self.calls.append(method)
            await asyncio.sleep(0.01)  # 放大并发窗口
            if method == "Target.attachToTarget":
                return {"sessionId": "s1"}
            return {}

    collector = Collector(event_sink=lambda event: None)
    client = FakeClient()
    info = {"targetId": "T1", "type": "page", "url": "https://www.goofish.com/item?id=1"}
    await asyncio.gather(
        collector._maybe_attach(client, info),
        collector._maybe_attach(client, info),
    )
    assert client.calls.count("Target.attachToTarget") == 1
    assert client.calls.count("Network.enable") == 1
