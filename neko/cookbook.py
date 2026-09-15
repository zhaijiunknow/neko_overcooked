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
    #: **这个食材允许进哪些容器**(加热方式), 元素是 `CookingStepData.m_uID`。
    #:
    #: 权威判据(反编译, 规则 1):
    #:   `CookableContainer.cs:46-47` `cp.AllowsCookingStep(_handler.AccessCookingType)`
    #:   `CookableProperties.cs:11-13` 比的是 `CookingStepData.m_uID`
    #: ⇒ "**米进锅、肉进平底锅**"这条规矩就长在这儿, 不在名字上。
    #: ⚠ 空列表 = **拿不到这件信息**(老 dll / 这食材没有 `CookableProperties`)
    #:   ⇒ 调用方**退回老行为**, 别把它当成"哪儿都不能进"。
    cook_steps: list = field(default_factory=list)

    def allows_cook(self, cook_id: int) -> bool:
        """这个食材能不能进"加热方式 = `cook_id`"的容器。**不知道就放行**(退回老行为)。"""
        if not self.cook_steps or not cook_id:
            return True
        return int(cook_id) in self.cook_steps

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
        #: `cookSteps` = `[{"id":N,"name":"..."}]`(见 `Item.cook_steps`)。
        #: ⚠ 老 dll 没这个键 ⇒ 空列表 ⇒ `allows_cook` 一律放行(退回老行为)。
        cook_steps=[int(s.get("id", 0) or 0)
                    for s in (d.get("cookSteps") or []) if s],
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
    def raw_for(self, ing: str, scene_only: bool = True) -> Item | None:
        """要得到 ing, 有没有需要先切的生料(有 `next == ing` 的物体)。

        注意: **不能用 Unity Tag 判断是否需切** —— 实测同一关卡里生虾的 tag 是
        Ingredient、生鱼却是 Pre-Ingredient, 靠 tag 会漏。看 `next` 字段才可靠。

        ☠☠ **`scene_only=False` 是给"判要不要切"用的**(`resolve_leaf` 用的就是它) ——
          "切完变什么、切几片"这些加工参数**只存在于 prefab 上**, 这是 C# 那边
          `ItemKnowledge.Snapshot()` 第 2 段自己写的理由:
            *"食材还在箱子里时场景里根本没有它的实例, 但'要用什么灶、煮多久、切几片'
              这些加工参数只存在于 prefab 上, 不扫就查不到(煮这一步会直接失败)"*
          ⇒ 开局限定"只看场景实例"时, 生鱼还没实例化 ⇒ 生料这一条查不到 ⇒
            `resolve_leaf` 落空 ⇒ **整条链丢掉 `chop`** ⇒ 脚本拿到生料**直接去装盘**
            (2026-09-15 实机打回来的"0 分那一局"就是这么来的)。
        ⚠ 但 **"去哪取"仍然必须只看场景实例**(默认 `scene_only=True`):
          prefab 没有位置, 导航不过去(`audit_leaves` 和 `resolve_leaf` 的
          `src = crate or raw` 用的是那一份)。
        """
        for i in self.items:
            if scene_only and i.prefab:
                continue
            if i.next == ing:
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
    raw = kb.raw_for(name)                      # 场景实例 —— "**去哪取**"要用它(有位置)
    # 判"**要不要切**"时**连 prefab 一起看**: 加工参数(prefab 名/切几片)只写在 prefab 上,
    # 开局限定"只看实例"会让整条链丢掉 `chop`(见 `raw_for` 的长注释)。
    # ⚠ `src` 仍然只取场景实例/箱子 —— prefab 没有位置, 导航不过去。
    raw_k = raw if raw is not None else kb.raw_for(name, scene_only=False)
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
    # ⚠ 判据跟着**实际走的那个分支**用 `raw_k`(连 prefab 一起看), 否则这条提示
    #   会和 `derive` 真做的事对不上(而"提示骗人"正是这个文件里踩过的坑)。
    if (raw_k is not None or from_crate_raw) and ready is not None:
        warn.append("既有要切的又有现成的 —— derive 会**优先切**")

    if raw_k is not None or from_crate_raw:
        if raw_k is not None:
            src = crate or raw          # ⚠ `raw` 才是场景实例; prefab 不进 `src`
            fetch = raw_k.ing or raw_k.name
            stages = raw_k.stages
            basis = ("场上有东西 next==%s" % name) if raw is not None else \
                    ("**prefab** 上写着 next==%s(还没实例化)" % name)
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
    #: **这条候选是"提前备料"提出来的**(`Engine._preps`: 订单栏要 N 份而现有 M 份 ⇒
    #: 把缺的那份的链上第一环提出来)。用户 2026-09-15 的"延迟收益":
    #: "当前这一步做不了的时候…**提前去切三条鱼**"。
    #: ⚠ 份数由 `lookahead.demand` **从订单算**(用户点名: "**别把 3 写进代码**")。
    #: ⚠ 和 `redo` 一样: `action` 是**现成的**(`fetch`/`chop`/`cook`), 所以认它只能靠
    #:   这个显式标记 —— 日志里也靠它打出"备料"两个字。
    prep: bool = False
    #: **这条 `pass` 替的是菜谱里的哪一步**(`"chop"` / `"cook"` / `"mix"` / `"assemble"`)。
    #: 只有 `pass` 会填它 —— 其余动作留空串。
    #:
    #: 为什么要它: `pass` op 自己的 `action` 是 `"pass"`、`target` 是那份料,
    #: **底层动作只写在 `note` 那串人话里**。而"传递指令"的台账要按
    #: `(被交出去那一步的 action, target)` 记账(记一笔、止重复提、拦回溯都要查它),
    #: 解析 `note` 拿字符串当判据是**不能接受**的 —— 见 `redo` 那段注释:
    #: "后者等于赌'以后不会有 fetch 类杂活'"。所以给个显式字段。
    handoff: str = ""
    #: **这一步的货源根本没解析出来**(`resolve_leaf` 落到了"没找到货源"那条兜底)。
    #:
    #: 它是"**这张知识表看着不全**"的**症状信号** —— 引擎据此重拉一次知识表
    #: (见 `Engine._derive_with_retry`)。为什么要有这个显式标记:
    #:   `resolve_leaf` 的 `raw_for`/`ready_for` **只看场景实例**(`not i.prefab`),
    #:   而知识表是**每场景拉一次的备份数据** ⇒ 传送带关卡开局时食材 prefab
    #:   **还没实例化** ⇒ 表里光秃秃 ⇒ 整条链**丢掉 `chop`**, 脚本拿到生料直接去装盘。
    #: ⚠ 认它只能靠这个字段, **不能去解析 `note`** —— 和 `redo`/`prep` 同一个理由
    #:   (那条注释在这儿也得再说一遍: 拿人话当判据等于赌"以后不会改措辞")。
    nosrc: bool = False
    #: **救的是哪个容器**(锅/平底锅/搅拌碗…)。只有 `rescue` 会填。
    #:
    #: 为什么要它: `rescue` 的动作**按容器形态分派** ——
    #:   · 锅/平底锅(`CookingUtensil`)是**可以端走**的 ⇒ "端下灶 → 取菜 → 放回";
    #:   · **搅拌碗/烤箱**是长在设备上的 ⇒ **没有东西可端**, 正解只"拿盘取菜"。
    #: 而 `rescue` 的 `at_name` 是**挂载点**(台面名, 见 `_rescues`), 容器自己的名字会丢
    #: ⇒ 给个显式字段, 别让 `op_rescue` 去猜(这个文件里"别赌"的教训写过好几次)。
    #: 空串 = 没填 ⇒ 退回老行为(按锅处理)。
    vessel: str = ""

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

    ☠☠ **已知未做: "搅拌碗"这条链是错的**(2026-09-15, 用户给了完整规格, 照抄在下)。

      现在的行为: 对**每个叶子**各发一条 `mix`
        (`if mixed: ops.append(Op("mix", name, "需要搅拌", …))`)
      ⇒ 一棵"蛋+面粉"的树会**搅两次、每次一份**, 而实际是**一次搅、一个碗、≤4 份一起进**。
      这是"**食材的处理顺序**"那块最直接的错(用户最早点名的那句)。

      用户的规格(原话):
        > "碗的容量是'**≤4 份任意处理过的料**'。**蛋和面粉直接放进碗就行**。
        >  搅完之后**碗里是一个成品**, **碗的内容物无法和盘子交互**。"
        > "**搅拌碗和烤箱其实可以端走**, 搅拌器里放了搅拌碗, ……**并且搅拌碗可以拿下来**,
        >  **烤箱的前置是搅拌碗**, 需要把**搅拌完的搅拌碗拿到烤箱前交互**。"

      ⇒ 正确的链该是(每个 `mix` 节点一组):
        `逐份 fetch → [chop] → [cook] → **放进碗** …(≤4 份) → **搅**(一次)`
        → `**端下碗** → **端到烤箱前交互**(烤箱的前置是**碗**)` → 之后才是摆盘/送餐。

      ☠☠ **为什么不能只改这里的分组**: `mix` 的可行性判据是
        `_held_is(held, op.target)`("手上必须拿着要搅的那个", `_op_actionable` 的 mix 分支),
        而"一组"不是一个能拿在手上的东西 ⇒ **只改分组会让那条 `mix` 永远不可执行**
        (target 没有合法取值) ⇒ 比现在更坏。**必须连同下面三件一起做**:
          ① **"碗"成为一个实体**: 位置/挂载点/内容物/容量 ≤4
             (`ScanMixing` 已经报出碗的名字/坐标/挂载点/内容物 —— 见 `map_model.Cooking`);
          ② **"把料放进碗"的动作**: `assemble` 是"放进**盘**", 碗是另一个放置目标
             (依据: 游戏里对着**碗**按放置 ⇒ 料进碗; 对着**盘**⇒ 进盘);
          ③ **`mix` 的判据改成"碗在搅拌器上、碗里有这一组"**, 而不是"手上拿着某个料"。
        ⚠ 做完之后 `derive` 这一处才跟着改(分组 + 不再对每个叶子发 `mix`)。
        ⚠ 验证手段现成: `tools/brief.py --chain` 会把"游戏认的链 vs `derive` 产的链"并排打出来。
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
            # ☠ **`src` 可能是 `None`** —— "判要不要切"连 prefab 一起看
            #   (`raw_for(..., scene_only=False)`), 而 **prefab 没有位置、导航不过去**
            #   ⇒ 它不进 `src`(见 `resolve_leaf`)。这时 `at_name`/坐标**留空**,
            #   由 `_op_target_for_score` **运行时**解析真实货源
            #   (计划坐标 → 实时台面 → 可达箱子 → **地上的料**)。
            #   ⚠ **绝不能拿 prefab 名当取货点** —— 那是一类没有位置的资源,
            #     `_stand_cell_of` 会拿不到站位格 ⇒ 整步判"够不着"。
            if src is None:
                ops.append(Op("fetch", res["fetch"],
                              f"生料({res['basis']}) —— 取货点**运行时再解析**",
                              optional=optional))
            else:
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
            # `nosrc=True` = "**这一步的货源没解析出来**"。这是"知识表看着不全"的
            # 症状信号, 引擎据此重拉一次(见 `Op.nosrc` 与 `Engine._derive_with_retry`)。
            ops.append(Op("fetch", name, "⚠ 找不到货源(箱子/生料/成品都没匹配上)",
                          optional=optional, nosrc=True))

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
