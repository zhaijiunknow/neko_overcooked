"""地图模型: 把桥采集的台子/厨师/烹饪状态组织成语义化地图。

台子语义归类依据(反编译确认):
  · kind 是组件类型名, 且 C# 侧已按 instanceID 去重(一个物体只归一个类型), 所以 kind 可靠
  · PlateStation    = 送餐口(往上面放"装了菜的盘子"才触发送餐)
  · CleanPlateStack = 干净盘子堆, 盘子的唯一来源
  · Workstation     = 切菜板(负责 chop);  AttachStation = 普通台面(只能放/拿)
  · 有 PickupItemSpawner(spawn 非空) = 食材箱
坐标: Unity 世界坐标 (x, z), y 忽略(平面)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# kind(组件类型名) → 语义
# 键一律小写, 对应 SceneScanner.StationTypes 里的类型名。
#
# ⚠ 这张表**故意不全** —— `classify()` 是**两段式**的, 别把下面这 4 个键补进来:
#     cookingstation / heatedcookingstation  → 要再看 `sub`(m_stationType) 才分得出
#                                              Hob/Oven/DeepFatFryer/…, 直接映射成 "hob"
#                                              会把烤箱炸锅全打死
#     workstation                            → 恒为 board
#     attachstation                          → 恒为 counter
#   这 4 个走 `classify()` 里的显式分支(它们在 StationTypes 里必须**排在派生类之后**,
#   见 SceneScanner.StationTypes 顶部的注释)。补进这张表 = 吞掉 sub 判断。
_KIND_SEM = {
    # 盘子体系
    "platestation": "serve",          # 送餐口
    "cleanplatestack": "plates",      # 干净盘子堆(取盘)
    "dirtyplatestack": "dirty_plates",
    "platereturnstation": "return_plates",
    # 功能台
    "rubbishbin": "bin",              # 垃圾桶
    "washingstation": "wash",         # 洗手池
    "conveyorstation": "conveyor",    # 台面传送带: 放上去的东西会被传走, 别当普通台面用
    "switchstation": "switch",        # 按钮(交互键可按)
    # 灶台类(HeatedCookingStation 是 CookingStation 的派生类, 由 classify 里单独分流)
    "heatedstation": "heat",          # 加热容器台
    "mixingstation": "mix",
    "autoworkstation": "auto",
    # 关卡机关
    "teleportal": "teleport",         # 传送门
    "terminal": "terminal",           # 驾驶台(移动平台的操控)
    "cannon": "cannon",               # 大炮
    "pushableobject": "pushable",     # 可推物体(会把厨师推开)
    "cookingregion": "cooking_region",
    # 生成器 = 食材箱 / 分发器
    "pickupitemspawner": "crate",
    "attachitemspawner": "crate",
    "placementitemspawner": "crate",
    # 危险物
    "firehazard": "hazard",
    "splathazard": "hazard",
}

# 灶台子类型 → 语义(CookingStation.m_stationType)
_STATION_SEM = {
    "Hob": "hob",
    "Oven": "oven",
    "DeepFatFryer": "fryer",
    "FirePit": "firepit",
    "Barbeque": "barbeque",
    "Flamethrower": "flamethrower",
    "FloorBurner": "floorburner",
}

#: 锅/平底锅的 Unity Tag(游戏自己给厨具的分类)
POT_TAG = "CookingUtensil"
#: 盘子的 Unity Tag
PLATE_TAG = "Plate"


def is_pot(name: str = "", tag: str = "") -> bool:
    """这东西是不是"能煮东西的锅/平底锅"。

    依据(实测组件清单, s_sushi_1_3 里那三口锅):
        utensil_pot_01 → CookableContainer + CookingHandler + ServerCookableContainer
                         + CookingUtensilRespawnBehaviour, tag=CookingUtensil
    它与灭火器 utensil_fire_extinguisher_01 的区别: 后者没有 CookableContainer/CookingHandler,
    所以**不能只看名字里的 "utensil"**; 看 "pot"/"pan" 才准, tag 只做兜底。
    """
    n = (name or "").lower()
    if "pot" in n or "pan" in n:
        return True
    if (tag or "") == POT_TAG and "extinguish" not in n and "fire" not in n:
        return True
    return False


def is_plate(name: str = "", tag: str = "") -> bool:
    if (tag or "") == PLATE_TAG:
        return True
    return "plate" in (name or "").lower()


def is_extinguisher(name: str = "", tag: str = "") -> bool:
    """这东西是不是**灭火器 / 水枪**(能喷的厨具)。

    为什么要有正面判据(用户提的"地图建模是否还有遗漏"):
      原来只在 `is_pot()` 里**反向排除**它("别把灭火器当锅"), 于是它在内容清单里
      归进"其他" —— 而**有的关卡必须靠它灭火**(s_balloon_5_2 开局就 5 处着火,
      还有 `air_balloon_burner_flame_on` 持续点火)。引擎连"这是灭火器"都不知道。

    判据只能靠**名字**: 它的 Unity Tag 是 `Untagged`(实测),
    所以 tag 一点忙都帮不上 —— 而锅是 `CookingUtensil`、盘子是 `Plate`。

    依据(反编译):
      · `FireExtinguishSpray : SprayingUtensil`(FireExtinguishSpray.cs:3, m_exinguishTime=0.5)
      · `ServerFireExtinguishSpray`(ServerFireExtinguishSpray.cs) 拿着它的人自己不会着火
      · `WaterGunSpray : FireExtinguishSpray`(WaterGunSpray.cs:3) —— 水枪是它的亲兄弟,
        除了灭火还能清洗和击退, 所以一并认进来。
    """
    n = (name or "").lower()
    return ("extinguish" in n) or ("water_gun" in n) or ("watergun" in n) or ("hose" in n)


def is_tool(name: str = "", tag: str = "") -> bool:
    """能拿在手上"用"的厨具(锅/灭火器/水枪…)—— 给内容清单分类用。

    注意**不是** `tag == CookingUtensil` 就够: 灭火器的 tag 是 Untagged。
    """
    return is_pot(name, tag) or is_extinguisher(name, tag)


def _aslist(v, key: str = "") -> list:
    """layout 里的字段**可能有两种形状**: 裸数组 `[...]`, 或被包成 `{"key":[...],...}`。

    为什么要有这个防御: `ScanItems()` 返裸数组, 而 `MoverScan.Snapshot()` 我一开始
    写成了 `{"movers":[...],"count":N}` —— 塞进 layout 就成了对象,
    Python 那边 `for m in layout["movers"]` 迭代出来的是**键(字符串)**,
    直接 `AttributeError: 'str' object has no attribute 'get'`。
    C# 已改成裸数组统一形状, 这里再兜一层: 老 dll 也不会把它弄崩。
    """
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        if key and isinstance(v.get(key), list):
            return v[key]
        for k in ("movers", "items"):
            if isinstance(v.get(k), list):
                return v[k]
    return []


def _norm_name(s: str) -> str:
    """只留字母数字, 用于比物品名(和 `engine.Engine._norm` 同一套约定)。

    先剥掉实例编号后缀: 场里同类物品叫 "SushiPrawn (2)"、"Plate 5 (3)"、
    "utensil_pot_01 (1)", 而计划里用的是 "SushiPrawn"。
    不剥就只能靠子串包含兜底, 那会误判(SushiPrawn 与 SushiPrawnCooked 互相包含)。
    """
    import re as _re
    t = (s or "").strip()
    t = _re.sub(r"\s*\(\d+\)\s*$", "", t)
    t = _re.sub(r"\s+\d+\s*$", "", t)
    return "".join(ch for ch in t.lower() if ch.isalnum())


@dataclass
class Station:
    id: str                    # 语义 id: crate0/board0/serve0...
    kind: str                  # 原始组件类型名
    sub: str                   # CookingStation.m_stationType 等
    name: str                  # 游戏对象名
    x: float
    z: float
    spawn: str = ""            # 箱子出的 prefab 名
    ing: str = ""              # 台面上的食材
    on: list = field(default_factory=list)  # 台面上放着的物品(子物体名)
    n: int = 0                 # 台面上/堆里的物品数量
    plate: str = ""            # 盘子堆/回收站提供哪种容器(PlatingStep 名)
    # 与 on 一一对应: 用**游戏自己的 Unity Tag** 说每样东西是什么
    # (锅=CookingUtensil / 盘=Plate / 食材=Ingredient/Pre-Ingredient),
    # 以及它自己容器里装了什么(盘子上的菜)。onhas 非空 = 这个盘子/锅不是空的。
    ontags: list = field(default_factory=list)
    onhas: list = field(default_factory=list)
    # ---- 传送门专属(kind == "teleport"; 其余台面这些字段全为空) ----
    #: **配对: 这扇门通向哪** —— 来自 `Teleportal.m_exitPortal`(一个直接引用)。
    #: 没有它就等于"三扇门是三个互不相干的点", 规划层没法把门当一条边用。
    exit_portal: str = ""
    exit_x: float = 0.0
    exit_z: float = 0.0
    #: 真正的**落点**(`m_teleportPoint`)。和门的视觉位置**不是一处** ——
    #: 实测 s_wizard_school_3_4: 占用表说门在 z=8.4 而物体在 z=7.28, 差约一格。
    #: 拿视觉位置当落点会"站在门外够不着"。
    land_x: float = 0.0
    land_z: float = 0.0
    cooldown: float = 0.0      # 冷却秒数(连着用会不会被卡住)
    arc: float = 0.0           # 出来之后的朝向弧(度)
    recv_delay: float = 0.0    # 接收延迟
    #: 这个台面**现在在不在场**(`GameObject.activeInHierarchy`)。
    #: 限时构件(如 s_wizard_school_3_4 的传送门, 与限时楼梯轮换)会整块启用/停用 ——
    #: 不判它的话, 一扇当前根本不存在的门在地图上照样被画成常驻的 `O`,
    #: **等于显示层在替限时构件打包票**。老 dll 没这字段 → 默认 True。
    active: bool = True
    # ---- 遥感控制台(kind == "Terminal")专属 ----
    #: **现在是不是正在驾驶它**(`ClientSessionInteractable.HasSession`)。
    #: 机制: 交互控制台 → 控制权交给被驾驶的物体, **厨师的 PlayerControls 被停用**
    #: ⇒ 此后发的移动键驱动的是平台而不是厨师(见 `Engine.session_station`)。
    session: bool = False
    #: 它驾驶的是哪个物体(`Terminal.m_pilotableObject` 的名字) —— 用来跟 dyn 里的
    #: platforms 表对上, 从而知道"平台现在在哪一格"。
    pilots: str = ""

    @property
    def paired(self) -> bool:
        """这扇门**读到配对**了没有。老 dll / 没读到 → False。"""
        return bool(self.exit_portal)

    def tag_of(self, i: int) -> str:
        return self.ontags[i] if i < len(self.ontags) else ""

    def has_of(self, i: int) -> str:
        return self.onhas[i] if i < len(self.onhas) else ""

    def pot_name(self) -> str:
        """台面上那口锅/平底锅的名字; 没有锅返回 ''。"""
        for i, o in enumerate(self.on):
            if is_pot(o, self.tag_of(i)):
                return o
        return ""

    def empty_plate_names(self) -> list:
        """台面上**空**的盘子(装了菜的盘子不能拿去锅里取菜 —— 会反过来倒进锅)。

        ☠☠ **整叠盘子不算"一个空盘"**(2026-09-15 `s_sushi_1_3` 实机打回来的)。
          `is_plate` 只判名字里有没有 "plate", 而 `dirtyplatestack` / `cleanplatestack`
          都含 "plate" ⇒ 整叠**被当成一只盘子**报出去。后果是引擎过去"拿盘子",
          结果把手伸进**整叠脏盘**里 —— 日志原话:
            `[引擎] ▶ [杂活] fetch Cucumber   厨师(11.0,9.8) 手持'DirtyPlateStack'`
            `[步骤] DirtyPlateStack 进不了盘 → 直接丢脚下`
          然后 `盘里=空` ⇒ `assemble`/`deliver` **全灭**, 最后锅里那份米**烧糊**
          (用户原话: "**脏盘不会识别并拿去洗**")。
          ⇒ 判据: 名字里带 **stack** 的是"**盘子的来源**", 不是盘子本身 —— 排除掉。
          (真正该拿的那只盘, 由 `dirty_plates`/`plates` 这两类台面的**取盘动作**给出。)
        """
        out = []
        for i, o in enumerate(self.on):
            if "stack" in (o or "").lower():
                continue                      # 整叠 ≠ 一只
            if is_plate(o, self.tag_of(i)) and not self.has_of(i):
                out.append(o)
        return out


@dataclass
class Chef:
    id: int                    # 0/1
    name: str
    x: float
    z: float
    held: str = ""             # 手上拿着什么
    player: str = ""           # 归属玩家(Player.One/Two) —— 决定该发哪套键盘


@dataclass
class Item:
    """**全场景按 tag 找出来的食材** —— 游戏自己的 `GameUtils.GetAllIngredients()`
    (`GameUtils.cs:504`: tag `Pre-Ingredient` ∪ `Ingredient`)那个视角。

    为什么和 `Station.on` 并存: 两边的**范围不同**。
      · `Station.on` = 挂在我们扫的那 25 个台面类型下的东西 —— 能知道"在哪个台面上"
      · `Item`       = 游戏认为"场上有哪些食材"的全部 —— **包括掉在地上的、
                       在移动平台/荷叶上的、任何不在台面下的**
    所以拿它当"有没有漏"的对照: 某件食材在 `items` 里但不在任何 `Station.on` 里、
    也不在任何厨师的 `held` 里 ⇒ 就是**我们地图看不见的东西**。
    """
    name: str
    tag: str                   # Pre-Ingredient(生料) / Ingredient(处理过的)
    x: float
    z: float
    #: **这个实例还能不能被加工**(= 生料还是成品)。来自 C# `ScanItems` 的 `work`
    #: (该实例身上有没有 `WorkableItem.m_nextPrefab`)。
    #:
    #: ☠ **判断加工阶段不能靠 `tag`, 也不能靠名字** —— 这是本轮(2026-09-15)为"传递指令"
    #: 补的字段, 两边的坑都在代码里记着:
    #:   · `tag` 不可靠: 同一关里**生虾的 tag 是 `Ingredient`、生鱼却是 `Pre-Ingredient`**
    #:     (见 `cookbook.raw_for` 的注释), 靠它会漏;
    #:   · 名字查知识表也不行: "生料和成品同名"那一族(`SushiFish --切8次--> SushiFish`)
    #:     在表里是**两条同名记录**, 查表取第一条 ⇒ 分不出手上这个是哪个
    #:     (见 `Engine._needs_work` 的注释)。
    #:   ⇒ 只有**实例自己**说得准。`workable=True` = 还是生料。
    workable: bool = False
    #: **这件东西现在挂在哪** —— 用户 2026-09-15:
    #:   "地图上预制体的位置、台面和台面上内容物**都需要包括灶台上的锅和地上的**,
    #:    这样交给**可行性检查**会更可信。"
    #: `on` = 挂在哪个台面上(物体名; 空 = 没挂在台面上); `carrier` = 谁拿着(空 = 没人拿)。
    #: 两个都空 ⇒ **自由对象**(在地上/在台面外的空间里)。
    #: ⚠ 这是**问游戏**要的挂载关系(规则 2), 不是我们按坐标猜的 ——
    #:   原来"在灶上 / 在地上 / 在手上"三态分不开(`cooking_on` 靠 0.6 格距离猜,
    #:   锅放在灶台边上或端在手上都会被猜成"在灶上")。
    #: ⚠ 老 dll 没这两个键 ⇒ 空串 ⇒ 调用方退回"按距离猜"。
    on: str = ""
    carrier: str = ""
    y: float = 0.0            # 高度 —— 多层关卡里同 (x,z) 不同层要靠它分


@dataclass
class Mover:
    """**会动的东西**: 路人 / 车辆 / 移动危险物。

    为什么单独建模(用户指出: "对于路人和车辆完全没有建模"):
      地形图是**整局一次的静态快照**, 而这两类会动:
        · 车辆 = 带 `RespawnCollider`(`RespawnType.Car`), **接触即死**
        · 路人 = 挡住路, 但它会走
      静态快照把它们冻在第一次扫到的位置 —— 于是"地图说安全的地方"可能是车的位置。
      **这比空白更糟: 它让人放心地走进去。**

    `moved` 是"和上一帧比位置变没变" —— 不依赖类名(反编译里根本没有 NPC/Vehicle 类)。
    """
    name: str
    kind: str                  # RespawnCollider / ByName
    death_by: str = ""         # RespawnType: Hit/Drowning/FallDeath/Car
    x: float = 0.0
    z: float = 0.0
    r: float = 0.6             # 水平半径(到碰撞体边), 用来算它占了哪几格
    moved: bool = False        # 这一帧它动了没有

    @property
    def deadly(self) -> bool:
        """碰到就死。`Car` 明确是死法; RespawnCollider 上挂着的都算(它们都是"重生触发器")。"""
        return self.kind == "RespawnCollider"


@dataclass
class Cooking:
    """正在灶上的东西。state 为 Cooked 时才是订单要的状态(Raw/Burnt 都不匹配)。

    注意(实测确认, 不是猜的): 在 s_sushi_1_3 这种"锅架在灶上"的关卡里,
    name 是**锅**的名字(utensil_pot_01 (3)), 因为 CookingHandler 长在锅身上,
    不是长在食材身上; 此时 ing 是**空串**, 锅里到底是什么只能看 inside
    (插件读 ServerIngredientContainer.GetContents → ItemKnowledge.ContentsNames)。
    """
    name: str
    ing: str
    prog: float                # 已煮秒数
    need: float                # 熟需要的秒数; > 2*need 就焦
    state: str                 # Raw / Cooked / Burnt
    burning: bool
    station: str
    x: float
    z: float
    tag: str = ""              # 游戏 Unity Tag(CookingUtensil = 锅)
    inside: str = ""           # 容器里装了什么("SushiRice" / "")
    #: **这个容器是哪种加热方式** —— `CookingHandler.m_cookingType.m_uID`。
    #: 它就是游戏"这道菜能不能进这口锅"的**权威判据**(和食材的 `cook_steps` 比):
    #:   `CookableContainer.cs:46-47` `cp.AllowsCookingStep(_handler.AccessCookingType)`
    #:   `CookableProperties.cs:11-13` 比的是 `CookingStepData.m_uID`
    #: ⚠ 只在**同一局内**可比值(两边都现读, 够用)。老 dll 没这字段 ⇒ 0 = 未知。
    cook_id: int = 0
    #: 上面那个 id 对应的资产名(只为**日志/离线**可读; 判据只用 `cook_id`)。
    cook_name: str = ""

    @property
    def ready(self) -> bool:
        return self.state == "Cooked"

    @property
    def burn_at(self) -> float:
        return 2.0 * self.need

    @property
    def is_pot(self) -> bool:
        return is_pot(self.name, self.tag)

    @property
    def busy(self) -> bool:
        """这口锅/这个灶上**有东西**吗。

        为什么不能用 "cooking_on(station) is not None" 判占用: 锅架在灶上时,
        空锅也有 CookingHandler(进度 0), 条目一直在 —— 那样所有"带锅的灶台"
        都会被当成"正在煮", 一个都用不了。
        """
        return bool(self.inside or self.ing or self.burning)


@dataclass
class KitchenMap:
    stations: dict = field(default_factory=dict)   # semantic id -> Station
    chefs: list = field(default_factory=list)
    cooking: list = field(default_factory=list)
    #: 全场景按 tag 找的食材(游戏 GetAllIngredients 的视角) —— 用来查"我们漏了什么"
    items: list = field(default_factory=list)
    #: 会动的东西(路人/车辆/移动危险物) —— 地形快照看不见的那一层
    movers: list = field(default_factory=list)

    #: 判断 `movers` 里的东西**像不像"会动的"**用的半径上限(单位: 格)。
    #: 真 movers(路人/车/推车)实测 `r = 0.5 ~ 0.6`; 而混进来的 `KillPlane` 是 12.5~35.0。
    #: 取 3 格 —— 比任何真的会动的东西都大, 又远小于那些"面"。
    MOVER_MAX_R_CELLS = 3.0

    def blocked_by_movers(self, tm) -> set:
        """**当前被会动的东西占住/威胁的格子**。给 A* 当动态禁行格。

        为什么要算这个: 地形是静态快照, 车开到哪它不知道。把 movers 的**当前位置**
        换算成格子并在寻路时禁掉, 才是"车开到哪, 危险区就在哪"。

        半径 `m.r` 用**切比雪夫**展开(取 max(|dx|,|dz|) 那圈), 因为格子是方的。

        ⚠ **别多禁**: 一开始我写成 `int(r/cell)+1`(保守多禁一圈), 结果离线测试里
          一辆车 + 一个路人就把一条走廊**完全堵死**, A* 无解 —— 反而走不动了。
          现在按**精确 footprint** 展开(向上取整到整格), 由调用方负责"真无解时怎么办"
          (见 `Engine.navigate_smart` 的兜底)。
        """
        out = set()
        if tm is None or not getattr(tm, "ok", False):
            return out
        cell = max(tm.cellx, tm.cellz, 1e-6)
        # ☠ **台面本身不是"会动的东西"**。
        #   `MoverScan.Snapshot()` 收的是"**所有挂 `RespawnCollider` 的物体**" ——
        #   那是"**会弄死你**"的标记, 不是"**会动**"的标记。于是**食材箱**
        #   (`dispenser_crate_04` 也挂了 RespawnCollider)被一起扫了进来:
        #     实测 s_wonderland_1_5: 18 个"mover" 里 **10 个是食材箱**、8 个是路人。
        #   ⇒ 引擎要去取的箱子**把自己禁掉了** ⇒ `find_path` 的 `ok()` 判目标不可走
        #     ⇒ 规划不出路 ⇒ 直线硬冲 ⇒ 卡在"还差 1.0 格"(正好够不到箱子)。
        #   判据用**名字**(台面名 = 同一个 Unity 对象名), 比关键词/半径都准。
        statics = set(s.name for s in self.stations.values() if s.name)
        for m in self.movers:
            if (getattr(m, "name", "") or "") in statics:
                continue
            # ☠ **别把"死亡面"当 mover**(实测踩过, 2026-09-14)。
            #   `movers` 里混进了 `KillPlane`(关卡地板下方那层死亡面), 它的 `r` 是
            #   **半边长** —— 实测 35.0 / 17.5 / 12.5。按半径展开后
            #   **一个就盖住 61x61 = 3721 格**, 而地图才 984 格
            #   ⇒ 禁行集算出来 **3845 格 > 整张图**。
            #   后果是一整条链: `find_path` 永远无解 → 退回"不避让" → 还是无解 →
            #   `navigate_smart` 走**直线硬冲**兜底 → 撞墙卡死。
            #   症状就是日志里**每一跳**都刷:
            #     `[导航] ⚠ 动态禁行(3845 格)导致无路可走`
            #   KillPlane 在地形层已经按 `RespawnCollider` 处理成危险区了,
            #   不该再进这个"会动的东西"的集合。
            _n = (getattr(m, "name", "") or "").lower()
            if "killplane" in _n or "falldeath" in _n:
                continue
            try:
                _r = float(getattr(m, "r", 0) or 0)
            except (TypeError, ValueError):
                _r = 0.0
            # 第二道保险: 半径超过 3 格的"会动的东西"(路人/车)不存在 ——
            # 真 movers 实测 r=0.5~0.6。名字万一变了, 这条还兜得住。
            if _r > self.MOVER_MAX_R_CELLS * cell:
                continue
            try:
                ci, cj = tm.cell_of(m.x, m.z)
            except Exception:
                continue
            n = max(1, int(_r / cell + 0.999))      # 向上取整到整格, 不加余量
            for di in range(-n, n + 1):
                for dj in range(-n, n + 1):
                    out.add((ci + di, cj + dj))
        return out

    def unseen_items(self) -> list:
        """**没主的**食材 —— 在 `items` 里, 但**按位置**看既不在任何台面上、也不在任何厨师手上:
        掉在地上的、被丢出来的、在移动平台/荷叶上的、台面类型没覆盖到的。

        两个用途(都是**唯一**的判据, 别人别自己再写一份):
          · 取料三源之一 —— "**地上那件能不能去捡**"(见 `Engine._fetch_source_live`)
          · `mapview` 的"地图看不见的食材" —— 回答"建模还有遗漏吗"

        ☠☠ **必须按"位置"认领, 不能按"名字"**(实测踩过, 用户原话"**不会拿地上的食物啊**"):
          旧写法是「`items` 里有没有哪个**名字**没在台面/手上出现过」——
          于是台面上放着一块 `SushiFish`、地上**另外**躺着三条 `SushiFish` 时,
          名字出现过 ⇒ 地上那三条**被一起吞掉** ⇒ 报告说"看不见 0 件"(假的),
          而脚本也就当脚边没料(它那时还在用同一个判据)。
          ⇒ 改成**按位置认领**: 台面/厨师报出的东西只认领"**就在它那个位置**"的那一件。
            容差 0.9 格: 台面报的是**台面中心**, 而它上面的东西就在同一格附近。
        """
        claimed = []                      # (归一化名, x, z)
        for s in self.stations.values():
            for i, o in enumerate(s.on or []):
                claimed.append((_norm_name(o), s.x, s.z))
                for part in (s.has_of(i) or "").split("+"):
                    if part.strip():
                        claimed.append((_norm_name(part), s.x, s.z))
        for c in self.chefs:
            if getattr(c, "held", ""):
                claimed.append((_norm_name(c.held), c.x, c.z))
        out = []
        for it in self.items:
            n = _norm_name(getattr(it, "name", ""))
            if not n:
                continue
            ix = float(getattr(it, "x", 0.0) or 0.0)
            iz = float(getattr(it, "z", 0.0) or 0.0)
            if any(cn == n and abs(cx - ix) < 0.9 and abs(cz - iz) < 0.9
                   for cn, cx, cz in claimed):
                continue                  # 有主了(在台面上 / 在某人手上)
            out.append(it)
        return out

    # ---- 语义归类 ----
    @staticmethod
    def classify(name: str, kind: str, sub: str, spawn: str = "") -> str:
        k = (kind or "").lower()
        if k in _KIND_SEM:
            return _KIND_SEM[k]
        if k in ("cookingstation", "heatedcookingstation"):
            return _STATION_SEM.get(sub, "hob")
        # 有生成器 = 食材箱(箱子本身可能只挂 AttachStation)
        if spawn:
            return "crate"
        if k == "workstation":
            return "board"             # 切菜板
        if k == "attachstation":
            return "counter"           # 普通台面(中转/放物)
        # 兜底: 名字关键词
        n = (name or "").lower()
        if "dispenser" in n or "crate" in n:
            return "crate"
        if "chopping" in n or "board" in n:
            return "board"
        if "plate_return" in n:
            return "return_plates"
        if "plate" in n:
            return "plates"
        if "washing" in n or "drying" in n:
            return "wash"
        if "bin" in n or "trash" in n:
            return "bin"
        return k or "unknown"

    @staticmethod
    def _station_sort_key(s: dict):
        """给台面排一个**稳定**的序 —— 决定 `counter0/counter1/...` 谁是谁。

        优先用游戏给的 `iid`(Unity instanceID, 物体存活期内不变);
        老 dll 没有这个字段时退回 `名字+坐标`, 也比"枚举顺序"稳。
        """
        iid = s.get("iid")
        if isinstance(iid, int):
            return (0, iid, "", 0.0, 0.0)
        return (1, 0, str(s.get("name", "")),
                float(s.get("x", 0) or 0), float(s.get("z", 0) or 0))

    @classmethod
    def from_layout(cls, layout: dict) -> "KitchenMap":
        km = cls()
        counters = {}
        # ⚠ **先按游戏的 instanceID 排序, 再编号**。
        #
        # `sid` 是 `语义+序号`(counter0/counter1/...) 形式的, 而序号原来是"按 C# 数组
        # 的出现顺序"编的 —— 那个顺序来自 `FindObjectsOfType`, **不保证稳定**。
        # 台面增删(着火、关卡变形、物件被拆)都会让后面所有 sid 平移。
        #
        # 这不是洁癖: `engine._assemble_sid` 靠 `counter7` 认回"上一次放半成品的那个台面",
        # sid 一漂它就会认到**另一个台面**上 —— 那正是"同一份材料反复取"的病根之一。
        # 按 iid 排完序, 同一局内 sid 与物理台面就是一一对应的。
        #
        # 老 dll 没有 iid → 退回按 名字+坐标 排, 仍比枚举顺序稳。
        stations = sorted(layout.get("stations") or [], key=cls._station_sort_key)
        for s in stations:
            sem = cls.classify(s.get("name", ""), s.get("kind", ""),
                               s.get("sub", ""), s.get("spawn", ""))
            n = counters.get(sem, 0)
            counters[sem] = n + 1
            sid = f"{sem}{n}"
            km.stations[sid] = Station(
                id=sid, kind=s.get("kind", ""), sub=s.get("sub", ""),
                name=s.get("name", ""), x=float(s.get("x", 0)), z=float(s.get("z", 0)),
                spawn=s.get("spawn", ""), ing=s.get("ing", ""),
                on=list(s.get("on") or []), n=int(s.get("n", 0) or 0),
                plate=s.get("plate", ""),
                ontags=list(s.get("ontags") or []),
                onhas=list(s.get("onhas") or []),
                exit_portal=s.get("exitPortal", "") or "",
                exit_x=float(s.get("exitX", 0) or 0),
                exit_z=float(s.get("exitZ", 0) or 0),
                land_x=float(s.get("landX", 0) or 0),
                land_z=float(s.get("landZ", 0) or 0),
                cooldown=float(s.get("cooldown", 0) or 0),
                arc=float(s.get("arc", 0) or 0),
                recv_delay=float(s.get("recvDelay", 0) or 0),
                active=bool(s.get("active", True)),
                session=bool(s.get("session", False)),
                pilots=s.get("pilots", "") or "")
        for i, c in enumerate(layout.get("chefs") or []):
            km.chefs.append(Chef(
                id=int(c.get("id", i)), name=c.get("name", f"P{i}"),
                x=float(c.get("x", 0)), z=float(c.get("z", 0)),
                held=c.get("held", ""), player=c.get("player", "")))
        for m in _aslist(layout.get("movers"), "movers"):
            km.movers.append(Mover(
                name=m.get("name", ""), kind=m.get("kind", ""),
                death_by=m.get("deathBy", ""),
                x=float(m.get("x", 0) or 0), z=float(m.get("z", 0) or 0),
                r=float(m.get("r", 0.6) or 0.6),
                moved=bool(m.get("moved"))))
        for it in _aslist(layout.get("items"), "items"):
            km.items.append(Item(
                name=it.get("name", ""), tag=it.get("tag", ""),
                x=float(it.get("x", 0) or 0), z=float(it.get("z", 0) or 0),
                workable=bool(it.get("work")),
                # 挂载关系(问游戏要的) —— 老 dll 没有 ⇒ 空串/0 ⇒ 退回按距离猜
                on=it.get("on", "") or "", carrier=it.get("carrier", "") or "",
                y=float(it.get("y", 0) or 0)))
        for c in layout.get("cooking") or []:
            km.cooking.append(Cooking(
                name=c.get("name", ""), ing=c.get("ing", ""),
                prog=float(c.get("prog", 0)), need=float(c.get("need", 0)),
                state=c.get("state", ""), burning=bool(c.get("burning")),
                station=c.get("station", ""),
                x=float(c.get("x", 0)), z=float(c.get("z", 0)),
                tag=c.get("tag", ""), inside=c.get("in", ""),
                # 老 dll 没有这两个键 ⇒ 0/"" = 未知 ⇒ 调用方退回"离我最近"那条老路
                cook_id=int(c.get("cookId", 0) or 0),
                cook_name=c.get("cookName", "") or ""))
        return km

    # ---- 查询 ----
    def of(self, sem: str) -> list:
        return [s for sid, s in self.stations.items() if sid.startswith(sem)
                and sid[len(sem):].isdigit()]

    def nearest(self, sem: str, x: float, z: float) -> Optional[Station]:
        best, bd = None, 1e18
        for s in self.of(sem):
            d = (s.x - x) ** 2 + (s.z - z) ** 2
            if d < bd:
                best, bd = s, d
        return best

    def sorted_by_dist(self, sem: str, x: float, z: float) -> list:
        return sorted(self.of(sem), key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2)

    def chef(self, cid: int) -> Optional[Chef]:
        for c in self.chefs:
            if c.id == cid:
                return c
        return None

    def find_source(self, ing_name: str, x: float = 0.0, z: float = 0.0,
                    ok=None) -> Optional[Station]:
        """找提供某食材的箱子。优先精确匹配(prefab 名/食材名), 再退化到模糊匹配。

        `ok`: 可选的过滤函数 `ok(station) -> bool` —— **用来滤掉"走不到"的货源**。
        为什么需要(实测 `s_wonderland_1_5`): 那关两个厨房被一道墙切开
        (两块连通块 28 格 / 108 格, **交集 0**), 而本函数**只按距离挑** ——
        于是挑中了对面厨房的箱子 ⇒ 到那儿才发现够不着 ⇒ 整步白费。
        **"哪个箱子最近"和"哪个箱子到得了"是两件事, 后者必须先满足。**
        """
        key = (ing_name or "").strip()
        if not key:
            return None
        low = key.lower()
        # 1) 精确: 箱子 prefab 名 或 ing 字段等于目标(或互为子串的基本形式)
        exact, loose = [], []
        for s in self.of("crate"):
            if ok is not None and not ok(s):
                continue                       # 走不到的货源, 直接不算候选
            hay = (s.spawn + " " + s.ing + " " + s.name)
            if hay.lower().find(low) >= 0:
                d = (s.x - x) ** 2 + (s.z - z) ** 2
                exact.append((d, s))
                continue
            # 2) 模糊: 去掉 sushi_ 之类前缀再比
            short = low.replace("sushi_", "").replace("sushi", "")
            if short and short in hay.lower():
                d = (s.x - x) ** 2 + (s.z - z) ** 2
                loose.append((d, s))
        best = exact or loose
        if not best:
            return None
        best.sort(key=lambda t: t[0])
        return best[0][1]

    def pot_on(self, station: Station) -> Optional[Item]:
        """**这个灶台上架着哪口锅** —— 按**游戏的挂载关系**找, 不按坐标猜。

        用户 2026-09-15: "台面和台面上内容物**都需要包括灶台上的锅和地上的**,
        这样交给**可行性检查**会更可信"。

        怎么找: `ScanItems` 现在把 `CookingUtensil` 也扫进来了, 每件都带
        `on`(挂在哪个台面上)。所以"这口锅在这个灶上"是**游戏说的事实**,
        而不是"它离这个坐标 0.6 格以内"的**推断** ——
        原来那套推断分不开三种情况: 锅在灶上 / 锅被端在手上 / 锅放在灶台边上。
        ⚠ 老 dll(没有 `on`)⇒ 返回 None, 调用方退回按距离猜。
        """
        if station is None or not station.name:
            return None
        for it in self.items:
            if it.on and it.on == station.name:
                return it
        return None

    def cooking_on(self, station: Station) -> Optional[Cooking]:
        """某个灶台上正在煮的东西。

        ☠☠ **先按挂载关系找锅, 找不到才退回按距离猜**(用户 2026-09-15 的要求):
          原来的判据只有 `abs(c.x - station.x) < 0.6 and abs(c.z - station.z) < 0.6` ——
          那是**推断**, 而且分不开"锅在灶上 / 锅端在手上(就在厨师身边) / 锅放在灶台边上"。
          有了 `ScanItems` 报的 `on`(见 `pot_on`)之后, "这口锅挂在哪个台面上"是**事实**:
          先由事实定位到**那口锅**, 再用**锅的名字**去 `cooking` 里找它的烹饪状态。
        ⚠ 两条都必须留着: 老 dll 没有 `on`,`pot_on` 返回 None ⇒ 走距离那条;
          而有些关卡锅里还没有 `CookingHandler` 对应条目 ⇒ 也别把距离那条删掉。
        """
        pot = self.pot_on(station)
        if pot is not None:
            for c in self.cooking:
                if c.name == pot.name:
                    return c
        for c in self.cooking:
            if abs(c.x - station.x) < 0.6 and abs(c.z - station.z) < 0.6:
                return c
        return None

    def summary(self) -> str:
        from collections import Counter
        cnt = Counter()
        for s in self.stations.values():
            sem = s.id.rstrip("0123456789")
            cnt[sem] += 1
        cook = f" 烹饪中{len(self.cooking)}" if self.cooking else ""
        return f"台子 {dict(cnt)} 厨师 {len(self.chefs)}{cook}"


def teleport_edges(km, tm) -> dict:
    """传送门的**额外边** —— `{可站格: [到的格, ...]}`，喂给泛洪和 A*。

    为什么传送门必须是"边"而不是地形: 它是**在这一点被送到别处**, 不是走过去 ——
    地形/高度/邻接都表达不了它。所以泛洪到这一格时, 额外把对端也加进可达集;
    A* 也在这一步多出几条候选。

    ⚠ 端点用**门旁边的可走格**, 不是门自己那格 —— 门是占用物(障碍),
      厨师得站在它旁边才进得去(和"台面旁边才够得着"同一个道理)。
    ⚠ 出口用 `exitX/exitZ`(也就是 `m_exitPortal` 那个物体的坐标), 不是靠名字猜 ——
      名字带实例编号("Teleportal (1)"), 会随关卡加载变。
    """
    if km is None or tm is None or not getattr(tm, "ok", False):
        return {}
    ports = [st for st in km.stations.values()
             if (st.kind or "").lower() == "teleportal"]
    if not ports:
        return {}
    by_name = dict((st.name, st) for st in ports)

    def around(c):
        """这一格 + 四邻 —— 门的"旁边"就是这一圈里能站的那些。"""
        i, j = c
        return [(i, j), (i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)]

    def standable(cells):
        return [c for c in cells
                if tm.inside(*c) and tm.walkable(c[0], c[1])]

    out = {}
    for st in ports:
        if not st.exit_portal:
            continue
        # ⚠ **边必须"经过门自己那一格"**, 不能从门旁直接连到出口旁 ——
        #   传送的触发方式是**走进门里**。少这一步的话, `navigate` 会拿着
        #   "门旁 → 出口旁"这条边直接朝对岸走 —— 而那是走不过去的(中间是空的),
        #   表现就是"朝墙一直走然后卡住"。
        #   拆成两跳之后, 路径里会带上门那一格, 走进去就触发了。
        own = tm.cell_of(st.x, st.z)
        src = standable(around(own))
        ex = by_name.get(st.exit_portal)
        ec = (tm.cell_of(ex.x, ex.z) if ex
              else tm.cell_of(st.exit_x, st.exit_z))
        dst = standable(around(ec))
        if not src or not dst:
            continue
        for a in src:                       # 门旁 → 门里
            lst = out.setdefault(a, [])
            if own != a and own not in lst:
                lst.append(own)
        lst2 = out.setdefault(own, [])      # 门里 → 出口旁
        for b in dst:
            if b != own and b not in lst2:
                lst2.append(b)
    return out


#: 传送带方向的两套箭头 —— **两类传送带推的东西不一样, 图上必须能分开**:
#:   · `ConveyorStation`(**台面**传送带, 推**物品**) → 引擎会用它算拦截点 ⇒ 细箭头
#:   · `Travelator`(**地面**传送带, 推**厨师**) → 引擎当普通可走格, 不管 ⇒ ASCII 箭头
#:
#: ⚠ **为什么不是更漂亮的 `⇒`/`▶◀`**: 那些 **GBK 编不出来** ——
#:   实测 `⇒⇐⇑⇓ ▶◀ ►◄ ▻◅ »« ↔↕` 全部 `UnicodeEncodeError`,
#:   GBK 里**唯一一套完整的四方向**就是细箭头 `→←↑↓`。
#:   (这条坑在本项目备忘里: 工具输出里的符号必须能过 GBK, 否则管道一抓就炸。)
#:   所以地面那套退而求其次用 ASCII —— 方向对、一格一字符、必定编得过。
#:
#: 坐标约定和图上一致 —— 图是 `j` 大的画在上面, 而 `j` 随世界 `z` 增大,
#: 所以 `+z` 是图上的"上"。
#:
#: ⚠ `!` 在**叠加图**里是"撞上就死"(`MV_DEADLY`)。地形图/可达图不用它,
#:   而这两张图才画传送带方向 —— 所以只在叠加图上会重号(那边不传 `conv`)。
BELT_ARROW = {(1, 0): "→", (-1, 0): "←", (0, 1): "↑", (0, -1): "↓"}
FLOOR_ARROW = {(1, 0): ">", (-1, 0): "<", (0, 1): "?", (0, -1): "!"}


def conveyor_arrows(tm, dyn) -> dict:
    """**传送带往哪边推** —— `{格: 箭头}`, 喂给 `TerrainMap.ascii(conv=...)`。

    为什么非要把方向画出来: 图上原来只有 `T`(推**厨师**的 `Travelator`) 和
    `C`(推**物品**的 `ConveyorStation`), 只说了"这是传送带"。
    而**方向才是关键** —— 知道它往哪推才知道东西会跑到哪、人站上去会被带到哪。
    一个不知道方向的 `T`, 对寻路和摆盘基本等于白标。

    **两套箭头不一样**(见 `BELT_ARROW` / `FLOOR_ARROW` 的注释): 台面那套(`→←↑↓`)
    **引擎会处理**, 地面那套(`> < ^ v`)**引擎不管** —— 图上分得开, 才知道
    "这块地板会把厨师带走, 而寻路并不知道"。

    数据来自插件 `dyn.conveyors` 的 `stepx/stepz`(**每格位移方向**),
    **不用动 C#**(`BridgeServer.cs:133` 那个 `dyn` 命令本来就带"传送带方向")。
    """
    out = {}
    if tm is None or not getattr(tm, "ok", False):
        return out
    for c in (dyn or {}).get("conveyors") or []:
        sx = float(c.get("stepx") or 0)
        sz = float(c.get("stepz") or 0)
        if abs(sx) < 0.05 and abs(sz) < 0.05:
            continue                      # 停着的传送带没有方向
        # 取**主导轴** —— OC2 的传送带都是轴向的, 但读数偶尔带噪声,
        # 硬要求恰好的 (0,±1)/(±1,0) 会漏掉。
        k = (1 if sx > 0 else -1, 0) if abs(sx) >= abs(sz) \
            else (0, 1 if sz > 0 else -1)
        table = FLOOR_ARROW if c.get("type") == "Travelator" else BELT_ARROW
        a = table.get(k)
        if not a:
            continue
        cell = tm.cell_of(float(c.get("x") or 0), float(c.get("z") or 0))
        if tm.inside(*cell):
            out[cell] = a
    return out


def conveyor_edges(tm, dyn) -> dict:
    """**地面传送带把你往哪送** —— `{格: [落点格]}`, 喂给泛洪/A* 的 `extra_edges`。

    和传送门边同一套机制(都是"到了这一格, 就也能到那一格"), 但**语义不一样**:
      · 传送门是**瞬移** —— 不判高度, 落点是不是地板都无所谓
      · 传送带是**被推着走** —— 落点还是地面。所以**落点必须可走**才算一条边:
        尽头是水/空洞时**不给边**(那不是"能去", 那是"会死", 是另一码事)。

    依据(反编译, 三处串起来才是完整链条):
      · `Travelator.cs:134` `GetSurfaceVelocity() = m_speed * 方向`(默认 `m_speed=1`)
      · `SurfaceMovable.cs:42` `Update()` **无条件每帧**算,
        和厨师有没有输入**无关**
      · `RigidbodyMotion.cs:43` `Movement(v,dt)` → **`MovePosition(pos + v*dt)`**
        —— 是**直接挪位置**, 不是设速度
    ⇒ 站在带子上**输入为 0 也照推**; 默认 1 u/s 而厨师 4 u/s, 所以**逆流仍走得动**。

    ⚠ 于是这条边**大多数时候和普通邻接重复**(`T` 可走 + 相邻同高, 泛洪本来就通)。
      它真正不重复的只有两种:
        · **带速被关卡调大**(≥4 u/s) ⇒ 上游上不去
        · **带子尽头有落差** ⇒ 带子送你下去, 而 `step_ok` 会拦
    """
    out = {}
    if tm is None or not getattr(tm, "ok", False):
        return out
    for c in (dyn or {}).get("conveyors") or []:
        if c.get("type") != "Travelator":
            continue                      # 台面传送带推的是物品, 不推人
        sx = float(c.get("stepx") or 0)
        sz = float(c.get("stepz") or 0)
        if abs(sx) < 0.05 and abs(sz) < 0.05:
            continue                      # 停着的带子不推人
        k = (1 if sx > 0 else -1, 0) if abs(sx) >= abs(sz) \
            else (0, 1 if sz > 0 else -1)
        src = tm.cell_of(float(c.get("x") or 0), float(c.get("z") or 0))
        if not tm.inside(*src):
            continue
        dst = (src[0] + k[0], src[1] + k[1])
        if not tm.inside(*dst) or not tm.walkable(*dst):
            continue                      # 尽头不能站 → 不给边(那是"会死", 不是"能去")
        lst = out.setdefault(src, [])
        if dst not in lst:
            lst.append(dst)
    return out


def wind_cells(tm, dyn) -> dict:
    """风区覆盖了哪些格、每格被往哪个方向推: `{(i,j): (vx, vz)}`(世界单位/秒)。

    **为什么和传送带不是一套**(别顺手合并):
      · 传送带是**逐格搬运** ⇒ 值得加一条连通边(`conveyor_edges`)
      · 风是**体积内的持续漂移** ⇒ 只影响"站在这里会被推", **不产生新的连通性**
        (风速通常远小于厨师速度 4 u/s, 顶着风也走得动; 真推不动的情况
         `|W| ≥ 4` 是"到不了", 由导航的可行性闸门判, 不该伪装成一条边)

    依据(反编译, 三处串起来):
      · `WindVolume.cs:18-21`  `GetVelocity() = enabled ? m_windSpeed * transform.right : 0`
      · `WindAccumulator.cs:32-39`  玩家身上的接收器把各源**矢量求和**
      · `ClientPlayerControlsImpl_Default.cs:902-906` `ApplyWindForce()`
        → `RigidbodyMotion.Movement(v, dt)` = `MovePosition(pos + v*dt)`
        ⇒ **不按键也照样被吹**(实机事故: 风把厨师推到边缘 → 掉空洞 → 烧掉整局)

    ⚠ **每次现算, 不要按 `TerrainMap` 身份缓存**(`_travel_edges` 那样):
      `enabled` / `m_windSpeed` 会被机关改 —— `WindVolume.Update` 起风时播 `WindGust`、
      `WindCosmeticDecisions.Update` 跟着点亮粒子, 都是"会变"的证据。
      数据源走调用方的 `Engine._dyn()`, 它本来就带 1 秒 TTL, 刷新频率天然受限。
    """
    out = {}
    if tm is None or not getattr(tm, "ok", False):
        return out
    for c in (dyn or {}).get("winds") or []:
        if not c.get("on"):
            continue                        # 停着的风区不推人
        try:
            vx = float(c.get("vx") or 0.0)
            vz = float(c.get("vz") or 0.0)
            cx = float(c.get("cx") if c.get("cx") is not None else (c.get("x") or 0.0))
            cz = float(c.get("cz") if c.get("cz") is not None else (c.get("z") or 0.0))
            ex = abs(float(c.get("ex") or 0.0))
            ez = abs(float(c.get("ez") or 0.0))
            rot = float(c.get("rot") or 0.0)
        except (TypeError, ValueError):
            continue
        if abs(vx) < 1e-3 and abs(vz) < 1e-3:
            continue                        # 风速为 0 = 此刻没风
        if ex <= 0.0 or ez <= 0.0:
            continue                        # 没有体积(插件没拿到 BoxCollider)

        # 世界 → 体积局部: Unity 绕 Y 转 θ 的逆变换
        th = math.radians(rot)
        ca, sa = math.cos(th), math.sin(th)
        # 有向矩形的世界 AABB 半长(用来只遍历相关格, 不扫全图)
        ax = ex * abs(ca) + ez * abs(sa)
        az = ex * abs(sa) + ez * abs(ca)

        i0, j0 = tm.cell_of(cx - ax, cz - az)
        i1, j1 = tm.cell_of(cx + ax, cz + az)
        for i in range(min(i0, i1) - 1, max(i0, i1) + 2):
            for j in range(min(j0, j1) - 1, max(j0, j1) + 2):
                if not tm.inside(i, j):
                    continue
                px, pz = tm.world_of(i, j)          # 格心
                wx, wz = px - cx, pz - cz
                lx = wx * ca - wz * sa              # Unity: R^T · (p-c)
                lz = wx * sa + wz * ca
                if abs(lx) <= ex and abs(lz) <= ez:
                    # ☠ **多股风要「求和」, 不是「覆盖」** —— `WindAccumulator.Update()`
                    #   是 `m_totalForce += m_sources[i].GetVelocity()`(WindAccumulator.cs:44-51),
                    #   两股风**方向相反时甚至互相抵消**。写成覆盖的话, 重叠风区上报的风速
                    #   **偏小还可能反向**(风箱喷雾 + 场景风区重叠就是这种局面)。
                    # ⚠ 这里只是"规划用的近似"(格心落在体积里); **补偿**那一侧不用它 ——
                    #   游戏自己报的合力才是权威, 见 `Engine._wind_of`。
                    prev = out.get((i, j))
                    out[(i, j)] = (vx, vz) if prev is None \
                        else (prev[0] + vx, prev[1] + vz)
    return out
