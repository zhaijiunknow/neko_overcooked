# -*- coding: utf-8 -*-
"""**"这一格是怎么被连通的?"** —— 把泛洪的**实际路径**打出来。

为什么要它(用户实测指出): "你不能因为下沉楼梯三个相邻就判断它可达"。
  泛洪是**从厨师往外扩散**的, 所以一片区域被判成可达, 一定存在一条**逐步 ≤STEP_MAX
  的路径**从厨师走到那儿。而"那片区域内部彼此相邻"本身**不构成**可达 ——
  必须有一跳**从别处跨进去**。
  所以当"某处不该可达却被判成可达"时, 唯一要查的就是:**那一跳在哪、落差多少**。

本工具就把它打出来: 从厨师到目标格的路径 + 每一跳的落差 + 最大的一跳。
最大的一跳若 > STEP_MAX, 就是泛洪的 bug; 若 ≤ STEP_MAX, 那就是**这条路径真的存在**
(该查的是"游戏里这条路径通不通", 而不是泛洪)。

用法:
  python -u tools/whyreach.py 20 13            # 目标格 (i, j)
  python -u tools/whyreach.py 20 13 --cid 1
  python -u tools/whyreach.py 20 13 --at 21.6 -8.4   # 也支持世界坐标定位目标格
"""
import argparse
import os
import sys
from collections import deque

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from bridge.client import BridgeClient          # noqa: E402
from terrain import TerrainMap, STEP_MAX        # noqa: E402
from map_model import KitchenMap, teleport_edges  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("i", type=int, help="目标格 i(或 --at 时的世界 x)")
    ap.add_argument("j", type=int, help="目标格 j(或 --at 时的世界 z)")
    ap.add_argument("--cid", type=int, default=0)
    ap.add_argument("--at", action="store_true", help="参数给的是世界坐标, 不是格号")
    ap.add_argument("--max", type=int, default=200, help="最多打几个途经点")
    args = ap.parse_args()

    b = BridgeClient()
    b.connect(retries=4, interval=1.0)
    st = b.get_state()
    if not st or not st.get("inRound"):
        print("不在对局里")
        b.close()
        return 1
    tm = TerrainMap(b.get_map(force=True))
    try:
        ed = teleport_edges(KitchenMap.from_layout(st.get("layout") or {}), tm)
    except Exception:
        ed = {}

    chef = None
    for c in (st.get("layout") or {}).get("chefs") or []:
        if int(c.get("id", -1)) == args.cid:
            chef = c
    if chef is None:
        print("没找到厨师 cid=%d" % args.cid)
        b.close()
        return 1
    sx, sz = float(chef.get("x") or 0), float(chef.get("z") or 0)
    start = tm.cell_of(sx, sz)
    goal = tm.cell_of(args.i, args.j) if args.at else (args.i, args.j)

    print("厨师 cid=%d 世界(%.1f,%.1f) y=%.2f → 起点格 %s"
          % (args.cid, sx, sz, float(chef.get("y") or 0), start))
    print("目标格 %s  世界(%.1f,%.1f)" % (goal, *tm.world_of(*goal)))
    print("跨步阈值 STEP_MAX = %.2f" % STEP_MAX)
    print()

    # ---- BFS 带父指针, 顺便记下每一步的落差 ----
    prev = {start: None}
    q = deque([start])
    while q:
        cur = q.popleft()
        for t in ed.get(cur, ()):                 # 传送门边
            if t not in prev:
                prev[t] = (cur, None)             # None = 传送(没有落差)
                q.append(t)
        i, j = cur
        for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nb = (i + di, j + dj)
            if nb in prev or not tm.inside(*nb):
                continue
            if not tm.walkable(nb[0], nb[1]) or not tm.step_ok(i, j, nb[0], nb[1]):
                continue
            prev[nb] = (cur, abs((tm.cell_floor_y(*nb) or 0)
                                 - (tm.cell_floor_y(*cur) or 0)))
            q.append(nb)

    if goal not in prev:
        print("目标格**不可达**(泛洪到不了) —— 那图上就该画成 'V'")
        b.close()
        return 0

    # ---- 还原路径 ----
    path = []
    cur = goal
    while cur is not None:
        path.append(cur)
        p = prev[cur]
        cur = p[0] if p else None
    path.reverse()
    print("可达 —— 路径 %d 步(下面只打落差大的和首尾):" % len(path))
    hops = []
    for k in range(1, len(path)):
        a, c = path[k - 1], path[k]
        fa, fc = tm.cell_floor_y(*a), tm.cell_floor_y(*c)
        hops.append((k, a, c, fa, fc))
    hops.sort(key=lambda t: -(t[3] is not None and t[4] is not None
                              and abs((t[3] or 0) - (t[4] or 0)) or 0))
    for k, a, c, fa, fc in hops[:8]:
        d = None if (fa is None or fc is None) else abs(fa - fc)
        print("   第%3d步  %s → %s   地板 %s → %s   落差 %s%s"
              % (k, a, c,
                 "?" if fa is None else "%.2f" % fa,
                 "?" if fc is None else "%.2f" % fc,
                 "?" if d is None else "%.2f" % d,
                 "   ← **超过阈值!**" if (d is not None and d > STEP_MAX + 1e-6) else ""))
    print()
    print("  完整路径: %s%s"
          % (path[:args.max], " ..." if len(path) > args.max else ""))
    b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
