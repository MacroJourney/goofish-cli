"""被动采集响应解析器 —— mtop envelope → 统一 item 结构。

方案：`docs/goofish-cli-被动采集方案.md` §4.6。

输入是 `Network.getResponseBody` 拿到的 raw body（字符串）；解析器只做两件事：
1. 把 envelope 拆开，识别 ret 语义（SUCCESS / 登录失效 / 风控信号）
2. 按 api 找到对应的商品卡片容器，逐条抽取为统一的 `ParsedItem`

**红线：解析器永不抛异常**——任何畸形/缺字段/类型意外都返回空列表或跳过单条，
调用方靠 `parse_errors` 计数观测。字段全用 `.get()` + 类型防御（实测形态差异大：
search 的 price 是段数组、recommend/feed 是字符串、detail 的 defaultPrice 可能是
布尔 false）。
"""
from __future__ import annotations

import json
import re
from typing import Any, NamedTuple

# ── ret 分类（复用 core/mtop.py 既有判定表）─────────────────────────────

RISK_KEYWORDS = ("RGV587_ERROR", "FAIL_SYS_USER_VALIDATE", "/punish")
AUTH_KEYWORDS = ("FAIL_SYS_SESSION_EXPIRED", "FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EMPTY")


class EnvelopeKind(NamedTuple):
    kind: str  # "ok" | "auth" | "risk" | "error"
    detail: str


class ParsedItem(NamedTuple):
    """跨来源统一商品结构。所有字段可为空（不同来源字段覆盖不同）。"""

    item_id: str
    title: str = ""
    description: str = ""
    price: float | None = None
    original_price: float | None = None
    item_condition: str = ""
    brand: str = ""
    category: str = ""
    location: str = ""
    seller_name: str = ""
    seller_id: str = ""
    want_cnt: int | None = None
    collect_cnt: int | None = None
    browse_cnt: int | None = None
    status: str = ""
    image_urls: tuple[str, ...] = ()
    source: str = ""


# ── 值清洗工具（全防御）─────────────────────────────────────────────────


def _clean(value: Any) -> str:
    """任意值 → 去空白字符串；None/bool 变空串。"""
    if value is None or isinstance(value, bool):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = _clean(value)
    match = re.search(r"\d+", text.replace(",", ""))
    return int(match.group(0)) if match else None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _clean(value)
    match = re.search(r"\d+(?:\.\d+)?", text.replace(",", ""))
    return float(match.group(0)) if match else None


def _price_from_segments(segments: Any) -> float | None:
    """search 卡片的富文本段数组 → 价格数字。

    实测形态：`[{"type":"sign","text":"¥"},{"type":"integer","text":"49"},
    {"type":"decimal","text":".90"}]`（sign/integer/decimal 顺序拼接）。
    """
    if not isinstance(segments, list):
        return _as_float(segments)
    parts = []
    for seg in segments:
        if isinstance(seg, dict):
            kind = seg.get("type")
            text = _clean(seg.get("text"))
            if kind in ("integer", "decimal") and text:
                parts.append(text)
    return _as_float("".join(parts)) if parts else None


def _tag_contents(fish_tags: Any) -> list[str]:
    """fishTags 容器 → 标签文案列表（用于提取"人想要"等）。"""
    if not isinstance(fish_tags, dict):
        return []
    out: list[str] = []
    for group in fish_tags.values():
        if not isinstance(group, dict):
            continue
        for entry in group.get("tagList") or []:
            if not isinstance(entry, dict):
                continue
            content = _clean((entry.get("data") or {}).get("content"))
            if content:
                out.append(content)
    return out


def _want_cnt_from_tags(tags: list[str]) -> int | None:
    for text in tags:
        match = re.search(r"(\d[\d,]*)\s*人想要", text)
        if match:
            return _as_int(match.group(1))
    return None


def _iter_json_list(value: Any) -> list[dict[str, Any]]:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


# ── envelope 与 api 分发 ────────────────────────────────────────────────


def classify_envelope(payload: Any) -> EnvelopeKind:
    """判断响应语义。`payload` 是 json.loads 后的 dict（或解析失败的原始值）。"""
    if not isinstance(payload, dict):
        return EnvelopeKind("error", "payload 不是 JSON 对象")
    ret = payload.get("ret")
    first = _clean(ret[0]) if isinstance(ret, list) and ret else ""
    code = first.split("::")[0] if first else ""
    joined = first
    if any(keyword in joined for keyword in RISK_KEYWORDS):
        return EnvelopeKind("risk", joined)
    if any(keyword in joined for keyword in AUTH_KEYWORDS):
        return EnvelopeKind("auth", joined)
    if code.startswith("SUCCESS") or first.startswith("SUCCESS"):
        return EnvelopeKind("ok", joined)
    return EnvelopeKind("error", joined or "ret 为空")


def parse_body(api: str, body: str) -> tuple[EnvelopeKind, list[ParsedItem]]:
    """raw body → (envelope 分类, 商品列表)。任何异常都落在空列表上。"""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return EnvelopeKind("error", "body 不是合法 JSON"), []
    kind = classify_envelope(payload)
    if kind.kind != "ok":
        return kind, []
    parser = _PARSERS.get(api)
    if parser is None:
        return kind, []
    try:
        return kind, parser(payload.get("data") or {})
    except Exception:  # noqa: BLE001 — 解析器红线：永不抛
        return kind, []


# ── 各来源解析器（对照 tests/fixtures/mtop/*.json）──────────────────────


def _item_from_detail(item: dict[str, Any], seller: dict[str, Any]) -> ParsedItem:
    labels = _iter_json_list(item.get("itemLabelExtList"))
    label_map: dict[str, str] = {}
    for entry in labels:
        name = _clean(entry.get("propertyText"))
        if name:
            label_map[name] = _clean(entry.get("text"))
    images = [
        _clean(img.get("url"))
        for img in _iter_json_list(item.get("imageInfos"))
        if _clean(img.get("url"))
    ]
    return ParsedItem(
        item_id=_clean(item.get("itemId")),
        title=_clean(item.get("title")),
        description=_clean(item.get("desc")),
        price=_as_float(item.get("soldPrice")) or _as_float(item.get("minPrice")),
        original_price=None,  # detail 无原价字段（对比项在 priceRelativeTags，暂不采）
        item_condition=label_map.get("成色", ""),
        brand=label_map.get("品牌", ""),
        category=label_map.get("分类", ""),
        location=_clean(seller.get("publishCity") or seller.get("city")),
        seller_name=_clean(seller.get("nick") or seller.get("uniqueName")),
        seller_id=_clean(seller.get("sellerId")),
        want_cnt=_as_int(item.get("wantCnt")),
        collect_cnt=_as_int(item.get("collectCnt")),
        browse_cnt=_as_int(item.get("browseCnt")),
        status=_clean(item.get("itemStatusStr")),
        image_urls=tuple(images),
        source="detail",
    )


def parse_detail(data: dict[str, Any]) -> list[ParsedItem]:
    item = data.get("itemDO") or {}
    seller = data.get("sellerDO") or {}
    if not isinstance(item, dict) or not isinstance(seller, dict):
        return []
    parsed = _item_from_detail(item, seller)
    return [parsed] if parsed.item_id else []


def parse_search(data: dict[str, Any]) -> list[ParsedItem]:
    """搜索页：`data.resultList[].data.item.main.exContent`（resultList[].data 是 dict）。"""
    out: list[ParsedItem] = []
    for row in _iter_json_list(data.get("resultList")):
        row_data = row.get("data")
        if not isinstance(row_data, dict):
            continue
        ex = ((row_data.get("item") or {}).get("main") or {}).get("exContent")
        if not isinstance(ex, dict):
            continue
        detail_params = ex.get("detailParams") or {}
        if not isinstance(detail_params, dict):
            detail_params = {}
        item_id = _clean(ex.get("itemId") or detail_params.get("itemId"))
        if not item_id:
            continue
        tags = _tag_contents(ex.get("fishTags"))
        out.append(
            ParsedItem(
                item_id=item_id,
                title=_clean(detail_params.get("title")),
                price=_price_from_segments(ex.get("price"))
                or _as_float(detail_params.get("soldPrice")),
                original_price=_as_float(ex.get("oriPrice")),
                location=_clean(ex.get("area")),
                seller_name=_clean(detail_params.get("userNick")),
                want_cnt=_want_cnt_from_tags(tags),
                image_urls=(_clean(ex.get("picUrl")),) if _clean(ex.get("picUrl")) else (),
                source="search",
            )
        )
    return out


def parse_feed(data: dict[str, Any]) -> list[ParsedItem]:
    """首页推荐流：`data.cardList[].cardData`（priceInfo 与 titleSummary 分列）。"""
    out: list[ParsedItem] = []
    for card in _iter_json_list(data.get("cardList")):
        cd = card.get("cardData")
        if not isinstance(cd, dict):
            continue
        item_id = _clean(cd.get("itemId"))
        if not item_id:
            continue
        price_info = cd.get("priceInfo") or {}
        if not isinstance(price_info, dict):
            price_info = {}
        title_summary = cd.get("titleSummary") or {}
        title = _clean(title_summary.get("text")) if isinstance(title_summary, dict) else ""
        main_pic = cd.get("mainPicInfo") or {}
        pic_url = _clean(main_pic.get("url")) if isinstance(main_pic, dict) else ""
        seller = cd.get("user") or {}
        tags = _tag_contents(cd.get("fishTags"))
        out.append(
            ParsedItem(
                item_id=item_id,
                title=title,
                price=_as_float(price_info.get("price")),
                original_price=_as_float(price_info.get("oriPrice")),
                location=_clean(cd.get("city")),
                seller_name=_clean(seller.get("userNick")) if isinstance(seller, dict) else "",
                want_cnt=_want_cnt_from_tags(tags),
                image_urls=(pic_url,) if pic_url else (),
                source="feed",
            )
        )
    return out


def parse_recommend(data: dict[str, Any]) -> list[ParsedItem]:
    """详情页"猜你喜欢"：`data.cardList[].cardData`（含非商品页头卡，必须跳过）。"""
    out: list[ParsedItem] = []
    for card in _iter_json_list(data.get("cardList")):
        cd = card.get("cardData")
        if not isinstance(cd, dict):
            continue
        item_id = _clean(cd.get("itemId"))
        if not item_id:  # 页头卡（61501）无 itemId，跳过
            continue
        seller = cd.get("user") or {}
        tags = _tag_contents(cd.get("fishTags"))
        image = cd.get("image") or {}
        pic_url = _clean(image.get("url")) if isinstance(image, dict) else ""
        out.append(
            ParsedItem(
                item_id=item_id,
                title=_clean(cd.get("title")),
                description=_clean(cd.get("desc")),
                price=_as_float(cd.get("price")),
                location=_clean(cd.get("area")),
                seller_name=_clean(seller.get("userNick")) if isinstance(seller, dict) else "",
                want_cnt=_want_cnt_from_tags(tags),
                image_urls=(pic_url,) if pic_url else (),
                source="recommend",
            )
        )
    return out


_PARSERS = {
    "mtop.taobao.idlemtopsearch.pc.search": parse_search,
    "mtop.taobao.idle.pc.detail": parse_detail,
    "mtop.taobao.idle.item.web.recommend.list": parse_recommend,
    "mtop.taobao.idlehome.home.webpc.feed": parse_feed,
}
