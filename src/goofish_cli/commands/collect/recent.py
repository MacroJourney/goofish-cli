"""collect recent — 最近捕获的商品（只读本地库）。"""
from __future__ import annotations

from typing import Any

from goofish_cli.core import Strategy, command


@command(
    namespace="collect",
    name="recent",
    description="最近被动捕获的商品（按最后出现时间倒序；只读本地库）",
    strategy=Strategy.PUBLIC,
    columns=["item_id", "title", "price", "want_cnt", "sources", "last_seen_at"],
)
def recent(limit: int = 20) -> dict[str, Any]:
    from goofish_cli.core.collect_store import open_store

    with open_store() as store:
        items = store.recent(limit)
    for item in items:
        item["sources"] = ",".join(item.get("sources") or [])
    return {"items": items, "total": len(items)}
