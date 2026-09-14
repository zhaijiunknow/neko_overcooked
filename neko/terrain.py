"""关卡地形: 用游戏自己的网格做寻路, 并且**知道哪里是水面/岩浆/空洞**。

为什么需要它(实测教训):
  游戏原生 `GridNavSpace.FindPath` 的可走判定只有一条 ——
  `m_nodeMap[x,z] = (GetGridOccupant(index) == null)`, 也就是"这格没有占用物"。
  而水面、岩浆、边界深渊是 **RespawnCollider 触发器**, 它们根本不占格子。
  ⇒ 原生寻路会把水面当成可走格, 直接横穿过去, 厨师当场淹死。

插件侧 (`LevelInfo.cs`) 把整张网格读出来, 每格一个字符:
  '.' 可走        '#' 被墙/橱柜/台面占住
  'F' 火焰危险物(占格且走不了)   'P' 移动平台(能站, 会动)
  'T' 传送带(能站, 会推着走)
  'H' 危险区(水面/岩浆/边界墙 —— 空着, 但踩上去会死)
  'V' 空洞(空着, 但脚下没地面, 会掉下去)
  'v' 地板太低(单向落差 / **正在下沉的平台**, 例如会沉的荷叶)

关于 'v' —— 这是最容易骗过脚本的一种:
  "会消失的平台"**不是被销毁的**。以 DLC13 的荷叶为例, 全工程没有 LilyPad/Lotus 类,
  唯一证据是成对音效 DLC_13_LilyPad_*_Plunge / _Pop (GameOneShotAudioTag.cs:317-321),
  说明它踩下去会沉、之后还会浮回来, 物理上就是**碰撞体跟着下沉动画走低**。
  于是对格子打向下射线**全程都有命中** —— 只判"有没有命中"的探测会一路认为这格能走,
  直到最后一刻才发现, 而游戏给的反应窗口只有 m_timeBeforeFalling = 0.2s
  (PlayerControls.cs:270)。
  所以插件侧改判"命中点比参考地面低过 StepHeightMax(0.65) 就算不可走"
  (PlayerControls.cs:50), 并单独标成 'v' 而不是混进 'V', 日志里一眼能看出是哪一种。

本模块只做三件事: 读这张图、按世界坐标定位格子、在**安全格**上跑 A*。
"""

from __future__ import annotations

import heapq
from collections import deque

# 能站的格子
CH_FREE = "."
CH_PLATFORM = "P"
CH_TRAVELATOR = "T"
# 绝对不能踏进去的格子
CH_BLOCKED = "#"      # 占用物
CH_FIRE = "F"         # 火焰
CH_HAZARD = "H"       # 水面 / 岩浆 / 边界
CH_VOID = "V"         # 空洞
CH_VOID_LOW = "v"     # 地板太低(单向落差 / 正在下沉的平台) —— **解析用**, 见下

#: **"站不下"的符号** —— 不来自 C#, 是画图时按厨师高度现算的
#: (见 `blocked_by_height`)。
#:
#: 用户定的: **太低和太高都用 `V`**(和空洞同一个符号)。
#: 理由: 三者对看图的人来说是同一件事 —— **这格过不去**;
#: 再细分符号只会让图多出几种记号, 反而难认。
#: (`'v'`(`CH_VOID_LOW`) 仍当**解析**语义留在 `DANGER_CHARS` 里, 别混。)
#: ⚠ 代价: 图上**分不出**"这格是空洞"和"这格是别层的平台" ——
#:   要区分得看 `TerrainMap.cell_floor_y()` 的数值。
CH_TOO_LOW = "V"      # 太低: 底下基本是空的/别层, 站不下
CH_TOO_HIGH = "V"     # 太高: 爬不上去, 同样站不下

#: **"到不了"的符号** —— 可走、但**泛洪没到**。
#:
#: ⚠ **原来它和空洞共用 `V`, 那是个真坑**(2026-09-14 用户连着两次被它误导):
#:   `s_wizard_5_5` 那关的可达集会从 42 摆到 78(两条转梯轮流当桥), 于是
#:   **同一张地形、同一份字符网格, 图面一会儿是一大片房间、一会儿只剩个十字** ——
#:   看上去像"地图没重构"或"哪儿都到不了", 其实只是可达集变了。
#:   而且 `gridwatch` 打的是**原始 `tm.grid`**(不走 `_mark`), 于是同一个房间
#:   在两个工具里长得完全不一样, 更说不清。
#:
#:   所以**"到不了"必须有自己的记号**: `V` 留给真空洞(和"站不下"),
#:   到不了用 `-` —— 一眼就能看出"这格地形上是地板, 只是从厨师这儿过不去"。
CH_UNREACHED = "-"
CH_MARK = "@"         # 图里标出的"你指定的那个坐标"
#: **动态格**: 监视期间"变过"的格子(地形字符或地板高度变过)。
#: 一张静态图没法表达"这格有时在有时不在", 所以标出来 ——
#: 否则限时平台/升降台看上去就是**常驻**, 而那是会骗人的。
CH_DYNAMIC = "~"

#: 物理阻挡: 不占格子、但有碰撞体挡路(装饰物/栏杆/花坛/街景)。
#: **这是"寻路说能走、厨师却撞墙"的根源** —— 原来只看"有没有格子占用物",
#: 这些一律被当空气。现在插件用厨师自己的胶囊半径做 OverlapSphere 检测出来。
CH_PHYS = "x"

DANGER_CHARS = CH_FIRE + CH_HAZARD + CH_VOID + CH_VOID_LOW

#: 游戏自己的**跨步吸附**阈值 —— 依据(反编译) `PlayerControls.StepHeightMax = 0.65`
#: (`PlayerControls.cs:50`), 用在 `ClientPlayerControlsImpl_Default.cs:889`:
#:   移动时垂直差小于它就把厨师直接挪到位;**超过它只是"不吸附"**。
#:
#: ⚠ **我们并不拿它当"走不走得过去"的判据**(原来当判据用, 是错的) ——
#:   它只是"吸附/不吸附"的分界。真正的可走性用下面的 `HEIGHT_TOLERANCE`。
#:   留着这个常量是为了把出处记下来, 别再有人拿它去判墙。
STEP_HEIGHT_MAX = 0.65

#: **真正用来判"迈不迈得过去"的跨步阈值** = 1.00。
#:
#: 为什么不是游戏那个 0.65(用户定的, 实测逼出来的):
#:   `StepHeightMax = 0.65` 是**跨步吸附**的分界 —— 超了只是"不吸附", 人不一定过不去。
#:   实测这个游戏里**能走的台阶比 0.65 高**:
#:     · 会转的阶梯: 0.38 / 1.18 / 1.88 —— **每级 0.80**
#:     · 坡道: 0.00 → 0.39 → 1.09 → 1.85 —— 每级 **0.70 / 0.76**
#:   这些在游戏里**都走得上**, 用 0.65 会把它们整条判成墙
#:   (用户连续两轮报"台面左右那几格该能走却标成 V")。
#:
#: ⚠ **0.85 → 1.00(2026-09-14, 用户实测指出; 原来是 0.85)**:
#:   `s_wizard_5_5` 有**两条镜像的转梯**。东梯在 `0.38/1.18/1.88` 时每级 0.80, 是通的;
#:   而**西梯在 `1.88/1.31/0.38` 时中间那一级是 0.93** —— 0.85 过不去, 于是
#:   "地面 → 西梯 → 左平台 → 传送门 → 右半边"整条链子断在这一跳上(用户指出)。
#:   两条梯子**反相**, 处在转动周期的不同角度; 转梯各级在**竖直方向上的投影**
#:   随角度变 ⇒ 0.93 是那个角度的真实读数, 不是测错。
#:
#:   拿这一关 69 帧实机落帧扫过一遍: **0.95~1.10 是一整段平台, 行为完全一致** ——
#:     · 西梯中间/最高进可达集:  6/7 帧 → 27/28 帧
#:     · 左平台门/右门/右半边:   39 帧  → 60 帧
#:     · **沉下去的那条梯子(东梯)在每一个取值下都纹丝不动(38/68 帧)** ⇒ 没有误放
#:   取 1.00 是这段平台的中点, 给"别的关可能有稍大的台阶"留余量。
#:
#: ⚠ 1.00 仍然拦得住**真该拦的**: 平台沉下去是 **1.65 / 7.58** 那种数量级, 离得远。
#:   所以一个数就够, 不需要"坡道豁免"那种特判。
STEP_MAX = 1.00

#: **默认高度容差** —— 只在"这一关算不出层间距"时才用(见 `_derive_height_tol`)。
#:
#: ⚠ 正常路径**不是**用它: 每张 `TerrainMap` 会从**自己这一关**的地板高度分布
#:   算出 `tm.htol`。原因(用户指出: "每一关的高度不一样啊, 这里就是有问题"):
#:   实测各关层间距根本不是一个量级 ——
#:     `s_space_6_2` 1.56/2.35/2.40、`s_wizard_school_3_4` 1.84、`s_wizard_5_5` 2.00。
#:   一个全局常量套所有关, 必然有的封死有的漏(踩过两次: 拍 ±2 把 2.40 那层封死,
#:   用户当场指出"把上方封死了, 但实际上是联通的")。
#:
#: 为什么不用游戏自己的 `StepHeightMax=0.65`: 那只是**跨步吸附**的分界
#: (`ClientPlayerControlsImpl_Default.cs:889`), 超了只是"不吸附", 不等于过不去。
#:
#: ⚠ 残留的近似(如实记着): 两层平台常常靠**别处的楼梯/坡道**连通, 而逐格比高度
#:   看不见坡道 —— 落差算出来很大时仍会误封。根治要靠"游戏告诉我们这格有没有真地板"
#:   (C# 侧 `lowHits` 诊断正在往那个方向走)。
DEFAULT_HEIGHT_TOLERANCE = 3.0
#: 兼容旧名(外部还有引用)。
HEIGHT_TOLERANCE = DEFAULT_HEIGHT_TOLERANCE
#: 兼容旧名(外部还有引用): 语义等同 `HEIGHT_TOLERANCE`。
DROP_DOWN_MAX = HEIGHT_TOLERANCE


def height_ok(fy, at_y, tol: float = None) -> bool:
    """地板高度 `fy` 对**站在 `at_y` 的厨师**来说, 这一步迈不迈得过去。

    **唯一权威规则**: 落差在 `±tol` 以内就算走得了。

    `tol` 应该传**这一关自己的** `TerrainMap.htol`(由本关地板分布算出) ——
    不传才退回 `DEFAULT_HEIGHT_TOLERANCE`。**别写死数字**: 各关层间距不是一个量级
    (`s_space_6_2` 2.40 vs `s_wizard_school_3_4` 1.84), 写死必然有的封死有的漏。

    抽成模块函数是为了让所有调用方共用一条 —— 否则 `tools/platformcheck.py`
    之类的地方会各写各的判法, 迟早和引擎不一致(实测踩过: 那边 4 处都在用
    旧的对称 `abs(diff) <= 0.65`)。

    `fy is None`(老 dll / 这格没地面数据) → True: 不因此否掉。
    """
    if fy is None or at_y is None:
        return True
    return abs(float(fy) - float(at_y)) <= (DEFAULT_HEIGHT_TOLERANCE if tol is None else tol)


class TerrainMap:
    """一张关卡网格。由 `bridge.get_map()` 的返回构造。"""

    def __init__(self, data: dict):
        data = data or {}
        self.error = data.get("error")
        self.w = int(data.get("w") or 0)
        self.h = int(data.get("h") or 0)
        self.hx = int(data.get("hx") or 0)
        self.hz = int(data.get("hz") or 0)
        self.ox = float(data.get("ox") or 0.0)
        self.oz = float(data.get("oz") or 0.0)
        self.cellx = float(data.get("cellx") or 1.2) or 1.2
        self.cellz = float(data.get("cellz") or 1.2) or 1.2
        #: C# 报的"整图参考高度"。☠ **地雷, 别拿它做任何判定**(用户指出, 未拆):
        #: 它的源头是**玩家位置** —— "所有厨师当前 y 的最大值", 所以**厨师一换层就跳**
        #: (实测: 走一趟传送门就可能从 -1.84 跳到 0.00)。
        #: C# 那边它喂着**射线起点窗口**和**危险区同层判定**(`CollectHazards`),
        #: 所以一次传送可能改掉整张图的危险区归类 —— 与"全局量 + 取样不确定"同一个病。
        #: 本文件里**没有任何判定用它**: 每格高度走 `cell_floor_y()`,
        #: `_`/`^` 走 `walkable(at_y=)`(一律用厨师自己的 y), 都按格/按厨师取。
        #: 留着它只是为了诊断打印(让你能看见它跳)。
        self.floor_y = float(data.get("floorY") or 0.0)
        self.regular = bool(data.get("regular"))
        #: C# 报的**活跃网格管理器个数**(`GridManager.GetActiveCount()`)。
        #: 为什么要留着: 多网格关卡里"主网格"取的是 `GetActive(0)`, 而活跃集合
        #: **可能随关卡子区域激活/停用而变** —— 一旦它变了, 统一网格的参照系
        #: (原点/步长/覆盖范围)就跟着变, 整张图会大面积"搬家"。
        #: 那种变化**不是关卡几何在动**, 别混为一谈(见 tools/gridwatch.py)。
        self.grids = int(data.get("grids") or 0)
        #: **地形版本号** —— C# 对"字符网格 + 每格地板高度"算的 FNV-1a。
        #: 它是判断"可走性变没变"的**便宜判据**: 字符一格不改、只有高度在动
        #: (限时平台升降就是), 只看 grid 会以为没事 —— 实测踩过。
        #: 老 dll 没这个字段 → 空串, 此时调用方应退化成"当它可能变"。
        self.ver = data.get("ver") or ""
        self.grid = data.get("grid") or ""
        #: 每格的**地面高度**(和 grid 同长; None = 没地面)。
        #: 反编译标定: 跨步上限 `PlayerControls.StepHeightMax = 0.65` —— 落差超过它就走不过去。
        #: 这就是"多平台关卡"的判据: 另一层的地板对自己的厨师来说就是墙。
        self.floors = data.get("floors") or []
        self.hazards = data.get("hazards") or []
        #: 本关的**站不下容差** —— 由这一关自己的地板高度分布算出来, 不是全局常量。
        #: 见 `_derive_height_tol()`。
        self.htol, self.htol_note = self._derive_height_tol()
        #: 每格的**地板会不会动** `"0101..."`(和 grid 等长; 空 = 老 dll 没有)。
        #: **会动 = 平台**: 它沉下去时**是一个洞**(踩上去掉进去) ——
        #: 用户实测给的反例: `s_space_6_2` 那 29 格 `-1.65 → 0.00`,
        #: **浮上来才能走**, 可容差 4.80 把它们当成能走。
        #: 而静止的下层地板(`-2.40` 那种)只是一个"层", 站上去照样走。
        #: ⚠ **这两者从高度上分不开**: 1.65(该拦) 比 2.40(该放) 还小 ——
        #:   所以"调阈值"这条路是死的(试过 2.0 / 3.0 / 自适应 4.80, 都不对)。
        self.movable = data.get("movableFloor") or ""
        #: 诊断: 每格**全层射线第一条命中在哪一层**(base36 一个字符; `-` = 没打到)。
        #: 用来回答"水面能不能被射线打到" —— 见 `layer_stats()`。
        self.probe_layer = data.get("probeLayer") or ""
        #: 诊断: 每格**全层射线打中的物体是不是危险面**(有 `RespawnCollider`)。
        #: 这是"水面能不能用射线判"的**决定性判据** —— 层名判不了(实测水面在 Default 层),
        #: 而 `RespawnCollider` 正是游戏自己标"碰到就死"的组件。
        self.probe_haz = data.get("probeHaz") or ""
        #: 诊断: 每格全层射线打中的**物体名** —— 名字表 + 每格索引。
        #: 为什么要有: "打中的是不是危险面"只能回答"是不是", 回答不了"那到底是什么" ——
        #: 而实测 `'H'`(水面) 报 0/49 时, 唯一能推进的就是**看名字**。
        self.probe_names = data.get("probeNames") or []
        self.probe_idx = data.get("probeIdx") or ""
        #: 诊断: 每格**全层射线打中的危险面的 `RespawnType`**(一个字符):
        #: `-` 无 / `D` Drowning(水) / `F` FallDeath(深渊) / `H` Hit / `C` Car。
        #: 这是"水"和"空洞"的**唯一客观区别** —— 两者都必须拦, 但标出来的意思不同。
        self.probe_rt = data.get("probeRT") or ""
        #: 每格**地面射线命中层**: `S` = SlopedGround(坡道) / `G` = 别的层 / `-` = 没打到。
        #: 用来让**坡道豁免跨步限制** —— 见 `step_ok`。
        self.ground_kind = data.get("groundKind") or ""
        #: 诊断: **地板最低那批格子, 向下射线到底打中了什么**
        #: (`[{what, cells, y}]`)。来自 C#, 只采集不下结论 ——
        #: 用来回答"平台收起来时底下是不是真地板"(靠高度答不了这个问题)。
        self.low_hits = data.get("lowHits") or []
        self.counts = data.get("counts") or {}
        self.phys_names = data.get("physNames") or []

    def first_hit_layer(self, i: int, j: int) -> int:
        """这一格**全层射线**第一条命中在哪一层(-1 = 没打到 / 老 dll 没这数据)。"""
        n = j * self.w + i
        if not (0 <= n < len(self.probe_layer)):
            return -1
        c = self.probe_layer[n]
        if c == "-":
            return -1
        v = "0123456789abcdefghijklmnopqrstuvwxyz".find(c)
        return v

    def is_slope(self, i: int, j: int) -> bool:
        """这一格的地面是不是**坡道**(`SlopedGround`)。"""
        n = j * self.w + i
        return 0 <= n < len(self.ground_kind) and self.ground_kind[n] == "S"

    def respawn_type(self, i: int, j: int) -> str:
        """这一格底下危险面的 `RespawnType` 代码('' = 没数据/不危险)。"""
        n = j * self.w + i
        if not (0 <= n < len(self.probe_rt)):
            return ""
        c = self.probe_rt[n]
        return "" if c == "-" else c

    def probe_name(self, i: int, j: int) -> str:
        """全层射线打中的物体名('' = 没打到 / 老 dll)。"""
        n = j * self.w + i
        if not (0 <= n < len(self.probe_idx)):
            return ""
        c = self.probe_idx[n]
        if c == "-":
            return ""
        k = "0123456789abcdefghijklmnopqrstuvwxyz".find(c)
        return self.probe_names[k] if 0 <= k < len(self.probe_names) else ""

    def is_probe_hazard(self, i: int, j: int) -> bool:
        """全层射线打中的是不是危险面(有 RespawnCollider)。老 dll → False。"""
        n = j * self.w + i
        return 0 <= n < len(self.probe_haz) and self.probe_haz[n] == "1"

    def layer_stats(self, layers: dict) -> list:
        """按**格类型**统计"全层射线第一条打在哪一层" —— 回答"水面能不能被射线打到"。

        `layers` = {层号: 层名}(从插件的 dyn 里拿)。
        用法: 看 `'H'`(危险区)那几格打中的是什么 —— 如果就是 `Water` 层,
        那水面**可以**用逐格射线判, 现在那套"范围投影 + 吃 floorY 的同层判定"
        就能整块拆掉。
        """
        from collections import Counter
        per = {}
        for j in range(self.h):
            for i in range(self.w):
                per.setdefault(self.at(i, j), Counter())[
                    layers.get(self.first_hit_layer(i, j), "?")] += 1
        out = []
        for ch in sorted(per):
            tot = sum(per[ch].values())
            top = per[ch].most_common(3)
            # 这批格子里,"全层射线打中的是危险面"的占比 —— **决定性的那个数**:
            #   'H' 那行若是 100%, 说明"逐格射线 + RespawnCollider"完全认得出水面,
            #   就能替掉现在那套范围投影(它吃着 floorY 那颗地雷)。
            haz = sum(1 for j in range(self.h) for i in range(self.w)
                      if self.at(i, j) == ch and self.is_probe_hazard(i, j))
            rts = Counter(self.respawn_type(i, j)
                          for j in range(self.h) for i in range(self.w)
                          if self.at(i, j) == ch and self.respawn_type(i, j))
            names = Counter(self.probe_name(i, j)
                            for j in range(self.h) for i in range(self.w)
                            if self.at(i, j) == ch and self.probe_name(i, j))
            out.append((ch, tot, top, haz, names.most_common(3), rts.most_common(4)))
        return out

    def is_movable(self, i: int, j: int) -> bool:
        """这一格的地板**会不会动**(会动 = 移动平台)。老 dll / 数据缺失 → False。"""
        n = j * self.w + i
        if 0 <= n < len(self.movable):
            return self.movable[n] == "1"
        return False

    def stand_blocked(self, i: int, j: int, at_y: float = None) -> bool:
        """⚠ **已不是可走性判据** —— 保留只为诊断打印。

        可走性现在按**边**判(`step_ok`), 不再比"这一格 vs 厨师所在层"。
        见 `step_ok` 的注释: 那种比法在 1.65(该拦) / 2.40(该放) 上必然错一头。


        **只有一条规则**(用户实测的反例逼出来的):
          · 地板**会动**(移动平台) → 落差超游戏自己的跨步上限 `0.65` 就算洞。
            实测 `-1.65` 时不能走(沉下去是个洞)、浮到 `0.00` 才能走。
          · 地板**静止** → **不做高度过滤**。"那是另一层"能不能过去是**连通性**问题,
            交给 BFS(坡道/楼梯的中间高度会让洪泛自然爬上去), 不在这儿猜。

        ⚠ 为什么静止的不能过滤: 实测 1.65(该拦, 会动) **比** 2.40(该放, 静止) **还小** ——
          大的放行、小的封死, **不单调**, 任何单一阈值都必然有一头是错的。
          前面试过 2.0(封死 2.40)、3.0 / 自适应 4.80(放行 1.65 那个洞), 全都不对 ——
          **不是数没调好, 是拿高度差当判据这件事本身不成立**。
        """
        if at_y is None:
            return False
        fy = self.cell_floor_y(i, j)
        if fy is None:
            return False
        # **只对"会动的地板"做高度过滤** —— 静止地板**一律不做**。
        #
        # 为什么静止的不做(用户实测逼出来的, 这一步推翻了前面好几轮):
        #   我们一路在调"静止地板的容差", 而每个反例都指向同一件事 ——
        #     · 差 2.40 的静止下层  → 该放行(用户: "实际上是联通的")
        #     · 差 1.65 的**会动**平台 → 该拦(用户: "上浮之后才是能走的")
        #     1.65 < 2.40 却一个拦一个放 ⇒ **高度差本身不是判据**。
        #   静止的落差只说明"那是另一层" —— 能不能过去是**连通性**问题,
        #   交给 BFS/游戏物理去算(坡道/楼梯会产生中间高度, 洪泛自然爬得上去),
        #   不该在这儿用一个局部阈值猜。
        #   而"会动的平台沉下去"是**真的洞**(踩上去掉下去) —— 那个必须拦,
        #   且它跟高度差是单调的(沉得越多越肯定是洞), 用游戏自己的跨步上限 0.65。
        if self.is_movable(i, j):
            return abs(float(fy) - float(at_y)) > STEP_HEIGHT_MAX
        return False

    def _derive_height_tol(self) -> tuple:
        """从**这一关自己的**地板高度分布推出"站不下"的容差。返回 `(容差, 说明)`。

        为什么不能用一个全局常量(用户指出: "每一关的高度不一样啊, 这里就是有问题"):
          实测各关的层间距根本不是一个量级 ——
            `s_space_6_2` 是 1.56 / 2.35 / 2.40, `s_wizard_school_3_4` 是 1.84,
            `s_wizard_5_5` 是 2.00。拿一个数字套所有关, 必然**有的封死有的漏**
            (踩过两次: 拍 ±2 把 2.40 那层整片封死, 用户当场指出"实际上是联通的")。
          更根本的: 两层平台常常不是靠**直接迈**, 而是别处有**楼梯/坡道**,
          所以"逐格比高度"本来就只是个近似 —— 至少让阈值跟着关卡走。

        **取法**: 用**格数最多的两个标高**的间距 × 2。
          为什么是"最多的两个"而不是全部标高: 平台收起来时那批格子只是**少数**,
          把它们算进去会把间距算飞(会算出 9 格的"层间距", 于是什么都不拦);
          而"格数最多的两个标高"就是这一关**主要的两个平台层**。

        只有一层(或数据不足) → 用默认值: 这种关卡没有"跨层"可言,
        高度判定只在有东西偏离常态时才有意义。
        """
        from collections import Counter
        c = Counter()
        for f in (self.floors or []):
            if f is not None:
                c[round(float(f), 2)] += 1
        top = c.most_common(2)
        if len(top) >= 2 and top[1][1] >= max(4, top[0][1] * 0.05):
            gap = abs(float(top[0][0]) - float(top[1][0]))
            if gap > 0.05:
                # 上限 6.0 是**兜底**, 不是随手拍的:
                #   当"收起来那批格子"恰好比真平台层还多时, `most_common(2)` 会把
                #   收起来后的那个深度(实测 -9.08)当成"第二层", 于是算出 9.08 的
                #   "层间距"、容差飞到 18 —— 那就什么都不拦了。
                #   而实测**最小的致命落差是 7.57**(平台收起来掉进空里),
                #   所以上限必须压在这之下。6.0 留了余量。
                tol = min(6.0, max(1.5, 2.0 * gap))
                return tol, ("2 × 本关层间距 %.2f (格数最多的两个标高 %.2f×%d / %.2f×%d)"
                             % (gap, top[0][0], top[0][1], top[1][0], top[1][1]))
        return DEFAULT_HEIGHT_TOLERANCE, "本关只有一层(或数据不足), 用默认值"

    def low_floor_detail(self) -> list:
        """**可疑的低地板**: 比"格数最多的那个标高"低出**本关容差**的那批, 返回它们的命中物。

        为什么要这道过滤: C# 那份 `lowHits` 是**无规则采集** —— 它只找"最低的那批格子",
        而那**通常就是主地板本身**(实测 `s_wizard_3_3`: 最低标高 0.00, 就是主层),
        于是每关都白报一遍。真正要看的是"**低得离谱**"那种 —— 那是"这底下不是地板"
        的候选(平台收起来 / 掉进关底)。

        判断放 Python 侧, 因为**容差是这儿算的**(`htol`), C# 不知道它。
        """
        if not self.low_hits or not self.floors:
            return []
        from collections import Counter
        c = Counter()
        for f in self.floors:
            if f is not None:
                c[round(float(f), 2)] += 1
        if not c:
            return []
        main = c.most_common(1)[0][0]          # 主层标高
        out = []
        for h in self.low_hits:
            try:
                y = float(h.get("y"))
            except (TypeError, ValueError):
                continue
            if (main - y) > self.htol:         # 比主层低出容差 → 才是可疑的
                out.append(h)
        return out

    @property
    def ok(self) -> bool:
        return (not self.error and self.w > 0 and self.h > 0
                and len(self.grid) == self.w * self.h)

    # ---------------------------------------------------------------- 坐标
    def cell_of(self, x: float, z: float) -> tuple:
        """世界坐标 → 格子下标 (i, j)。i 沿 x, j 沿 z。"""
        i = int(round((x - self.ox) / self.cellx))
        j = int(round((z - self.oz) / self.cellz))
        return i, j

    def world_of(self, i: int, j: int) -> tuple:
        """格子下标 → 格心世界坐标 (x, z)。"""
        return self.ox + i * self.cellx, self.oz + j * self.cellz

    def inside(self, i: int, j: int) -> bool:
        return 0 <= i < self.w and 0 <= j < self.h

    def at(self, i: int, j: int) -> str:
        if not self.inside(i, j):
            return CH_BLOCKED
        return self.grid[j * self.w + i]

    def at_world(self, x: float, z: float) -> str:
        return self.at(*self.cell_of(x, z))

    # ---------------------------------------------------------------- 判定
    def cell_floor_y(self, i: int, j: int):
        """这一格的**地面高度**; 没地面返回 None。

        ⚠ 名字带 `cell_`: 类里已经有个属性 `self.floor_y`(整张图的参考高度,
           来自 C# 的 `floorY`)。同名的话属性会把方法盖掉 —— 实测踩过
           (`TypeError: 'float' object is not callable`)。"""
        n = j * self.w + i
        if 0 <= n < len(self.floors):
            return self.floors[n]
        return None

    def walkable(self, i: int, j: int, allow_platform: bool = True,
                 allow_travelator: bool = True, at_y: float = None) -> bool:
        """**这一格本身能不能站** —— 只看格子字符, **不看高度**。

        高度是**边**的属性: "能不能从邻格迈过来"由 `step_ok()` 判,
        泛洪(见 `reachable_from`)和 A*(见 `find_path`)逐边调用它。

        ⚠ `at_y` 是**旧签名留下的**, 现在**不用**(留着是为了不惊动一堆调用方)。
          旧模型是"拿这一格的高度和厨师所在层比", 那套在实测里必然错一头:
            1.65(会动的平台沉下去, 该拦) 比 2.40(静止的另一层, 该放) **还小** ——
            大的放行、小的封死, 不单调, 任何阈值都必有一头错。见 `step_ok`。

        ⚠ **遥感(遥控驾驶)的那块平台不在这个函数的知识范围内**: 它**不占格子**
          (实测 `MovingPlatform5` 报 `平台0`), 所以地形图上没有它 —— 它停在哪、
          能不能当桥, `walkable` 一概不知。判"某几格能不能靠它变可达"要用
          `bridge_cells()`(见那里的注释)。
        """
        ch = self.at(i, j)
        return (ch == CH_FREE
                or (ch == CH_PLATFORM and allow_platform)
                or (ch == CH_TRAVELATOR and allow_travelator))

    def _mark(self, i: int, j: int, at_y: float = None, reach: set = None,
              dynamic: set = None, conv: dict = None) -> str:
        """这一格在图上画什么 —— **泛洪结果优先**。

        `conv`: `{格: 箭头字符}` —— **传送带的方向**(见 `conveyor_arrows` 那边的说明)。
          两套传送带推的东西不一样, 但都该看得出**往哪边推** ——
          否则图上一个 `T` / `C` 只说了"这是传送带", 没说方向, 等于白标。

        为什么以泛洪为准(用户要求: "泛洪也应该用来做地形图和叠加图"):
          静态投影(`blocked_by_height`)只答"有没有邻居能迈进来", 答不了
          "**这一片和厨师连不连通**" —— 水对面那片地每格都迈得进来, 可厨师过不去。
          能到得了哪本来就是**连通性**问题, 只有泛洪能答。
        没给 `reach`(比如工具手上没有厨师位置)时, 退回静态投影。
        """
        if dynamic is not None and (i, j) in dynamic:
            return CH_DYNAMIC              # 监视期间变过 → 它不是"常驻"
        # **传送带方向优先于字符**(`T`/`C` 只说"这是传送带", 没说往哪边推 = 白标);
        # 但**不优先于"到不了"** —— 可达图里那格过不去就该显示 `-`,
        # 否则"能不能过去"被方向箭头盖掉了。
        if conv:
            _a = conv.get((i, j))
            if _a:
                if reach is None or (i, j) in reach:
                    return _a
                return CH_UNREACHED
        ch = self.at(i, j)
        if ch not in (CH_FREE, CH_PLATFORM, CH_TRAVELATOR):
            return ch                      # 危险/占用/空洞: 原样画, 别拿 V 盖掉
        if reach is not None and (i, j) not in reach:
            # **到不了** —— 用 `-`, **不要用 `V`**: `V` 是"空洞/站不下"的意思,
            # 两者共用一个字符时, 可达集一摆(见 `CH_UNREACHED` 的注释)图面就会
            # 整个变样, 看上去像地图坏了。
            return CH_UNREACHED
        return self.blocked_by_height(i, j, at_y) or ch

    def blocked_by_height(self, i: int, j: int, at_y: float = None) -> str:
        """这一格**字符上能走, 但没有任何邻居能一步迈进来**时, 返回该显示的字符。

        **静态投影**(真正的可达性看泛洪/可达图)。判据见函数末尾 ——
        它和 `step_ok` 是同一套"边"规则, 只是把"从哪一格迈进来"放宽成"任意邻居"。

        为什么必须能从字符上"看出来"(用户实测指出):
          限时平台**收起来**的时候, 格子的字符**一格都不变** —— 它只是变低了
          (实测 s_wizard_school_3_4: 12 格从 -1.51/-1.00 掉到 -9.08, 字符全是 '.')。
          而 `ascii()` / `overlay_ascii()` 只画字符 ⇒ **图上仍然显示"这里能走"**。
          那是最危险的一种错: 图在替限时构件打包票, 人就放心走进去。
          (`TerrainMap.walkable(at_y=)` 一直懂这件事, 只是画图时没把高度传下来。)

        顺带: `'v'` 这个字符从 C# 侧不再产生之后**一直没有生产者**(等于死字符),
        现在这类"站不下"的格子有了诚实的来源 —— 渲染时按高度判。
        渲染符号用 `_`/`^`(`CH_TOO_LOW`/`CH_TOO_HIGH`) ——
        `'v'` 和空洞 `'V'` 只差大小写, 图上一眼分不清(用户指出)。
        """
        # ⚠ 这里**不再需要 `at_y`** —— 标记是"没有任何邻居能迈进来"这个**静态**属性,
        #   和厨师站在哪一层无关(改成边模型之后, 高度只在边上有意义)。
        #   原来那句 `if at_y is None: return ""` 是旧模型的守卫, 留着会让
        #   `ascii()` 不传 at_y 时**整张图一个标记都不打**(实测踩过)。
        ch = self.at(i, j)
        if ch not in (CH_FREE, CH_PLATFORM, CH_TRAVELATOR):
            return ""
        fy = self.cell_floor_y(i, j)
        if fy is None:
            return ""
        # ⚠ **必须走和 `walkable` 同一个入口** `stand_blocked` ——
        #   这里原来自己判(用硬编码的 HEIGHT_TOLERANCE), 而 walkable 已经改成
        #   按本关容差 + "会动的地板"两套规则 ⇒ **两边判得不一样**:
        #   图上说能走、寻路说不能(或反过来), 就是用户报的"地图不对"。
        #   显示和判定只能有一条规则, 分开写迟早会漂。
        # **静态投影**: "从任何一个可走的邻居, 能不能一步迈进来"。
        # 这正是"边"模型的图面表达 —— 沉下去的平台: 邻居在 0.00、它在 -1.65,
        # 落差 1.65 > 步长 → 谁都迈不进来 → 标出来 ✓
        # 台阶: 上一级和它 ≤0.65 → 迈得进来 → 不标 ✓
        #
        # ⚠ 这张图只能给**静态投影**; 真正的"我到得了哪"是**泛洪**的结果
        #   (可达图/`ascii_reach`) —— 那里还算了连通性、额外边(传送门)。
        for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ni, nj = i + di, j + dj
            if not self.inside(ni, nj):
                continue
            if not self.walkable(ni, nj):
                continue
            if self.step_ok(ni, nj, i, j):
                return ""       # 有邻居能迈进来 → 这格到得了
        return CH_TOO_LOW

    def step_ok(self, i: int, j: int, ni: int, nj: int) -> bool:
        """**从格 (i,j) 迈一步到相邻格 (ni,nj)** 行不行 —— 高度判定就在这儿。

        用户定的模型(2026-09-14, 把前面几轮全推翻了):
          用**厨师的步长**去比**相邻两格之间**的高度差 ——
            · 有台阶 → 每级落差 ≤ 0.65 → 泛洪自然爬得上去
            · 没台阶 → 一步落差 > 0.65 → 过不去(若别处有坡道, 洪泛会从那儿绕)
            · 平台**沉下去** → 邻格在 0.00、它在 -1.65 → 差 1.65 → 进不去 ✓ 自动就是洞

        ⚠ **别退回"和厨师比"**(那是前面几轮的错法): 比较"这一格 vs 厨师所在层"
          会让 1.65(会动的平台沉下去) 和 2.40(静止的另一层) 无法区分 ——
          实测 1.65 该拦、2.40 该放, **不单调**, 任何阈值都必有一头错。
          改成**边**(相邻格之间)之后, 两个反例同时成立, 而且
          `movableFloor`(查组件)和自适应容差**都不需要了**。

        高度数据缺失(老 dll / 没地面) → **不因此否掉**(当作能迈)。

        ⚠ **不做"坡道豁免"**(曾经做过, 已按用户要求撤掉): 那是拿"这格是不是
          `SlopedGround`"特判。既然 1.00 一个数就盖住了坡道和转梯, 就没必要引入
          那个概念 —— **少一个特判, 少一处会错的地方**。
        """
        a = self.cell_floor_y(i, j)
        b = self.cell_floor_y(ni, nj)
        if a is None or b is None:
            return True
        # **一个数就够**: 1.00 盖住了实测能走的台阶(坡道 0.70/0.76、转梯 0.80,
        # 以及 `s_wizard_5_5` 西梯那个角度上的 0.93), 又远低于真该拦的
        # (平台沉下去 1.65 / 7.58)。见 STEP_MAX 的注释。
        return abs(float(a) - float(b)) <= STEP_MAX

    def is_danger(self, i: int, j: int) -> bool:
        """这一格会不会弄死厨师(水面/岩浆/火/空洞)。"""
        return self.at(i, j) in DANGER_CHARS

    def is_danger_world(self, x: float, z: float) -> bool:
        return self.is_danger(*self.cell_of(x, z))

    def nearest_walkable(self, i: int, j: int, radius: int = 5) -> tuple | None:
        """找离 (i,j) 最近的可走格 —— 台子本身是障碍, 得站到它旁边。

        必须在整圈里挑**真正最近**的那个: 按方向顺序返回会在水边给出隔着一格的
        斜角点, 厨师跑到那里还是够不到目标。
        """
        if self.walkable(i, j):
            return (i, j)
        best = None
        best_d = None
        for dj in range(-radius, radius + 1):
            for di in range(-radius, radius + 1):
                if di == 0 and dj == 0:
                    continue
                cand = (i + di, j + dj)
                if not self.walkable(*cand):
                    continue
                d = di * di + dj * dj
                if best_d is None or d < best_d:
                    best_d = d
                    best = cand
        return best

    # ---------------------------------------------------------------- 寻路
    def find_path(self, sx: float, sz: float, tx: float, tz: float,
                  allow_platform: bool = True, allow_travelator: bool = True,
                  max_nodes: int = 8000, use_reach: bool = True,
                  blocked: set = None, at_y: float = None,
                  extra_edges: dict = None) -> list:
        """在**安全格**上跑 A*, 返回途经点世界坐标列表(不含起点)。

        目标本身通常是台子(障碍格), 所以终点取它周围最近的可走格。
        起点即使不可走(例如人被平台推到了边上)也允许出发, 否则会原地锁死。

        use_reach: 只在**从起点真正连通**的格子上搜。实测 s_sushi_4_5:
          总可走格 182, 但从厨师出发只到得了 122 —— 另外 60 格是关卡里
          装饰地面/边界外的地皮, 地形上是 '.' 却和厨房不连通。
          不加这道约束, A* 会把这些格子当候选:
            · 目标最近的可走格若落在不连通的一片里, 整条路就规划不出来
            · 还会白搜一大片无关格子

        blocked: **动态禁行格**(路人/车辆当前占住的格子, 见
          `KitchenMap.blocked_by_movers`)。
          为什么必须单独传: 地形是**整局一次的静态快照**, 而车会开、路人会走 ——
          快照把它们冻在第一次扫到的位置, 于是"地图说安全的地方"可能是车的位置。
          把它当静态障碍画进地里, 比不知道更危险。
        """
        if not self.ok:
            return []
        start = self.cell_of(sx, sz)
        goal_cell = self.cell_of(tx, tz)
        blk = blocked or ()

        reach = None
        if use_reach:
            # ⚠ **必须把 `extra_edges` 一起传进来** —— 这个可达集是给"哪些格算有效目标"
            #   做约束的; 不传的话, 只有靠传送门才到得了的目标会被当成"不可达"直接滤掉,
            #   A* 于是返回空(实测踩过: 泛洪能到、A* 却规划不出来)。
            reach = self.reachable_from(sx, sz, allow_platform, allow_travelator,
                                        at_y, extra_edges)

        def ok(c, frm=None):
            if not self.walkable(c[0], c[1], allow_platform, allow_travelator):
                return False
            if frm is not None and not self.step_ok(frm[0], frm[1], c[0], c[1]):
                return False        # 高度是"边"的属性(见 step_ok)
            if c in blk:
                return False                # 动态禁行(路人/车当前在这)
            if reach is not None and c not in reach:
                return False
            return True

        goals = []
        if ok(goal_cell):
            goals.append(goal_cell)
        # 目标通常是台子(=障碍), 所以取它**紧邻**的可走格当终点。
        # 邻域只放相邻 4 格, 不要放宽: 放宽会"退而求其次"选到隔着墙的格子,
        # 于是"目标根本去不了"被伪装成"规划成功"。
        for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            cand = (goal_cell[0] + di, goal_cell[1] + dj)
            if ok(cand) and cand not in goals:
                goals.append(cand)
        if not goals:
            return []
        goalset = set(goals)

        if start in goalset:
            return [self.world_of(*start)]

        def h(c):
            return min(abs(c[0] - g[0]) + abs(c[1] - g[1]) for g in goals)

        openq = [(h(start), 0, start)]
        came = {start: None}
        best = {start: 0}
        found = None
        while openq:
            _, gc, cur = heapq.heappop(openq)
            if cur in goalset:
                found = cur
                break
            if len(best) > max_nodes:
                break
            nbs = [(cur[0] + dx, cur[1] + dz) for dx, dz in
                   ((1, 0), (-1, 0), (0, 1), (0, -1))]
            # 额外边(传送门): 从这一格可以直接"到"对端那一格。
            nbs += [t for t in (extra_edges or {}).get(cur, ())]
            for nb in nbs:
                if not ok(nb, cur) and nb not in (extra_edges or {}).get(cur, ()):
                    continue
                ng = gc + 1
                if nb in best and best[nb] <= ng:
                    continue
                best[nb] = ng
                came[nb] = cur
                heapq.heappush(openq, (ng + h(nb), ng, nb))

        if found is None:
            return []
        cells = []
        cur = found
        while cur is not None:
            cells.append(cur)
            cur = came[cur]
        cells.reverse()
        return [self.world_of(i, j) for i, j in cells[1:]]

    # ---------------------------------------------------------------- 连通性
    def distances_from(self, x: float, z: float,
                       allow_platform: bool = True,
                       allow_travelator: bool = True,
                       at_y: float = None, extra_edges: dict = None) -> dict:
        """从某个世界坐标出发, 到每个**真能走到**的格子的**步数**(BFS)。`{cell: 步数}`。

        和 `reachable_from` 是**同一套边规则**(后者现在就是 `set(这个函数)`) ——
        规则只能有一份, 抄一份迟早会漂(见 `CH_TRAVELATOR` 那段注释)。

        为什么需要距离: 评分要把"这一步离我多远"算成一个数(`scoring.W_DIST`)。
        单纯判"可不可达"选不出"先做近的那件"。

        起点恒在表里(步数 0), **即使起点本身不可走** —— 人被平台推到边上是常见情况。
        """
        if not self.ok:
            return {}
        start = self.cell_of(x, z)
        if not self.inside(*start):
            return {}
        ex = extra_edges or {}
        dist = {start: 0}
        queue = deque([start])
        while queue:
            i, j = queue.popleft()
            d = dist[(i, j)]
            # **额外边(传送门那种)**: 到了这一格就也能到它的对端 ——
            # 传送门不是"走过去", 是"在这一点被送到别处", 所以只能是额外的边,
            # 不能靠高度/邻接表达。
            for t in ex.get((i, j), ()):
                if t not in dist:
                    dist[t] = d + 1
                    queue.append(t)
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (i + di, j + dj)
                if nb in dist:
                    continue
                if not self.walkable(nb[0], nb[1], allow_platform, allow_travelator):
                    continue
                # **高度按"边"判**: 从当前格迈到它这一步的落差 ≤ 厨师的步长。
                # 有台阶时每级都 ≤0.65 → 洪泛爬得上去; 沉下去的平台 → 迈不进去。
                if not self.step_ok(i, j, nb[0], nb[1]):
                    continue
                dist[nb] = d + 1
                queue.append(nb)
        return dist

    def reachable_from(self, x: float, z: float,
                       allow_platform: bool = True,
                       allow_travelator: bool = True,
                       at_y: float = None, extra_edges: dict = None) -> set:
        """从某个世界坐标出发, 能走到的所有格子(BFS)。

        为什么这个比"可走格"重要:
          网格图上大片 '.' 看着像"能走", 但**能站不等于到得了**。
          关卡边界外的地皮/装饰地面在格子图上同样是 '.'(没有占用物、也有地面),
          可它和厨房根本不相连 —— 寻路永远不会过去, 画出来却吓人。
          反过来说: 如果某片区域**是**连通的, 那它就是真能走过去的, 不该当成 bug。

        寻路只关心"起点所在连通块"; 这个函数把它算出来。
        **边规则只有 `distances_from` 一份**, 这里只是丢掉步数。
        """
        return set(self.distances_from(x, z, allow_platform, allow_travelator,
                                       at_y, extra_edges))

    def ascii_reach(self, x: float, z: float, at_y: float = None,
                    dynamic: set = None) -> str:
        """画出"从某点出发真正到得了的区域": '#' 表示到不了。

        用它替代一眼看过去全是 '.' 的网格图 —— 到不了的格子直接打掉,
        避免把装饰地面误当成"边界被解析成可走"。

        ⚠ `at_y` 必须传(否则这张图会**高估**可达范围): BFS 走的是
          `walkable(at_y=)`, 不给高度就等于无视"另一层的地板"/"收起来的平台"。
        """
        reach = self.reachable_from(x, z, at_y=at_y)
        mark = self.cell_of(x, z)
        rows = []
        for j in range(self.h - 1, -1, -1):
            line = []
            for i in range(self.w):
                if mark == (i, j):
                    line.append(CH_MARK)
                elif (i, j) in reach:
                    line.append(self._mark(i, j, at_y, reach, dynamic))
                else:
                    line.append(" ")     # 到不了 -> 留白
            rows.append("".join(line))
        return "\n".join(rows)

    def bridge_cells(self, from_xz: tuple, goal_xz: tuple,
                     at_y: float = None, max_frontier: int = 300) -> list:
        """**"把哪一格变成可走, 目标就通了?"** —— 返回候选格 `[(i,j), ...]`, 按离起点近排序。

        用途(用户要求: 移动平台"开到让某几格变得可达就行"):
          那块平台本质是**一块可挪的地板**。要让它当桥, 得先知道**停在哪几格有用**。
          本函数就是答案: 逐个试"只把这一格变可走", 看目标是不是就可达了。

        做法: 先从起点算出连通块 `reach0`; 它的**边界外侧**(走得过去但当前不可走)就是
          候选 —— 平台停在那儿, 就把边界往外推了一格。对每个候选做一次"加上这一格"的
          BFS, 看目标进不进得来。单格当桥不够就返回空(那种要靠多格/别的机制)。

        纯函数, 不改地图状态, 所以能离线测。
        """
        if not self.ok:
            return []
        si, sj = self.cell_of(*from_xz)
        gi, gj = self.cell_of(*goal_xz)
        goal = (gi, gj)
        if not self.inside(*goal):
            return []

        def bfs(extra=frozenset()):
            seen = {c for c in self.reachable_from(from_xz[0], from_xz[1], at_y=at_y)}
            stack = list(seen)
            while stack:
                i, j = stack.pop()
                for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nb = (i + di, j + dj)
                    if nb in seen:
                        continue
                    if nb in extra or self.walkable(nb[0], nb[1], at_y=at_y):
                        seen.add(nb)
                        stack.append(nb)
            return seen

        reach0 = bfs()
        if goal in reach0:
            return []                       # 已经通了, 不用平台

        # 边界: 与 reach0 相邻、但自己不可走(=外面那圈)
        frontier = []
        for (i, j) in reach0:
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (i + di, j + dj)
                if nb in reach0 or nb in frontier:
                    continue
                if not self.inside(*nb):
                    continue
                frontier.append(nb)
        out = []
        for f in frontier[:max_frontier]:
            if goal in bfs({f}):
                d = (f[0] - si) ** 2 + (f[1] - sj) ** 2
                out.append((f[0], f[1], d))
        out.sort(key=lambda t: t[2])
        return [(i, j) for i, j, _ in out]

    # ---------------------------------------------------------------- 诊断
    def describe_dangers(self) -> str:
        """给日志用: 危险区清单 + 各类格子计数。"""
        parts = []
        for hz in self.hazards:
            if not hz.get("kills"):
                if hz.get("killPlane"):
                    parts.append("KillPlane(关卡地板下方, 忽略)")
                continue
            parts.append("%s/%s x[%.1f,%.1f] z[%.1f,%.1f]" % (
                hz.get("name") or "?", hz.get("type") or "?",
                float(hz.get("x0") or 0), float(hz.get("x1") or 0),
                float(hz.get("z0") or 0), float(hz.get("z1") or 0)))
        c = self.counts
        summary = ("可走%d 障碍%d 物理阻挡%d 台面传送带%d 危险%d 空洞%d 低地板%d "
                   "平台%d 地面传送带%d 火%d 占用(层≠0)%d" % (
            int(c.get("free") or 0), int(c.get("blocked") or 0),
            int(c.get("phys") or 0),
            int(c.get("conveyor") or 0),
            int(c.get("hazard") or 0), int(c.get("void") or 0),
            int(c.get("voidLow") or 0),
            int(c.get("platform") or 0), int(c.get("travelator") or 0),
            int(c.get("fire") or 0),
            # 「占用物是在 y != 0 那一层找到的」个数 —— LevelInfo 里那个 bug
            # (查占用表把层号写死成 0)修没修上, 就看这一个数:
            # 老 dll 恒为 0; 修好之后应当明显 > 0(实测 s_wizard_school_3_4 是 15)。
            int(c.get("occOffY") or 0)))
        if int(c.get("phys") or 0) > 0:
            names = self.phys_names[:3]
            summary += " (物理阻挡例: %s)" % ",".join(names)
        if int(c.get("conveyor") or 0) > 0:
            summary += (" ⚠有台面传送带(ConveyorStation): 放上去的物品会被一格一格传走 —— "
                        "切好的料不能存在上面")
        if int(c.get("voidLow") or 0) > 0:
            summary += " ⚠低地板格>0: 这一关有会下沉/单向落差的地面(如荷叶), 上面站不住"
        if not parts:
            return summary + "; 无危险区"
        return summary + "; 危险区: " + "; ".join(parts)

    def ascii(self, cx: float = None, cz: float = None, at_y: float = None,
              reach: set = None, dynamic: set = None, conv: dict = None) -> str:
        """把网格画成文本, 方便在终端里肉眼确认(大图片段看得很直观)。

        `at_y` = **厨师当前的高度**。传了它, "字符上能走、但对这只厨师站不下"
        的格子会画成 `'_'`(太低) / `'^'`(太高)。

        ⚠ **不传的话这张图会说谎**(用户实测指出): 限时平台收起来时
          **字符一格不变**, 只是高度掉了(实测 12 格从 -1.51 掉到 -9.08);
          只看字符的话, 图上照样写"能走" —— 图在替限时构件打包票。
        """
        rows = []
        mark = None
        if cx is not None and cz is not None:
            mark = self.cell_of(cx, cz)
        for j in range(self.h - 1, -1, -1):
            line = []
            for i in range(self.w):
                if mark == (i, j):
                    line.append(CH_MARK)
                    continue
                line.append(self._mark(i, j, at_y, reach, dynamic, conv))
            rows.append("".join(line))
        return "\n".join(rows)
