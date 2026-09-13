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
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from neko.bridge.client import BridgeClient, BridgeError   # noqa: E402
from neko.terrain import TerrainMap                        # noqa: E402
from neko.map_model import (KitchenMap, is_extinguisher,    # noqa: E402
                            is_pot, is_plate)

# 语义 → 台面分布图里的字母。地形图和台面图用**不同**的字母集, 免得两套含义打架
# (地形图里 'C' 是台面传送带格子, 台面图里 'C' 也只是同一个意思, 但 'S'/'W' 等只属于台面图)。
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

SEM_NAME = {
    "serve": "送餐口", "plates": "干净盘子堆", "dirty_plates": "脏盘子堆",
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
                line.append("@")
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
       V  空洞(没地面, 会掉下去)
       v  地板太低(单向落差 / 正在下沉的平台, 例如会沉的荷叶)
       C  台面传送带(走不上去, 而且放上去的东西会被传走)
       @  你指定的坐标所在格
"""

OVERLAY_LEGEND = """
叠加图: 地形字符之上再画**台面语义**和**厨师**(厨师优先级最高, 盖住台面)
       数字 1/2 = 厨师 (P1/P2);  字母 = 台面语义, 见 SEM_LETTER, 常用几个:
       S 送餐口  P 干净盘子堆  D 脏盘子堆  R 盘子回收  B 切菜板  K 灶台/锅
       c 普通台面  G 食材箱  W 洗手池  X 垃圾桶  M 搅拌  C 台面传送带  ! 危险物
       (台面上放着什么不在这张图里 —— 一格一个字符装不下, 看下面的内容清单)
"""


def to_markdown(text: str, title: str = "胡闹厨房 2 — 关卡地图") -> str:
    """把整份终端输出转成 Markdown。

    做法: 按 `---- 小节 ----` / `==== 小节 ====` 切段, 每段:
      · 标题行 → `## 小节名`
      · 其余(含 ASCII 图) → 放进代码块 —— 否则 Markdown 会把图里的
        `.` `#` 缩进吃掉, 或者把 `---` 当成水平线, 图就散了。
    """
    import datetime
    out = ["# " + title, "",
           "> 由 `tools/mapview.py --md` 生成于 %s" %
           datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""]
    cur_title = None
    buf = []

    def flush():
        if cur_title is None and not buf:
            return
        if cur_title:
            out.append("## " + cur_title)
            out.append("")
        if buf:
            out.append("```")
            out.extend(buf)
            out.append("```")
            out.append("")
        del buf[:]

    import re as _re
    for line in text.split("\n"):
        m = _re.match(r"^\s*(?:-{2,}|={2,})\s*(.+?)\s*(?:-{2,}|={2,})\s*$", line)
        if m:
            flush()
            cur_title = m.group(1)
        else:
            buf.append(line)
    flush()
    return "\n".join(out)


def overlay_ascii(tm, stations, chefs=(), mark=None) -> str:
    """把**台面层**叠到地形网格上 —— 一张图看清"哪一格是什么"。

    为什么需要: 地形图里所有台面都是 `#`(都走不上去), 分不出灶台/切菜板/盘子堆。
    这张图把语义字母画上去, 厨师画成 1/2(优先级最高, 盖住台面)。
    台面上**放着什么**不在这张图里 —— 一格只能画一个字符装不下, 见下面的内容清单。
    """
    over = {}
    for s in stations or []:
        try:
            cell = tm.cell_of(float(s.get("x") or 0), float(s.get("z") or 0))
        except (TypeError, ValueError):
            continue
        over.setdefault(cell, SEM_LETTER.get(_sem_of(s), "?"))
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
                line.append("@")
            elif (i, j) in over:
                line.append(over[(i, j)])
            else:
                line.append(tm.at(i, j))
        rows.append("".join(line))
    return "\n".join(rows)


#: 台面上那件东西按 **Unity Tag** 分类 —— 游戏自己就是这么分的
#: (GameUtils.cs:504-707 那一整套查找器全是"按 tag 找 + 按组件筛")。
#: 字母用来说明"这一格上放的是什么", 和 SEM_LETTER(台面本身是什么)是两套。
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
            over[tm.cell_of(float(c.get("x") or 0), float(c.get("z") or 0))] = "H"
        except (TypeError, ValueError):
            pass
    rows = []
    for j in range(tm.h - 1, -1, -1):
        line = []
        for i in range(tm.w):
            if mark == (i, j):
                line.append("@")
            elif (i, j) in over:
                line.append(over[(i, j)])
            elif tm.is_danger(i, j):
                line.append(tm.at(i, j))       # 危险区照画, 免得看不出边界
            else:
                line.append(" ")
        rows.append("".join(line))
    return "\n".join(rows)


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

    print()
    print("网格 %dx%d  半步(%d,%d)  原点(%.2f,%.2f)  步长(%.3f,%.3f)  地面高度 %.2f  规则=%s  活跃网格数=%s" % (
        tm.w, tm.h, tm.hx, tm.hz, tm.ox, tm.oz, tm.cellx, tm.cellz, tm.floor_y, tm.regular,
        data.get("grids")))
    if int(data.get("grids") or 1) > 1:
        print("⚠ 这一关有多个网格管理器, 网格内容可能不完整 —— 需要人工确认")
    print("格子统计:", tm.describe_dangers())
    print()

    cx, cz = (at if at else (None, None))
    stations = (st.get("layout") or {}).get("stations") or []
    chefs = (st.get("layout") or {}).get("chefs") or []

    print("---- 地形图 (原始网格) ----")
    print(tm.ascii(cx, cz))
    print(LEGEND)

    print("\n---- 叠加图 (地形 + 台面语义 + 厨师) ----")
    print(overlay_ascii(tm, stations, chefs, tm.cell_of(cx, cz) if at else None))
    print(OVERLAY_LEGEND)

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
            reach = tm.reachable_from(px, pz)
            print("\n---- 从厨师(id=%s)出发**真正到得了**的区域 (空白 = 到不了) ----" % chefs[0].get("id"))
            print(tm.ascii_reach(px, pz))
            print("可走格 %d 个, 其中从厨师出发到得了的 %d 个" % (
                sum(1 for j in range(tm.h) for i in range(tm.w) if tm.walkable(i, j)),
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
    try:
        _rc = main()
    finally:
        sys.stdout = _old
    try:
        with open(_md, "w", encoding="utf-8") as _f:
            _f.write(to_markdown(_buf.getvalue()))
        print("\n[md] 已写出: %s (%d 字节)" % (_md, os.path.getsize(_md)))
    except Exception as _e:
        print("\n[md] 写出失败:", _e)
    sys.exit(_rc)
