# -*- coding: utf-8 -*-
"""**一条命令验多平台关卡** —— 判 B 方案(按厨师高度判地板)在实机上对不对。

背景(交接包 2026-09-14 §1①): `ReadChefFloorY` 原来取 `objs[0]` 的**当前 y**
当整张图的参考地面, 于是
  ① 结果随厨师移动而变(同一关两次读出 -1.84 / 0.00);
  ② 只覆盖厨师所在那一层 —— **另一层平台全判成空洞 'V'**,
     厨师站在自己平台上却被判成"站在空洞上", 可达格数 = 1, 一步都走不了。
B 方案把地面高度做成**每格一个值**(C# 的 `floors` 数组), 判定搬到 Python,
由引擎把"这只厨师现在的 y"传下去 (`at_y`)。

这个工具就是把上述症状量化: 同一只厨师, 给不给 `at_y` 各算一次可达格数。

用法:
  python -u tools/platformcheck.py                 # 查 cid=0
  python -u tools/platformcheck.py --cid 1
  python -u tools/platformcheck.py --all           # 每只厨师都查
  python -u tools/platformcheck.py --ascii         # 额外画可达图
  python -u tools/platformcheck.py --wait 120      # 等进对局(留给开局)

判据(全部自动化, 最后给一行总结):
  [1] dll 是新的      floors 数组长度 == w*h 且有非空值
  [2] 脚下不是空洞    厨师站的格子 != 'V'          ← 之前是 'V'
  [3] 脚下这格站得住   高度判定走 `terrain.height_ok`: **落差 ±2 格以内算走得了**
                      (游戏自己的 StepHeightMax=0.65 只是"跨步吸附"的分界,
                       不是"过不过得去"; 拿它当墙会把正常的两层平台整层判死)
  [4] 可达格数合理    带 at_y 的可达数 明显 > 1    ← 之前是 1
  [5] 各层能分开      BFS 不会顺着"另一层"爬到别的高度去
  [6] 无误伤          at_y 压掉的每一格都是跨层的(同层被压掉 = 假阴性 = 真 bug)

⚠ [6] 别写成"带 at_y 的格数 ≥ 不带": 不给 at_y 时高度判定被整个跳过, 格数必然更多,
  B 方案就是要把它压下去 —— 那是个**方向写反**的判据, 第一版实机当场假 FAIL。

⚠ 别再拿 `'v'` 当判据: `FloorChar`(LevelInfo.cs:713) 现在只返回 `'V'` / `'\0'`,
   `'v'`(低地板/下沉平台) 由 `walkable(at_y=)` 的落差判定**取代**了 —— 全仓已无人产生
   这个字符, 所以"脚下不是 'v'"是空判据, 永远通过。顺着这个看还会发现
   `counts["voidLow"]` 与 `describe_dangers()` 里那条警告也永远不会触发(死诊断)。
"""
import argparse
import os
import sys
import time
from collections import Counter

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from bridge.client import BridgeClient                          # noqa: E402
from engine import Engine                                       # noqa: E402
from terrain import (HEIGHT_TOLERANCE, CH_VOID, CH_MARK,      # noqa: E402
                     height_ok)


def _reach_ascii(tm, cx, cz, at_y):
    """按 `at_y` 画出"从 (cx,cz) 真正到得了的区域"(terrain.ascii_reach 不带 at_y)。"""
    reach = tm.reachable_from(cx, cz, at_y=at_y)
    mark = tm.cell_of(cx, cz)
    rows = []
    for j in range(tm.h - 1, -1, -1):
        line = []
        for i in range(tm.w):
            if mark == (i, j):
                line.append(CH_MARK)
            elif (i, j) in reach:
                line.append(tm.at(i, j))
            else:
                line.append(" ")       # 到不了 -> 留白
        rows.append("".join(line))
    return "\n".join(rows)


def _floor_levels(tm):
    """每格地板高度的直方图 —— 一眼看出这一关有几层平台。"""
    c = Counter()
    for n in range(tm.w * tm.h):
        fy = tm.cell_floor_y(n % tm.w, n // tm.w)
        if fy is not None:
            c[round(float(fy), 2)] += 1
    return c


def check_chef(tm, chef, show_ascii=False):
    """查一只厨师, 返回 (是否全过, 判据清单)。"""
    cid = int(chef.get("id", -1))
    x = float(chef.get("x", 0.0))
    z = float(chef.get("z", 0.0))
    y = float(chef.get("y") or 0.0)
    i, j = tm.cell_of(x, z)
    ch = tm.at(i, j)
    fy = tm.cell_floor_y(i, j)

    # 同一只厨师, 两种算法各算一遍 —— 差值就是 B 方案挣来的东西
    n_new = len(tm.reachable_from(x, z, at_y=y))
    n_old = len(tm.reachable_from(x, z))            # 不给 at_y = 老行为

    print("\n" + "=" * 68)
    print("厨师 cid=%d  世界(%.2f, %.2f) y=%.2f  →  格(%d,%d) 字符 %r"
          % (cid, x, z, y, i, j, ch))
    if fy is None:
        print("  这一格没有地板高度记录 → dll 可能还是旧的")
    else:
        print("  这格地板 y=%.2f   差 %.2f  (本关容差 ±%.2f: %s) → %s"
              % (fy, float(fy) - y, tm.htol, tm.htol_note,
                 "站得住" if height_ok(fy, y, tm.htol) else "太高/太低, 算墙"))

    checks = []
    checks.append(("脚下不是空洞 'V'", ch != CH_VOID,
                   "站的是 %r" % ch))
    # 这条才是 B 方案的**真判据**(旧的"脚下不是 'v'"是空判据 —— 见下方注释)。
    standable = fy is not None and height_ok(float(fy), y, tm.htol)
    checks.append(("脚下这格站得住", standable,
                   "地板 %.2f vs 厨师 %.2f, 差 %.2f"
                   % (fy, y, abs(float(fy) - y)) if fy is not None
                   else "这格没报高度(dll 旧?)"))
    checks.append(("可达格数 > 1", n_new > 1,
                   "带 at_y 可达 %d 格" % n_new))
    # 判据 [5]: `at_y` 排除掉的格子, 必须**都是跨层的**(地板高度和自己差过阈值)。
    #
    # ⚠ 不要再写成 "带 at_y 的格数 >= 不带"(第一版就是这么写的, 实机当场报 FAIL):
    #   不给 at_y 时高度判定整个被跳过, 什么格子都算能走 —— 格数**必然更多**。
    #   B 方案的意义恰恰是把这个数**压下去**, 压掉的正是"另一层平台上、厨师根本
    #   走不过去"的格子。所以"变少"是预期行为, 不是退化。
    #   真正该问的是: 压掉的那些, 是不是每一格都真的跨了层? 有一格同层却被压掉,
    #   那才是假阴性(真 bug)。
    dropped = tm.reachable_from(x, z) - tm.reachable_from(x, z, at_y=y)
    wrong = []
    for (di_, dj_) in sorted(dropped):
        dfy = tm.cell_floor_y(di_, dj_)
        if dfy is None or height_ok(float(dfy), y, tm.htol):
            wrong.append((di_, dj_, dfy))
    detail = "at_y 压掉 %d 格(不带时 %d → 带时 %d)" % (len(dropped), n_old, n_new)
    if wrong:
        detail += "; ⚠ 其中 %d 格是同层却被压掉(假阴性): %s" % (
            len(wrong), wrong[:4])
    else:
        detail += ", 全部跨层 → 无假阴性"
    checks.append(("at_y 压掉的格子都跨层", not wrong, detail))

    # 各层是否真分开: 可达集里不该出现"和厨师脚下差超过跨步上限"的格子。
    # 这是 BFS 顺着另一层爬过去的直接证据(等价于"跨层走不通"那条离线用例)。
    reach = tm.reachable_from(x, z, at_y=y)
    off = 0
    for (ri, rj) in reach:
        rfy = tm.cell_floor_y(ri, rj)
        if rfy is not None and not height_ok(float(rfy), y, tm.htol):
            off += 1
    checks.append(("可达集不跨层", off == 0,
                   "可达集里跨层的格子 %d 个" % off))

    print()
    for name, ok, detail in checks:
        print("   %s %-22s %s" % ("✓" if ok else "✗", name, detail))

    if show_ascii:
        print("\n  可达区域(留白 = 到不了, 我 = 厨师):")
        for ln in _reach_ascii(tm, x, z, y).splitlines():
            print("    " + ln)

    return all(ok for _, ok, _ in checks), checks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cid", type=int, default=0)
    ap.add_argument("--all", action="store_true", help="查所有厨师")
    ap.add_argument("--ascii", action="store_true", help="画可达图")
    ap.add_argument("--wait", type=float, default=0.0, help="最多等几秒进对局")
    args = ap.parse_args()

    b = BridgeClient()
    b.connect(retries=6, interval=1.0)
    eng = Engine(b, cid=args.cid, log=lambda *a: print(*a, flush=True))

    try:
        t0 = time.time()
        st = None
        while True:
            st = eng.state(force=True)
            if st and st.get("inRound") and ((st.get("layout") or {}).get("chefs")):
                break
            if time.time() - t0 > args.wait:
                print("[多平台] 不在对局里(或读不到厨师)。先打开关卡再跑。")
                return 1
            time.sleep(1.0)

        tm = eng.terrain(force=True)
        if tm is None or not tm.ok:
            print("[多平台] 地形读不到 —— 检查 dll 是否部署、桥是否通。")
            return 1

        print("[多平台] %dx%d 格  步长(%.2f,%.2f)  场景 %s"
              % (tm.w, tm.h, tm.cellx, tm.cellz, st.get("scene", "")))
        print("[多平台] C# 报的整图参考高度 floorY = %.2f" % tm.floor_y)

        # 判据 [1]: floors 数组存不存在 —— 这是"dll 换没换"最直接的证据
        n_floors = sum(1 for n in range(tm.w * tm.h)
                       if tm.cell_floor_y(n % tm.w, n // tm.w) is not None)
        dll_ok = n_floors > 0
        print("[多平台] floors 数组: %d 项有高度 (期望 %d) → %s"
              % (n_floors, tm.w * tm.h,
                 "新 dll ✓" if dll_ok else "**老 dll ✗ —— 多平台逻辑根本没跑上**"))

        levels = _floor_levels(tm)
        print("[多平台] 地板标高分布: %s"
              % ", ".join("%.2f×%d" % (h, n) for h, n in sorted(levels.items())))
        if len(levels) > 1:
            print("         → 这一关有 %d 个高度层, 正是要验的多平台关卡" % len(levels))
        else:
            print("         → 只有 1 个高度层; 换 s_wizard_school_3_4 这类两层关卡才验得动")

        print("[多平台] " + tm.describe_dangers())

        chefs = (st.get("layout") or {}).get("chefs") or []
        if not args.all:
            chefs = [c for c in chefs if int(c.get("id", -1)) == args.cid]
        if not chefs:
            print("[多平台] 没找到厨师 cid=%d" % args.cid)
            return 1

        results = [check_chef(tm, c, args.ascii) for c in chefs]

        print("\n" + "=" * 68)
        if not dll_ok:
            print("总结: ✗ 部署的 dll 里没有 floors —— 先确认 build/Overcooked2AI.dll 已拷进 plugins")
            return 1
        bad = [name for ok, cs in results if not ok for name, o, _ in cs if not o]
        if bad:
            print("总结: ✗ 有判据没过: " + "; ".join(bad))
            return 2
        print("总结: ✓ 多平台判定正常(%d 只厨师全过)" % len(results))
        return 0
    finally:
        try:
            eng.kb.release_all()
        except Exception:
            pass
        b.close()


if __name__ == "__main__":
    sys.exit(main())
