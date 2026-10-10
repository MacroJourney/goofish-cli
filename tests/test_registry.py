"""验 registry discover 能找到所有命令且无重名。"""
from __future__ import annotations


def test_discover_all_commands():
    from goofish_cli.core.registry import discover, iter_commands

    discover()
    names = [c.full_name for c in iter_commands()]
    # 至少有这些命令
    expected = {
        "auth.login",
        "auth.status",
        "auth.reset-guard",
        "item.get",
        "item.delete",
        "item.publish",
        "media.upload",
        "category.recommend",
        "location.default",
    }
    assert expected.issubset(set(names)), f"缺失命令: {expected - set(names)}"


def test_write_commands_marked():
    from goofish_cli.core.registry import discover, registry

    discover()
    r = registry()
    assert r["item.delete"].write is True
    assert r["item.publish"].write is True
    assert r["media.upload"].write is True
    assert r["item.get"].write is False


def test_collect_commands_registered_readonly():
    """采集命令必须注册且全部为只读（被动采集不产生任何闲鱼请求）。"""
    from goofish_cli.core.registry import discover, registry

    discover()
    r = registry()
    collect = {name for name in r if name.startswith("collect.")}
    assert collect == {
        "collect.watch",
        "collect.status",
        "collect.search",
        "collect.item",
        "collect.recent",
        "collect.export",
    }
    assert all(r[name].write is False for name in collect)


def test_collect_positional_args_declared():
    """带默认值的参数会被 Typer 当选项；需位置参数时必须显式声明 `arguments`。

    回归背景：collect search 的 query 最初未声明（带默认值 → 被当 `--query`），
    CLI 报 "unexpected extra argument"。无默认值的参数（item_id / out）会自动
    位置化，无需声明——保持与 `item view <item_id>` 等既有命令一致。
    """
    from goofish_cli.core.registry import discover, registry

    discover()
    r = registry()
    assert r["collect.search"].arguments == ["query"]
    assert r["collect.item"].arguments == []
    assert r["collect.export"].arguments == []
