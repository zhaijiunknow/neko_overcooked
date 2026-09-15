# -*- coding: utf-8 -*-
"""**试一条路** —— 从厨师(或指定起点)走到 (x,z), 把 A* 的路径画在地形图上。

为什么要有这个工具: 用户点名过"**寻路还是有很大的问题**", 而引擎的日志只有
"这个路径点到不了 / 地形 A* 无解" —— 它**说不出为什么**(地形本来就不通?
目标不在可达集里? 动态禁行把路堵了? 拉直拉出一条走不了的线?)。
这个工具把"我们规划出来的那条线"画出来, 顺带回答三个具体问题:
  · 目标格**在不在**从起点出发的可达集里(不在 ⇒ 是连通性问题, 不是 A* 的问题)
  · 路径有没有**穿过**走不了/危险的格子(每段直线都是密集采样逐格验的)
  · 拉直前后差多少(路点数 / 总长 / 最长一段) —— 见 `--compare`

用法:
  python -u tools\\pathtest.py 12.0 6.0                 # 从厨师#0 走到 (12.0, 6.0)
  python -u tools\\pathtest.py 12.0 6.0 --chef 1        # 指定厨师
  python -u tools\\pathtest.py 12.0 6.0 --from 3.6 2.4  # 指定起点(不用厨师)
  python -u tools\\pathtest.py 12.0 6.0 --compare       # 4向/不拉直 vs 对角/拉直 并排比
  python -u tools\\pathtest.py 12.0 6.0 --raw           # 顺带问游戏原生 GridNavSpace
  python -u tools\\pathtest.py 12.0 6.0 --all           # = --compare --raw

⚠ **只读**: 不按键、不移动、不装手柄 —— 引擎正在跑的时候也能随时开。
⚠ 连不上桥就**直接报错退出**, 不要写"反复重连"的包装器: 桥的 `AcceptLoop`
  一出异常就 `break`, 之后整个会话都不再接新连接(见 `neko/bridge/client.py`)。
"""
from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from bridge.client import BridgeClient          # noqa: E402
from map_model import KitchenMap                # noqa: E402
from terrain import TerrainMap                  # noqa: E402

#: 画线时沿每一段直线**密集采样**的步长(格)。取 0.2 是为了"两个路点之间跨了
#: 十几格"时也不会漏掉中间穿过的墙 —— 拉直之后这是必须的(见 `scan_path`)。
SAMPLE = 0.2

#: 两种规划模式: (名字, find_path 的参数)。`--compare` 会把它们都跑一遍。
MODES = (
    ("对角+拉直(现在)",  {}),
    ("4向+不拉直(旧)",   {"diagonal": False, "smooth": False}),
)


def cells_along(sx: float, sz: float, path: list) -> list:
    """路径**实际会踩过的**格子序列(起点 + 每一段密集采样)。

    不能只看路点: 拉直之后相邻两个路点可能隔着十几格, 只标路点会画出一条
    "穿墙的直线"却看不出来 —— 而这正是要验的东西。
    """
    out = [(sx, sz)]
    for (px, pz) in path or []:
        x0, z0 = out[-1]
        seg = ((px - x0) ** 2 + (pz - z0) ** 2) ** 0.5
        n = max(1, int(seg / SAMPLE))
        for k in range(1, n + 1):
            t = k / float(n)
            out.append((x0 + (px - x0) * t, z0 + (pz - z0) * t))
    return out


def draw(tm: TerrainMap, sx: float, sz: float, tx: float, tz: float,
         path: list, marks: dict = None) -> str:
    """地形图 + 路径(`*`)、起点(`@`)、目标(`X`)。

    `X` 标的是**目标那一格**; 目标通常是台子(障碍格), 厨师其实站在它旁边,
    所以看到 `*` 停在 `X` 隔壁是正常的 —— 引擎最后一步会自己找相邻可站格。
    """
    grid = {}
    for (x, z) in cells_along(sx, sz, path):
        grid[tm.cell_of(x, z)] = "*"
    grid[tm.cell_of(sx, sz)] = "@"
    grid.setdefault(tm.cell_of(tx, tz), "X")
    for c, ch in (marks or {}).items():
        grid[c] = ch
    rows = []
    for j in range(tm.h - 1, -1, -1):
        rows.append("".join(grid.get((i, j)) or tm.at(i, j) for i in range(tm.w)))
    return "\n".join(rows)


def scan_path(tm: TerrainMap, sx: float, sz: float, path: list) -> list:
    """沿路径逐格查"这一步能不能站", 返回问题清单 `[(格, 字符, 原因)]`。

    判据只用**地形自己**的: `walkable`(能不能站) 和 `is_danger`(会不会死)。
    它回答不了"路上有没有车"(那是 `KitchenMap.blocked_by_movers` 的动态禁行),
    所以工具层面只报地形问题 —— 引擎规划时还会带一层动态禁行。
    """
    bad = []
    seen = set()
    for (x, z) in cells_along(sx, sz, path):
        c = tm.cell_of(x, z)
        if c in seen:
            continue
        seen.add(c)
        if not tm.walkable(*c):
            bad.append((c, tm.at(*c), "站不下(墙/占用)"))
        elif tm.is_danger(*c):
            bad.append((c, tm.at(*c), "危险格(水/空洞/火)"))
    return bad


def describe(tm: TerrainMap, sx: float, sz: float, path: list) -> str:
    """路点数 / 总长 / 最长一段 —— 拉直的效果就看这三个数。"""
    if not path:
        return "规划不出来(空)"
    total = 0.0
    longest = 0.0
    x0, z0 = sx, sz
    for (px, pz) in path:
        d = ((px - x0) ** 2 + (pz - z0) ** 2) ** 0.5
        total += d
        longest = max(longest, d)
        x0, z0 = px, pz
    return ("路点 %d 个 | 总长 %.1f 格 | 最长一段 %.1f 格"
            % (len(path), total / max(1e-9, tm.cellx), longest / max(1e-9, tm.cellx)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tx", type=float, help="目标世界坐标 x")
    ap.add_argument("tz", type=float, help="目标世界坐标 z")
    ap.add_argument("--chef", type=int, default=0, help="用哪只厨师的当前位置当起点(默认 0)")
    ap.add_argument("--from", dest="src", type=float, nargs=2, default=None,
                    metavar=("X", "Z"), help="直接指定起点, 不用厨师")
    ap.add_argument("--compare", action="store_true", help="旧(4向)与新(对角+拉直)并排比")
    ap.add_argument("--raw", action="store_true", help="顺带问游戏原生 GridNavSpace 怎么走")
    ap.add_argument("--all", action="store_true", help="= --compare --raw")
    args = ap.parse_args()
    if args.all:
        args.compare = args.raw = True

    br = BridgeClient()
    try:
        br.connect(retries=3, interval=1.0)
    except Exception as e:                                    # noqa: BLE001
        print("连不上桥(游戏没开?): %s" % e)
        return 1
    try:
        st = br.get_state() or {}
        if not st.get("inRound"):
            print("[路径] 不在对局里(scene=%s) —— 地形是整局建的, 没有对局就没有图"
                  % (st.get("scene") or "?"))
            return 1

        at_y = 0.0
        if args.src is not None:
            sx, sz = float(args.src[0]), float(args.src[1])
            src_desc = "指定起点"
        else:
            km = KitchenMap.from_layout(st.get("layout") or {})
            chef = next((c for c in (km.chefs or []) if int(getattr(c, "id", -1)) == args.chef),
                        None)
            if chef is None:
                print("没有厨师#%d(场上 %d 只)" % (args.chef, len(km.chefs or [])))
                return 1
            sx, sz, held = chef.x, chef.z, getattr(chef, "held", "")
            src_desc = "厨师#%d%s" % (args.chef, (" 手持 " + held) if held else " 空手")
            for c in ((st.get("layout") or {}).get("chefs") or []):
                if int(c.get("id", -1)) == args.chef:
                    at_y = float(c.get("y") or 0.0)

        tm = TerrainMap(br.get_map(force=True))
        if not tm.ok:
            print("地图错误: %s" % tm.error)
            return 1

        print("场景 %s | 网格 %dx%d 步长(%.2f,%.2f) 原点(%.2f,%.2f)"
              % (st.get("scene") or "?", tm.w, tm.h, tm.cellx, tm.cellz, tm.ox, tm.oz))
        print("%s (%.2f,%.2f) → 目标 (%.2f,%.2f) | 厨师 y=%.2f"
              % (src_desc, sx, sz, args.tx, args.tz, at_y))

        # ---- 先回答"到不了是不是连通性问题" ----
        reach = tm.reachable_from(sx, sz, at_y=at_y)
        walk = sum(1 for j in range(tm.h) for i in range(tm.w) if tm.walkable(i, j))
        goal_cell = tm.cell_of(args.tx, args.tz)
        in_reach = goal_cell in reach
        print("可达 %d / 全图可走 %d | 目标格 %s %s"
              % (len(reach), walk, goal_cell,
                 "在可达集里 ✓" if in_reach else "**不在可达集里** ✗ "
                 "(走到它附近也没用 —— 这是连通性/地图问题, 不是 A* 的问题)"))

        modes = MODES if args.compare else MODES[:1]
        for name, kw in modes:
            print("\n---- %s ----" % name)
            path = tm.find_path(sx, sz, args.tx, args.tz, at_y=at_y, **kw)
            print(describe(tm, sx, sz, path))
            bad = scan_path(tm, sx, sz, path)
            if bad:
                head = ", ".join("(%d,%d)=%r %s" % (c[0], c[1], ch, why)
                                 for c, ch, why in bad[:5])
                print("⚠ 路径上有 %d 格走不了(前 5 个): %s" % (len(bad), head))
            else:
                print("✓ 路径整段都在能站的格上(逐格采样的)")
            print(draw(tm, sx, sz, args.tx, args.tz, path))

        if args.raw:
            print("\n---- 游戏原生 GridNavSpace ----")
            try:
                raw = br.get_path(args.tx, args.tz, chef=args.chef)
            except Exception as e:                            # noqa: BLE001
                print("原生寻路失败: %s" % e)
                raw = {}
            pts = raw.get("path") or raw.get("points") or []
            if not pts:
                print("游戏说没有路(空) —— 它的网格只认边界/橱柜/墙, 不认水面空洞")
            else:
                pts = [((p.get("x"), p.get("z")) if isinstance(p, dict) else p) for p in pts]
                print("原生 %d 个点: %s" % (len(pts),
                      ", ".join("(%.1f,%.1f)" % (float(a), float(b)) for a, b in pts[:12])
                      + (" ..." if len(pts) > 12 else "")))
                # ⚠ 原生路径**可能横穿水面**(水是 RespawnCollider 触发器, 不占格子),
                #   所以它只能当"参考路线", 不能直接拿去走 —— 这里把危险格数报出来。
                rbad = scan_path(tm, sx, sz, [(float(a), float(b)) for a, b in pts])
                print("⚠ 按我们的地形, 这条原生路径上有 %d 格走不了/危险" % len(rbad)
                      if rbad else "✓ 原生路径在地形上也干净")
                print(draw(tm, sx, sz, args.tx, args.tz,
                           [(float(a), float(b)) for a, b in pts]))
    finally:
        try:
            br.close()
        except Exception:                                     # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
