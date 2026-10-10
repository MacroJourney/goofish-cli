"""collect item — 单商品详情 + 观测历史（价格/热度时间序列；只读本地库）。"""
from __future__ import annotations

import re
from typing import Any

from goofish_cli.core import GoofishError, Strategy, command


@command(
    namespace="collect",
    name="item",
    description="查看本地语料中单个商品的全字段 + 观测历史（价格/想要数走势；不发请求）",
    strategy=Strategy.PUBLIC,
    columns=["item_id", "title", "price", "want_cnt", "seen_count", "sources"],
)
def item(item_id: str) -> dict[str, Any]:
    from goofish_cli.core.collect_store import open_store

    normalized = str(item_id or "").strip()
    if not re.fullmatch(r"\d+", normalized):
        raise GoofishError(f"item_id 必须是数字，收到：{item_id!r}")
    with open_store() as store:
        record = store.get_item(normalized)
        if record is None:
            raise GoofishError(f"语料库中没有商品 {normalized}（先在被监听窗口里浏览它）")
    record["sources"] = ",".join(record.get("sources") or [])
    record["image_urls_count"] = len(record.get("image_urls") or [])
    return record
