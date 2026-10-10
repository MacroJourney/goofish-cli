"""验解析器（对照 fixtures）与 SQLite 存储的合并语义。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from goofish_cli.core.collect_parsers import (
    ParsedItem,
    _as_float,
    _as_int,
    _price_from_segments,
    _want_cnt_from_tags,
    classify_envelope,
    parse_body,
)
from goofish_cli.core.collect_store import CorpusStore

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mtop"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


# ── 值清洗工具 ──────────────────────────────────────────────────────────

def test_price_from_segments_handles_sign_integer_decimal():
    segs = [
        {"type": "sign", "text": "¥"},
        {"type": "integer", "text": "49"},
        {"type": "decimal", "text": ".90"},
    ]
    assert _price_from_segments(segs) == 49.90
    assert _price_from_segments([{"type": "integer", "text": "24"}]) == 24.0
    assert _price_from_segments([]) is None
    assert _price_from_segments("¥12.5") == 12.5  # 非数组兜底
    assert _price_from_segments(None) is None


def test_as_int_as_float_defensive():
    assert _as_int("13044人想要") == 13044
    assert _as_int(True) is None  # 布尔不算数字
    assert _as_int("") is None
    assert _as_float("¥99") == 99.0
    assert _as_float(False) is None
    assert _as_float(None) is None


def test_want_cnt_from_tags():
    assert _want_cnt_from_tags(["累计降价20%", "13044人想要"]) == 13044
    assert _want_cnt_from_tags(["包邮"]) is None


# ── envelope 分类 ───────────────────────────────────────────────────────

def test_classify_envelope_kinds():
    assert classify_envelope({"ret": ["SUCCESS::调用成功"]}).kind == "ok"
    assert classify_envelope({"ret": ["FAIL_SYS_SESSION_EXPIRED::Session过期"]}).kind == "auth"
    assert classify_envelope({"ret": ["RGV587_ERROR::哎哟喂,被挤爆啦"]}).kind == "risk"
    assert classify_envelope({"ret": ["FAIL_SYS_USER_VALIDATE::需要验证"]}).kind == "risk"
    assert classify_envelope({"ret": ["FAIL_SYS_XXX::未知"]}).kind == "error"
    assert classify_envelope("not-a-dict").kind == "error"
    assert classify_envelope({}).kind == "error"


def test_parse_body_rejects_malformed_json():
    kind, items = parse_body("mtop.taobao.idle.pc.detail", "{not json")
    assert kind.kind == "error"
    assert items == []


def test_parse_body_non_ok_envelope_yields_no_items():
    body = json.dumps({"ret": ["FAIL_SYS_SESSION_EXPIRED::x"], "data": {}})
    kind, items = parse_body("mtop.taobao.idle.pc.detail", body)
    assert kind.kind == "auth"
    assert items == []


def test_parse_body_unknown_api_returns_empty():
    body = json.dumps({"ret": ["SUCCESS::ok"], "data": {}})
    kind, items = parse_body("mtop.unknown.api", body)
    assert kind.kind == "ok"
    assert items == []


# ── 各解析器（fixtures）────────────────────────────────────────────────

def test_parse_search_fixture():
    data = load_fixture("search.json")
    kind, items = parse_body(data["api"], json.dumps(data))
    assert kind.kind == "ok"
    assert len(items) == 2
    first, second = items
    assert first.item_id == "959850333847"
    assert first.price == 24.0            # 整数价（段数组拼接）
    assert first.original_price == 99.0   # ¥99
    assert first.location == "广东"
    assert first.source == "search"
    assert "清仓特价" in first.title
    assert first.want_cnt == 13044
    assert second.price == 49.90          # 小数价（decimal 段）
    assert second.want_cnt == 244


def test_parse_detail_fixture():
    data = load_fixture("detail.json")
    kind, items = parse_body(data["api"], json.dumps(data))
    assert kind.kind == "ok"
    assert len(items) == 1
    item = items[0]
    assert item.item_id == "859076799075"
    assert item.price == 49.0             # soldPrice 优先
    assert item.brand == "EWEADN/前行者"
    assert item.category == "键盘"
    assert item.location == "郑州"
    assert item.want_cnt == 5632
    assert item.collect_cnt == 2200
    assert item.browse_cnt == 119914
    assert item.status == "在线"
    assert len(item.image_urls) == 3
    assert item.source == "detail"


def test_parse_feed_fixture():
    data = load_fixture("feed.json")
    kind, items = parse_body(data["api"], json.dumps(data))
    assert kind.kind == "ok"
    assert len(items) == 2
    first = items[0]
    assert first.item_id == "1035955247603"
    assert first.price == 8.80            # priceInfo.price 字符串
    assert first.original_price == 3695.0
    assert first.location == "广州"
    assert first.source == "feed"


def test_parse_recommend_fixture_skips_header_card():
    data = load_fixture("recommend.json")
    kind, items = parse_body(data["api"], json.dumps(data))
    assert kind.kind == "ok"
    assert len(items) == 2                # 3 张卡里页头卡（61501）被跳过
    assert all(item.item_id for item in items)
    assert items[0].item_id == "1087759972043"
    assert items[0].price == 2.88         # recommend 的 price 是纯字符串
    assert items[0].source == "recommend"


def test_parsers_never_raise_on_garbage():
    for api in (
        "mtop.taobao.idlemtopsearch.pc.search",
        "mtop.taobao.idle.pc.detail",
        "mtop.taobao.idle.item.web.recommend.list",
        "mtop.taobao.idlehome.home.webpc.feed",
    ):
        for bad_data in ({}, {"resultList": "x"}, {"cardList": [None]}, {"itemDO": []}):
            body = json.dumps({"ret": ["SUCCESS::ok"], "data": bad_data})
            kind, items = parse_body(api, body)
            assert kind.kind == "ok"
            assert items == []


# ── 存储合并语义 ────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    with CorpusStore(tmp_path / "test.db") as s:
        yield s


def test_store_insert_then_merge(store):
    base = ParsedItem(
        item_id="1", title="标题", price=10.0, location="北京",
        seller_name="卖家", want_cnt=5, source="search",
    )
    assert store.upsert(base, observed_at=1000) == "inserted"
    # 列表页字段少 → 详情页补全；空值不覆盖旧值
    fuller = ParsedItem(
        item_id="1", title="", description="详情描述", price=10.0, brand="品牌X",
        location="", want_cnt=5, collect_cnt=3, source="detail",
    )
    assert store.upsert(fuller, observed_at=2000) == "updated"
    record = store.get_item("1")
    assert record["title"] == "标题"          # 空值不覆盖
    assert record["location"] == "北京"       # 空值不覆盖
    assert record["description"] == "详情描述"  # 新值补全
    assert record["brand"] == "品牌X"
    assert record["collect_cnt"] == 3
    assert record["first_seen_at"] == 1000    # 首见保留
    assert record["last_seen_at"] == 2000
    assert record["seen_count"] == 2
    assert sorted(record["sources"]) == ["detail", "search"]


def test_store_observations_dedup_identical_snapshot(store):
    item = ParsedItem(item_id="2", title="A", price=20.0, want_cnt=1, source="feed")
    store.upsert(item, observed_at=1000)
    store.upsert(item, observed_at=2000)        # 完全相同 → 不加观测
    changed = item._replace(price=18.0)         # 降价 → 加观测
    store.upsert(changed, observed_at=3000)
    record = store.get_item("2")
    prices = [obs["price"] for obs in record["observations"]]
    assert prices == [18.0, 20.0]               # 倒序：最新在前
    assert record["price"] == 18.0              # 非空覆盖
    assert record["seen_count"] == 3


def test_store_skips_empty_item_id(store):
    assert store.upsert(ParsedItem(item_id="", source="search")) == "skipped"
    assert store.stats()["items"] == 0


def test_store_search_and_stats(store):
    store.upsert(ParsedItem(item_id="10", title="机械键盘", price=50.0, source="search"), observed_at=1)
    store.upsert(ParsedItem(item_id="11", title="鼠标", price=30.0, source="feed"), observed_at=2)
    store.upsert(ParsedItem(item_id="12", title="机械键盘 二手", price=80.0, source="search"), observed_at=3)

    assert [r["item_id"] for r in store.search("机械键盘")] == ["12", "10"]
    assert [r["item_id"] for r in store.search(price_min=40, price_max=60)] == ["10"]
    assert [r["item_id"] for r in store.search(source="feed")] == ["11"]
    stats = store.stats()
    assert stats["items"] == 3
    assert stats["by_source"] == {"search": 2, "feed": 1}
    assert stats["last_seen_at"] == 3


def test_store_risk_event(store):
    store.record_risk_event(api="mtop.taobao.idle.pc.detail", detail="RGV587_ERROR::x")
    store.record_risk_event(api="mtop.x", detail="FAIL_SYS_SESSION_EXPIRED::y")
    assert store.stats()["risk_events"] == 2


def test_store_export_jsonl(store):
    store.upsert(ParsedItem(item_id="20", title="A", source="search"), observed_at=1)
    store.upsert(ParsedItem(item_id="21", title="B", source="search"), observed_at=2)
    content = store.export_jsonl()
    assert len(content.splitlines()) == 2
    first = json.loads(content.splitlines()[0])
    assert first["item_id"] == "21"  # 最新在前


# ── schema 版本（跨仓库契约：桌面应用启动校验用）─────────────────────────

def test_store_writes_schema_version(tmp_path):
    import sqlite3

    from goofish_cli.core.collect_store import SCHEMA_VERSION

    path = tmp_path / "v.db"
    with CorpusStore(path) as store:
        store.upsert(ParsedItem(item_id="1", title="A", source="search"))
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


def test_store_rejects_newer_schema_version(tmp_path):
    import sqlite3

    import pytest as _pytest

    from goofish_cli.core.collect_store import SCHEMA_VERSION

    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with _pytest.raises(sqlite3.DatabaseError, match="高于本工具支持"):
        CorpusStore(path)


def test_store_upgrades_legacy_zero_version(tmp_path):
    """旧库（user_version=0，无标记）打开时升级到当前版本，不报错。"""
    import sqlite3

    from goofish_cli.core.collect_store import SCHEMA_VERSION

    path = tmp_path / "legacy.db"
    corpus = CorpusStore(path)
    corpus.close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    with CorpusStore(path) as store:
        store.upsert(ParsedItem(item_id="1", title="A", source="search"))
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()
