"""评分机制 —— 把"这一步该做哪个动作"变成一个可调参的打分问题。

规格出处: `docs/交接-20260914-行为层.md` §4(用户原话):

  "需要引入评分机制, 要是评分太低就需要查询另一方能否执行, 另一方无法执行再由我方执行。
   评分需要**距离, 可达性, 菜谱步骤, 后续行为**。
   评分用在**全局行为驱动**, 还需要引入**状态**: 好帮手是高效(取最优),
   笨手笨脚是随机评分波动, 捣蛋鬼是低效(做一些重复的, 没有意义的)。"

四项各自怎么算:

  · **可达性** —— **硬闸门**: 到不了直接 `-inf`, **不是减分**(规格原话"到不了 = 直接出局")。
  · **距离**   —— 厨师走到"台面旁那个可站的格子"的格距, 每格扣 `W_DIST`。
  · **步骤价** —— 这一步在菜谱里的分量。送餐/摆盘这类"临门一脚"比取料值钱, 见 `STEP_VALUE`。
  · **后续行为** —— 做完这步之后, 离"下一个还要做的目标"有多远(顺路就近)。见 `follow` 参数。

  ⇒ `score = 步骤价 − W_DIST*格距 − W_FOLLOW*顺路格距`; 到不了则 `-inf`。

**本模块是纯函数模块**: 不 import bridge / 游戏 / KitchenMap, 只吃数字和列表。
这样评分逻辑能脱离游戏肉眼核对 —— 本仓硬规则(开发约定 §3)是"能在离线验证的绝不留给实机",
而实机一次要用户手动开一局 150 秒。

调参就改下面这一节常量。
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------- 可调常量

#: 各动作的"步骤价"。送餐 > 摆盘 > 煮 > 搅 > 切 > 取 —— 越靠近"临门一脚"越值钱。
#:
#: **杂活**(press/wash/work/serve_any) 也在这里 —— 它们和菜谱动作**同一张表**比,
#: 这是"全局行为驱动"的落地(交接包 §4.4 / 阶段二)。三种杂活的分工:
#:   · `press`/`wash`/`work`(8~11) —— **故意低于 `YIELD_MIN_SCORE`**:
#:     人类队友更近且没挂机 ⇒ 自动**让位**(规格:"可以让人类处理一部分评分不高的行为");
#:     人类挂机(`AFK_SECONDS`) ⇒ 自己做("如果人类也在挂机, 那确实只能脚本去自己做")。
#:   · `serve_any`(交菜 45) —— **例外, 比阈值高 ⇒ 不让位**: 它是临门一脚,
#:     不该推给人类; 但仍低于 `cook 45`/`assemble 60`/`deliver 100`,
#:     所以不会压过"把自己那盘做完再交"。
STEP_VALUE = {
    "deliver": 100.0,
    "assemble": 60.0,
    "cook": 45.0,
    "mix": 35.0,
    "chop": 30.0,
    "plate": 15.0,
    "fetch": 20.0,
    "tool": 0.0,          # tool/mix 这类 do_op 本来就不处理, 给 0 让它永远排最后
    # ---- 杂活(阶段二) ----
    "pass": 38.0,         # **传球**: 链条上"我这边做不了"的那一环, 把料丢给人类队友
    "serve_any": 45.0,    # 交菜: 台面上已经拼好的一盘, 端去送餐口
    "work": 11.0,         # 加工台面上没加工完的料(切/搅/烘同一条路)
    "wash": 10.0,         # 洗盘子
    "press": 8.0,         # 按机关
    # 救快糊的锅。**基础分故意低**(低于 `YIELD_MIN_SCORE`): 刚进报警时它该让位给
    # 队友/让位给主流程; 真正让它涨起来的是**紧迫度加成分**(见 `burn_urgency`)。
    "rescue": 15.0,
}
STEP_VALUE_DEFAULT = 10.0   # 没见过的动作

#: 锅从"刚报警"到"糊掉"之间, 紧迫度最多能加多少分。
#:
#: **两个交叉点决定了这个数该多大**(算给下一个调参的人看, 别凭手感):
#:   · 压过 `deliver`(100, 表里最高的菜谱动作) 的位置 —— 那之前脚本会"先把手上这盘送完"。
#:     解 `15 + M·(ratio−1) > 100` ⇒ `ratio > 1 + 85/M`。M=200 ⇒ **ratio ≈ 1.43**。
#:   · 高于 `YIELD_MIN_SCORE`(不再让位给队友)的位置: `ratio > 1 + (12−15)/M` ⇒ M=200 ⇒ **1.0**(立刻)。
#:
#: 为什么交叉点要落在 **1.4 左右**(而不是更晚): 剩下的时间 `(2−ratio)·need`, 对 10 秒的菜
#:   · ratio 1.43 ⇒ **还剩约 5.7 秒** —— 够走过去(一格 0.3 秒 + 站位的开销);
#:   · ratio 1.65 ⇒ 只剩 3.5 秒 —— 多半**走到了也来不及**(我第一版取 130 就是这里错)。
#: 而 1.0~1.2 之间(刚熟、还有 8 秒以上)不该抢别人的活 —— 那时让位给站在旁边的人更合理。
BURN_URGENCY_MAX = 200.0

#: **"手上这份先做完"的加成分**。用户 2026-09-15 讲的机制:
#:   > "比如 SushiRice, 想要放在盘子上需要**先煮熟**" —— 生米**永远**上不了盘,
#:   > 它唯一的出路就是进锅; 而腾出手又只能靠"放进盘子"或"丢地上"。
#:
#: 为什么必须有这一项(实测 `s_sushi_1_3`): 手里拿着**生** SushiRice 时, 评分选了
#:   `fetch Cucumber`(4 格, -12.0) 而不是 `cook SushiRice`(20 格, -19.0) —— **纯按距离**,
#:   于是生米一直晾在手上 → `op_fetch` 想把它搁到摆盘位 → **游戏拒收**(生料上不了盘)
#:   → 一整局绕死。加 20 分之后: `cook`(45+20=65) 压过 `fetch`(12) ⇒ 先把手上这份推进。
#:
#: ⚠ 它同时也修好了"材料散落"那一类: 拿着切好的料时, `assemble`(60+20=80) 会压过
#:   "再去取下一份"(≈19) ⇒ 走的是 `derive` 注释里写的节奏——"每个材料处理完就立刻
#:   放到组装台面, 把手腾出来给下一个材料"。
HAND_ADVANCE_BONUS = 20.0

#: 杂活"**明显顺路**"的两个阈值(格)。菜谱还有能做的动作时, 只有同时满足这两条
#: 的杂活才允许插进候选池(用户选的"混合: 能插就插") ——
#:   · `|d(杂活) − d(最近的菜谱候选格)| ≤ CHORE_ENROUTE_CELLS` —— 基本在同一段路上
#:   · `d(杂活) ≤ CHORE_NEARBY_CELLS` —— 而且本来就没几步
#: `d(...)` 全部查**已经算好的**那个距离表(`TerrainMap.distances_from`), 不额外跑 BFS。
#: 菜谱一个可做的都没有时**不看这两个阈值** —— 那时候是"拿不到食材就做别的"。
CHORE_ENROUTE_CELLS = 1
CHORE_NEARBY_CELLS = 3

#: 同一件杂活在一轮 `execute()` 里最多做几次。
#: 为什么需要: 杂活每轮重新生成、且**不从 `pending` 销号**(它不是菜谱的一步),
#: 于是"按钮按了但还是 pressable"这种会让它**反复被选中**。超了就本轮不再入池。
CHORE_REPEAT_MAX = 2

W_DIST = 2.0                # 每格扣多少分
W_FOLLOW = 1.5              # "不顺路"每格扣多少分

#: 低于这个分就"先问另一方能不能做"(规格: "评分太低就需要查询另一方能否执行")。
YIELD_MIN_SCORE = 12.0

CLUMSY_NOISE = 8.0          # 笨手笨脚: 加在分数上的高斯噪声 σ(单位=分)
SABOTAGE_REPEAT_BONUS = 25.0  # 捣蛋鬼: 重复做过的动作额外加这么多分
SABOTAGE_MEMORY = 6         # 捣蛋鬼记住最近几个动作

#: 队友多久没动就当他"没动" —— 规格: "他也到不了/**没动** → 我做"。
#:
#: ⚠ 取 8 秒而不是两三秒, 理由是**人类"站着不动"经常是正常操作**:
#:   切一盘菜要 ~4.5 秒、站在锅边等熟更久, 这期间坐标一动不动。
#:   阈值小了就变成"人家正在干活, 我们把活抢了" —— 比不合作更糟。
#:   (判"动了"的信号除了坐标还带上手持物, 见 `engine._mate`。)
AFK_SECONDS = 8.0

#: 位置变化超过这么多格才算"动了"(世界单位, 一格 ≈ 1.2)。
MATE_MOVE_EPS = 0.25

NEG_INF = float("-inf")


def step_value(action: str) -> float:
    return STEP_VALUE.get(action, STEP_VALUE_DEFAULT)


def burn_urgency(ratio: float) -> float:
    """**锅快糊了该加多少分**。`ratio = prog / need`(已煮秒 / 需煮秒):

      1.0 = 刚进报警窗口 —— 游戏那边就是这一刻开始 `OverDoing`
            (警告图标脉冲 + `GameOneShotAudioTag.CookingWarning` 音效)
      2.0 = 糊了(`Ruined`)

    线性上涨: 刚报警时 **0 分**, 越接近糊涨得越猛(`BURN_URGENCY_MAX`)。
    为什么不设硬阈值闸门(用户 2026-09-15 定的规矩: "如果报警, 对应的评分应该上涨"):
      **涨分自动穿过 `YIELD_MIN_SCORE`** ⇒ 不再让位给队友 ⇒ 我去救;
      而刚报警时它还是低分(低于阈值) ⇒ 让位给正在旁边的人。
      一条连续规则同时表达了"什么时候该让位"和"什么时候必须我上", 不用两个阈值。
    """
    if ratio is None or ratio <= 1.0:
        return 0.0
    return BURN_URGENCY_MAX * min(1.0, float(ratio) - 1.0)


def score(action: str, dist: float | None, follow: float = 0.0,
          urgency: float = 0.0, advance: float = 0.0) -> float:
    """给一个候选动作打分。`dist is None` = 到不了 ⇒ 直接出局(`-inf`)。

    dist    —— 厨师到"台面旁可站格子"的**格距**(不是欧氏距离: 要绕墙走)
    follow  —— 到"下一个还要做的目标"的格距; 没有下一个就传 0(不扣分)
    urgency —— **紧迫度加成分**(见 `burn_urgency`)。位置无关 ⇒ 队友那份也加同样的值,
               否则"我 vs 队友"就不是在比同一件事了。
    advance —— **"这一步用的是我手上正拿着的那份东西"** 的加成分
               (`HAND_ADVANCE_BONUS`)。**谁拿着算谁的** ⇒ 队友那份要按**队友手上**的算。
    """
    if dist is None:
        return NEG_INF
    return step_value(action) - W_DIST * dist - W_FOLLOW * follow + urgency + advance


# ---------------------------------------------------------------- 状态变换


def transform(scores: list, mode: str, rng, sigs=None, recent=None) -> list:
    """**状态 = 对评分向量的变换**(规格 §4.2: "状态不再是在评分向量上额外撒一层随机捣乱")。

    · coop(好帮手)     —— 原样, 于是取最高分 = 高效取最优
    · clumsy(笨手笨脚) —— 每个**有限**分加高斯噪声 ⇒ 常常不是最优, 但不主动害人
    · sabotage(捣蛋鬼) —— 取负(偏好低分) + 重复奖励 ⇒ 自然演成"反复做同一件没意义的事"

    ⚠ `-inf` 在**所有**模式下都是硬闸门, 连噪声都不能翻过它 ——
      否则笨手笨脚/捣蛋鬼会挑到"根本走不到"的动作, 那既不叫笨也不叫捣蛋, 叫卡死:
      撞上 run() 的"连续 3 次失败就停机"(开发约定 §5: 别烧整局)。
    """
    scores = list(scores)
    if mode == "clumsy":
        return [s if s == NEG_INF else s + rng.gauss(0.0, CLUMSY_NOISE) for s in scores]
    if mode == "sabotage":
        out = []
        for i, s in enumerate(scores):
            if s == NEG_INF:
                out.append(NEG_INF)
                continue
            v = -s
            if sigs and recent and sigs[i] in recent:
                v += SABOTAGE_REPEAT_BONUS       # 做过的再做一遍 —— "重复的、没有意义的"
            out.append(v)
        return out
    return scores                                 # coop 原样


# ---------------------------------------------------------------- 让位判定


def choose(scores: list, other_scores: list | None = None,
           other_idle_s: float | None = None, yield_on: bool = True) -> tuple:
    """挑一个候选。返回 `(下标或None, 每项判定)`。

    规格原话: "评分太低就需要查询另一方能否执行, 另一方无法执行再由我方执行"。

    判定按序:
      1. 我 `-inf`            → 不可达, 出局
      2. 队友 `-inf`          → **队友也做不了 → 我做**(不管我的分多低)
      3. 我的分 < YIELD_MIN_SCORE 且队友比我高 → **让位**, 去做别的
      4. 否则                 → 我做

    `yield_on=False` —— **完全不让位**。规格里那段是讲**人类**队友的
    ("对方是人类时, 帮助他评距离和可达性")。两个脚本队友**不能**用这套比:
    他们各自领的是**不同的订单**, 步骤价没有共同基准; 而且队友在煮他那道菜时,
    从我这看就是"他站在我的灶台边", 我会一直让位 —— 让到天荒地老。

    ⚠ 让位是拿**队友的原始分**比的 —— 不能拿我自己的人格(噪声/取负)去评估别人的能力。
    ⚠ 队友"没动"超过 AFK_SECONDS 就**不再让位**(否则对着一个发呆的人类会永远让位, 脚本干站着)。
    """
    n = len(scores)
    verdict = []
    cands = []
    idle = other_idle_s is not None and other_idle_s >= AFK_SECONDS
    if not yield_on:
        idle = True                       # 复用同一条短路: "不让位" == "当作队友没动"

    for i in range(n):
        s = scores[i]
        if s == NEG_INF:
            verdict.append("不可达")
            continue
        o = NEG_INF
        if other_scores and i < len(other_scores):
            o = other_scores[i]
        if (not idle) and o > s and s < YIELD_MIN_SCORE:
            verdict.append("让位")
            continue
        verdict.append("候选")
        cands.append(i)

    reclaimed = False
    if not cands:
        # **全被让位 → 收回自己上**。硬规则 5: 宁可做错也别让死(脚本干站着 = 白烧一局)。
        # 这条兜底放在 choose 里面而不是调用方, 是为了让"永远能返回一个候选"成为
        # 这个函数**自己保证**的性质 —— 调用方不需要记得再兜一次。
        cands = [i for i in range(n) if scores[i] != NEG_INF]
        if not cands:
            return None, verdict                  # 真·一个都做不了
        reclaimed = True
        for i in cands:
            if verdict[i] == "让位":
                verdict[i] = "让位收回"

    best = max(cands, key=lambda i: scores[i])
    # ⚠ 收回来的要**标成"让位收回"**而不是"选" —— 日志是省掉测试后的唯一线索,
    #   "这一步本来想让人、结果自己上了" 正是排查协作问题时最该看见的一行。
    verdict[best] = "让位收回" if (reclaimed and verdict[best] == "让位收回") else "选"
    return best, verdict


def fmt(v: float) -> str:
    """分数格式化: `-inf` 不打成 `-inf.0`, 便于日志对齐。"""
    if v == NEG_INF:
        return "-inf"
    return f"{v:.1f}"


def row(i: int, sig: str, reach: bool, dist: float, step: float,
        follow: float, raw: float, final: float, verdict: str,
        urg: float = 0.0, adv: float = 0.0) -> dict:
    """造一行日志记录(键用 ASCII, 只有显示用的表头是中文)。"""
    return {"i": i, "sig": sig, "reach": reach, "dist": dist, "step": step,
            "follow": follow, "raw": raw, "final": final, "verdict": verdict,
            "urg": urg, "adv": adv}


def table(rows: list) -> str:
    """把候选表排成等宽文本。

    这是**省掉离线测试的替代品** —— 没有单测, 就得让一局(150 秒)的日志把
    "每个候选的每一项原始值 → 变换后分数 → 为什么选它"全摊开, 见交接包 §8。
    列和评分的每一项**一一对应**: 可达 / 格距 / 步骤价 / 顺路 / 紧急 / 推进。
      · `紧急` 平时恒为 0, 只有"锅快糊了"那种候选才非 0(用户 2026-09-15: 报警了评分就该涨);
      · `推进` 只有"这一步用的正是手上拿着的那份"才非 0(用户 2026-09-15: 手上这份先做完)。
    """
    out = [f"  {'#':>2} {'动作':<24} {'可达':<4} {'格距':>5} {'步骤价':>6} "
           f"{'顺路':>5} {'紧急':>6} {'推进':>6} {'原始':>7} {'变换':>7}  判定"]
    for r in rows:
        d = f"{r['dist']:.1f}" if r["reach"] else "-"
        u = f"{r.get('urg', 0.0):.0f}" if r.get("urg") else "-"
        a = f"{r.get('adv', 0.0):.0f}" if r.get("adv") else "-"
        out.append(
            f"  {r['i']:>2} {r['sig']:<24} {'✓' if r['reach'] else '✗':<4} "
            f"{d:>5} {r['step']:>6.0f} {r['follow']:>5.1f} {u:>6} {a:>6} "
            f"{fmt(r['raw']):>7} {fmt(r['final']):>7}  {r['verdict']}")
    return "\n".join(out)
