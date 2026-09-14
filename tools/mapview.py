# -*- coding: utf-8 -*-
"""看地图: 把游戏自己的关卡网格整张打出来, 危险区/空洞/平台一目了然。

用途:
  · 验证插件真的读到了网格 (而不是我们猜的障碍)
  · 确认水面/岩浆在哪 —— 这就是以前厨师掉水的原因
  · 开局前先看一眼这关有没有移动平台/传送带(脚本目前不该上的图)

用法:
  python -u tools/mapview.py            # 打当前关卡
  python -u tools/mapview.py --force    # 强制重取(插件的图有 5 秒缓存)
  python -u tools/mapview.py --at 5.4 3.2   # 标出某个坐标在哪一格
  python -u tools/mapview.py --watch        # 打完初始图后**持续监视**, 地图一变
                                            # 就往末尾追加一份"变化切片"(只打差异)
  python -u tools/mapview.py --watch 5      # 轮询间隔 5 秒(默认 2)
  python -u tools/mapview.py --md           # 顺带导出 Markdown 到桌面
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from neko.bridge.client import BridgeClient, BridgeError   # noqa: E402
from neko.terrain import (TerrainMap, HEIGHT_TOLERANCE,     # noqa: E402
                          CH_MARK)
from neko.map_model import (KitchenMap, is_extinguisher,    # noqa: E402
                            is_pot, is_plate, teleport_edges,
                            conveyor_arrows)

#: 叠加图里"会动的东西"和厨师手上那件东西的符号。
MV_DEADLY = "!"      # 撞上就死(RespawnCollider: 车/水)
PLATFORM_MARK = "&"  # **可开动的平台**(遥感驾驶的那种) —— 它不占格子, 地形图上完全看不见
TERM_LIVE = "*"      # 控制台, 而且**会话进行中**(此刻移动键驱动的是平台, 不是厨师)
MV_BLOCK = "%"       # 只是挡路(路人)
CH_HELD = "H"        # 厨师手上拿着东西(内容图)
UNKNOWN = "?"        # 认不出来的语义

SEM_LETTER = {
    "serve": "S",           # 送餐口
    "plates": "P",          # 干净盘子堆
    "dirty_plates": "D",    # 脏盘子堆
    "return_plates": "R",   # 盘子回收
    "board": "B",           # 切菜板
    "hob": "K", "oven": "K", "fryer": "K", "heat": "K",   # 灶台/加热
    "mix": "M",             # 搅拌
    "auto": "A",            # 自动工位
    "wash": "W",            # 洗手池
    "bin": "X",             # 垃圾桶
    "crate": "G",           # 食材箱/分发器
    "counter": "c",         # 普通台面
    "conveyor": "C",        # 台面传送带
    "switch": "U",          # 按钮
    "teleport": "O",        # 传送门
    "terminal": "N",        # 驾驶台
    "cannon": "Z",          # 大炮
    "pushable": "Q",        # 可推物体
    "cooking_region": "Y",  # 烹饪区域
    "hazard": "!",          # 危险物
}

SEM_NAME = {    "serve": "送餐口", "plates": "干净盘子堆", "dirty_plates": "脏盘子堆",
    "return_plates": "盘子回收", "board": "切菜板", "hob": "灶台/锅",
    "oven": "烤箱", "fryer": "炸锅", "heat": "加热台", "mix": "搅拌台",
    "auto": "自动工位", "wash": "洗手池", "bin": "垃圾桶", "crate": "食材箱",
    "counter": "普通台面", "conveyor": "台面传送带", "switch": "按钮",
    "teleport": "传送门", "terminal": "驾驶台", "cannon": "大炮",
    "pushable": "可推物体", "cooking_region": "烹饪区域", "hazard": "危险物",
}

# 台面清单的分组顺序(按做菜流程排, 不是字母序)
SEM_ORDER = ["crate", "board", "hob", "oven", "fryer", "heat", "mix", "auto",
             "plates", "dirty_plates", "return_plates", "serve", "wash", "bin",
             "counter", "conveyor", "switch", "teleport", "terminal", "cannon",
             "pushable", "cooking_region", "hazard"]


def _sem_of(s: dict) -> str:
    return KitchenMap.classify(s.get("name", ""), s.get("kind", ""),
                               s.get("sub", ""), s.get("spawn", ""))


def _snapshot(st: dict, dyn: dict, tm=None) -> dict:
    """把"会变的那部分"压成可比对的字典 —— 给 `--watch` 的 diff 用。

    只取**会变**的: 地形网格、台面集合/类别/台上东西/容器内容、厨师位置与手持、
    着火点、全场景食材。坐标取到 0.1(浮点抖动不算变化)。

    ⚠ **地形网格必须一起抓** —— 用户指出的: 原来只跟台面和厨师, 于是关卡变形 /
    荷叶沉浮 / 平台移动这些**只动地形不动台面**的变化, 切片里一格都看不到。
    """
    snap = {"stations": {}, "chefs": {}, "fires": [], "items": [], "terrain": None}
    if tm is not None and getattr(tm, "ok", False):
        # ⚠ **必须连原点和步长一起存**。只存 (宽,高,grid) 的话, 一旦游戏重采样
        #   (原点/步长变了但 w/h 没变), 逐格对比会把**整幅图**报成"变了 N 格" ——
        #   实测在 s_mine_2_5 上就误报了一次(19 格全落在同一行 j=13, 横跨整幅图,
        #   那是整行错位的样子, 不是局部地形变化)。
        # ⚠ **必须连每格的地板高度一起存**。只比字符网格的话,
        #   "上平台 / 下平台"这种**纯垂直**变化在监视里一声不响 ——
        #   格子还是 '.'(能站), 可它低了一格多, 对厨师来说已经不可走
        #   (`TerrainMap.walkable(at_y=...)` 用的正是这个高度)。
        #   实测踩过: 190 秒跨 3 个切换周期、0 切片, 差点据此断定"模型看不见
        #   限时平台" —— 其实是**监视器自己看不见高度**。
        #   高度取到 0.01, 避免浮点抖动被当成变化。
        _fl = tuple(None if f is None else round(float(f), 2)
                    for f in (tm.floors or []))
        # ⚠ **floorY 也要跟** —— 它是那颗"源头是玩家位置"的地雷
        #   (`LevelInfo.ReadChefFloorY`: 所有厨师 y 的**最大值**)。
        #   厨师一换层它就跳, 而它喂着**射线起点窗口**和**危险区同层判定**。
        #   不跟它就**看不见雷什么时候引爆**(实测踩过: P1 从 y=0.0 上到 2.0,
        #   而输出里 floorY 那项恒为"没变" —— 因为快照里根本没这个字段)。
        snap["terrain"] = (tm.w, tm.h, round(tm.ox, 2), round(tm.oz, 2),
                           round(tm.cellx, 3), round(tm.cellz, 3),
                           tm.grid or "", _fl, round(float(tm.floor_y or 0), 2))
    # 全场景食材(游戏按 tag 找的) —— 也监视, 顺便验证插件的 ScanItems 真的在工作
    for it in ((st or {}).get("layout") or {}).get("items") or []:
        snap["items"].append((it.get("name", ""),
                              round(float(it.get("x") or 0), 1),
                              round(float(it.get("z") or 0), 1)))
    snap["items"].sort()
    # 会动的东西(路人/车): 位置也跟 —— 它们动起来不该悄无声息
    snap["movers"] = []
    for m in ((st or {}).get("layout") or {}).get("movers") or []:
        snap["movers"].append((m.get("name", ""),
                               round(float(m.get("x") or 0), 1),
                               round(float(m.get("z") or 0), 1)))
    for s in ((st or {}).get("layout") or {}).get("stations") or []:
        snap["stations"][s.get("id")] = (
            round(float(s.get("x") or 0), 1), round(float(s.get("z") or 0), 1),
            _sem_of(s), tuple(s.get("on") or []), tuple(s.get("onhas") or []),
            s.get("spawn") or "")
    for c in ((st or {}).get("layout") or {}).get("chefs") or []:
        # ⚠ **y 必须一起存** —— 原来只有 (x, z, held), 于是**垂直移动完全不可见**:
        #   一次传送门换层 `(20.3,-4.3)→(14.4,-4.1)` 在输出里和"横向走一段"
        #   长得一模一样, 分不出来(用户连问两次"这个 y 是多少"才发现)。
        snap["chefs"][int(c.get("id", 0))] = (
            round(float(c.get("x") or 0), 1), round(float(c.get("z") or 0), 1),
            round(float(c.get("y") or 0), 2),
            c.get("held") or "")
    for f in (dyn or {}).get("fires") or []:
        snap["fires"].append((round(float(f.get("x") or 0), 1),
                              round(float(f.get("z") or 0), 1)))
    return snap


def _diff(prev: dict, cur: dict) -> list:
    """两次快照的差异, 人类可读。没变化返回空列表。"""
    out = []

    # ---- 地形网格(最先看, 因为它是别的变化的前提) ----
    # 关卡变形 / 荷叶沉浮 / 平台移动 —— 这些只动地形不动台面, 不单独跟就会漏掉。
    pt, ct = prev.get("terrain"), cur.get("terrain")
    if pt and ct and len(pt) == 9 and len(ct) == 9:
        pw, ph, pox, poz, pcx, pcz, pg, pf, pfy = pt
        cw, ch, cox, coz, ccx, ccz, cg, cf, cfy = ct
        # floorY 是**会动的全局量**(源头是所有厨师 y 的最大值) ——
        # 它一变, 射线窗口和危险区同层判定都可能跟着变。**必须单独报**。
        if abs(pfy - cfy) > 1e-6:
            out.append("  ★⚠ floorY 变了 %.2f → %.2f  ← 源头是**厨师位置** "
                       "(max所有厨师y); 射线窗口/危险区同层判定都吃它"
                       % (pfy, cfy))
        if (pw, ph) != (cw, ch):
            out.append("  ★ 网格尺寸变了 %dx%d → %dx%d" % (pw, ph, cw, ch))
        elif (pox, poz, pcx, pcz) != (cox, coz, ccx, ccz):
            # 重采样 ≠ 地形变化。逐格比会整幅图报错, 所以单独说清楚。
            out.append("  ★ 网格重采样了(原点/步长变了), **逐格对比无意义**: "
                       "原点 (%.2f,%.2f)→(%.2f,%.2f)  步长 (%.3f,%.3f)→(%.3f,%.3f)"
                       % (pox, poz, cox, coz, pcx, pcz, ccx, ccz))
        elif pg != cg:
            diff = []
            for j in range(ph):
                row = j * pw
                for i in range(pw):
                    a = pg[row + i] if row + i < len(pg) else "?"
                    b = cg[row + i] if row + i < len(cg) else "?"
                    if a != b:
                        diff.append((i, j, a, b))
            out.append("  ★ 地形变了 %d 格:" % len(diff))
            for i, j, a, b in diff[:24]:
                out.append("      (%2d,%2d)  %s → %s" % (i, j, a, b))
            if len(diff) > 24:
                out.append("      … 另有 %d 格" % (len(diff) - 24))
        else:
            # 字符网格没变, **但每格的地板高度可能变了** —— 平台上下移动就是这样:
            # 格子还是 '.' 站得上, 可它现在低了/高了一格多, 对厨师来说已经不可走。
            # 以前只比字符, 这类纯垂直变化**一声不响**, 会被误读成"关卡是静态的"。
            fd = []
            for j in range(ph):
                row = j * pw
                for i in range(pw):
                    n = row + i
                    a = pf[n] if n < len(pf) else None
                    b = cf[n] if n < len(cf) else None
                    if a != b:
                        fd.append((i, j, a, b))
            if fd:
                ups = sum(1 for _, _, a, b in fd
                          if a is not None and b is not None and b > a)
                downs = sum(1 for _, _, a, b in fd
                            if a is not None and b is not None and b < a)
                out.append("  ★⚠ **地板高度变了 %d 格(网格字符没变)** —— "
                           "上移 %d / 下移 %d:" % (len(fd), ups, downs))
                for i, j, a, b in fd[:12]:
                    out.append("      (%2d,%2d)  %s → %s"
                               % (i, j, "—" if a is None else "%.2f" % a,
                                  "—" if b is None else "%.2f" % b))
                if len(fd) > 12:
                    out.append("      … 另有 %d 格" % (len(fd) - 12))

    ps, cs = prev.get("stations", {}), cur.get("stations", {})
    added = [k for k in cs if k not in ps]
    removed = [k for k in ps if k not in cs]
    if added:
        out.append("  台面 +%d: %s" % (len(added), "  ".join(
            "%s@(%s,%s)" % (k, cs[k][0], cs[k][1]) for k in added[:6])))
    if removed:
        out.append("  台面 -%d: %s" % (len(removed), "  ".join(removed[:6])))
    for k in sorted(set(ps) & set(cs)):
        a, b = ps[k], cs[k]
        if a == b:
            continue
        bits = []
        if a[2] != b[2]:
            bits.append("类别 %s→%s" % (a[2], b[2]))
        if a[3] != b[3]:
            bits.append("台上 %s→%s" % (list(a[3]), list(b[3])))
        if a[4] != b[4]:
            bits.append("容器内 %s→%s" % (list(a[4]), list(b[4])))
        if bits:
            out.append("  %-20s (%s,%s)  %s" % (k, b[0], b[1], "  ".join(bits)))
    pc, cc = prev.get("chefs", {}), cur.get("chefs", {})
    for k in sorted(set(pc) & set(cc)):
        a, b = pc[k], cc[k]
        if a == b:
            continue
        bits = []
        if (a[0], a[1]) != (b[0], b[1]):
            bits.append("位置 (%s,%s)→(%s,%s)" % (a[0], a[1], b[0], b[1]))
        # **垂直方向的移动也要报** —— 否则一次传送门换层/掉下去
        # 和"横向走了一段"在输出里长得一样(用户指出过)。
        # |Δy| > 0.65 时额外标"换层": 那不是走路, 是传送/掉落/电梯。
        if a[2] != b[2]:
            cross = "  ★换层" if abs(float(a[2]) - float(b[2])) > 0.65 else ""
            bits.append("y %s→%s%s" % (a[2], b[2], cross))
        if a[3] != b[3]:
            bits.append("手持 %r→%r" % (a[3], b[3]))
        out.append("  P%d  %s" % (k + 1, "  ".join(bits) or "(变了)"))
    pf, cf = set(prev.get("fires", [])), set(cur.get("fires", []))
    for f in sorted(cf - pf):
        out.append("  ★ 新着火 @ (%s,%s)" % f)
    for f in sorted(pf - cf):
        out.append("  ✓ 火灭了 @ (%s,%s)" % f)
    # 会动的东西(路人/车) —— 位置变化就是"路上多了个东西"
    pm, cm = set(prev.get("movers", [])), set(cur.get("movers", []))
    for m in sorted(cm - pm):
        out.append("  ★ 出现 %-20s @ (%.1f,%.1f)" % m)
    for m in sorted(pm - cm):
        out.append("  ✓ 离开 %-20s @ (%.1f,%.1f)" % m)
    # 全场景食材(游戏按 tag 找的) —— 这条同时是 ScanItems 的活体检测:
    # 只要从箱子里取出过东西, 这里就该从 0 变成非 0。
    pi, ci = set(prev.get("items", [])), set(cur.get("items", []))
    for it in sorted(ci - pi):
        out.append("  + 场上食材 %-24s @ (%s,%s)" % it)
    for it in sorted(pi - ci):
        out.append("  - 场上食材 %-24s @ (%s,%s)" % it)
    return out


def _reach_now(tm, st):
    """当前可达集 —— **两只厨师取并集**, 含传送门边。

    返回 (可达集 | None, [(标记字符, 格)], 传送门边数)。
    在空中的厨师不参与(泛洪不知道人在空中, 算出来的"可达"是坑底)。
    """
    chefs = [c for c in ((st or {}).get("layout") or {}).get("chefs") or []
             if not c.get("respawning")]
    if tm is None or not getattr(tm, "ok", False) or not chefs:
        return None, [], 0
    try:
        ed = teleport_edges(KitchenMap.from_layout((st or {}).get("layout") or {}), tm)
    except Exception:
        ed = {}
    r = set()
    marks = []
    for c in chefs:
        cx, cz = float(c.get("x") or 0), float(c.get("z") or 0)
        r |= tm.reachable_from(cx, cz, extra_edges=ed)
        marks.append((str(int(c.get("id", 0) or 0) + 1), tm.cell_of(cx, cz)))
    return r, marks, len(ed)


def watch_loop(b, st0: dict, dyn0: dict, interval: float = 2.0,
               label: str = "可达变化") -> None:
    """轮询地图, **只在「可达集」变了的时候**追加一张**可达图**。

    为什么不跟着"任何变化"刷(用户要求: "只输出变化的可达图"):
      原来一变就出切片 —— 台面内容/厨师位置/手持/着火**都算变化**, 一局能刷出
      几十片, 而其中绝大多数跟"哪儿到得了"无关, 真正要看的东西被淹掉了。
      而这一关真正会变的本来就是**可达集**(两条转梯轮流当桥, 实测 75 ↔ 78),
      所以**触发条件就用可达集**, 输出也只要那一张图。

    没变化时打心跳 —— **没输出 = 可达真的没变**(不是监视器死了; 这个坑踩过)。
    Ctrl+C 停。
    """
    import time as _t
    try:
        tm0 = TerrainMap(b.get_map())
    except Exception:
        tm0 = None
    prev = _snapshot(st0, dyn0, tm0)
    prev_floor = list((tm0.floors if tm0 is not None else []) or [])
    prev_grid = ((tm0.grid if tm0 is not None else "") or "")
    prev_reach = None
    prev_marks = None
    prev_nedges = None
    n = 0
    polls = 0
    dyn_cells = set()        # 监视期间"变过"的格子(限时平台/升降台…)
    t_beat = _t.time()
    #: 没变化时每隔这么久打一次心跳。
    #: **为什么必须有**: 没有心跳时, "地图一直没变"和"监视器已经死了"在输出上
    #: **完全一样** —— 实测因此白丢过一局观察(用户: "没有看到切片输出",
    #: 事后才发现是监视器压根没跑起来)。有了心跳, 静默才等于"真的没变"。
    beat = 30.0
    print("\n===== %s: 开始监视 (每 %.1fs 轮询, Ctrl+C 停) =====" % (label, interval))
    print("  初始: 台面 %d 个, 厨师 %d 个, 着火 %d 处" % (
        len(prev["stations"]), len(prev["chefs"]), len(prev["fires"])))
    print("  初始: 场上食材 %d 件" % len(prev.get("items", [])))
    print("  没变化时每 %.0fs 打一次心跳 —— **没输出 = 地图真的没变**" % beat)
    _flush_md()      # 先把"初始图 + 监视头"落盘, 免得还没变就被杀
    while True:
        _t.sleep(interval)
        polls += 1
        try:
            st = b.get_state()
            if not st or not st.get("inRound"):
                print("\n---- %s #%d: 离开对局, 停止 ----" % (label, n + 1))
                return
            dyn = b.get_dyn()
            try:
                tm = TerrainMap(b.get_map())
            except Exception:
                tm = None
        except Exception as e:
            print("\n---- %s #%d: 读状态失败(%s), 停止 ----" % (label, n + 1, e))
            return
        cur = _snapshot(st, dyn, tm)
        # **累积"监视期间变过的格子"** —— 静态图画不出"这格有时在有时不在",
        # 只能标出来: 限时平台/升降台/轮换的地板**看上去就是常驻**, 那会骗人
        # (用户实测: "这一关是限时平台, 平台会轮换的, 但是图上在常驻")。
        _pt, _ct = prev.get("terrain"), cur.get("terrain")
        if _pt and _ct and len(_pt) == 9 and len(_ct) == 9 and _pt[:6] == _ct[:6]:
            _w = _pt[0]
            for _k in (6, 7):        # 6 = 字符网格, 7 = 每格地板高度
                _a, _b = _pt[_k], _ct[_k]
                for _n in range(min(len(_a), len(_b))):
                    if _a[_n] != _b[_n]:
                        dyn_cells.add((_n % _w, _n // _w))
        # ---- 触发条件 = **可达集变了** ----
        _r, _marks, _nedges = _reach_now(tm, st)
        if _r is not None and prev_reach is not None and _r == prev_reach:
            if _t.time() - t_beat >= beat:
                t_beat = _t.time()
                print("[心跳] %s  已轮询 %d 次, 可达未变 (%d 格)" % (
                    _t.strftime("%H:%M:%S"), polls, len(_r)), flush=True)
            continue
        n += 1
        t_beat = _t.time()
        chefs = (st.get("layout") or {}).get("chefs") or []
        try:
            cy = float(chefs[0].get("y")) if chefs else None
        except (TypeError, ValueError):
            cy = None
        if _r is None:
            print("\n---- %s #%d  %s  可达算不出来(没有厨师 / 图不完整) ----"
                  % (label, n, _t.strftime("%H:%M:%S")))
            prev_reach = _r
            prev = cur
            continue
        _d = (len(_r) - len(prev_reach)) if prev_reach is not None else 0
        print("\n---- %s #%d  %s  可达 %d%s%s ----"
              % (label, n, _t.strftime("%H:%M:%S"), len(_r),
                 ("  上片 %d" % len(prev_reach)) if prev_reach is not None else "",
                 ("  (%+d)" % _d) if _d else ""))
        # **为什么变了** —— 把地形变过的地方列出来, 一行装下。
        # 没有它的话只看到一个数在跳, 还得回头翻上一片才知道是哪块构件动了。
        #
        # ⚠ **必须同时看「网格字符」和「地板高度」两样**(实测踩过):
        #   原来只比地板高度, 于是 18 片可达变化里只有 4 片打出了原因 ——
        #   剩下 14 片字符变了、高度没变。而 `teleport_edges` 是按
        #   `tm.walkable()` 算的(**只看字符**), 字符一变传送门边数就在
        #   4/6/8/12 之间跳, 可达跟着变 —— 一条原因都看不见。
        _fl = list((tm.floors if tm is not None else []) or [])
        _chg = []
        for _n2 in range(min(len(_fl), len(prev_floor))):
            _a, _b = prev_floor[_n2], _fl[_n2]
            if _a != _b:
                _chg.append("(%d,%d) %s→%s"
                            % (_n2 % tm.w, _n2 // tm.w,
                               "?" if _a is None else "%.2f" % _a,
                               "?" if _b is None else "%.2f" % _b))
        if _chg:
            print("  地板高度变了 %d 格: %s%s"
                  % (len(_chg), "  ".join(_chg[:8]),
                     " …" if len(_chg) > 8 else ""))
        _g, _pg = (tm.grid or ""), prev_grid
        _gchg = []
        for _n2 in range(min(len(_g), len(_pg))):
            if _g[_n2] != _pg[_n2]:
                _gchg.append("(%d,%d) %r→%r"
                             % (_n2 % tm.w, _n2 // tm.w, _pg[_n2], _g[_n2]))
        if _gchg:
            print("  网格字符变了 %d 格: %s%s"
                  % (len(_gchg), "  ".join(_gchg[:8]),
                     " …" if len(_gchg) > 8 else ""))
        # **可达还有第三个输入**: 传送门边。它的边数一变压根不是地形的事 ——
        # 门的台面自己变了(位置挪了 / 在场不在场)。原来只报"地形没变 ⇒ 厨师位置变了",
        # 而实测 39 片里厨师格**一直是同一对**, 那句话是**错的**(实测打脸)。
        _why = []
        if prev_reach is not None and _marks != prev_marks:
            _why.append("厨师位置变了 %s → %s"
                        % ("  ".join("%s%s" % t for t in prev_marks),
                           "  ".join("%s%s" % t for t in _marks)))
        if prev_nedges is not None and _nedges != prev_nedges:
            _why.append("**传送门边 %d→%d**(门的台面变了: 挪位/在场与否)"
                        % (prev_nedges, _nedges))
        if not _chg and not _gchg:
            if prev_reach is None:
                print("  (基线: 第一片, 没有上一片可对比)")
            elif _why:
                print("  ⚠ 地形一个字都没变 —— 变的是: " + "; ".join(_why))
            else:
                print("  ⚠⚠ 地形、厨师格、传送门边**三项都没变**, 可达却变了 —— "
                      "这对不上, 要查(可能是没覆盖到的输入)")
        elif _why:
            print("  另外还变了: " + "; ".join(_why))
        print("  厨师: %s   |   可走 %s   传送门边 %d"
              % ("  ".join("%s%s" % (m, c) for m, c in _marks) or "-",
                 (tm.counts or {}).get("free"), _nedges))
        # **只打这一张** —— 原始地形图/叠加图在主图里已经有了, 这一节只回答
        # "哪儿到得了", 别把日志淹掉。
        print("  可达图(到不了的画成 '-'):")
        for line in tm.ascii(at_y=cy, reach=_r,
                             conv=conveyor_arrows(tm, dyn)).split("\n"):
            print("    " + line)
        _flush_md()          # ← 每片都落盘: 被 timeout 杀掉也不丢(见 _MD 的注释)
        prev_reach = _r
        prev_marks = _marks
        prev_nedges = _nedges
        prev_floor = _fl
        prev_grid = _g
        prev = cur


def _crate_kinds(b) -> dict:
    """箱子 → (直接出的食材, 切完变成的食材)。来自 `know` 表, 键是箱子的 spawn(prefab 名)。

    为什么要交叉引用: 台面扫描给的 `spawn` 只是 **`PickupItemSpawner.m_itemPrefab.name`**
    (裸 prefab 名), 它**分不出**这两种箱子:
      · `spawnIng='Egg'`            → 直接出**成品**, 拿出来就能用
      · `spawnIng='' spawnNext='X'` → 出的是**需切的生料**, 切开才叫 X
    做菜流程完全不同(后者要先去切菜板), 而地图上原本两个都只写 "产出=X"。

    游戏自己的读法是三步(`GameUtils.GetIngredientCrates` / `ItemKnowledge.cs:191-211`):
      `m_itemPrefab` → `IngredientPropertiesComponent`(spawnIng)
                     → `WorkableItem.m_nextPrefab`(spawnNext)
    引擎侧(`cookbook.derive` → `kb.crate_for`)一直用的是这份完整数据 ——
    这里只是把地图显示补上。
    """
    out = {}
    try:
        k = b.get_knowledge()
    except Exception:
        return out
    items = k.get("items") or k.get("crates") or []
    if isinstance(items, dict):
        items = list(items.values())
    for it in items:
        sp = it.get("spawn")
        if sp:
            out[sp] = (it.get("spawnIng") or "", it.get("spawnNext") or "")
    return out


def _crate_label(spawn: str, kinds: dict) -> str:
    """箱子产出的一行文字(带"需切"标注)。认不出来就退回裸 prefab 名。"""
    ing, nxt = (kinds or {}).get(spawn, ("", ""))
    if not ing and nxt:
        return "产出=%s ★需切(箱子出的是生料)" % nxt
    if ing and nxt and ing != nxt:
        return "产出=%s(切后=%s)" % (ing, nxt)
    return "产出=%s" % (ing or spawn)


def _print_stations(st: dict, tm, at=None, crate_kinds=None) -> list:
    """把台面层画成第二张图 + 分组清单。

    为什么要单独一张图: 在地形图里所有台面都是 '#' —— 因为都走不上去。
    于是"哪是菜板、哪是送餐口、哪是垃圾桶"完全看不出来, 而这些恰恰是脚本要交互的目标。
    """
    stations = ((st or {}).get("layout") or {}).get("stations") or []
    print("\n================ 台面分布 ================")
    if not stations:
        print("(这一帧没有读到台面 —— 不在对局里? 或者需要 --force)")
        return []

    cellmap = {}
    for s in stations:
        try:
            i, j = tm.cell_of(float(s.get("x") or 0), float(s.get("z") or 0))
        except (TypeError, ValueError):
            continue
        cellmap.setdefault((i, j), []).append(s)

    mark = tm.cell_of(at[0], at[1]) if at else None

    for j in range(tm.h - 1, -1, -1):
        line = []
        for i in range(tm.w):
            if mark == (i, j):
                line.append(CH_MARK)
                continue
            lst = cellmap.get((i, j))
            if not lst:
                # 非台面格: 显示地形(只保留 走/墙 两种, 免得跟台面字母混)
                ch = tm.at(i, j)
                line.append(ch if ch in "#." else ".")
                continue
            line.append(SEM_LETTER.get(_sem_of(lst[0]), "?"))
        print("".join(line))

    # 图例按字母去重(K/烤箱/炸锅/加热台 共用一个字母, 别重复列四遍)
    seen_letter = []
    for k in SEM_ORDER:
        if k not in SEM_LETTER:
            continue
        L = SEM_LETTER[k]
        if L in [x[0] for x in seen_letter]:
            continue
        seen_letter.append((L, SEM_NAME.get(k, k)))
    print("台面图例: " + "  ".join("%s=%s" % (L, nm) for L, nm in seen_letter))

    groups = {}
    for s in stations:
        groups.setdefault(_sem_of(s), []).append(s)
    print("\n--- 台面清单 (%d 个) ---" % len(stations))
    keys = [k for k in SEM_ORDER if k in groups] + \
           [k for k in sorted(groups) if k not in SEM_ORDER]
    for sem in keys:
        lst = sorted(groups[sem],
                     key=lambda a: (float(a.get("z") or 0), float(a.get("x") or 0)))
        print("  [%s] %s ×%d" % (SEM_LETTER.get(sem, "?"), SEM_NAME.get(sem, sem), len(lst)))
        for s in lst:
            bits = []
            if s.get("sub"):
                bits.append("子类=%s" % s["sub"])
            if s.get("spawn"):
                bits.append(_crate_label(s["spawn"], crate_kinds))
            if s.get("ing"):
                bits.append("内含=%s" % s["ing"])
            n = s.get("n")
            if n:
                bits.append("台上%d件%s" % (int(n), s.get("on") or []))
            print("      %-24s (%6.2f,%6.2f)  %s" % (
                s.get("id", "?"), float(s.get("x") or 0), float(s.get("z") or 0),
                "  ".join(bits)))
    return stations

LEGEND = """
图例:  .  可走        #  被墙/橱柜/台面占住
       F  火焰危险物  P  移动平台(能站, 会动)
       T  传送带      H  危险区(水面/岩浆/边界墙) —— 踩上去会死
       V  空洞(没地面, 会掉下去) / **站不下**(太低或太高)
       x  物理阻挡(不占格子但有碰撞体)
       C  台面传送带(走不上去, 而且放上去的东西会被传走)
       →↓←↑ **台面**传送带(`C`, 推**物品**)的方向 —— **引擎会用它算拦截点**
       > < ? ! **地面**传送带(`T`, 推**厨师**)的方向 —— ⚠**引擎当普通可走格, 不管**
            两套分开是因为推的东西不一样, 处理也不一样。只标"这是传送带"没用:
            知道**往哪推**才知道东西会跑到哪、人站上去会被带到哪。
            图是俯视: `→`/`>` = 世界 +x, `↑`/`^` = 世界 +z
            (⚠ 不用 `⇒`: GBK 编不出来, 管道一抓就炸。GBK 里完整的四方向只有 `→←↑↓`)
       ~  **动态格**: 监视期间"变过"的格子(限时平台/升降台/轮换的地板)。
          一张静态图没法表达"这格有时在有时不在", 所以标出来 ——
          **不标的话它们看上去就是常驻**, 而那会骗人。
       @  你指定的坐标所在格(--at)
       ⚠ **这张图不判可达** —— 它画的是地形本身。走得到走不到看下一节「可达图」。
          没给厨师位置时, `V` 里额外含"没有任何邻居能迈进来"的静态投影。
"""

REACH_LEGEND = """
可达图: **从厨师出发的泛洪**(逐边判"相邻两格落差 ≤ STEP_MAX", 含传送门边);
       **两只厨师取并集**(两个人算一份)。
       `-` = 这格**地形上是地板, 但从厨师这儿到不了**(水面/沟对面那种
             "看着能走、其实过不去"的地, 一眼就能看出来)。
             ⚠ 它**和空洞不是一回事** —— 空洞是 `V`。两者原来共用一个字符,
             于是可达集一变(实测 42 ↔ 78)整张图就换个样子, 看着像地图坏了。
       `.` = 可走且到得了;  其余字符(V/#/x/H/F/…)和地形图同义。
       `→↓←↑`(台面传送带) / `> < ? !`(地面传送带) = 传送带方向。
       ⚠ 这里**"到不了"优先于方向** —— 传送带那格如果从厨师过不去,
       画的是 `-` 不是箭头(否则"能不能过去"被方向盖掉了)。
       落差按**相邻格之间**算: 有台阶就爬得上去、落差太大就过不去;
       平台沉下去 = 邻格和它差一大截 = 进不去 = 洞。
"""

OVERLAY_LEGEND = """
叠加图: 地形字符之上再画**台面语义 + 厨师 + 会动的东西**(优先级: 厨师 > 台面 > 地形)
       数字 1/2 = 厨师 (P1/P2);  会动的: ! = 撞上就死   % = 只是挡路
       & = **可开动的平台**(遥感驾驶的那种; 它不占格子, 地形图上本来完全看不见)
       N = 控制台(驾驶台)
       字母 = 台面语义(见 SEM_LETTER), 常用几个:
       S 送餐口  P 干净盘子堆  D 脏盘子堆  R 盘子回收  B 切菜板  K 灶台/锅
       c 普通台面  G 食材箱  W 洗手池  X 垃圾桶  M 搅拌  C 台面传送带  ! 危险物
       O 传送门  U 按钮  N 驾驶台  Z 大炮  Q 可推物体  A 自动工位
       **当前不在场的台面(限时构件收起)不画** —— 隐去了几个会在图下注明。
       (台面上放着什么不在这张图里 —— 一格一个字符装不下, 看下面的内容清单)
"""


#: `--md` **只导出这些小节**(用户要求: 只看这两张图)。
#: 终端上照旧打全部 13 节 —— 这里筛的只是**落盘的那份**。
#:
#: `变化切片` 也在白名单里: 它是 `--watch` 的产物, 不是那 11 节"清单类"噪音 ——
#: 筛掉的话"边监视边导出"就白做了(用户刚验证过这个用法)。
#: 想全都要就把它设成 None。
#: ⚠ **改 watch 的标签就必须同时改这里** —— 白名单是**按标题子串**筛的,
#:   而它只管**落盘那份**。实测踩过: 把切片标签从 `变化切片` 改成 `可达变化`
#:   却忘了加进来, 于是 md 里**一片变化都没有**, 看着像"监视器没输出"。
MD_SECTIONS = ("地形图", "可达图", "叠加图", "变化切片", "可达变化", "传送门配对")


def to_markdown(text: str, title: str = "胡闹厨房 2 — 关卡地图",
                sections=MD_SECTIONS, header: bool = True) -> str:
    """把终端输出转成 Markdown。

    做法: 按 `---- 小节 ----` / `==== 小节 ====` 切段, 每段:
      · 标题行 → `## 小节名`
      · 其余(含 ASCII 图) → 放进代码块 —— 否则 Markdown 会把图里的
        `.` `#` 缩进吃掉, 或者把 `---` 当成水平线, 图就散了。
    `sections` 是**白名单**: 只留标题里含这些词的小节(None = 全留)。

    `header=False` = **追加模式**: 不写 H1 导言, 改成以
    `## <时间> · <关卡>` 开一个新小节、里面用 `### <小节名>`。
    每跑一次就往后摞一节, 于是同一份文件里能**纵向对比**历次的两张图
    (用户要求: "追加到同一个文件末尾, 带时间戳小节")。
    """
    import datetime
    import re as _re

    # 先切段: [(标题 or None, [行])]
    chunks, cur, buf = [], None, []
    for line in text.split("\n"):
        m = _re.match(r"^\s*(?:-{2,}|={2,})\s*(.+?)\s*(?:-{2,}|={2,})\s*$", line)
        if m:
            chunks.append((cur, buf))
            cur, buf = m.group(1), []
        else:
            buf.append(line)
    chunks.append((cur, buf))

    # 场景名从开头那段抽出来放进导言 —— 用户在意"这份是哪一关的"
    scene = ""
    for line in chunks[0][1] if chunks else []:
        if line.startswith("场景 ="):
            scene = line.strip().replace("场景 = ", "").split("  在局")[0].strip()
            break

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # `header` 只管**要不要写 H1 那一块**; 时间戳小节**每一节都要有** ——
    # 一开始我把这两件事混在一起, 结果第一次落盘的那节没有时间戳,
    # 和后面几节格式不一致, 纵向对比时看不出它是什么时候的。
    out = ["# " + title, "", "> 每跑一次追加一节 · 最新的在最下面", ""] if header else []
    out += ["## %s · %s" % (stamp, scene or "?"), ""]
    h = "### "

    for t, lines in chunks:
        if t is None:
            continue                       # 开头那段(桥/场景/网格统计)已抽进导言
        if sections and not any(s in t for s in sections):
            continue
        body = list(lines)
        while body and not body[-1].strip():
            body.pop()
        if not body:
            continue
        out.append(h + t)
        out.append("")
        out.append("```")
        out.extend(body)
        out.append("```")
        out.append("")
    return "\n".join(out)


#: `--md` 时由 __main__ 填上 `{"path": ..., "buf": ...}`, 供 `_flush_md()` 增量重写。
#: 为什么需要: Markdown 本来是在 `main()` **返回之后**才写的, 而 `--watch` 是无限循环 ——
#: 一旦被 `timeout`/SIGTERM 杀掉, finally 不执行, **文件就丢了**(实测踩过:
#: 跑了 30 秒监视, 中间拿料的过程全抓到了, 但桌面那份文件没更新)。
_MD = {"path": None, "buf": None, "offset": None}


def _flush_md() -> None:
    """把当前已攒下的输出**追加**进 Markdown 文件(每次运行一节, 带时间戳)。

    追加而不是覆盖 —— 用户要求: 历次的地形图/叠加图都留在同一份文件里,
    纵向摞着就能直接对比。

    `_MD["offset"]` = **本次运行那一节的起点**。第一次落盘时记下当时的文件末尾,
    之后每次都从那里截断重写 —— 监视模式下 buffer 一直在变长, 不截断的话
    同一节会被重复追加 N 遍。
    """
    p, buf = _MD.get("path"), _MD.get("buf")
    if not p or buf is None:
        return
    try:
        cur_size = os.path.getsize(p) if os.path.exists(p) else 0
        off = _MD.get("offset")
        if off is None:
            off = cur_size
            _MD["offset"] = off
        elif off > cur_size:
            # ⚠ 文件**变短了** —— 说明被外部动过(删了重建/被截断/另一个进程在写)。
            #   这时沿用旧的 offset 去 `truncate(off)` 会**把别人的内容削掉**,
            #   甚至 seek 到文件末尾之外留一堆空洞。所以重新从当前末尾算。
            #   (实测踩过: 一次 --md 之后桌面文件只剩最新一节, 之前两节没了。)
            print("\n[md] ⚠ 文件比预期短(%d < %d), 可能被外部改过 —— 按现状重算偏移"
                  % (cur_size, off))
            off = cur_size
            _MD["offset"] = off
        body = to_markdown(buf.getvalue(), header=(off == 0))
        mode = "r+b" if os.path.exists(p) else "wb"
        with open(p, mode) as f:
            f.truncate(off)
            f.seek(off)
            f.write(body.encode("utf-8"))
    except Exception as _e:
        # ⚠ **别吞异常**(实测踩过): 原来这里是 `except: pass`, 于是"写失败"和
        #   "没东西可写"在输出上**一模一样** —— 桌面上那份 md 整个不见了、
        #   而终端一声不吭, 谁也不知道。至少要**吭一声**(每片只喊一次)。
        if not _MD.get("_warned"):
            _MD["_warned"] = True
            print("\n[md] ⚠ **写不进去**: %r  —— 文件=%s  桌面那份不会有内容"
                  % (_e, p), flush=True)


def overlay_ascii(tm, stations, chefs=(), mark=None, movers=None,
                  at_y=None, platforms=None, reach=None, dynamic=None) -> str:
    """把**台面层 + 会动的东西**叠到地形网格上 —— 一张图看清"哪一格是什么"。

    为什么需要: 地形图里所有台面都是 `#`(都走不上去), 分不出灶台/切菜板/盘子堆。
    这张图把语义字母画上去, 厨师画成 1/2(优先级最高, 盖住台面)。

    `movers`(路人/车辆): 画成 `!`(致命, 撞上就死) 和 `%`(只是挡路)。
    **必须画** —— 地形是整局一次的静态快照, 看不见会动的东西; 不画出来的话
    "地图说安全"的地方可能就是车当前的位置, 而那比空白更危险。

    `at_y` = **厨师当前高度**, 传给底层地形: 平台收起来时格子字符不变、只有高度
    掉了, 不给高度的话这张图同样会替它打包票(见 `TerrainMap.ascii` 的注释)。
    """
    over = {}
    for s in stations or []:
        # **当前不在场的台面不画** —— 限时构件(传送门/限时楼梯)会整块启用停用,
        # 照画就等于图在说"这儿有个门"(用户实测指出这一点)。
        # 老 dll 没有 active 字段 → 默认在场, 行为不变。
        if not s.get("active", True):
            continue
        try:
            cell = tm.cell_of(float(s.get("x") or 0), float(s.get("z") or 0))
        except (TypeError, ValueError):
            continue
        # **会话进行中**的控制台画成 '*' —— 这个状态比它在哪更重要:
        # 此刻移动键驱动的是平台而不是厨师, 谁看这张图都得先知道这件事。
        if s.get("session"):
            over[cell] = TERM_LIVE
        else:
            over.setdefault(cell, SEM_LETTER.get(_sem_of(s), UNKNOWN))
    for m in movers or []:
        try:
            x = float(m.get("x") if isinstance(m, dict) else getattr(m, "x", 0))
            z = float(m.get("z") if isinstance(m, dict) else getattr(m, "z", 0))
        except (TypeError, ValueError):
            continue
        deadly = (m.get("kind") if isinstance(m, dict) else getattr(m, "kind", "")) == "RespawnCollider"
        over[tm.cell_of(x, z)] = MV_DEADLY if deadly else MV_BLOCK
    # **可开动的平台**(插件 dyn 的 platforms 表) —— 遥感驾驶的那块地板。
    # 必须单独画: 它**不占格子**(实测这关 `平台0`), 所以地形图/叠加图上**什么都没有** ——
    # 而"平台现在停在哪"正是决定"哪几格可达"的东西(见 `TerrainMap.bridge_cells`)。
    for p in platforms or []:
        try:
            x = float(p.get("x") if isinstance(p, dict) else getattr(p, "x", 0))
            z = float(p.get("z") if isinstance(p, dict) else getattr(p, "z", 0))
        except (TypeError, ValueError):
            continue
        over[tm.cell_of(x, z)] = PLATFORM_MARK
    for c in chefs or []:
        try:
            cell = tm.cell_of(float(c.get("x") or 0), float(c.get("z") or 0))
        except (TypeError, ValueError):
            continue
        over[cell] = str(int(c.get("id", 0)) + 1)
    rows = []
    for j in range(tm.h - 1, -1, -1):
        line = []
        for i in range(tm.w):
            if mark == (i, j):
                line.append(CH_MARK)
            elif (i, j) in over:
                line.append(over[(i, j)])
            else:
                # 空地才判 —— 台面/厨师/车子的字母优先, 那些格子本来就画的是"有什么"
                line.append(tm._mark(i, j, at_y, reach, dynamic))
        rows.append("".join(line))
    return "\n".join(rows)


#: 台面上那件东西按 **Unity Tag** 分类 —— 游戏自己就是这么分的
#: (GameUtils.cs:504-707 那一整套查找器全是"按 tag 找 + 按组件筛")。
#: 字母用来说明"这一格上放的是什么", 和 SEM_LETTER(台面本身是什么)是两套。
#: 第二个元素 = 内容图上显示的那个字符。
_TAG_KIND = {
    "plate": ("盘子", "p"),
    "cookingutensil": ("锅", "K"),
    "preingredient": ("生料", "i"),
    "ingredient": ("食材", "i"),
    "crate": ("箱子", "G"),
    "player": ("厨师", "1"),
}


def _kind_of(tag: str, name: str = "") -> tuple:
    # ⚠ 游戏里的 tag 是 `Pre-Ingredient`(带连字符), 只 strip+lower 匹配不上 ——
    #   要先把非字母数字去掉再查表。(这个 bug 是在离线演示里看出来的:
    #   台面上的 SushiPrawn 明明带着 Pre-Ingredient, 却被归成"其他"。)
    t = "".join(ch for ch in (tag or "").lower() if ch.isalnum())
    if t in _TAG_KIND:
        return _TAG_KIND[t]
    # tag 认不出来时按**名字**兜底 —— 灭火器/水枪的 tag 是 `Untagged`,
    # 光看 tag 它们永远是"其他", 而有的关卡必须靠它们灭火(见 map_model.is_extinguisher)。
    if is_extinguisher(name):
        return ("灭火器", "E")
    if is_pot(name, tag):
        return ("锅", "K")
    if is_plate(name, tag):
        return ("盘子", "p")
    return ("其他", "*")


def content_ascii(tm, stations, chefs=(), mark=None) -> str:
    """**内容图**: 只在"有东西"的格子上画 —— 这一格上放的是什么。

    和叠加图的区别: 叠加图答"这台子是什么"(灶台/切菜板/盘子堆),
    这张图答"**东西在哪**"(盘子在 p / 锅在 K / 生料在 i)。
    食材是脚本真正要找的东西, 光看台面语义是看不出来的。
    """
    over = {}
    for s in stations or []:
        for i, o in enumerate(s.get("on") or []):
            tags = s.get("ontags") or []
            tg = tags[i] if i < len(tags) else ""
            _, ch = _kind_of(tg, o)
            try:
                over[tm.cell_of(float(s.get("x") or 0), float(s.get("z") or 0))] = ch
            except (TypeError, ValueError):
                pass
    for c in chefs or []:
        if not c.get("held"):
            continue
        try:
            over[tm.cell_of(float(c.get("x") or 0), float(c.get("z") or 0))] = CH_HELD
        except (TypeError, ValueError):
            pass
    rows = []
    for j in range(tm.h - 1, -1, -1):
        line = []
        for i in range(tm.w):
            if mark == (i, j):
                line.append(CH_MARK)
            elif (i, j) in over:
                line.append(over[(i, j)])
            elif tm.is_danger(i, j):
                line.append(tm.at(i, j))       # 危险区照画, 免得看不出边界
            else:
                line.append(" ")
        rows.append("".join(line))
    return "\n".join(rows)


def _fnum(v) -> str:
    try:
        return "%.2f" % float(v)
    except (TypeError, ValueError):
        return "?"


def _print_teleports(stations) -> None:
    """传送门配对 —— **哪扇通向哪扇**。

    为什么要单独一节: 叠加图里门只是一个个 `O`, 完全看不出拓扑; 而"配对"正是
    规划层能不能把门**当一条边**用的前提(见 `map_model.Station.exit_portal`)。
    顺带打出**落点** —— 那才是真正把人放下的位置, 和门的视觉位置可能差一格
    (实测 s_wizard_school_3_4 上差约 1 格, 拿视觉位置当落点会够不着)。
    """
    ports = [s for s in stations or []
             if s.get("exitPortal")
             or (s.get("kind") or "").lower() == "teleportal"]
    if not ports:
        return
    print("\n---- 传送门配对 ----")
    got = 0
    n_off = 0
    for s in ports:
        nm = s.get("name") or "?"
        x, z = float(s.get("x") or 0), float(s.get("z") or 0)
        # 限时构件: 门**现在在不在场**。
        off = not s.get("active", True)
        if off:
            n_off += 1
        mark = "  [当前不在场]" if off else ""
        ex = s.get("exitPortal")
        if not ex:
            print("  %-18s 门(%6.2f,%6.2f)   **没有配对信息**%s"
                  % (nm, x, z, mark))
            continue
        got += 1
        print("  %-18s 门(%6.2f,%6.2f)  →  %s @ (%s,%s)   冷却 %ss  弧度 %s°%s"
              % (nm, x, z, ex, _fnum(s.get("exitX")), _fnum(s.get("exitZ")),
                 _fnum(s.get("cooldown")), _fnum(s.get("arc")), mark))
        if s.get("landX") is not None and s.get("landZ") is not None:
            print("  %-18s   落点 (%s,%s) ← 真正站上去的位置"
                  % ("", _fnum(s.get("landX")), _fnum(s.get("landZ"))))
    if got == 0:
        print("  (一扇都没读到配对 —— 确认 dll 里含传送门解析, 且这关真有 Teleportal)")
    else:
        print("  %d/%d 扇读到配对%s" % (
            got, len(ports),
            "; **其中 %d 扇当前不在场**" % n_off if n_off else ""))


def _print_unseen(st: dict) -> None:
    """**地图看不见的食材** —— 回答"建模还有遗漏吗"。

    两边范围不同:
      · 台面层看到的是 `station.on` / `onhas` —— 只知道"在哪个台面上"
      · 游戏自己的 `GameUtils.GetAllIngredients()`(按 tag 全场景找)才是权威全集
    差集就是**我们漏掉的**: 掉在地上的、在移动平台/荷叶上的、台面类型没覆盖到的。
    """
    km = KitchenMap.from_layout(st.get("layout") or {})
    items = km.items
    print("\n--- 全场景食材(游戏按 tag 找的, 共 %d 件) ---" % len(items))
    if not items:
        print("  (没有 —— 不在对局里? 或这关真的没食材)")
        return
    by_tag = {}
    for it in items:
        by_tag.setdefault(it.tag, []).append(it)
    for tag, lst in sorted(by_tag.items()):
        print("  [%s] ×%d" % (tag, len(lst)))
        for it in sorted(lst, key=lambda a: (a.z, a.x))[:12]:
            print("      %-30s (%6.2f,%6.2f)" % (it.name, it.x, it.z))
        if len(lst) > 12:
            print("      … 另有 %d 件" % (len(lst) - 12))

    unseen = km.unseen_items()
    print("\n--- ★ 地图**看不见**的食材 (%d 件) ---" % len(unseen))
    if not unseen:
        print("  (无 —— 台面层和游戏的全集对得上)")
        return
    print("  这些在游戏里存在, 但既不在任何台面的 on/onhas 里、也不在任何厨师手上:")
    for it in sorted(unseen, key=lambda a: (a.z, a.x)):
        print("      %-30s tag=%-16s (%6.2f,%6.2f)" % (it.name, it.tag, it.x, it.z))


def _print_foods(st: dict, stations) -> None:
    """**食材都在哪** —— 场上每一件东西的位置清单。"""
    chefs = ((st.get("layout") or {}).get("chefs") or [])
    rows = []          # (种类, 名字, 在哪, x, z, 备注)

    for s in stations or []:
        tags = s.get("ontags") or []
        hases = s.get("onhas") or []
        for i, o in enumerate(s.get("on") or []):
            tg = tags[i] if i < len(tags) else ""
            ha = hases[i] if i < len(hases) else ""
            kind, _ = _kind_of(tg, o)
            note = ("容器内=%s" % ha) if ha else "台上"
            rows.append((kind, o, s.get("id", "?"),
                         float(s.get("x") or 0), float(s.get("z") or 0), note))
        if s.get("spawn"):
            rows.append(("货源", s["spawn"], s.get("id", "?"),
                         float(s.get("x") or 0), float(s.get("z") or 0), "箱子里可取"))

    for c in chefs:
        if c.get("held"):
            rows.append(("手上", c["held"], "P%d" % (int(c.get("id", 0)) + 1),
                         float(c.get("x") or 0), float(c.get("z") or 0), "厨师拿着"))

    print("\n--- 食材都在哪 (%d 项) ---" % len(rows))
    if not rows:
        print("  (场上没有任何食材/盘子/锅 —— 关卡刚开始?)")
    for kind, name, where, x, z, note in sorted(rows, key=lambda r: (r[0], r[1])):
        print("  %-4s %-26s @ %-26s (%6.2f,%6.2f)  %s" % (kind, name, where, x, z, note))


def _print_live(st: dict, stations) -> None:
    """**每帧在变**的那两层: 台面/容器里现在有什么, 厨师此刻会作用到谁。

    这两层是这次地图重构的重点(台面内容从 1 秒变成每帧), 也是"材料到底进没进盘"
    "站对了没有"唯一的观测手段 —— 上一局 9 次失败都是靠它们定位的。
    """
    busy = [s for s in (stations or [])
            if (s.get("on") or []) or (s.get("ing") or "")]
    print("\n--- 台面/容器里**现在有东西**的 (%d 个) ---" % len(busy))
    if not busy:
        print("  (全是空的)")
    for s in sorted(busy, key=lambda a: (float(a.get("z") or 0), float(a.get("x") or 0))):
        print("  [%s] %-24s (%6.2f,%6.2f)" % (
            SEM_LETTER.get(_sem_of(s), "?"), s.get("id", "?"),
            float(s.get("x") or 0), float(s.get("z") or 0)))
        ons = s.get("on") or []
        tags = s.get("ontags") or []
        hases = s.get("onhas") or []
        for i, o in enumerate(ons):
            tg = tags[i] if i < len(tags) else ""
            ha = hases[i] if i < len(hases) else ""
            # onhas 非空 = 这个容器**不是空的**(盘子上的菜/锅里的料) —— 取菜前必须认得出空盘
            print("        台上: %-26s tag=%-18s 容器内=%s" % (o, tg or "-", ha or "(空)"))
        if s.get("ing"):
            print("        内含食材: %s" % s["ing"])

    print("\n--- 厨师此刻的交互目标 (游戏自己算的, 权威) ---")
    for c in ((st.get("layout") or {}).get("chefs") or []):
        bits = []
        if c.get("held"):
            bits.append("手持=%r" % c["held"])
        bits.append("抓取=%r" % (c.get("pick") or "(空)"))
        bits.append("工位=%r" % (c.get("use") or "(空)"))
        # placeh = m_iHandlePlacement 所在物体: **ReceivePlaceEvent 带的就是它**,
        # 所以"按下去会放到谁那儿"看这一个字段就够了(引擎的 B 修复用的就是它)。
        bits.append("放置=%r" % (c.get("placeh") or "(空)"))
        print("  P%s (%5.1f,%5.1f) canpress=%s  %s" % (
            int(c.get("id", 0)) + 1, float(c.get("x") or 0), float(c.get("z") or 0),
            c.get("canpress"), "  ".join(bits)))


def main() -> int:
    force = "--force" in sys.argv
    at = None
    if "--at" in sys.argv:
        i = sys.argv.index("--at")
        try:
            at = (float(sys.argv[i + 1]), float(sys.argv[i + 2]))
        except (IndexError, ValueError):
            print("--at 用法: --at <x> <z>")
            return 2
    # `--at-y <高度>`: 手动指定"按哪一层的厨师来画图"。
    # 默认取厨师自己的 y; 指定它可以问"如果我站在另一层会怎样"。
    at_y = None
    if "--at-y" in sys.argv:
        i = sys.argv.index("--at-y")
        try:
            at_y = float(sys.argv[i + 1])
        except (IndexError, ValueError):
            print("--at-y 用法: --at-y <高度>")
            return 2

    b = BridgeClient()
    try:
        if not b.connect(retries=2):
            print("桥连不上 —— 游戏没开, 或者插件没加载")
            return 1
    except BridgeError:
        print("桥连不上 —— 游戏没开, 或者插件没加载(端口 48778 没人监听)")
        return 1

    st = b.get_state()
    print("场景 =", st.get("scene"), " 在局 =", st.get("inRound"), " 模式 =", st.get("mode"))
    for c in ((st.get("layout") or {}).get("chefs") or []):
        print("  厨师 id=%s player=%s (%s, %s)" % (
            c.get("id"), c.get("player"), c.get("x"), c.get("z")))

    data = b.get_map(force=force)
    if data.get("error"):
        print("!! 取图失败:", data["error"])
        print("   (旧版 dll 没有 map 命令 —— 需要重启游戏加载新插件)")
        return 1

    tm = TerrainMap(data)
    if not tm.ok:
        print("!! 网格数据不完整:", data)
        return 1

    cx, cz = (at if at else (None, None))
    stations = (st.get("layout") or {}).get("stations") or []
    chefs = (st.get("layout") or {}).get("chefs") or []

    # **厨师现在的高度** —— 两张图都要按它来画。
    # 为什么(用户实测指出): 限时平台收起来时**字符一格不变**, 只是高度掉了
    # (实测 s_wizard_school_3_4: 12 格从 -1.51/-1.00 掉到 -9.08)。
    # 不给高度的图会照写"能走" —— **图在替限时构件打包票**, 那比空白更危险。
    # `--at-y` 可以手动指定(比如想看"如果我站在另一层会怎样")。
    try:
        chef_y = float(at_y) if at_y is not None else (
            float(chefs[0].get("y")) if chefs else None)
    except (TypeError, ValueError):
        chef_y = None
    # 机关表**在地形图之前取**: 它同时供三处用 —— 层表(判危险区能不能用射线)、
    # 平台表(叠加图上要画"可开动的平台")、以及后面那节机关清单。
    # ⚠ 一开始放在叠加图那里取, 结果地形图小节里先用到了它 →
    #   `UnboundLocalError` 被 try 吞掉, 那条诊断**静默消失**(实测踩过)。
    try:
        _dyn = b.get_dyn() or {}
    except Exception:
        _dyn = {}
    _plats = _dyn.get("platforms") or []
    # **传送带方向** —— 图上把 `T`/`C` 换成箭头(见 `map_model.conveyor_arrows`)。
    # 和 `_dyn` 一起在地形图之前算好: 图要用, 后面那节机关清单也要用。
    _conv = conveyor_arrows(tm, _dyn)

    # **泛洪一次, 两张图共用** —— 图上要表达的是"**我到得了哪**"(连通性),
    # 而那是泛洪才能答的: 水面/沟对面的地每格都"迈得进来", 可厨师过不去。
    # 传 `extra_edges`(传送门): 靠门才到得了的地方也该算"到得了"。
    _reach = None
    try:
        _edges = teleport_edges(KitchenMap.from_layout(st.get("layout") or {}), tm)
    except Exception as _e:
        _edges = {}
        print("(传送门边构造失败: %s)" % _e)
    # **两个厨师分别算** —— 他们站在不同位置, 可达集可以不同(用户要求)。
    # ⚠ 一个人到得了不等于另一个到得了: 分厨房/隔断关卡里差别很大。
    # 图上画的是**并集**(队伍能到哪); 每只各自的数字单独打出来。
    _reaches = {}
    for c in chefs or []:
        try:
            _reaches[int(c.get("id", 0))] = tm.reachable_from(
                float(c.get("x") or 0), float(c.get("z") or 0), extra_edges=_edges)
        except Exception as _e:
            print("(泛洪失败 id=%s: %s)" % (c.get("id"), _e))
    if _reaches:
        _reach = set()
        for r in _reaches.values():
            _reach |= r
        print("泛洪(含 %d 条传送门边): %s   → 并集 %d 格"
              % (len(_edges),
                 "  ".join("id=%s→%d 格" % (k, len(v))
                           for k, v in sorted(_reaches.items())),
                 len(_reach)))
        if len(_reaches) > 1:
            only = {k: len(v - set().union(*[w for kk, w in _reaches.items() if kk != k]))
                    for k, v in _reaches.items()}
            print("      其中只有自己到得了的: %s"
                  % "  ".join("id=%s→%d 格" % (k, n) for k, n in sorted(only.items())))

    print("---- 地形图 (原始网格, **不判可达**) ----")
    # ⚠ **这些行必须放在"小节标题之后"** —— `to_markdown` 只把**小节之内**的内容
    #   写进 md, 标题之前的文字只用来抽场景名(见 to_markdown 的切段逻辑)。
    #   原来是打在标题之前的: 于是**网格尺寸/floorY/格子统计/多网格警告**这些
    #   "这份图可不可信"的关键信息, 在 md 里**一条都看不到** ——
    #   而用户只看 md(实测踩过: "需要输出一下标注floorY")。
    print("网格 %dx%d  半步(%d,%d)  原点(%.2f,%.2f)  步长(%.3f,%.3f)  规则=%s  活跃网格数=%s" % (
        tm.w, tm.h, tm.hx, tm.hz, tm.ox, tm.oz, tm.cellx, tm.cellz, tm.regular,
        data.get("grids")))
    print("判定基准: 厨师当前高度 y=%s   |   C# 全局参考高度 floorY=%s"
          % (_fnum(chef_y), _fnum(getattr(tm, "floor_y", None))))
    print("  · 站不下的格(太低/太高)按**厨师自己的 y** 判, 与空洞一样画成 'V'; 不用 floorY")
    print("  · **只有「地板会动」时才判高度**(移动平台沉下去 = 一个洞, 踩上去掉下去);")
    print("    **静止地板一律不判高度** —— 落差只说明「那是另一层」, 能不能过去是**连通性**")
    print("    问题, 交给寻路去算(坡道/楼梯的中间高度会让它自然爬上去)。")
    print("  · floorY = 所有厨师 y 的**最大值**, 只用来定射线起点窗口和危险区归类")
    if int(data.get("grids") or 1) > 1:
        print("⚠ 这一关有多个网格管理器, 网格内容可能不完整 —— 需要人工确认")
    print("格子统计:", tm.describe_dangers())
    # 诊断: 每类格子的"全层射线第一条打在哪一层" —— 回答"水面能不能被射线打到"。
    # 只看危险区('H')那一行就够: 若打中的是 Water 层, 危险区判定可以换成逐格射线,
    # 顺带拆掉 CollectHazards 里那颗吃 floorY 的地雷。
    try:
        _lay = dict((int(L.get("index")), L.get("name"))
                    for L in (_dyn.get("layers") or []))
        if any(tm.first_hit_layer(i, j) >= 0
               for j in range(tm.h) for i in range(tm.w)):
            print("各格型「全层射线第一条命中层」(诊断):")
            print("   (末列 = 这批格子里「射线打中的是危险面」的占比)")
            for ch, tot, top, haz, nms, rts in tm.layer_stats(_lay):
                print("   %-3r %4d 格 → %-34s 危险面 %d/%d"
                      % (ch, tot, "  ".join("%s×%d" % (n, c) for n, c in top),
                         haz, tot))
                if nms:
                    print("        打中的物体: %s"
                          % "  ".join("%s×%d" % (n, c) for n, c in nms))
                if rts:
                    print("        危险面类型: %s   (D水 F深渊 H撞击 C车)"
                          % "  ".join("%s×%d" % (n, c) for n, c in rts))
    except Exception as _e:
        print("(层统计失败: %s)" % _e)
    _low = tm.low_floor_detail() if hasattr(tm, "low_floor_detail") else []
    if _low:
        # 只在**地板低得可疑**时有内容 —— 平时是空的, 不会刷屏。
        print("最低那批地板的射线命中(诊断, 用来判「底下是不是真地板」):")
        for h in _low[:6]:
            print("   y=%-7s %4s 格  打中的是: %s"
                  % (_fnum(h.get("y")), h.get("cells"), h.get("what")))
    # **原始网格**: 不判可达, 只画地形本身(加上"站不下"的静态投影)。
    # 为什么要单独一张(用户定): 带可达的那张, 可达集一大一小图面就整个变样
    # (实测 42 ↔ 78), 于是"地形到底长什么样"反而看不出来了。
    print(tm.ascii(at_y=chef_y, conv=_conv))
    print(LEGEND)

    print("\n---- 可达图 (地形 + 泛洪: 到不了的画成 '-') ----")
    print(tm.ascii(cx, cz, at_y=chef_y, reach=_reach, conv=_conv))
    print(REACH_LEGEND)

    print("\n---- 叠加图 (地形 + 台面语义 + 厨师) ----")
    movers = (st.get("layout") or {}).get("movers") or []
    print(overlay_ascii(tm, stations, chefs, tm.cell_of(cx, cz) if at else None,
                        movers=movers, at_y=chef_y, platforms=_plats,
                        reach=_reach))
    print(OVERLAY_LEGEND)
    # ---- 遥感(遥控驾驶)标记 ----
    # 为什么必须显式打出来: **会话一开, 移动键驱动的是平台而不是厨师** ——
    # 看图的人(和脚本)不知道这件事, 就会把平台当厨师开(见 engine.session_station)。
    _terms = [t for t in stations
              if (t.get("kind") or "") == "Terminal" or t.get("pilots")
              or t.get("session")]
    for t in _terms:
        _live = bool(t.get("session"))
        print("  遥感: 控制台 %s @ (%.1f,%.1f)  驾驶 %s  会话=%s%s"
              % (t.get("name") or "?", float(t.get("x") or 0),
                 float(t.get("z") or 0), t.get("pilots") or "?",
                 "开" if _live else "关",
                 "   ← **此刻移动键驱动的是平台, 不是厨师**" if _live else ""))
    for p in _plats:
        print("  可开动平台 %s @ (%.1f,%.1f)   ← 图上画成 '%s'"
              % (p.get("name") or "?", float(p.get("x") or 0),
                 float(p.get("z") or 0), PLATFORM_MARK))
    # 隐去了什么必须说清楚 —— 不然就是"静默地少画了东西", 和原来那个
    # "静默地多画了东西"一样会误导。
    hid = [s for s in stations if not s.get("active", True)]
    if hid:
        print("  ⚠ **%d 个台面当前不在场**(限时构件收起), 已从上面的叠加图隐去:" % len(hid))
        print("     %s" % "  ".join((s.get("name") or "?") for s in hid[:8]))

    _print_teleports(stations)

    print("\n---- 内容图 (东西在哪: p盘 K锅 i生料/食材 H厨师手上) ----")
    print(content_ascii(tm, stations, chefs, tm.cell_of(cx, cz) if at else None))
    print("      空白 = 那格上没有东西")

    _print_foods(st, stations)
    _print_unseen(st)
    _print_live(st, stations)

    # ---- 连通性: 从厨师出发真正到得了哪些格 ----
    chefs = ((st.get("layout") or {}).get("chefs") or [])
    if chefs:
        try:
            px = float(chefs[0].get("x") or 0)
            pz = float(chefs[0].get("z") or 0)
        except (TypeError, ValueError):
            px = pz = None
        if px is not None:
            # ⚠ **必须带 at_y**: 不带的话 BFS 会无视高度 ——
            #   收起来的平台/另一层的地板都算"能走", 这张图就会**高估**可达范围。
            reach = tm.reachable_from(px, pz, at_y=chef_y)
            print("\n---- 从厨师(id=%s)出发**真正到得了**的区域 (空白 = 到不了) ----" % chefs[0].get("id"))
            print(tm.ascii_reach(px, pz, at_y=chef_y))
            print("可走格 %d 个, 其中从厨师出发到得了的 %d 个" % (
                sum(1 for j in range(tm.h) for i in range(tm.w)
                    if tm.walkable(i, j, at_y=chef_y)),
                len(reach)))
            print("说明: 空白处虽然地形上是'.'(有地面、没占用物), 但和厨师不连通 ——")
            print("      寻路不会去, 也不该算作'边界被解析成可走'。")
            if len(reach) < 10:
                print("⚠ 到得了的格子非常少! 厨师可能被卡在角落里, 需要人工确认")

    # 顺手给出"哪里有危险"的格子坐标清单
    danger = [(i, j) for j in range(tm.h) for i in range(tm.w) if tm.is_danger(i, j)]
    if danger:
        xs = [tm.world_of(i, j)[0] for i, j in danger]
        zs = [tm.world_of(i, j)[1] for i, j in danger]
        print("危险格数量 %d, 世界范围 x[%.1f,%.1f] z[%.1f,%.1f]" % (
            len(danger), min(xs), max(xs), min(zs), max(zs)))
    else:
        print("这一关没有危险格 —— 脚本可以放心直走。")

    # 台面层: 地形图之外真正要交互的那一层
    _print_stations(st, tm, at, crate_kinds=_crate_kinds(b))

    # 机关/陷阱: 静态网格看不见的那一层
    try:
        dyn = b.get_dyn()
    except Exception as e:
        print("\n(机关扫描不可用: %s)" % e)
        return 0
    c = dyn.get("counts") or {}
    print("\n================ 机关 / 陷阱 ================")
    print("按钮%d  传送带%d  触发机器%d  平台%d  着火%d  关卡变形%d" % (
        int(c.get("buttons") or 0), int(c.get("conveyors") or 0), int(c.get("triggers") or 0),
        int(c.get("platforms") or 0), int(c.get("fires") or 0), int(c.get("transitions") or 0)))

    for x in dyn.get("buttons") or []:
        print("  [按钮] %-14s (%6.2f,%6.2f)  此刻可按=%s" % (
            x.get("type"), float(x.get("x") or 0), float(x.get("z") or 0), x.get("pressable")))

    # 传送带分两套, 别混: Travelator 推**厨师**, ConveyorStation 推**物品**。
    convs = dyn.get("conveyors") or []
    chefbelt = [x for x in convs if x.get("type") == "Travelator"]
    itembelt = [x for x in convs if x.get("type") != "Travelator"]

    def _fmt(x):
        step = ""
        sx, sz = float(x.get("stepx") or 0), float(x.get("stepz") or 0)
        if sx or sz:
            step = " 传送方向=(%+.0f,%+.0f)格" % (sx, sz)
        spc = x.get("secPerCell")
        spc_s = "  每格%.2fs" % float(spc) if spc is not None else ""
        return "  [传送带/推人] %-11s (%6.2f,%6.2f) 开=%s 朝向=%s 速度=%.2f%s%s%s" % (
            x.get("type"), float(x.get("x") or 0), float(x.get("z") or 0), x.get("on"),
            x.get("dir"), float(x.get("speed") or 0),
            " " + str(x.get("unit") or ""), step, spc_s)

    for x in chefbelt:
        print(_fmt(x))

    if itembelt:
        # 台面传送带可能有几十个, 只汇总 + 列前几个, 免得淹掉其它信息
        dirs = {}
        for x in itembelt:
            k = (x.get("dir"), float(x.get("speed") or 0), x.get("on"))
            dirs[k] = dirs.get(k, 0) + 1
        print("  [传送带/推物品] ConveyorStation 共 %d 个  —— 台面上放的东西会被一格一格传走" % len(itembelt))
        for (d, sp, on), n in sorted(dirs.items(), key=lambda kv: -kv[1]):
            print("      朝向=%-11s 速度=%.2f 格/秒  开=%s  ×%d" % (d, sp, on, n))
        step_by_dir = {}
        for x in itembelt:
            step_by_dir.setdefault(
                (x.get("dir"), float(x.get("stepx") or 0), float(x.get("stepz") or 0)),
                []).append((float(x.get("x") or 0), float(x.get("z") or 0)))
        for (d, sx, sz), pts in step_by_dir.items():
            print("      朝向=%s → 往 (%+.0f,%+.0f) 格传, 共 %d 个" % (d, sx, sz, len(pts)))
        for x in sorted(itembelt, key=lambda a: (float(a.get("z") or 0), float(a.get("x") or 0)))[:8]:
            print("      · (%6.2f,%6.2f) 朝向=%s" % (
                float(x.get("x") or 0), float(x.get("z") or 0), x.get("dir")))
        if len(itembelt) > 8:
            print("      · … 另有 %d 个" % (len(itembelt) - 8))

    for x in dyn.get("platforms") or []:
        print("  [平台] %-14s (%6.2f,%6.2f) %s" % (
            x.get("type"), float(x.get("x") or 0), float(x.get("z") or 0), x.get("name")))
    for x in dyn.get("fires") or []:
        print("  [着火] %-14s (%6.2f,%6.2f)" % (
            x.get("type"), float(x.get("x") or 0), float(x.get("z") or 0)))
    for x in dyn.get("transitions") or []:
        print("  [变形] %-30s (%6.2f,%6.2f) flags=%s" % (
            x.get("type"), float(x.get("x") or 0), float(x.get("z") or 0), x.get("flags")))
    for x in (dyn.get("triggers") or [])[:40]:
        print("  [触发] %-24s (%6.2f,%6.2f) 开=%s" % (
            x.get("type"), float(x.get("x") or 0), float(x.get("z") or 0), x.get("on")))
    if len(dyn.get("triggers") or []) > 40:
        print("  ... 另有 %d 个触发机器未列出" % (len(dyn["triggers"]) - 40))

    tags = dyn.get("tags") or []
    if tags:
        print("\n--- 关卡 tag 总表 (游戏自己就是靠 tag 找东西的) ---")
        for t in tags:
            print("  %-20s ×%-5s 例: %s" % (t.get("tag"), t.get("n"), t.get("eg")))

    layers = dyn.get("layers") or []
    if layers:
        print("\n--- 关键 layer 的运行时编号 (地面探测用 Ground|SlopedGround) ---")
        for L in layers:
            idx = L.get("index")
            if idx is None or int(idx) < 0:
                print("  %-22s 未定义" % L.get("name"))
            else:
                print("  %-22s index=%-3s mask=0x%08X" % (
                    L.get("name"), idx, int(L.get("mask") or 0)))

    # ---- --watch: 地图一变就往末尾追加一份"变化切片" ----
    if "--watch" in sys.argv:
        i = sys.argv.index("--watch")
        iv = 2.0
        if i + 1 < len(sys.argv):
            try:
                iv = float(sys.argv[i + 1])
            except (TypeError, ValueError):
                pass
        watch_loop(b, st, dyn, interval=iv)
    return 0


if __name__ == "__main__":
    _md = None
    if "--md" in sys.argv:
        _i = sys.argv.index("--md")
        if _i + 1 < len(sys.argv) and not sys.argv[_i + 1].startswith("--"):
            _md = sys.argv[_i + 1]
            del sys.argv[_i:_i + 2]
        else:
            _md = os.path.join(os.path.expanduser("~"), "Desktop", "overcooked_map.md")
            del sys.argv[_i]
    if _md is None:
        sys.exit(main())

    # 捕获整份输出 → 写 Markdown, 同时照常打到终端(不改变任何现有行为)
    import io

    class _Tee:
        def __init__(self, *fs):
            self.fs = fs

        def write(self, s):
            for f in self.fs:
                f.write(s)

        def flush(self):
            for f in self.fs:
                f.flush()

    _buf = io.StringIO()
    _old = sys.stdout
    sys.stdout = _Tee(_old, _buf)
    # offset=None → 本次运行的第一次 _flush_md 会记下"这一节从哪开始"
    _MD["path"], _MD["buf"], _MD["offset"] = _md, _buf, None
    try:
        _rc = main()
    finally:
        sys.stdout = _old
    # ⚠ **只有这一次运行真的取到地图了才写文件**。
    #   原来是无条件写 —— 结果一次"游戏不在局"的运行(main 返回 1, 输出只有
    #   4 行错误信息)把桌面上 17KB 的好地图**覆盖成了 298 字节的错误信息**。
    #   覆盖别人的成果比不写更糟, 所以失败时宁可什么都不做并说清楚。
    if _rc == 0:
        _flush_md()
        try:
            print("\n[md] 已追加一节: %s (%d 字节)" % (_md, os.path.getsize(_md)))
        except Exception:
            pass
    else:
        # ⚠ 别不加检查就说"旧内容还在" —— 实测踩过: 文件其实已经被删掉了,
        #   工具却照样报"里面的旧内容还在", 等于**编了一句它没验证过的话**。
        #   有文件就说有, 没有就直说没有(并且说清下次会新建)。
        if os.path.exists(_md):
            print("\n[md] 这次没取到地图(退出码 %d), **没有动** %s —— 里面的旧内容还在"
                  % (_rc, _md))
        else:
            print("\n[md] 这次没取到地图(退出码 %d); 而且 %s **不存在** —— "
                  "下次跑成功时会**新建**一份(之前几节的内容已经不在了)"
                  % (_rc, _md))
    sys.exit(_rc)
