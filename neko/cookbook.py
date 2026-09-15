"""从游戏数据推导"这道菜到底怎么做"。

依据(全部反编译自游戏本体, 不是猜的):
  · 配方树      : OrderDefinitionNode.Convert() → AssembledDefinitionNode 嵌套树
  · 加工标记    : 树里 CookedCompositeAssembledNode = 要煮; MixedCompositeAssembledNode = 要搅拌
  · 生熟判定    : CookingHandler.GetCookedOrderState(progress)
                      progress <=  cookTime             → Raw    (订单不匹配)
                      cookTime < progress <= 2*cookTime → Cooked (订单要这个)
                      progress >  2*cookTime            → Burnt  (订单不匹配)
                  且 CookedCompositeAssembledNode.IsMatch 同时比较 m_cookingStep 与 m_progress
                  ⇒ 必须煮到"刚熟"窗口内取下, 生和焦都算白做
  · 加工阶段    : Unity Tag 区分
                      Pre-Ingredient = 生料(WorkableItem.m_nextPrefab 说明切完变成什么)
                      Ingredient     = 可直接用的成品
                      Crate          = 食材箱(PickupItemSpawner.m_itemPrefab)
  · 灶台要求    : CookingHandler.m_stationType
                      Hob=煮锅 Oven=烤箱 DeepFatFryer=炸锅 FirePit=火坑 Barbeque=烤架 ...
  · 组装顺序无关: CompositeAssembledNode.AssumeTypeMatch 用集合配对(Contains), 不看顺序
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------- 食材知识

@dataclass
class Item:
    tag: str = ""
    name: str = ""
    ing: str = ""          # 物体代表的食材名
    x: float = 0.0
    z: float = 0.0
    next: str = ""         # 切完变成什么(有值 = 可以切)
    stages: int = 0        # 切片数
    station: str = ""      # 要哪种灶(有值 = 可以煮)
    cookTime: float = 0.0  # 熟的时间(超过 2 倍就焦)
    spawn: str = ""        # 箱子里的 prefab 名
    spawnIng: str = ""     # 箱子直接出的食材名
    spawnNext: str = ""    # 箱子出的生料切完后是什么
    spawnStages: int = 0   # 生料的切片数
    prefab: bool = False   # true = 来自 prefab 资源(没位置, 只用于查加工参数)

    @property
    def cookable(self) -> bool:
        return bool(self.station)

    @property
    def workable(self) -> bool:
        return bool(self.next)


def item_from_json(d: dict) -> Item:
    return Item(
        tag=d.get("tag", ""), name=d.get("name", ""), ing=d.get("ing", ""),
        x=float(d.get("x", 0)), z=float(d.get("z", 0)),
        next=d.get("next", ""), stages=int(d.get("stages", 0)),
        station=d.get("station", ""), cookTime=float(d.get("cookTime", 0)),
        spawn=d.get("spawn", ""), spawnIng=d.get("spawnIng", ""),
        spawnNext=d.get("spawnNext", ""), spawnStages=int(d.get("spawnStages", 0) or 0),
        prefab=bool(d.get("prefab", False)),
    )


class Knowledge:
    """食材知识表。用名字把"订单要的东西"接到"场景里的货源/加工手段"。"""

    def __init__(self, items: list[Item]):
        self.items = items

    @classmethod
    def from_json(cls, payload: dict) -> "Knowledge":
        return cls([item_from_json(d) for d in (payload.get("items") or [])])

    def by_tag(self, tag: str) -> list[Item]:
        return [i for i in self.items if i.tag == tag]

    @property
    def pre(self) -> list[Item]:
        """生料(需要切/加工)"""
        return self.by_tag("Pre-Ingredient")

    @property
    def ready(self) -> list[Item]:
        """成品食材(拿来就能用)"""
        return self.by_tag("Ingredient")

    @property
    def crates(self) -> list[Item]:
        return self.by_tag("Crate")

    # ---- 查询 ----
    def raw_for(self, ing: str) -> Item | None:
        """要得到 ing, 场上有没有需要先切的生料(有 next == ing 的物体)。

        注意: **不能用 Unity Tag 判断是否需切** —— 实测同一关卡里生虾的 tag 是
        Ingredient、生鱼却是 Pre-Ingredient, 靠 tag 会漏。看 next 字段才可靠。
        也只看场景实例(prefab 没有位置, 导航不过去)。
        """
        for i in self.items:
            if not i.prefab and i.next == ing:
                return i
        return None

    def ready_for(self, ing: str) -> Item | None:
        """场上有没有现成可拿的 ing(不需要再加工的成品)。"""
        for i in self.items:
            if not i.prefab and i.ing == ing and not i.next:
                return i
        return None

    def crate_for(self, ing: str) -> Item | None:
        """哪个箱子能提供 ing(直接出成品, 或出需要切的生料)。"""
        for c in self.crates:
            if c.spawnIng == ing:
                return c
        for c in self.crates:
            if c.spawnNext == ing:
                return c
        return None

    def cook_tool_for(self, ing: str) -> Item | None:
        """煮 ing 要用什么。可能是食材自带灶台要求(直接放灶台上),
        也可能是"得装进能煮的容器"(例如米饭要放进锅 utensil_pot_01)。"""
        for i in self.items:
            if i.ing == ing and i.cookable:
                return i
        return None

    def cook_container(self) -> Item | None:
        """能煮的容器(锅/平底锅)。米饭这类食材自己没有 CookingHandler, 只能靠容器。"""
        for i in self.items:
            if i.cookable and ("pot" in i.name.lower() or "pan" in i.name.lower()):
                return i
        for i in self.items:
            if i.cookable:
                return i
        return None

    def describe(self) -> str:
        lines = []
        for tag, label in (("Pre-Ingredient", "生料(需加工)"), ("Ingredient", "成品食材"),
                           ("Crate", "食材箱"), ("Utensil", "厨具")):
            its = self.by_tag(tag)
            if not its:
                continue
            lines.append(f"[{label}] {len(its)}")
            seen = set()
            for i in its:
                key = (i.ing, i.next, i.station, i.spawnIng, i.spawnNext)
                if key in seen:
                    continue
                seen.add(key)
                bits = [f"ing={i.ing or '?'}"]
                if i.next:
                    bits.append(f"切{i.stages}次→{i.next}")
                if i.station:
                    bits.append(f"灶={i.station}({i.cookTime:.0f}s后熟,超{2*i.cookTime:.0f}s焦)")
                if i.spawnIng:
                    bits.append(f"出={i.spawnIng}")
                if i.spawnNext:
                    bits.append(f"出生料→切后={i.spawnNext}")
                lines.append(f"   {' '.join(bits)}   @({i.x:.1f},{i.z:.1f})")
        return "\n".join(lines)


# ---------------------------------------------------------------- 配方树遍历

def walk(node, chain: tuple = ()):
    """深度遍历配方树, 产出 (kind, name, chain)。

    chain 是外层加工链, 例如 ('cook','Cooked') → 说明这个叶子需要煮到 Cooked。
    只走 i/o 字段, 不碰任何迭代器(游戏里 IngredientAssembledNode 的迭代器会自引用死循环)。
    """
    if not isinstance(node, dict):
        return
    k = node.get("k")
    if k in ("ing", "item"):
        yield k, node.get("n", ""), chain
        return
    if k == "null":
        return
    mark = (k, node.get("p"))
    for child in node.get("i") or []:
        yield from walk(child, chain + (mark,))
    for child in node.get("o") or []:
        yield from walk(child, chain + (("opt", None),))


def steps_text(node) -> str:
    """把配方树压成一行人类可读文本。"""
    if not isinstance(node, dict):
        return "?"
    k = node.get("k")
    if k == "ing":
        return node.get("n", "?")
    if k == "item":
        return "@" + node.get("n", "?")
    if k == "null":
        return "-"
    inner = "+".join(steps_text(c) for c in (node.get("i") or []))
    if k == "cook":
        p = node.get("p") or "Cooked"
        return f"煮{p}{{{inner}}}" if p != "Cooked" else f"煮{{{inner}}}"
    if k == "mix":
        # 和 cook 一样**带进度档**: 游戏匹配时 `m_progress` 要**精确相等**
        # (`MixedCompositeAssembledNode.cs:38`), Unmixed/Mixed/OverMixed 是三种
        # 不同的要求 —— 只画个 `搅{}` 会把它们抹成一样。
        p = node.get("p") or "Mixed"
        return f"搅{p}{{{inner}}}" if p != "Mixed" else f"搅{{{inner}}}"
    opt = node.get("o") or []
    if opt:
        inner += "  [可选:" + "+".join(steps_text(c) for c in opt) + "]"
    return inner


# ---------------------------------------------------------------- 叶子 → 货源

def _same_family(a: str, b: str) -> bool:
    """两个名字看着是不是**同一样食材** —— DLC 前缀/下划线/大小写都不算区别。

    只用来**报可疑**, 不做判据(判据是 `next == 叶子名`, 那是游戏自己的数据)。
    例: `DLC10_Grapes` vs `Grapes` → 同类; `SushiFish` vs `SushiFish` → 同类。
    """
    import re as _re

    def n(s: str) -> str:
        s = _re.sub(r"^dlc\d+[_]?", "", (s or "").lower())
        return "".join(ch for ch in s if ch.isalnum())

    x, y = n(a), n(b)
    return bool(x) and bool(y) and (x in y or y in x)


def resolve_leaf(kb: "Knowledge", name: str) -> dict:
    """菜谱叶子 → **取哪个东西、切不切、依据是什么**。

    ☠ **这是唯一的判据**: `derive()` 和 `audit_leaves()` 都走这里。
      为什么必须只有一份 —— 本项目已经踩过两次"同一件事两处各写一份":
        · `OP_PREREQ` 和 `derive()` 对"先煮还是先搅"给了**相反**的顺序
        · `brief.py` 按猜的键名找游戏那条链, 键名根本不存在 ⇒ 它一直在骗人
      所以自检**不允许**自己再写一遍解析逻辑, 只能调这个函数。
    """
    raw = kb.raw_for(name)
    ready = kb.ready_for(name)
    crate = kb.crate_for(name)
    from_crate_raw = crate is not None and crate.spawnNext == name
    warn = []

    # **撞名**: 多个东西切开都叫这个名字 → `raw_for` 只回第一个, 可能取错。
    same = [i for i in kb.items if not i.prefab and i.next == name]
    if len(same) > 1:
        warn.append("有 %d 个东西切开都叫 %s, 只取了第一个(%s)"
                    % (len(same), name, (same[0].ing or same[0].name)))
    crates_hit = [c for c in kb.crates if c.spawnNext == name]
    if len(crates_hit) > 1:
        warn.append("有 %d 个箱子都出这种生料, 只取了第一个" % len(crates_hit))
    # **多来源**: 既有要切的、又有现成的 → derive 会优先切(多花几刀)
    if (raw is not None or from_crate_raw) and ready is not None:
        warn.append("既有要切的又有现成的 —— derive 会**优先切**")

    if raw is not None or from_crate_raw:
        if raw is not None:
            src = crate or raw
            fetch = raw.ing or raw.name
            stages = raw.stages
            basis = "场上有东西 next==%s" % name
        else:
            src, fetch, stages = crate, (crate.spawnIng or crate.spawn or name), crate.spawnStages
            basis = "箱子 spawnNext==%s" % name
        if not _same_family(fetch, name):
            warn.append("来源 %r 与叶子 %r 名字看着不是一类 —— 可能接错了货源" % (fetch, name))
        return {"leaf": name, "kind": "chop", "fetch": fetch, "src": src,
                "stages": stages, "basis": basis, "warnings": warn}

    if ready is not None:
        return {"leaf": name, "kind": "ready", "fetch": name, "src": crate or ready,
                "stages": 0, "basis": "场上有现成的(没有 next)", "warnings": warn}
    if crate is not None:
        return {"leaf": name, "kind": "crate", "fetch": name, "src": crate,
                "stages": 0, "basis": "箱子直接出这个", "warnings": warn}

    warn.append("这道菜做不了: 场上/箱子里都没有 %s 的货源" % name)
    return {"leaf": name, "kind": "none", "fetch": "", "src": None,
            "stages": 0, "basis": "**没找到货源**", "warnings": warn}


def audit_leaves(kb: "Knowledge", leaves: list) -> list:
    """一批菜谱叶子的**来源自检** —— 逐条摊开解析结果 + 可疑之处。

    为什么要有: `derive()` 是按**名字**把叶子接到货源上的(`next == 叶子名`),
    这条匹配在几种情形下会悄悄选错, 而症状(卡在摆盘/多做一步/取错东西)隔得很远。
    这个函数把那几种情形**当场点出来**, 让新关卡/DLC 一进来就能被看见。
    """
    seen, out = set(), []
    for name in leaves:
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(resolve_leaf(kb, name))
    return out


# ---------------------------------------------------------------- 流程推导

@dataclass
class Op:
    action: str            # fetch / chop / cook / mix / plate / assemble / deliver
    target: str
    note: str = ""
    wait: float = 0.0      # 需要等待的秒数(煮)
    optional: bool = False
    at_name: str = ""      # 去哪做(箱子/盘子堆的物体名)
    at_x: float = 0.0
    at_z: float = 0.0
    chop_stages: int = 0   # 要切几片(WorkableItem.m_stages)
    #: 这个材料必须**装进锅(能煮的容器)里**再放到灶上煮, 不能直接放在灶台上。
    #: 判据见 derive(): 配方说要煮, 但食材自己身上没有 CookingHandler(没有 m_stationType),
    #: 就只能是容器在煮 —— 典型例子是米饭 SushiRice。
    #: 这种菜"取出来"的方式也不同: **手拿空盘对着锅按交互**, 锅留在灶上不动。
    in_pot: bool = False
    #: **紧迫度加成分**(分)。目前只有一种东西用它: `rescue`(锅快糊了) ——
    #: 分随"离糊还有多远"线性上涨, 见 `scoring.burn_urgency`。
    #: 放在 `Op` 上而不是另开一张表: 候选的紧迫度是**这一轮探测时算出来的瞬时值**,
    #: 跟着候选走最不容易失真(另存一张表就得在候选增删时同步维护)。
    urgency: float = 0.0
    #: **这条候选是"回溯"提出来的**(`Engine._redos`: 下游那一步要的料没了 ⇒ 把产出它的
    #: 上游那一步重新提出来)。用户 2026-09-15: "菜谱不是链吗, 走不通就回溯到上一级…
    #: **就不应该发呆**"。
    #: ⚠ 它的 `action` 是**现成的**(`fetch`/`chop`/`cook`), 所以**不能靠 `action` 认它**
    #:   —— `_chore_admitted` 要放它过闸门(和 `pass`/`rescue` 同理: 它是"链断了"时的
    #:   唯一出路, 不是"顺手做的杂活"), 而 `NEKO_CHORES=0` 那个总开关**不该顺手把
    #:   链的自修也关掉**。给个显式标记, 别让判据依赖"现在恰好没有 fetch 类杂活"。
    redo: bool = False

    def __str__(self) -> str:
        extra = f"  ({self.note})" if self.note else ""
        if self.wait:
            extra += f"  [等{self.wait:.0f}s]"
        return f"{self.action:<9} {self.target}{extra}"


@dataclass
class DishFlow:
    name: str
    plate: str = ""        # 订单要求的容器(OrderDefinitionNode.m_platingStep)
    ops: list = field(default_factory=list)

    def __str__(self) -> str:
        head = f"【{self.name}】" + (f" 容器={self.plate}" if self.plate else " 容器=无")
        body = "\n".join(f"  {i+1}. {op}" for i, op in enumerate(self.ops))
        return f"{head}\n{body}"


def derive(detail: dict, kb: Knowledge) -> DishFlow:
    """把一道菜的配方推成可执行步骤序列(含"去哪做")。

    关键约束: **厨师一次只能拿一个东西**。所以不能"连续取两个材料再组装",
    必须每个材料处理完就放到组装台面腾出手 —— 否则第二个 fetch 会把第一个材料放回去。
    """
    flow = DishFlow(name=detail.get("name", "?"), plate=detail.get("plate", "") or "")
    tree = detail.get("tree")
    ops = []

    for kind, name, chain in walk(tree):
        kinds = [c for c, _ in chain]
        cooked = "cook" in kinds
        mixed = "mix" in kinds
        optional = "opt" in kinds

        if kind == "item":
            ops.append(Op("tool", name, "订单要求的器皿/成品物件", optional=optional))
            continue

        # 「这个叶子取什么、切不切」**只有一份判据**(`resolve_leaf`), 见那里的注释 ——
        # 自检 (`audit_leaves`) 走的是同一个函数, 不允许另写一遍。
        res = resolve_leaf(kb, name)
        src = res["src"]
        if res["kind"] == "chop":
            ops.append(Op("fetch", res["fetch"], f"生料, 来自 {src.name}",
                          optional=optional,
                          at_name=src.name, at_x=src.x, at_z=src.z))
            stages = res["stages"]
            ops.append(Op("chop", name,
                          f"切到变成 {name}" + (f" ({stages} 片)" if stages else ""),
                          optional=optional, chop_stages=stages))
        elif res["kind"] == "ready":
            ops.append(Op("fetch", name, f"直接取成品, 来自 {src.name}",
                          optional=optional,
                          at_name=src.name, at_x=src.x, at_z=src.z))
        elif res["kind"] == "crate":
            ops.append(Op("fetch", name, f"从箱子 {src.name} 取",
                          optional=optional,
                          at_name=src.name, at_x=src.x, at_z=src.z))
        else:
            ops.append(Op("fetch", name, "⚠ 找不到货源(箱子/生料/成品都没匹配上)",
                          optional=optional))

        if cooked:
            tool = kb.cook_tool_for(name)
            if tool is not None:
                note = f"用 {tool.station}, {tool.cookTime:.0f}s 熟 / 超 {2 * tool.cookTime:.0f}s 就焦"
                wait = tool.cookTime
                in_pot = False
            else:
                # 配方说要煮, 但这个食材自己没有 CookingHandler(m_stationType 为空)
                # ⇒ 煮它的是**容器**(锅/平底锅), 必须"装进锅里再放到灶上"。
                # 典型: 米饭 SushiRice(实测 s_sushi 系关卡的锅 utensil_pot_01 带
                # CookableContainer + CookingHandler, 米饭本身不带)。
                note = "需装进锅(能煮的容器)后放灶上 —— 取菜时手拿空盘对锅按交互, 锅留在灶上"
                wait = 0.0
                in_pot = True
            ops.append(Op("cook", name, note, wait=wait, optional=optional,
                          in_pot=in_pot))
        if mixed:
            ops.append(Op("mix", name, "需要搅拌", optional=optional))

        # 这个材料处理完 → 立刻放到组装台面, 把手腾出来给下一个材料
        if not optional and not kind == "item":
            ops.append(Op("assemble", name, "把这个材料放到组装台面"))

    if any(op.action != "tool" for op in ops):
        # 这里**不生成"取盘子"步骤**: 摆盘不是独立动作。
        # 游戏里食材是对着"已经有盘子的台面"放下就自动进盘
        # (PlacementContainer + IngredientToContainerBehaviour.TransferToContainer),
        # 而台面上本来就有现成盘子 —— 所以引擎挑摆盘位时直接挑"有盘子的台面"即可,
        # 材料放上去就是摆盘。只有台面上一个盘子都没有时才需要真去拿一个。
        ops.append(Op("deliver", flow.name, "端起盘子送到送餐口(PlateStation)"))
    flow.ops = ops
    return flow
