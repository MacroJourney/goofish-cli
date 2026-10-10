"""collect status — 语料库统计（只读，不产生任何闲鱼请求）。"""
from __future__ import annotations

from typing import Any

from goofish_cli.core import Strategy, command


@command(
    namespace="collect",
    name="status",
    description="被动采集语料库统计（条数/来源分布/最后捕获/风险事件；只读本地库）",
    strategy=Strategy.PUBLIC,
    columns=["items", "observations", "last_seen_at", "risk_events", "db"],
)
def status() -> dict[str, Any]:
    from goofish_cli.core.collect_store import open_store

    with open_store() as store:
        stats = store.stats()
    stats["by_source_text"] = ", ".join(
        f"{name}:{count}" for name, count in sorted(stats.get("by_source", {}).items())
    )
    return stats
