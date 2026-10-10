"""browser open — 打开/确认常驻 Chrome 窗口。

常驻窗口是浏览器路径命令（search items / item view / token 自动续命）的默认
执行环境：工具在窗口里开新标签页干活，窗口本身常驻不关、可被用户日常浏览使用。

本命令用于：首次启用（打开窗口后在里面登录闲鱼）、窗口被误关/电脑重启后恢复。
窗口已在运行时幂等——直接返回现状，不会重复拉起。
"""
from __future__ import annotations

import asyncio
from typing import Any

from goofish_cli.core import Strategy, command


@command(
    namespace="browser",
    name="open",
    description="打开/确认常驻 Chrome 窗口（浏览器命令共用；首次需在窗口内登录闲鱼）",
    strategy=Strategy.PUBLIC,
    columns=["ok", "port", "profile", "started"],
)
def open_window() -> dict[str, Any]:
    """确保常驻窗口在运行：已在跑直接返回；没跑则拉起（停在闲鱼首页）。

    窗口关掉后重跑本命令即可恢复；同 profile 重复启动会被 Chrome 自动合并，
    不会出现两个窗口抢同一份登录态。
    """
    from goofish_cli.core.browser import (
        GOOFISH_HOME_URL,
        _cdp_port,
        _cdp_ready,
        _persistent_profile,
        ensure_persistent_chrome,
    )

    async def _run() -> dict[str, Any]:
        alive = await _cdp_ready()
        started = False
        if not alive:
            started = await ensure_persistent_chrome(initial_url=GOOFISH_HOME_URL)
        return {
            "ok": alive or started,
            "port": _cdp_port(),
            "profile": str(_persistent_profile()),
            "started": started,
        }

    return asyncio.run(_run())
