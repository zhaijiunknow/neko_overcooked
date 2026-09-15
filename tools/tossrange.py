# -*- coding: utf-8 -*-
"""**量一次投掷射程** —— 手拿着东西、朝当前朝向丢一次, 报"飞了多远、落在哪"。

为什么必须先量: 用户 2026-09-15 提的策略是"**取货时把食材往下一步的方向丢**"
(那关米箱在 (-1.2,7.2)、三口锅在 (10.8~13.2, 10.8), 直线 12 格开外)。但射程**我们不知道**:
  · `ServerAttachmentThrower.CalculateThrowVelocity` = `方向 × m_throwForce`
    —— **`m_throwForce` 是 prefab 上的定值, 插件没上报**;
  · 所以 `neko/engine.py` 里传球用的 `NEKO_PASS_RANGE` 默认 **3 格**,
    注释原话是"m_throwForce 没上报 → 这个数是**保守估计**"。
⇒ 没有这个数, "往哪丢/丢不丢得到"全无从判断; 而丢歪就是把料扔进水里或扔在原地。

用法(引擎**别同时跑** —— 这个工具会真的丢东西):
  python -u tools\\tossrange.py                  # 就地丢一次, 报落点与距离
  python -u tools\\tossrange.py --player 0       # 指定玩家号(0=P1, 1=P2 默认)
  python -u tools\\tossrange.py --repeat 3       # 连丢 3 次

⚠ **朝向就是飞出去的方向** —— 先用方向键把人转到位再跑; 工具会把飞出去的
  **方向向量**打出来, 那就是当时的朝向。
⚠ 落点是**取前后快照的差集**算的(新出现的那个人就是它), 不按距离猜 ——
  否则旁边本来就有一样东西时会认错。
⚠ 丢出去的东西落在**地上**(不是台面), 要它回来得走回去捡
  (取料三源里本来就有"地上的料")。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from bridge.client import BridgeClient          # noqa: E402
from map_model import KitchenMap                # noqa: E402
from terrain import TerrainMap                  # noqa: E402


def _want(player: int) -> str:
    return "two" if player else "one"


def _is_mine(raw_player: str, player: int) -> bool:
    """厨师归属匹配。⚠ 实机里 `player` 字段是 **`"One"` / `"Two"`**,
    **不是** `"Player.One"` —— 我第一版按后者写, 结果"找不到 P2 的厨师"(场上明明有 2 只)。
    引擎那边有 `replace("player.", "")` 兜着, 工具里漏了。"""
    p = (raw_player or "").strip().lower().replace("player.", "").replace("player", "")
    return p == _want(player)


def _chef_y(st: dict, player: int):
    """这只厨师的当前高度 —— 投掷是**抛物线**, 发射高度决定飞多远, 必须一起看。"""
    for c in ((st.get("layout") or {}).get("chefs") or []):
        if _is_mine(c.get("player"), player):
            try:
                return float(c.get("y") or 0.0)
            except (TypeError, ValueError):
                return None
    return None


def _floor_at(tm, x: float, z: float):
    """这一格的**地板高度** —— 落点地面比发射点低, 就会飞得更远。取不到给 None。"""
    if tm is None or not getattr(tm, "ok", False):
        return None
    try:
        return tm.cell_floor_y(*tm.cell_of(x, z))
    except Exception:                                         # noqa: BLE001
        return None


def _norm(s: str) -> str:
    t = re.sub(r"\s*\(\d+\)\s*$", "", (s or "").strip())
    return "".join(ch for ch in t.lower() if ch.isalnum())


def _spots(km: KitchenMap, name: str) -> set:
    """这东西此刻都在哪些位置(地上 + 台面上)。"""
    n = _norm(name)
    out = set()
    if not n:
        return out
    for it in (km.items or []):
        if n in (_norm(it.ing), _norm(it.name)):
            out.add((round(it.x, 2), round(it.z, 2)))
    for s in (km.stations or {}).values():
        for o in (s.on or []):
            if _norm(o) == n:
                out.add((round(s.x, 2), round(s.z, 2)))
    return out


def _me(km: KitchenMap, player: int):
    for c in (km.chefs or []):
        if _is_mine(getattr(c, "player", ""), player):
            return c
    return None


def _report(b, x0, z0, y0, bx, bz, held) -> float:
    """打一行"飞了多远/什么方向/什么落差", 返回水平距离。"""
    dx, dz = bx - x0, bz - z0
    d = (dx * dx + dz * dz) ** 0.5
    print("     落点 (%.2f,%.2f)   飞了 **%.2f 格**   方向 (%.2f, %.2f)" % (bx, bz, d, dx, dz))
    # **高度**: 投掷是抛物线 —— 同样的力, 落点地面越低飞得越远。
    #   所以"射程"这个数必须和落点高度一起看, 否则换个台面就变了(用户 2026-09-15 提醒)。
    try:
        tm = TerrainMap(b.get_map(force=True))
        fa, fb = _floor_at(tm, x0, z0), _floor_at(tm, bx, bz)
        if fa is not None and fb is not None:
            print("     高度: 发射点地板 %+.2f, 落点地板 %+.2f, **落差 %+.2f**"
                  % (fa, fb, fb - fa))
            if y0 is not None:
                print("           丢出时厨师 y=%+.2f ⇒ 离地 %.2f" % (y0, y0 - fa))
        else:
            print("     高度: 读不到地板高度 —— 这一栏先空着")
    except Exception as e:                                    # noqa: BLE001
        print("     高度: 取地形失败(%s)" % e)
    return d


def _observe(b, a, dists: list) -> int:
    """**你自己丢, 我只量**。判据: 手上那个东西**从有到无**的那一刻 = 出手。

    每次读都记下"它此刻都在哪"(差集要的 before)和厨师的位置/高度, 所以出手瞬间
    我们手上就有完整的"丢之前"快照 —— 不需要预测, 也不需要自己按键。

    ⚠ **默认盯两只**(`--player -1`) —— 第一版只盯 P2, 而**人在玩 P1**(脚本占 P2 是这个
      项目的常态), 于是"丢了但没数据"。盯两只没有代价, 别再按号盯。
    """
    watch = [0, 1] if a.player < 0 else [a.player]
    who = "两只都盯" if a.player < 0 else ("P%d" % (a.player + 1))
    print("[看] %s: 你丢, 我来量 —— 手上东西一出手就报落点/方向/落差 (Ctrl+C 停)" % who)
    stt = {p: {"held": "", "before": set(), "pos": None, "y": None} for p in watch}
    while True:
        try:
            st = b.get_state() or {}
        except Exception as e:                                # noqa: BLE001
            print("     读状态失败: %s" % e)
            return 1
        if not st.get("inRound"):
            time.sleep(0.5)
            continue
        km = KitchenMap.from_layout(st.get("layout") or {})
        for p in watch:
            me = _me(km, p)
            if me is None:
                continue
            s = stt[p]
            held = getattr(me, "held", "") or ""
            if held:
                # 手上一直有东西 → 持续更新"丢之前"的快照
                s.update(held=held, before=_spots(km, held),
                         pos=(me.x, me.z), y=_chef_y(st, p))
                continue
            if not (s["held"] and s["pos"]):
                continue
            # **从有到无 = 出手了** —— 等它落地再读
            print("[看] P%d 的 %r 出手了(从 (%.2f,%.2f)) —— 等落点..."
                  % (p + 1, s["held"], s["pos"][0], s["pos"][1]))
            time.sleep(a.wait)
            st2 = b.get_state() or {}
            km2 = KitchenMap.from_layout(st2.get("layout") or {})
            new = _spots(km2, s["held"]) - s["before"]
            if new:
                x0, z0 = s["pos"]
                bx, bz = min(new, key=lambda q: (q[0] - x0) ** 2 + (q[1] - z0) ** 2)
                dists.append(_report(b, x0, z0, s["y"], bx, bz, s["held"]))
            else:
                print("     ⚠ 没看到新落点(被队友捡走了/被传走了/掉进水里没了?)")
            stt[p] = {"held": "", "before": set(), "pos": None, "y": None}
        time.sleep(0.12)


def main() -> int:
    ap = argparse.ArgumentParser(description="量一次投掷射程")
    ap.add_argument("--player", type=int, default=-1,
                    help="0=P1, 1=P2, **-1=两只都盯(默认)**")
    ap.add_argument("--repeat", type=int, default=1, help="连丢几次")
    ap.add_argument("--wait", type=float, default=1.6, help="丢完等多久再看落点(默认 1.6s)")
    ap.add_argument("--observe", action="store_true",
                    help="**旁观模式: 你自己丢, 我只量** —— 手上东西一出手就报落点/距离/落差")
    a = ap.parse_args()

    b = BridgeClient()
    try:
        b.connect(retries=3, interval=1.0)
    except Exception as e:                                    # noqa: BLE001
        print("连不上桥(游戏没开?): %s" % e)
        return 1
    dists = []
    if a.observe:
        return _observe(b, a, dists)
    try:
        for k in range(max(1, a.repeat)):
            st = b.get_state() or {}
            if not st.get("inRound"):
                print("[扔] 不在对局里")
                return 1
            km = KitchenMap.from_layout(st.get("layout") or {})
            me = _me(km, a.player)
            if me is None:
                print("[扔] 找不到 P%d 的厨师(场上 %d 只)"
                      % (a.player + 1, len(km.chefs or [])))
                return 1
            held = getattr(me, "held", "") or ""
            if not held:
                print("[扔] 手上是空的(第 %d 次) —— 先拿一样东西。"
                      "**挑不心疼的**(比如随手取的生料), 别把要用的丢了" % (k + 1))
                break
            x0, z0 = me.x, me.z
            y0 = _chef_y(st, a.player)
            before = _spots(km, held)
            print("[扔] 第 %d 次: (%.2f,%.2f) y=%s 手拿 %r(全场它现在有 %d 处) —— 丢!"
                  % (k + 1, x0, z0, ("%.2f" % y0) if y0 is not None else "?", held, len(before)))
            try:
                r = b.direct("throw", player=a.player)
            except Exception as e:                            # noqa: BLE001
                print("     直调 throw 失败: %s" % e)
                return 1
            if not r.get("ok"):
                print("     游戏没收下: %s" % (r.get("error") or r))
                return 1
            time.sleep(a.wait)

            st2 = b.get_state() or {}
            km2 = KitchenMap.from_layout(st2.get("layout") or {})
            new = _spots(km2, held) - before
            if not new:
                print("     ⚠ 没看到新的落点 —— 可能被传走/被吃了/还在原地")
                continue
            bx, bz = min(new, key=lambda p: (p[0] - x0) ** 2 + (p[1] - z0) ** 2)
            dx, dz = bx - x0, bz - z0
            d = (dx * dx + dz * dz) ** 0.5
            dists.append(d)
            print("     落点 (%.2f,%.2f)   飞了 **%.2f 格**   方向 (%.2f, %.2f)"
                  % (bx, bz, d, dx, dz))
            # **高度**: 投掷是抛物线, 同样的力, 落点地面越低飞得越远 ——
            #   所以这个数必须和落点高度一起看, 否则"射程"换个台面就变了。
            try:
                tm = TerrainMap(b.get_map(force=True))
                fa, fb = _floor_at(tm, x0, z0), _floor_at(tm, bx, bz)
                if fa is not None and fb is not None:
                    print("     高度: 发射点地板 %+.2f, 落点地板 %+.2f, **落差 %+.2f**"
                          % (fa, fb, fb - fa))
                    if y0 is not None:
                        print("           发射时厨师 y=%+.2f ⇒ 离地 %.2f" % (y0, y0 - fa))
                else:
                    print("     高度: 读不到地板高度(老 dll?) —— 这一栏先空着")
            except Exception as e:                            # noqa: BLE001
                print("     高度: 取地形失败(%s)" % e)
    finally:
        try:
            b.close()
        except Exception:                                     # noqa: BLE001
            pass
    if dists:
        print("\n[扔] 本次 %d 次: %s" % (len(dists), ", ".join("%.2f" % d for d in dists)))
        print("     ⇒ 把 `NEKO_PASS_RANGE`(默认 3.0)按这个数改, 或者告诉开发者这个数")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
