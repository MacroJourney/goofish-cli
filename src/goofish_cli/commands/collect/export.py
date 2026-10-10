"""collect export — 导出语料（JSONL，供外部工具消费；只读本地库）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from goofish_cli.core import Strategy, command


@command(
    namespace="collect",
    name="export",
    description="导出被动采集语料为 JSONL 文件（--out 指定路径；只读本地库）",
    strategy=Strategy.PUBLIC,
    columns=["ok", "out", "lines"],
)
def export(out: str, limit: int = 0) -> dict[str, Any]:
    from goofish_cli.core.collect_store import open_store

    path = Path(out).expanduser()
    with open_store() as store:
        content = store.export_jsonl(limit)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + ("\n" if content else ""), encoding="utf-8")
    return {"ok": True, "out": str(path), "lines": content.count("\n") + 1 if content else 0}
