"""测常驻窗口模式：模式解析、三档降级、cookie 回写、keep_tab 与原子写。

Playwright 不进单测（走 fake page / monkeypatch），保证确定性、无网络。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import goofish_cli.core.browser as browser_mod
from goofish_cli.core.browser import (
    _resolve_backend,
    _resolve_mode,
    _should_close_tab,
    _sync_cookies_back,
)
from goofish_cli.core.crypto import decrypt_cookies
from goofish_cli.core.errors import GoofishError
from goofish_cli.core.session import write_cookies_json

# ── _resolve_mode 真值表 ────────────────────────────────────────────────

def test_resolve_mode_default_auto(monkeypatch):
    monkeypatch.delenv("GOOFISH_BROWSER_MODE", raising=False)
    monkeypatch.delenv("GOOFISH_HEADLESS", raising=False)
    assert _resolve_mode(None) == "auto"


def test_resolve_mode_env_value(monkeypatch):
    monkeypatch.delenv("GOOFISH_HEADLESS", raising=False)
    monkeypatch.setenv("GOOFISH_BROWSER_MODE", "temp")
    assert _resolve_mode(None) == "temp"
    monkeypatch.setenv("GOOFISH_BROWSER_MODE", "persistent")
    assert _resolve_mode(None) == "persistent"


def test_resolve_mode_explicit_wins_over_env(monkeypatch):
    monkeypatch.delenv("GOOFISH_HEADLESS", raising=False)
    monkeypatch.setenv("GOOFISH_BROWSER_MODE", "temp")
    assert _resolve_mode("persistent") == "persistent"


def test_resolve_mode_headless_forces_temp(monkeypatch):
    """规则 2：GOOFISH_HEADLESS=1（CI）无条件走 temp，绝不弹窗。"""
    monkeypatch.setenv("GOOFISH_HEADLESS", "1")
    monkeypatch.setenv("GOOFISH_BROWSER_MODE", "persistent")
    assert _resolve_mode(None) == "temp"
    assert _resolve_mode("persistent") == "temp"


def test_resolve_mode_invalid_falls_back_auto(monkeypatch):
    monkeypatch.delenv("GOOFISH_HEADLESS", raising=False)
    monkeypatch.setenv("GOOFISH_BROWSER_MODE", "bogus")
    assert _resolve_mode(None) == "auto"


# ── _resolve_backend 三档降级 ───────────────────────────────────────────

def _patch_probe(monkeypatch, *, ready: bool, start_ok: bool):
    async def _ready(timeout=1.0):
        return ready

    async def _start(**kwargs):
        return start_ok

    monkeypatch.setattr(browser_mod, "_cdp_ready", _ready)
    monkeypatch.setattr(browser_mod, "ensure_persistent_chrome", _start)


def _clean_env(monkeypatch):
    for var in ("GOOFISH_HEADLESS", "GOOFISH_BROWSER_MODE", "GOOFISH_CHROME_AUTOSTART"):
        monkeypatch.delenv(var, raising=False)


def test_backend_temp_short_circuits_without_probe(monkeypatch):
    """temp 档不探测 CDP（qr 扫码走这里，不能受常驻窗口状态影响）。"""
    probed: list[bool] = []

    async def _ready(timeout=1.0):
        probed.append(True)
        return True

    monkeypatch.setattr(browser_mod, "_cdp_ready", _ready)
    _clean_env(monkeypatch)
    assert asyncio.run(_resolve_backend("temp")) == "temp"
    assert probed == []


def test_backend_auto_uses_existing_window(monkeypatch):
    _patch_probe(monkeypatch, ready=True, start_ok=False)
    _clean_env(monkeypatch)
    assert asyncio.run(_resolve_backend(None)) == "persistent"


def test_backend_auto_starts_window_when_missing(monkeypatch):
    _patch_probe(monkeypatch, ready=False, start_ok=True)
    _clean_env(monkeypatch)
    assert asyncio.run(_resolve_backend(None)) == "persistent"


def test_backend_auto_degrades_to_temp(monkeypatch):
    """连不上 + 拉起失败 → 回退临时档（保证命令仍能工作）。"""
    _patch_probe(monkeypatch, ready=False, start_ok=False)
    _clean_env(monkeypatch)
    assert asyncio.run(_resolve_backend(None)) == "temp"


def test_backend_auto_respects_autostart_off(monkeypatch):
    """GOOFISH_CHROME_AUTOSTART=0：只连不起。"""
    started: list[bool] = []

    async def _ready(timeout=1.0):
        return False

    async def _start(**kwargs):
        started.append(True)
        return True

    monkeypatch.setattr(browser_mod, "_cdp_ready", _ready)
    monkeypatch.setattr(browser_mod, "ensure_persistent_chrome", _start)
    _clean_env(monkeypatch)
    monkeypatch.setenv("GOOFISH_CHROME_AUTOSTART", "0")
    assert asyncio.run(_resolve_backend(None)) == "temp"
    assert started == []


def test_backend_persistent_raises_when_unavailable(monkeypatch):
    """显式 persistent 不静默降级：失败要报错。"""
    _patch_probe(monkeypatch, ready=False, start_ok=False)
    _clean_env(monkeypatch)
    with pytest.raises(GoofishError):
        asyncio.run(_resolve_backend("persistent"))


# ── keep_tab 语义 ───────────────────────────────────────────────────────

def test_should_close_tab_default_is_keep(monkeypatch):
    """默认保留标签（用户手动关闭）→ 不调用 page.close。"""
    monkeypatch.delenv("GOOFISH_KEEP_TAB", raising=False)
    assert _should_close_tab(None) is False
    assert _should_close_tab(True) is False
    assert _should_close_tab(False) is True


def test_should_close_tab_env_off(monkeypatch):
    monkeypatch.setenv("GOOFISH_KEEP_TAB", "0")
    assert _should_close_tab(None) is True
    monkeypatch.setenv("GOOFISH_KEEP_TAB", "1")
    assert _should_close_tab(None) is False
    # 显式参数优先于 env
    assert _should_close_tab(True) is False


# ── _sync_cookies_back 守卫（窗口 → cookies.json）────────────────────────

class _FakeCtx:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self):
        return self._cookies


def _goofish_cookies():
    return [
        {"name": "unb", "value": "u1", "domain": ".goofish.com", "path": "/"},
        {"name": "_m_h5_tk", "value": "tok_123", "domain": ".goofish.com", "path": "/"},
        {"name": "cookie2", "value": "c2", "domain": ".goofish.com", "path": "/"},
    ]


def test_sync_cookies_back_writes_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOFISH_COOKIES_PATH", str(tmp_path / "cookies.json"))
    asyncio.run(_sync_cookies_back(_FakeCtx(_goofish_cookies())))
    path = tmp_path / "cookies.json"
    assert path.exists()
    flat = {r["name"]: r["value"] for r in decrypt_cookies(path.read_bytes())}
    assert flat["unb"] == "u1"
    assert flat["_m_h5_tk"] == "tok_123"


def test_sync_cookies_back_skips_without_required_fields(tmp_path, monkeypatch):
    """缺 unb / _m_h5_tk 一律不写——避免半残快照覆盖可用缓存。"""
    monkeypatch.setenv("GOOFISH_COOKIES_PATH", str(tmp_path / "cookies.json"))
    asyncio.run(_sync_cookies_back(_FakeCtx([
        {"name": "cookie2", "value": "c2", "domain": ".goofish.com", "path": "/"},
    ])))
    assert not (tmp_path / "cookies.json").exists()


def test_sync_cookies_back_filters_foreign_domains(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOFISH_COOKIES_PATH", str(tmp_path / "cookies.json"))
    cookies = _goofish_cookies() + [
        {"name": "evil", "value": "x", "domain": ".example.com", "path": "/"},
    ]
    asyncio.run(_sync_cookies_back(_FakeCtx(cookies)))
    flat = {r["name"] for r in decrypt_cookies((tmp_path / "cookies.json").read_bytes())}
    assert "evil" not in flat
    assert "unb" in flat


# ── cookies.json 原子写 ─────────────────────────────────────────────────

def test_write_cookies_json_atomic_no_tmp_left(tmp_path):
    path = tmp_path / "cookies.json"
    write_cookies_json(path, {"unb": "u1", "_m_h5_tk": "t1"})
    assert path.exists()
    assert list(tmp_path.glob("*.tmp")) == []
    # 覆盖写：新值生效、旧值不残留
    write_cookies_json(path, {"unb": "u2", "_m_h5_tk": "t2"})
    flat = {r["name"]: r["value"] for r in decrypt_cookies(path.read_bytes())}
    assert flat["unb"] == "u2"


# ── qr_login 强制 temp（常驻窗口已登录会跳过扫码）────────────────────────

def test_qr_login_forces_temp_mode(monkeypatch):
    import goofish_cli.core.qr_login as qr

    captured: dict[str, object] = {}

    class _Page:
        async def goto(self, *args, **kwargs):
            return None

        async def wait_for_timeout(self, *args, **kwargs):
            return None

    @asynccontextmanager
    async def fake_page(*, cookies=None, mode=None, keep_tab=None):
        captured["cookies"] = cookies
        captured["mode"] = mode
        yield _Page()

    async def no_qr(page, timeout_ms=15000):
        return False

    monkeypatch.setattr(browser_mod, "goofish_page", fake_page)
    monkeypatch.setattr(qr, "_wait_for_qr", no_qr)

    out = asyncio.run(qr._login_via_qr_async(1))
    assert out == []
    assert captured["mode"] == "temp"
    assert captured["cookies"] == {}


# ── 双栈探测：IPv4 被占时 Chrome 回退绑 [::1]（实测行为）────────────────────

def test_cdp_candidates_prefer_ipv4_then_ipv6(monkeypatch):
    monkeypatch.setenv("GOOFISH_CHROME_CDP_PORT", "12345")
    assert browser_mod._cdp_candidates() == [
        "http://127.0.0.1:12345",
        "http://[::1]:12345",
    ]


class _FakeResp:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self.ok = ok

    def json(self):
        return self._payload


def test_probe_falls_back_to_ipv6(monkeypatch):
    """IPv4 连不上、IPv6 返回合法 Chrome 端点 → 命中 [::1]。"""
    calls: list[str] = []

    def fake_get(url, timeout=None):
        calls.append(url)
        if "127.0.0.1" in url:
            raise OSError("connection refused")
        return _FakeResp({"Browser": "Chrome/154"})

    monkeypatch.setattr(browser_mod, "requests", SimpleNamespace(get=fake_get))
    assert browser_mod._probe_cdp_sync(1.0) == "http://[::1]:37821"
    assert calls == [
        "http://127.0.0.1:37821/json/version",
        "http://[::1]:37821/json/version",
    ]


def test_probe_rejects_non_chrome_listener(monkeypatch):
    """端口被别的服务占用（无 Browser 标识）→ 不视为可用。"""
    def fake_get(url, timeout=None):
        return _FakeResp({"hello": "world"})

    monkeypatch.setattr(browser_mod, "requests", SimpleNamespace(get=fake_get))
    assert browser_mod._probe_cdp_sync(1.0) is None


# ── 拉起超时回收（防孤儿 Chrome）────────────────────────────────────────

class _FakeProc:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True


def _patch_spawn(monkeypatch, proc):
    monkeypatch.setattr(browser_mod.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(browser_mod, "_chrome_bin", lambda: "/bin/echo")


def test_ensure_reclaims_orphan_on_timeout(tmp_path, monkeypatch):
    proc = _FakeProc()
    _patch_spawn(monkeypatch, proc)

    async def never_ready(timeout=1.0):
        return False

    monkeypatch.setattr(browser_mod, "_cdp_ready", never_ready)
    monkeypatch.setenv("GOOFISH_CHROME_PROFILE", str(tmp_path / "prof"))

    assert asyncio.run(browser_mod.ensure_persistent_chrome(wait=0.6)) is False
    assert proc.terminated is True


def test_ensure_does_not_kill_merged_singleton(tmp_path, monkeypatch):
    """同 profile 重复启动被 Chrome 合并（rc=21）：不得误杀背后的用户窗口。"""
    proc = _FakeProc(returncode=21)
    _patch_spawn(monkeypatch, proc)

    async def never_ready(timeout=1.0):
        return False

    monkeypatch.setattr(browser_mod, "_cdp_ready", never_ready)
    monkeypatch.setenv("GOOFISH_CHROME_PROFILE", str(tmp_path / "prof"))

    assert asyncio.run(browser_mod.ensure_persistent_chrome(wait=0.6)) is False
    assert proc.terminated is False
