"""Playwright 浏览器上下文 —— 常驻窗口优先 + 临时 profile 兜底。

三档降级（`_resolve_backend`）：
  Tier 1  连接**常驻 Chrome 窗口**（用户自己的窗口；CDP 只绑 127.0.0.1）
  Tier 2  连不上 → 自动拉起常驻窗口（同 profile 重复启动会被 Chrome 自动合并）
  Tier 3  仍失败 → 回退临时独立 profile（旧行为；`GOOFISH_BROWSER_MODE=temp` 强制）

设计要点：
1. **常驻窗口优先**（默认 `GOOFISH_BROWSER_MODE=auto`）：浏览器命令在用户自己的
   常驻窗口里**开新标签页**干活，窗口本身不关；标签默认保留、由用户手动关闭。
   cookie / 缓存 / 登录态 / x5sec 与窗口同源（设备身份一致），消除"每次调用都是
   一台新设备"导致的半失效 session 根因。实现红线：不注入 cookie（窗口会话即
   真源）；不加 stealth 脚本（正常启动的 Chrome 天然干净，`navigator.webdriver`
   实测定为 false）；只关自己的标签，绝不 `context.close()` / `browser.close()`；
   每条命令结束 best-effort 把窗口 cookie 回写 cookies.json（供 HTTP 路径复用）。
   **双栈探测**：IPv4 端口被占时 Chrome 会回退绑 `[::1]`（实测），探测与连接覆盖
   双栈；拉起超时回收自己拉起的进程，避免留下不可达的孤儿 Chrome。
2. **兜底：临时 profile**（旧行为）：每次调用独立 tmp profile，退出即删；cookie 从
   `Session.load()` 三级兜底（cookies.json → 本机浏览器导入 → 报错）注入。
3. **默认 headful**：实测 headless 即便 channel=chrome 也会被闲鱼判「非法访问」；
   CI / 无桌面场景用 `GOOFISH_HEADLESS=1` 自动走临时档（绝不弹窗）。

首次使用：跑一次 `goofish browser open` 打开常驻窗口并手动登录闲鱼，之后所有浏览器
命令复用该窗口。配置见 `.env.example`（端口 / profile 路径 / 是否自动拉起等）。

同步命令怎么用：用 `asyncio.run(...)` 驱动本模块的 async 上下文（参考 list_chats
`--watch-secs` 的 `asyncio.run(collect_session_cids(...))` 模式）。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import requests
from loguru import logger

from goofish_cli.core.cookie_types import CookieRecord
from goofish_cli.core.errors import GoofishError
from goofish_cli.core.session import Session

PROFILES_PARENT = Path.home() / ".goofish-cli" / "profiles"

# ── 常驻窗口默认配置（均可用环境变量覆盖，见 .env.example）──────────────────
DEFAULT_CDP_PORT = 37821
DEFAULT_CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEFAULT_PERSISTENT_PROFILE = Path.home() / ".goofish-cli" / "chrome-profile"
PERSISTENT_START_WAIT = 20.0
GOOFISH_HOME_URL = "https://www.goofish.com"

# 淘系签名链路 cookie（历史上跨 .taobao.com），其余默认落 .goofish.com。
# 仅用于 legacy flat dict 格式（无 domain 信息时）的窄映射兜底。
# 有 domain 的 records 直接用其 domain，不再广播。
_TAOBAO_COOKIE_NAMES = {"_m_h5_tk", "_m_h5_tk_enc", "x5sec", "sgcookie", "cookie2", "_tb_token_"}


def _legacy_domain(name: str) -> str:
    """按 cookie 名字反推归属域。仅用于 domain='' 的 legacy 记录。"""
    return ".taobao.com" if name in _TAOBAO_COOKIE_NAMES else ".goofish.com"


def _cookies_to_playwright(cookies) -> list[dict[str, Any]]:
    """把 flat dict 或 records list 转成 playwright add_cookies 需要的列表。

    有 domain 的 records 单域注入（无广播）。domain='' 的 legacy 记录
    走 _legacy_domain 窄映射分发到单域。记录中的 path 会被注入（不再硬编码 '/'）。
    """
    from .session import _coerce_records
    records = _coerce_records(cookies)
    now = int(__import__("time").time())
    expires = now + 7 * 24 * 3600
    out: list[dict[str, Any]] = []
    for r in records:
        if not r.get("value"):
            continue
        domain = r["domain"] or _legacy_domain(r["name"])
        out.append({
            "name": r["name"],
            "value": r["value"],
            "domain": domain,
            "path": r.get("path") or "/",   # ← 用记录的 path
            "expires": expires,
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        })
    return out


def _load_cookies_from_session() -> list[CookieRecord]:
    """返回 session.cookie_records（已带 domain），fallback 展平 jar（生成 domain='' records）。"""
    session = Session.load()
    if session.cookie_records:
        return session.cookie_records
    # 边缘：手工构造 Session（无 cookie_records）时回退
    return [
        {"name": name, "value": value, "domain": ""}
        for name, value in session.http.cookies.items()
        if value
    ]


# ── 常驻窗口配置读取（调用时读取，方便测试与运行时切换）────────────────────

def _cdp_port() -> int:
    raw = os.environ.get("GOOFISH_CHROME_CDP_PORT")
    if not raw:
        return DEFAULT_CDP_PORT
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            f"[browser] GOOFISH_CHROME_CDP_PORT={raw!r} 不是整数，回退默认 {DEFAULT_CDP_PORT}"
        )
        return DEFAULT_CDP_PORT


def _cdp_candidates() -> list[str]:
    """CDP 候选地址：IPv4 优先，IPv6 环回兜底——两者都只在本机可达。

    实测：IPv4 端口被其它进程占用时，Chrome 会回退绑定 `[::1]`（IPv6-only）。
    只探 127.0.0.1 会把这种情况误判为"窗口不可用"，因此探测与连接都覆盖双栈。
    """
    port = _cdp_port()
    return [f"http://127.0.0.1:{port}", f"http://[::1]:{port}"]


def _persistent_profile() -> Path:
    raw = os.environ.get("GOOFISH_CHROME_PROFILE")
    return Path(os.path.expanduser(raw)) if raw else DEFAULT_PERSISTENT_PROFILE


def _chrome_bin() -> str:
    return os.environ.get("GOOFISH_CHROME_PATH") or DEFAULT_CHROME_BIN


def _autostart_enabled() -> bool:
    return os.environ.get("GOOFISH_CHROME_AUTOSTART", "1") != "0"


def _should_close_tab(keep_tab: bool | None) -> bool:
    """标签是否自动关闭。默认保留（GOOFISH_KEEP_TAB=1），由用户手动关闭。"""
    if keep_tab is not None:
        return not keep_tab
    return os.environ.get("GOOFISH_KEEP_TAB", "1") == "0"


# ── 常驻窗口：探测 / 拉起 ────────────────────────────────────────────────

def _probe_cdp_sync(timeout: float) -> str | None:
    """同步探测双栈：返回第一个给出合法 Chrome 端点的地址，都不可用则 None。

    要求响应含 `Browser` 标识——排除"端口被别的服务占用"的假阳性。
    """
    for url in _cdp_candidates():
        try:
            resp = requests.get(f"{url}/json/version", timeout=timeout)
            if resp.ok and "Browser" in (resp.json() or {}):
                return url
        except Exception:  # noqa: BLE001 — 未启动 / 端口被占 / 超时都算不可用
            continue
    return None


async def _cdp_ready(timeout: float = 1.0) -> bool:
    """常驻窗口 CDP 是否可用（双栈任一命中即算就绪）。"""
    return await asyncio.to_thread(_probe_cdp_sync, timeout) is not None


async def _resolve_cdp_url(timeout: float = 1.0) -> str | None:
    """解析可连接的 CDP 地址（IPv4 优先，IPv6 兜底）；都不可用返回 None。"""
    return await asyncio.to_thread(_probe_cdp_sync, timeout)


async def ensure_persistent_chrome(
    *, initial_url: str = "about:blank", wait: float = PERSISTENT_START_WAIT
) -> bool:
    """拉起常驻窗口并等 CDP 就绪。失败返回 False（由调用方决定是否降级）。

    幂等：同一 profile 重复启动会被 Chrome 自动合并（第二个进程立即退出 rc=21），
    已有窗口不受影响。`start_new_session=True` 让窗口与命令进程解耦——命令退出后
    窗口继续存活。
    """
    chrome = _chrome_bin()
    if not os.path.exists(chrome):
        logger.warning(f"[browser] 找不到 Chrome：{chrome}（可用 GOOFISH_CHROME_PATH 指定）")
        return False

    profile = _persistent_profile()
    profile.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            [
                chrome,
                f"--user-data-dir={profile}",
                f"--remote-debugging-port={_cdp_port()}",
                "--no-first-run",
                "--no-default-browser-check",
                initial_url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        logger.warning(f"[browser] 拉起常驻 Chrome 失败：{e}")
        return False

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if await _cdp_ready():
            logger.info("[browser] 常驻 Chrome 窗口已就绪")
            return True
        await asyncio.sleep(0.3)

    # 超时：回收自己拉起的进程，避免留下"端口不可达"的孤儿 Chrome。
    # 同 profile 重复启动被 Chrome 合并（rc=21，进程已退出）时不误杀——
    # 那个场景背后可能还跑着用户自己的窗口，不能动。
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    logger.warning(f"[browser] 常驻 Chrome 在 {wait:.0f}s 内未就绪（已回收拉起进程）")
    return False


# ── 模式解析与降级链 ─────────────────────────────────────────────────────

def _resolve_mode(mode: str | None) -> str:
    """解析浏览器模式：'persistent' | 'auto' | 'temp'。

    优先级：显式参数 > GOOFISH_BROWSER_MODE > auto。GOOFISH_HEADLESS=1（CI）
    无条件走 temp，避免 GUI 弹窗。
    """
    if mode is None:
        mode = (os.environ.get("GOOFISH_BROWSER_MODE") or "auto").strip().lower()
    if mode not in ("auto", "persistent", "temp"):
        logger.warning(f"[browser] 未知 GOOFISH_BROWSER_MODE={mode!r}，按 auto 处理")
        mode = "auto"
    if os.environ.get("GOOFISH_HEADLESS") == "1":
        return "temp"
    return mode


async def _resolve_backend(mode: str | None) -> str:
    """决定实际使用哪一档：'persistent' | 'temp'（含 Tier 2 拉起与 Tier 3 降级）。

    - temp：直接临时档
    - persistent（显式要求）：连不上 → 尝试拉起 → 仍失败则报错，不静默降级
    - auto：连不上 → 尝试拉起（GOOFISH_CHROME_AUTOSTART=0 时只连不起）→
      仍失败退回临时档 + warning
    """
    resolved = _resolve_mode(mode)
    if resolved == "temp":
        return "temp"

    if await _cdp_ready():
        return "persistent"

    if (resolved == "persistent" or _autostart_enabled()) and await ensure_persistent_chrome(
        initial_url=GOOFISH_HOME_URL  # 自动拉起时停在闲鱼首页：用户可见，便于登录
    ):
        return "persistent"

    if resolved == "persistent":
        raise GoofishError(
            "常驻 Chrome 窗口不可用，且拉起失败（GOOFISH_BROWSER_MODE=persistent 不降级）。"
            "请检查 GOOFISH_CHROME_CDP_PORT / GOOFISH_CHROME_PATH，或先跑 `goofish browser open`。"
        )

    logger.warning("[browser] 常驻窗口不可用，回退临时 profile（行为同旧版 goofish-cli）")
    return "temp"


# ── cookie 回写：窗口 → cookies.json（供 HTTP 直签路径复用）────────────────

async def _sync_cookies_back(context: Any) -> None:
    """把窗口 cookie best-effort 回写 cookies.json。

    守卫：只保留闲鱼/淘系白名单域；必须含 unb + _m_h5_tk，否则不写——
    避免半残快照覆盖可用缓存（与 Session.load 的校验策略一致）。
    并发写由 write_cookies_json 的原子写兜底。
    """
    from goofish_cli.core.browser_cookie import ALLOWED_HOSTS
    from goofish_cli.core.session import resolve_cookie_path, write_cookies_json

    cookies = await context.cookies()
    records: list[CookieRecord] = []
    for c in cookies:
        name, value = c.get("name"), c.get("value")
        domain = c.get("domain") or ""
        if not name or not value:
            continue
        if not any(h in domain.lstrip(".") for h in ALLOWED_HOSTS):
            continue
        records.append({
            "name": name,
            "value": value,
            "domain": domain,
            "path": c.get("path") or "/",
        })

    flat = {r["name"]: r["value"] for r in records}
    if not flat.get("unb") or not flat.get("_m_h5_tk"):
        logger.debug("[browser] 窗口 cookie 缺 unb/_m_h5_tk，跳过回写（保留现有缓存）")
        return

    try:
        write_cookies_json(resolve_cookie_path(), records)
        logger.debug(f"[browser] 已回写窗口 cookie 快照（{len(records)} 条）")
    except OSError as e:
        logger.warning(f"[browser] 回写 cookies.json 失败（不影响本次操作）：{e}")


# ── 两种档位的 page 上下文 ──────────────────────────────────────────────

@asynccontextmanager
async def _persistent_page(*, keep_tab: bool | None = None) -> AsyncIterator[Any]:
    """连接常驻窗口：开自己的标签 → yield → 回写 cookie → 按配置关标签。

    红线：不注入 cookie、不加 stealth 脚本、不 ctx.close() / browser.close()——
    窗口归用户所有，命令只借一个标签。
    """
    from playwright.async_api import async_playwright

    # 双栈解析：IPv4 优先；IPv4 被占时 Chrome 会回退绑 [::1]（实测）
    cdp_url = await _resolve_cdp_url()
    if not cdp_url:
        raise GoofishError("常驻窗口在连接前不可达（可能刚被关闭），本次命令中止；下次调用会重新解析")
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise GoofishError("常驻窗口没有可用的 browser context（请重开窗口）")
        context = browser.contexts[0]
        page = await context.new_page()
        logger.debug(f"[browser] 已连接常驻窗口（{cdp_url}）")
        try:
            yield page
        finally:
            with contextlib.suppress(Exception):
                await _sync_cookies_back(context)
            if _should_close_tab(keep_tab):
                with contextlib.suppress(Exception):
                    await page.close()
        # 注意：不调用 browser.close()。实测它对 CDP 连接只断开、不关窗口，
        # 但仍按最小动作原则避免，窗口生命周期完全归用户。


@asynccontextmanager
async def _temp_page(
    *,
    headless: bool | None = None,
    viewport: tuple[int, int] = (1440, 900),
    cookies: dict | list | None = None,
) -> AsyncIterator[Any]:
    """临时 profile 档（旧行为）：启动系统 Chrome（独立 tmp profile）+ 灌 cookie。"""
    from playwright.async_api import async_playwright

    if headless is None:
        # 默认 headful。CI 用户显式 GOOFISH_HEADLESS=1 切回（可能触发风控）。
        headless = os.environ.get("GOOFISH_HEADLESS") == "1"

    PROFILES_PARENT.mkdir(parents=True, exist_ok=True)
    # 每次调用独立 profile 目录，避开 Chrome SingletonLock 并发冲突
    profile_dir = Path(tempfile.mkdtemp(prefix="chrome-", dir=str(PROFILES_PARENT)))
    if cookies is None:
        cookies = _load_cookies_from_session()
    pw_cookies = _cookies_to_playwright(cookies)

    try:
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="chrome",
                headless=headless,
                viewport={"width": viewport[0], "height": viewport[1]},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-default-browser-check",
                    "--no-first-run",
                ],
            )
            try:
                await context.add_cookies(pw_cookies)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"注入 cookie 失败：{e}")

            page = context.pages[0] if context.pages else await context.new_page()
            # 隐藏 webdriver 特征（CDP 检测 cookie 外的最后一道门）
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            try:
                yield page
            finally:
                await context.close()
    finally:
        # 清理 tmp profile。忽略错误（进程被 kill 时残留目录由用户手动清 ~/.goofish-cli/profiles/）
        shutil.rmtree(profile_dir, ignore_errors=True)


# ── 对外入口 ────────────────────────────────────────────────────────────

@asynccontextmanager
async def goofish_page(
    *,
    headless: bool | None = None,
    viewport: tuple[int, int] = (1440, 900),
    cookies: dict | list | None = None,
    mode: str | None = None,
    keep_tab: bool | None = None,
) -> AsyncIterator[Any]:
    """打开一页闲鱼：优先常驻窗口（默认），失败降级临时 profile。

    - `cookies`：临时档的注入快照；常驻档忽略（窗口会话即真源）。
    - `mode`：None=读 GOOFISH_BROWSER_MODE（auto）；也可显式 'persistent' / 'temp'。
    - `keep_tab`：None=读 GOOFISH_KEEP_TAB（默认 1=保留）；仅常驻档生效。

    用法：
        async with goofish_page() as page:
            await page.goto("https://www.goofish.com/search?q=foo")
            ...
    """
    backend = await _resolve_backend(mode)
    if backend == "persistent":
        if cookies is not None:
            logger.debug("[browser] 常驻档忽略传入的 cookies（以窗口会话为准）")
        async with _persistent_page(keep_tab=keep_tab) as page:
            yield page
        return
    async with _temp_page(headless=headless, viewport=viewport, cookies=cookies) as page:
        yield page


async def auto_scroll(page: Any, times: int = 2, pause_ms: int = 800) -> None:
    """模拟 OpenCLI 的 `page.autoScroll({times})`：滚到底 N 次触发懒加载。"""
    for _ in range(times):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(pause_ms)
    await page.evaluate("window.scrollTo(0, 0)")
