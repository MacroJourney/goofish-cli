"""被动采集存储 —— SQLite（WAL），items 主表 + observations 时间序列。

方案：`docs/goofish-cli-被动采集方案.md` §4.7。

合并语义（移植 XHS Collector 去重经验）：
  - 主键 item_id：重复捕获不新增行
  - first_seen_at 保留首见；last_seen_at 更新；seen_count += 1
  - 新值非空才覆盖旧值（列表页字段少、详情页字段全，"越补越完整"）
  - sources 并集去重
  - observations 每次追加；与上一行完全相同（价格与计数均未变）时跳过

单连接单线程写入（采集进程内），WAL 保证 CLI/MCP 只读并发不阻塞。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import suppress
from pathlib import Path

from goofish_cli.core.collect_parsers import ParsedItem

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  item_id TEXT PRIMARY KEY,
  title TEXT, description TEXT, price REAL, original_price REAL,
  item_condition TEXT, brand TEXT, category TEXT,
  location TEXT, seller_name TEXT, seller_id TEXT,
  want_cnt INTEGER, collect_cnt INTEGER, browse_cnt INTEGER, status TEXT,
  image_urls TEXT,
  first_seen_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  seen_count INTEGER DEFAULT 1,
  sources TEXT
);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL,
  observed_at INTEGER NOT NULL,
  price REAL, want_cnt INTEGER, collect_cnt INTEGER, browse_cnt INTEGER,
  status TEXT, source TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_last_seen ON items(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_item ON observations(item_id, observed_at DESC);
"""

#: 非空才覆盖的字段（None/""/空列表不覆盖旧值）。
_SCALAR_FIELDS = (
    "title", "description", "price", "original_price", "item_condition", "brand",
    "category", "location", "seller_name", "seller_id", "want_cnt", "collect_cnt",
    "browse_cnt", "status", "image_urls",
)

#: 参与 observations 变化判定的字段。
_OBS_FIELDS = ("price", "want_cnt", "collect_cnt", "browse_cnt", "status")

#: schema 版本（写入 PRAGMA user_version）。桌面应用（Tauri）启动时校验——
#: 版本高于其预期时提示"请更新应用"，避免字段不符导致诡异报错。变更 schema 时 +1。
SCHEMA_VERSION = 1


def default_db_path() -> Path:
    raw = os.environ.get("GOOFISH_COLLECT_DB")
    if raw:
        return Path(os.path.expanduser(raw))
    return Path.home() / ".goofish-cli" / "corpus" / "goofish.db"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _is_empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple)):
        return len(value) == 0
    return False


class CorpusStore:
    """采集语料库。`upsert()` 单条写入（采集器热路径），只读查询供 CLI/MCP。

    schema 版本写入 `PRAGMA user_version`（见 `SCHEMA_VERSION`）：本进程只接受
    「版本相同」或「版本 0（旧库，升级到当前版本）」；更高版本（新库+旧工具）
    直接拒绝，避免写坏未来 schema——桌面应用侧同样按此语义校验。
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        existing = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if existing > SCHEMA_VERSION:
            self._conn.close()
            raise sqlite3.DatabaseError(
                f"语料库 schema 版本 {existing} 高于本工具支持的 {SCHEMA_VERSION}"
                "（请更新 goofish-cli）"
            )
        self._conn.executescript(_SCHEMA)
        if existing < SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()
        self.upserted = 0

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> CorpusStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── 写入 ────────────────────────────────────────────────────────────

    def upsert(self, item: ParsedItem, *, observed_at: int | None = None) -> str:
        """写入/合并一条商品，返回 "inserted" | "updated" | "skipped"(无效)。"""
        if not item.item_id:
            return "skipped"
        ts = observed_at or _now_ms()
        images_json = json.dumps(list(item.image_urls), ensure_ascii=False)
        row = self._conn.execute(
            "SELECT * FROM items WHERE item_id = ?", (item.item_id,)
        ).fetchone()

        if row is None:
            self._conn.execute(
                """
                INSERT INTO items (
                  item_id, title, description, price, original_price,
                  item_condition, brand, category, location, seller_name, seller_id,
                  want_cnt, collect_cnt, browse_cnt, status, image_urls,
                  first_seen_at, last_seen_at, seen_count, sources
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
                """,
                (
                    item.item_id, item.title, item.description, item.price, item.original_price,
                    item.item_condition, item.brand, item.category, item.location,
                    item.seller_name, item.seller_id,
                    item.want_cnt, item.collect_cnt, item.browse_cnt, item.status, images_json,
                    ts, ts, json.dumps([item.source], ensure_ascii=False),
                ),
            )
            self._append_observation(item, ts, force=True)
            self._conn.commit()
            self.upserted += 1
            return "inserted"

        # 合并：新值非空才覆盖；首见/计数/来源特殊处理
        values = {
            "title": item.title, "description": item.description, "price": item.price,
            "original_price": item.original_price, "item_condition": item.item_condition,
            "brand": item.brand, "category": item.category, "location": item.location,
            "seller_name": item.seller_name, "seller_id": item.seller_id,
            "want_cnt": item.want_cnt, "collect_cnt": item.collect_cnt,
            "browse_cnt": item.browse_cnt, "status": item.status,
        }
        updates: dict[str, object] = {}
        for field in _SCALAR_FIELDS:
            if field == "image_urls":
                new_images = list(item.image_urls)
                if new_images:
                    updates[field] = images_json
                continue
            new_value = values.get(field)
            if not _is_empty(new_value):
                updates[field] = new_value
        sources = set(json.loads(row["sources"] or "[]"))
        sources.add(item.source)
        updates["sources"] = json.dumps(sorted(sources), ensure_ascii=False)
        updates["last_seen_at"] = ts
        updates["seen_count"] = int(row["seen_count"] or 1) + 1

        assignments = ", ".join(f"{key} = ?" for key in updates)
        self._conn.execute(
            f"UPDATE items SET {assignments} WHERE item_id = ?",
            (*updates.values(), item.item_id),
        )
        self._append_observation(item, ts, force=False)
        self._conn.commit()
        self.upserted += 1
        return "updated"

    def _append_observation(self, item: ParsedItem, ts: int, *, force: bool) -> None:
        """追加观测；force=False 时与上一行完全相同则跳过（控制表膨胀）。"""
        if not force:
            last = self._conn.execute(
                "SELECT price, want_cnt, collect_cnt, browse_cnt, status FROM observations "
                "WHERE item_id = ? ORDER BY observed_at DESC, id DESC LIMIT 1",
                (item.item_id,),
            ).fetchone()
            if last is not None:
                same = all(
                    (last[field] if last[field] is not None else None) == (getattr(item, field))
                    for field in _OBS_FIELDS
                )
                if same:
                    return
        self._conn.execute(
            """
            INSERT INTO observations (item_id, observed_at, price, want_cnt, collect_cnt, browse_cnt, status, source)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                item.item_id, ts, item.price, item.want_cnt, item.collect_cnt,
                item.browse_cnt, item.status, item.source,
            ),
        )

    def record_risk_event(self, *, api: str, detail: str, url: str = "") -> None:
        """风控/登录信号落库（供 status 展示；只记录，不动作）。"""
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS risk_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at INTEGER NOT NULL, "
            "kind TEXT, api TEXT, detail TEXT, url TEXT)"
        )
        kind = "auth" if "SESSION_EXPIRED" in detail or "TOKEN" in detail else "risk"
        self._conn.execute(
            "INSERT INTO risk_events (observed_at, kind, api, detail, url) VALUES (?,?,?,?,?)",
            (_now_ms(), kind, api, detail[:500], url[:500]),
        )
        self._conn.commit()

    # ── 只读查询 ────────────────────────────────────────────────────────

    def stats(self) -> dict[str, object]:
        total = self._conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
        observations = self._conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()["n"]
        last_seen = self._conn.execute("SELECT MAX(last_seen_at) AS t FROM items").fetchone()["t"]
        by_source: dict[str, int] = {}
        for row in self._conn.execute("SELECT sources FROM items"):
            for source in json.loads(row["sources"] or "[]"):
                by_source[source] = by_source.get(source, 0) + 1
        risk_events = 0
        with suppress(sqlite3.OperationalError):
            risk_events = self._conn.execute(
                "SELECT COUNT(*) AS n FROM risk_events"
            ).fetchone()["n"]
        return {
            "items": total,
            "observations": observations,
            "by_source": by_source,
            "last_seen_at": last_seen,
            "risk_events": risk_events,
            "db": str(self.path),
            "db_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    def recent(self, limit: int = 20) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT * FROM items ORDER BY last_seen_at DESC LIMIT ?", (max(1, limit),)
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def search(
        self,
        query: str = "",
        *,
        price_min: float | None = None,
        price_max: float | None = None,
        source: str = "",
        limit: int = 20,
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        args: list[object] = []
        if query:
            clauses.append("(title LIKE ? OR description LIKE ?)")
            pattern = f"%{query}%"
            args.extend([pattern, pattern])
        if price_min is not None:
            clauses.append("price >= ?")
            args.append(price_min)
        if price_max is not None:
            clauses.append("price <= ?")
            args.append(price_max)
        if source:
            clauses.append("sources LIKE ?")
            args.append(f'%"{source}"%')
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM items {where} ORDER BY last_seen_at DESC LIMIT ?",
            (*args, max(1, limit)),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_item(self, item_id: str) -> dict[str, object] | None:
        row = self._conn.execute(
            "SELECT * FROM items WHERE item_id = ?", (item_id,)
        ).fetchone()
        if row is None:
            return None
        out = self._row_to_dict(row)
        history = self._conn.execute(
            "SELECT observed_at, price, want_cnt, collect_cnt, browse_cnt, status, source "
            "FROM observations WHERE item_id = ? ORDER BY observed_at DESC, id DESC LIMIT 200",
            (item_id,),
        ).fetchall()
        out["observations"] = [dict(h) for h in history]
        return out

    def export_jsonl(self, limit: int = 0) -> str:
        sql = "SELECT * FROM items ORDER BY last_seen_at DESC"
        if limit > 0:
            sql += f" LIMIT {int(limit)}"
        rows = self._conn.execute(sql).fetchall()
        return "\n".join(
            json.dumps(self._row_to_dict(row), ensure_ascii=False) for row in rows
        )

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, object]:
        data = dict(row)
        for field in ("image_urls", "sources"):
            try:
                data[field] = json.loads(data.get(field) or "[]")
            except (ValueError, TypeError):
                data[field] = []
        return data


def open_store(path: Path | None = None) -> CorpusStore:
    return CorpusStore(path)
