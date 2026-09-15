# -*- coding: utf-8 -*-
"""前瞻备料: 由**订单栏**算出"每种材料还该备几份"。

为什么要这一层 —— 用户 2026-09-15 提的"延迟收益":

  > "像这种都不可用的, 直接把盘子放地上去做其他事就行了, 比如可以做一些**延迟收益**,
  >  **提前去切三条鱼**。"

随后点名的约束(这条比前面那句重要):

  > "它得从'**当前场上有几张单、每张要几条**'**推**出来。"
  > "**'切三条'是举例, 不是常数。** … **别把 3 写进代码。**"

现在做不到的原因有两个, 都核过:
  ① **评分是近视的** —— `chop`(30) 打不过 `assemble`(60)/`deliver`(100) ⇒ 脚本永远
     "先把眼前这一环做完";
  ② **"备多少"是死数** —— 全仓唯一的批量备料是 `Engine._prep_and_toss`, 份数来自
     `TOSS_BATCH_MAX = NEKO_BATCH or 3` —— **写死的 3**, 和订单、配方都无关。

☠☠ **树那半不用自己做。** 最初的设想是"模拟对食材的每一步处理 dfs+剪枝", 核完发现
  那一步**已经存在**: `cookbook.derive()` 就是把配方树走成线性链的那次遍历
  (`fetch → [chop] → [cook|mix] → assemble`), 而且它**纯静态、与"现在在做哪单"解耦**;
  而 `state.details` 报的是**整关菜谱池**、不是只有当前单(`StateCollector.cs:364-385`)
  ⇒ **任意一张挂单都能推出完整 `DishFlow`**, 不需要任何新桥接。
  ⇒ 于是这里只剩两件事: **跨订单汇总** + **扣掉现有库存**。别把 `derive` 做过的重做一遍。

**为什么必须是纯函数**: "备多少"是这套东西里唯一需要**看未来**的判断 —— 它一旦依赖
  桥/游戏状态, 就只能靠"打一局看运气"来验。写成 `(订单, 库存) → 缺口` 之后,
  "两张单都要鱼 ⇒ 缺 2 条" 这种断言能**离线钉死**(见 `runtime/_lookahead_probe.py`)。
"""

from __future__ import annotations

import os

from map_model import _norm_name as norm

#: **整个备料计划**最多几份(**总份数**, 不是每种几份) —— 这是**刹车, 不是份数的来源**。
#:
#: 为什么是总份数而不是"每种上限": 管这个数的是**一局的时间预算** ——
#: 备料是"主链受阻时"才做的副业, 同时铺开 4 种料 × 每种 4 份 = 16 件,
#: 一局 150 秒根本做不完。而"每种上限"既管不住总量, 又让**紧急度**失去意义
#: (每种各算各的, 先算哪张单一模一样)。
#: ⚠ **别把它当成"默认备 3 份"那种常数用** —— 份数永远由订单算出来;
#:   订单要 2 份就是 2 份, 这个数只在"订单要得太多"时刹一脚。
#: `NEKO_PREP_MAX` 可调。
PREP_MAX = int(float(os.environ.get("NEKO_PREP_MAX") or 4))

#: **每种材料在"订单算出来的份数"之上多备几份** —— 用户 2026-09-15:
#: "提前备料的份数**可以比订单多一两份**"。
#:
#: 为什么该有余量(三条都是实测会遇到的):
#:   · **订单会陆续来** —— 备料本来就是为"下一张单"做的, 只按当前订单栏算会永远差一口;
#:   · **人类队友也会拿** —— 我们备的料不专属;
#:   · **我们自己会失手** —— 掉地上、被传走、切废。
#:
#: ⚠ **它和"别把 3 写进代码"不冲突**: 份数**仍然由订单算**(`demand` 的累加那半),
#:   这一项是**额外的一层余量**, 不是份数的来源。`0` = 严格按订单(回到老行为)。
#: `NEKO_PREP_BUFFER` 可调。
PREP_BUFFER = int(float(os.environ.get("NEKO_PREP_BUFFER") or 1))


def required(flow) -> list:
    """一张单**必需**哪些材料 —— 归一化名, **按出现次数**(同名料要两份就出现两次)。

    只看 `assemble` 且**非 `optional`** 的步骤:
      · `assemble` 是 `derive()` 给**每个材料**收尾的那一步(链是 `fetch → [chop] →
        [cook|mix] → assemble 同名`), 所以"这张单要哪些料"就是数它的 `assemble`;
      · **可选材料不加也能成菜**(`cookbook.derive` 里 `optional` 的来历), 所以
        不备它**也不算缺** —— 把它算进需求会让脚本去备一堆用不上的东西。

    ⚠ 数的是**次数**不是"有没有": 同名的 `assemble` 出现两次就是两份。
    """
    out = []
    for op in getattr(flow, "ops", None) or []:
        if getattr(op, "action", "") != "assemble":
            continue
        if getattr(op, "optional", False):
            continue
        n = norm(getattr(op, "target", ""))
        if n:
            out.append(n)
    return out


def demand(flows, have=None, cap=None, buffer=None) -> dict:
    """按订单栏算出**每种材料还缺几份**。返回**只含缺口 > 0** 的 `{材料: 份数}`。

    `flows` —— `[(DishFlow, 剩余时间比例 t), …]`。**按 `t` 升序**满足
              (`t` 越小越紧急, 见 `Engine.live_orders` 的排序):
              `cap` 用完时, 落空的是**排在后面**那几张单 —— 这是 `t` **唯一**起作用的地方。
    `have`  —— `{归一化材料名: 现有份数}`, 来自 `Engine._inventory`。
    `cap`   —— **整个计划的总份数上限**(不是每种), `None` 用 `PREP_MAX`。
              **它只是刹车**: 订单要 2 份 ⇒ **2**(不是 4); 订单要 9 份而 cap=4 ⇒ 4。
    `buffer` —— 每种材料在订单份数之上**多备几份**, `None` 用 `PREP_BUFFER`(默认 1)。
              用户 2026-09-15: "提前备料的份数**可以比订单多一两份**"。
              **不受 `cap` 约束**(理由见常量注释)。`0` = 严格按订单。

    顺序是 **先按订单汇总(带总上限) → 加余量 → 再扣库存**:
    "订单要 5、最多备 4、余量 1、现有 2" ⇒ 缺 3。
    这样 `cap` 的含义始终是"**这张订单栏最多值得铺开几件**", 而库存算在计划之内
    (已经躺在那儿的当然也算占了一件)。

    **订单栏空 ⇒ 返回 `{}`** —— 调用方据此"什么都不提"。
    """
    cap = PREP_MAX if cap is None else int(cap)
    buf = PREP_BUFFER if buffer is None else int(buffer)
    need, used = {}, 0
    for item in sorted(flows or [],
                       key=lambda ft: float(ft[1] if ft[1] is not None else 1.0)):
        flow = item[0]
        if flow is None:
            continue
        for n in required(flow):
            if used >= cap:
                # 额度用完 ⇒ 后面更不紧急的单就不算了。
                # ⚠ **但余量照加**(见下), 所以不能在这里直接 return。
                break
            need[n] = need.get(n, 0) + 1
            used += 1
        if used >= cap:
            break
    # ★ **余量**: 每种材料在"订单推出来的份数"之上多备 `buf` 份(用户要求)。
    #   ⚠ **不受 `cap` 约束** —— `cap` 管的是"订单推出来多少"(别铺开太大),
    #     而余量的意义本来就是"**比订单多一点**"; 被 cap 吃掉就白设了。
    for n in need:
        need[n] += buf
    return _shortfall(need, have)


def _shortfall(need: dict, have) -> dict:
    """需求 − 现有, 只留 > 0 的。"""
    out = {}
    for n, want in need.items():
        left = want - int((have or {}).get(n, 0) or 0)
        if left > 0:
            out[n] = left
    return out


def shortfall_of(flow, have=None) -> dict:
    """**这一张单自己**还缺哪些料、各缺几份 —— 返回只含缺口 > 0 的 `{材料: 份数}`。

    ☠☠ **它和 `demand` 是两个口径, 别混**(2026-09-15 用户: "**没有完全按菜谱来啊,
      导致两个错单**"):

      · `demand(订单栏, 库存)` —— **订单栏口径**: "整个订单栏一共还该备几份"。
        它是**份数的来源**(用户点名: "别把 3 写进代码"), 也是 `_toss_budget` 用的那个;
      · `shortfall_of(flow, 库存)` —— **本单口径**: "**正在做的这一张**自己还缺几份"。
        它回答的是**提不提**。

    为什么非要分: 备料候选是在**当前这一单的决策循环**里执行的(`_execute_scored(flow)`)。
    只按订单栏口径筛 ⇒ 会**替别的单把料拿到手里** ⇒ 本单要用那只手 ⇒ 按"空手是回退态"
    把料**丢在地上** ⇒ 白跑一趟, 而且那份料从此躺在一个没人找的地方(**两个单一起受伤**)。

    ⚠ **判据是"本单还缺", 不是"本单菜谱里有这个名字"** —— 本单那份**已经拼进盘子**时,
      按订单栏口径它照样算"缺"(那其实是**别的单**的份数) ⇒ 只按名字筛挡不住这一类。

    ⚠ **不含余量**(`PREP_BUFFER`): 余量的意义是"比订单多备一点", 而这里问的是
      "本单**这一份**够了没有"。掺进余量会让"本单已经拼好的那份"又被算成缺
      (要 1 + 余量 1 − 现有 1 = 1 > 0), 判据当场失效。
    """
    need = {}
    for n in required(flow):
        need[n] = need.get(n, 0) + 1
    return _shortfall(need, have)
