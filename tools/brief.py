# -*- coding: utf-8 -*-
"""**一句话概要** —— 随时看一眼"这局现在是什么情况"(纯 Python, 不动 C#/不重编)。

用户要求(2026-09-15): "接口暴露…**主要是拿到当前游戏的概要信息**"。
`state` 里其实**什么都有**(台面/厨师/食材/订单/菜谱明细), 但它是给机器看的 JSON ——
这个工具把它压成**一行**人话, 再也不用为了"现在几点了/他手上拿的啥"去翻几百行日志。

用法:
  python -u tools\\brief.py            # 一句话
  python -u tools\\brief.py --chain    # 顺带摊开**每张订单的步骤链**(游戏认的 vs derive 产的)
  python -u tools\\brief.py --watch    # 每 3 秒重打一行(盯现场用)

⚠ 只读: 不按键、不移动、不装手柄 —— 引擎正在跑的时候也能随时开。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from bridge.client import BridgeClient          # noqa: E402
from map_model import KitchenMap                # noqa: E402


def one_line(b, st, km, live, dyn) -> str:
    """把这一局的现状压成一行。"""
    scene = st.get("scene") or "?"
    chefs = km.chefs or []
    parts = []
    for c in chefs:
        parts.append("P%s(%.1f,%.1f)%s" % (
            int(getattr(c, "id", 0)) + 1, c.x, c.z,
            ("拿" + c.held) if getattr(c, "held", "") else "空手"))
    orders = []
    for o in live:
        nm = o.get("name") or "?"
        try:
            orders.append("%s(剩%.0f%%)" % (nm, float(o.get("t", 0)) * 100))
        except (TypeError, ValueError):
            orders.append(str(nm))
    counts = []
    nf = len((dyn or {}).get("fires") or [])
    if nf:
        counts.append("**火%d**" % nf)
    dirty = sum(int(getattr(s, "n", 0) or 0) for s in km.stations.values()
                if s.id.startswith("dirty_plates"))
    if dirty:
        counts.append("脏盘%d" % dirty)
    for sem, label in (("mix", "搅拌台"), ("wash", "洗手池"), ("crate", "箱子")):
        n = len(km.of(sem))
        if n:
            counts.append("%s%d" % (label, n))
    # 台面上"有东西"的（看一眼谁在占着台面）
    busy = ["%s:%s" % (s.id, ",".join(s.on)) for s in km.stations.values()
            if s.on and not s.id.startswith(("counter", "board"))
            ][:4]
    return "[brief] %s | 订单 %s | %s | %s%s" % (
        scene,
        " + ".join(orders) or "(没有订单)",
        "  ".join(parts) or "(没有厨师)",
        " ".join(counts) or "-",
        ("  | 台面: " + " ".join(busy)) if busy else "")


def chains(b, st, km) -> None:
    """**摊开每张订单的步骤链** —— 游戏认的那条 vs `derive()` 产的那条。

    这一节是给"行为解析"那块的诊断用(交接包 §0.5 B):
    两条链**逐行对得上**才算 `derive()` 忠实; 对不上的地方就是下一批 bug 的产地。
    """
    from cookbook import Knowledge, derive
    details = st.get("details") or []
    if not details:
        print("  (游戏没报菜谱明细 —— 不在对局里?)")
        return
    try:
        know = Knowledge.from_json(b.get_knowledge() or {})
    except Exception as e:                                        # noqa: BLE001
        print("  知识表读不到(%r) —— 只打游戏侧" % e)
        know = None
    for d in details:
        name = d.get("name") or "?"
        print("\n  【%s】" % name)
        raw = d.get("chain") or d.get("steps") or d.get("nodes")
        print("    游戏认的: %s" % (raw if raw else
                                   "(这条 key 没解析出来, 原样: %s)"
                                   % str({k: v for k, v in d.items() if k != "name"})[:300]))
        if know is None:
            continue
        try:
            flow = derive(d, know)
        except Exception as e:                                    # noqa: BLE001
            print("    derive(): **炸了** %r" % e)
            continue
        print("    derive(): %s" % " → ".join(
            "%s %s" % (o.action, o.target) for o in flow.ops))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", action="store_true", help="顺带摊开每张订单的步骤链")
    ap.add_argument("--watch", type=float, nargs="?", const=3.0, default=None,
                    help="每 N 秒重打(默认 3)")
    ap.add_argument("--json", action="store_true", help="原样打 state(调试用)")
    args = ap.parse_args()

    b = BridgeClient()
    try:
        b.connect(retries=3, interval=1.0)
    except Exception as e:                                        # noqa: BLE001
        print("连不上桥(游戏没开?): %s" % e)
        return 1
    try:
        while True:
            st = b.get_state() or {}
            if args.json:
                import json
                print(json.dumps(st, ensure_ascii=False)[:4000])
            elif not st.get("inRound"):
                print("[brief] 不在对局里(scene=%s)" % (st.get("scene") or "?"))
            else:
                km = KitchenMap.from_layout(st.get("layout") or {})
                try:
                    live = (b.get_live_orders() or {}).get("live") or []
                except Exception:                                 # noqa: BLE001
                    live = []
                try:
                    dyn = b.get_dyn() or {}
                except Exception:                                 # noqa: BLE001
                    dyn = {}
                print(one_line(b, st, km, live, dyn), flush=True)
                if args.chain:
                    chains(b, st, km)
            if args.watch is None:
                break
            time.sleep(args.watch)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            b.close()
        except Exception:                                         # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
