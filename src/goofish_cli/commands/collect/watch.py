"""collect watch — 被动监听常驻窗口的 mtop 响应（只读；Ctrl-C 退出）。

方案：`docs/goofish-cli-被动采集方案.md`。只发 Target.* / Network.enable /
Network.getResponseBody，不 evaluate、不点击、不注入——页面 JS 上下文零改动。

输出：stdout JSONL 事件流（listening / attached / captured），不产生任何闲鱼请求。
采样：`GOOFISH_COLLECT_DEBUG_DUMP=1` 时原始响应落盘（默认 `~/.goofish-cli/corpus/raw`）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from goofish_cli.core import Strategy, command


@command(
    namespace="collect",
    name="watch",
    description="被动监听常驻窗口的 mtop 响应（只读；stdout JSONL 事件流；Ctrl-C 退出）",
    strategy=Strategy.PUBLIC,
    columns=[],
)
def watch() -> dict[str, Any]:
    from goofish_cli.core.collect import Collector

    collector = Collector.from_env()
    apis = "全部（去隐私前缀）" if collector.allowed_apis is None else sorted(collector.allowed_apis)
    logger.info(
        f"[collect] 监听中：dump={'on' if collector.dump_enabled else 'off'}，apis={apis}"
    )
    try:
        asyncio.run(collector.run_forever())
    except KeyboardInterrupt:
        logger.info(f"[collect] 退出（共捕获 {collector.captured} 条）")
    return {"ok": True, "captured": collector.captured}
