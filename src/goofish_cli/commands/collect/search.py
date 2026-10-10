"""collect search — 语料库检索（关键词/价格区间/来源；只读本地库）。"""
from __future__ import annotations

from typing import Any

from goofish_cli.core import Strategy, command


@command(
    namespace="collect",
    name="search",
    description="检索被动采集的本地商品语料（title/desc 关键词 + 价格区间 + 来源；不发请求）",
    strategy=Strategy.PUBLIC,
    columns=["item_id", "title", "price", "location", "want_cnt", "sources", "last_seen_at"],
    arguments=["query"],
)
def search(
    query: str = "",
    price_min: float | None = None,
    price_max: float | None = None,
    source: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    from goofish_cli.core.collect_store import open_store

    with open_store() as store:
        items = store.search(
            query, price_min=price_min, price_max=price_max, source=source, limit=limit
        )
    for item in items:
        item["sources"] = ",".join(item.get("sources") or [])
    return {"items": items, "total": len(items), "query": query}
