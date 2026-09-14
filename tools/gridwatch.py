# -*- coding: utf-8 -*-
"""**只盯一件事: 地形网格到底变不变。** —— 用来判定移动/限时平台、传送门这类
"动态关卡构件"有没有被建模进去。

为什么要单独一个工具(用户实测指出): 楼梯和传送门是**限时轮换**的, 可是
`mapview --md --watch` 那期间**一条变化切片都没输出**。但那次监视器其实压根
没启动(卡在"等进对局"), 所以"没输出"证明不了任何事。

于是这里把判据**收窄到唯一一个**:
    每隔 N 秒强制重取一次地形, **只比网格字符串**。
  · 网格变了 → 打印哪些格、从什么字符变成什么字符
  · 一直不变 → 说明地形建模**根本看不见**这种变化(而不是"没抓到")

为什么值得单独查: `LevelInfo` 里**凡是有占用物的格子, 字符直接取占用表,
完全不看真实几何**(见 `LevelInfo.cs` 第二遍循环: `if (occ[n] != '\0') ch = occ[n];`)。
而占用表是游戏在关卡加载时登记的、之后不随物体移动而变 ——
⇒ 会移动/会消失的平台, 它的格子**永远**报同一套字符。

用法:
  python -u tools/gridwatch.py              # 每 2 秒, 不强制(用 C# 5 秒缓存)
  python -u tools/gridwatch.py --force      # 每轮强制重建(更准, 更重)
  python -u tools/gridwatch.py --iv 1
  python -u tools/gridwatch.py --wait 300   # 最多等 300 秒进对局

说明: **心跳**每 30 秒打一次, 所以"没输出"和"没在跑"永远能区分开 ——
      上一次就是因为分不清这两者, 白丢了一局观察。
"""
import argparse
import os
import sys
import time
from collections import Counter

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from bridge.client import BridgeClient      # noqa: E402
from terrain import TerrainMap              # noqa: E402


def fetch(b, force):
    """取一帧地形。返回 TerrainMap 或 None(取不到时**大声说**, 不装作无事)。"""
    try:
        data = b.get_map(force=force)
    except Exception as e:
        print("  !! 取图失败: %r" % (e,), flush=True)
        return None
    if not isinstance(data, dict) or data.get("error"):
        print("  !! 取图报错: %s" % (data,), flush=True)
        return None
    tm = TerrainMap(data)
    if not tm.ok:
        print("  !! 网格不完整: %dx%d grid=%d" % (tm.w, tm.h, len(tm.grid)), flush=True)
        return None
    return tm


def floors_diff(old, new):
    """只比**每格地板高度**(不比字符), 返回 [(i, j, 旧高度, 新高度)]。

    ⚠ 为什么必须单独有这个: 平台的"上/下"是**纯垂直**变化 ——
      格子字符一直是 `'.'`(站得上), 变的是高度。只比字符网格的话这类变化
      **一声不响**, 会被误读成"关卡是静态的/模型看不见平台"。
      实测踩过: 190 秒跨 3 个切换周期 0 输出, 差点据此下错结论。
    """
    of, nf = (old.floors or []), (new.floors or [])
    if (old.w, old.h) != (new.w, new.h):
        return []
    out = []
    for j in range(old.h):
        for i in range(old.w):
            n = j * old.w + i
            a = of[n] if n < len(of) else None
            b = nf[n] if n < len(nf) else None
            ra = None if a is None else round(float(a), 2)
            rb = None if b is None else round(float(b), 2)
            if ra != rb:
                out.append((i, j, ra, rb))
    return out


def show_floors_diff(old, new, fd):
    """报"网格没变、只有高度变了" —— 平台升降的签名。"""
    ups = sum(1 for _, _, a, b in fd if a is not None and b is not None and b > a)
    downs = sum(1 for _, _, a, b in fd if a is not None and b is not None and b < a)
    print("  ★⚠ **地板高度变了 %d 格(网格字符没变)** —— 上移 %d / 下移 %d:"
          % (len(fd), ups, downs), flush=True)
    for i, j, a, b in fd[:12]:
        print("      格(%2d,%2d)  %s → %s"
              % (i, j, "—" if a is None else "%.2f" % a,
                 "—" if b is None else "%.2f" % b), flush=True)
    if len(fd) > 12:
        print("      ... 另有 %d 格" % (len(fd) - 12), flush=True)
    chg = set((i, j) for i, j, _, _ in fd)
    print("  高度变化位置图('#'=这一格高度变了):", flush=True)
    for j in range(old.h - 1, -1, -1):
        print("    " + "".join("#" if (i, j) in chg else "."
                               for i in range(old.w)), flush=True)


def show_diff(old, new):
    """打印逐格变化, 按 `旧→新` 归类 —— 一眼看出是"平台没了"还是"平台挪了"。"""
    n = min(len(old.grid), len(new.grid))
    changes = [(i, old.grid[i], new.grid[i]) for i in range(n) if old.grid[i] != new.grid[i]]
    print("  ★ 地形变了 %d 格" % len(changes), flush=True)
    kinds = Counter("%s→%s" % (a, c) for _, a, c in changes)
    for k, v in kinds.most_common():
        print("      %-8s %d 格" % (k, v), flush=True)
    # 逐格坐标只给几条(下面那张位置图信息量大得多, 不必在这里刷屏)
    for i, a, c in changes[:8]:
        print("      格(%2d,%2d)  %r → %r" % (i % old.w, i // old.w, a, c), flush=True)
    if len(changes) > 8:
        print("      ... 还有 %d 格(看下面的位置图)" % (len(changes) - 8), flush=True)
    # ---- 变化位置图: **这张最能说明问题** ----
    #   变化聚成一块   → 真有个会动的大构件(移动平台/转盘/限时楼梯)
    #   变化散在全图   → 多半是坐标系或映射在抖, 不是几何在动
    #   成对对称(A→B 与 B→A 数量相等) → 整块内容在"搬家", 通常是坐标映射换了参照
    if old.w and old.h:
        chg = set(i for i, _, _ in changes)
        print("  变化位置图('#'=这一格变了):", flush=True)
        for j in range(old.h - 1, -1, -1):
            print("    " + "".join(
                "#" if (j * old.w + i) in chg else "."
                for i in range(old.w)), flush=True)
    if abs(old.floor_y - new.floor_y) > 1e-6:
        print("  ★ floorY 变了: %.2f → %.2f  (← 全局量, 随厨师上下平台而变)"
              % (old.floor_y, new.floor_y), flush=True)
    # ⚠ **坐标系有没有动** —— 这一条不能漏:
    #   网格大面积变化有两种完全不同的原因, 而它们的处置方式相反:
    #     · 关卡几何真在动   → 移动平台/限时构件, 需要动态建模
    #     · 原点/步长变了     → **是我们在用错参照系看同一张图**,
    #       即"整块内容搬家、成对对称、数量精确相等"那种特征
    #   漏检这一条, 就会把后者误判成前者。
    # 活跃网格集合变了 → "主网格"可能换人 → 参照系整体换 ⇒ 同样表现为"大面积搬家"
    if getattr(old, "grids", 0) != getattr(new, "grids", 0):
        print("  ★⚠ 活跃网格数变了: %s → %s  ← 主网格/参照系可能换人!"
              % (getattr(old, "grids", 0), getattr(new, "grids", 0)), flush=True)
    for f, label in (("ox", "原点 x"), ("oz", "原点 z"),
                     ("cellx", "步长 x"), ("cellz", "步长 z")):
        a2, b2 = getattr(old, f, None), getattr(new, f, None)
        if a2 is None or b2 is None:
            continue
        if abs(float(a2) - float(b2)) > 1e-4:
            print("  ★⚠ %s 变了: %s → %s  ← 参照系动了, 不是几何在动!"
                  % (label, a2, b2), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iv", type=float, default=2.0, help="轮询间隔秒")
    ap.add_argument("--force", action="store_true", help="每轮强制重建地形")
    ap.add_argument("--wait", type=float, default=0.0, help="最多等几秒进对局")
    ap.add_argument("--heartbeat", type=float, default=30.0, help="心跳间隔秒")
    args = ap.parse_args()

    b = BridgeClient()
    b.connect(retries=999, interval=2.0)

    t0 = time.time()
    tm = None
    scene0 = "?"
    # ⚠ 等待阶段**必须吭声**: 静默等待和"挂了/没在跑"分不出来 ——
    #   这个坑踩过一次(包装器静默轮询, 白丢一局观察)。
    print("等待进入对局(需要有厨师)... Ctrl+C 停", flush=True)
    t_wait = time.time()
    while tm is None:
        st = b.get_state()
        if st and st.get("inRound") and ((st.get("layout") or {}).get("chefs")):
            tm = fetch(b, args.force)
            if tm is None:
                return 1
            scene0 = st.get("scene") or "?"
            break
        if time.time() - t_wait > 15.0:
            t_wait = time.time()
            print("  [等待中] %s  scene=%r inRound=%s"
                  % (time.strftime("%H:%M:%S"), (st or {}).get("scene"),
                     (st or {}).get("inRound")), flush=True)
        if args.wait and time.time() - t0 > args.wait:
            print("等超时: 一直没进对局")
            b.close()
            return 1
        time.sleep(1.0)

    print("\n===== 开始盯地形 =====", flush=True)
    print("  场景 %s  %dx%d  步长(%.2f,%.2f)  floorY=%.2f  floorRef=%s  活跃网格数=%d"
          % (scene0, tm.w, tm.h, tm.cellx, tm.cellz, tm.floor_y, tm.regular,
             getattr(tm, "grids", 0)), flush=True)
    print("  %s" % tm.describe_dangers(), flush=True)
    print("  每 %.1fs 取一帧%s; 心跳每 %.0fs 一次(没输出 = 地形真的没变)"
          % (args.iv, "(强制重建)" if args.force else "(用 5 秒缓存)", args.heartbeat),
          flush=True)
    print("  场景变 / 网格变 都会报. Ctrl+C 停.\n", flush=True)

    prev = tm
    n = 0
    t_last = time.time()
    while True:
        time.sleep(args.iv)
        n += 1
        st = b.get_state()
        if not st or not st.get("inRound"):
            print("\n  ---- 离开对局, 停止 ----", flush=True)
            return 0
        cur = fetch(b, args.force)
        if cur is None:
            time.sleep(2.0)
            continue
        if (cur.w, cur.h) != (prev.w, prev.h):
            print("\n  ★ 网格尺寸变了: %dx%d → %dx%d (换关/重采样)"
                  % (prev.w, prev.h, cur.w, cur.h), flush=True)
        elif cur.grid != prev.grid:
            print("\n[%s] 第 %d 次轮询" % (time.strftime("%H:%M:%S"), n), flush=True)
            show_diff(prev, cur)
        elif floors_diff(prev, cur):
            print("\n[%s] 第 %d 次轮询" % (time.strftime("%H:%M:%S"), n), flush=True)
            show_floors_diff(prev, cur, floors_diff(prev, cur))
        elif abs(cur.floor_y - prev.floor_y) > 1e-6:
            print("\n  ★ 只有 floorY 变了: %.2f → %.2f (网格没动)"
                  % (prev.floor_y, cur.floor_y), flush=True)
        prev = cur
        if time.time() - t_last >= args.heartbeat:
            t_last = time.time()
            print("  [心跳] %s  已轮询 %d 次  地形未变 (floorY=%.2f)"
                  % (time.strftime("%H:%M:%S"), n, cur.floor_y), flush=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
