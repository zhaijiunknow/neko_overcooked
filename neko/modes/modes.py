"""三模式（合作 / 失误 / 捣蛋）—— **个体级**、可热切换。

规格来自《胡闹厨房2-全脚本通关方案v1.md》§4/§5（D6/D7/D8 决策）：

| 模式 | 行为 |
|---|---|
| `coop`     主脑派啥好好做，**带低概率自然失误**（发呆/绕路/多切/忘盘折返） |
| `clumsy`   想做对但笨手笨脚：**高失误率、反应慢、常弄砸**（可恢复） |
| `sabotage` 抗命：领了单不干正事、净搞破坏；**间歇性 + 可变** |

关键设计（v1 §5.1）：捣蛋个体**不是"坏30秒好30秒"的定时器**，而是有一个
**良心值（conscience）** 在随机游走，每个决策点按良心值掷骰决定使不使坏：

  · **频率可变**：良心低就老使坏，良心高就认真干
  · **强度可变**：轻（挡路/慢）→ 中（半成品放错台）→ 重（倒队友菜/烧糊）
  · **良心漂移**：随时间随机游走；**被主脑点名/队友求助时回升**（"被发现了，老实一会"）
  · **情境收敛**：订单快超时 / 局面快崩时，捣蛋概率**自动降低**

本模块只产出"意图"（要不要使坏、使哪种坏），**不直接碰游戏** ——
执行仍由 engine 负责，这样模式是可测的、且不会与真游戏规则打架（v1 规则 D2）。
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

# 评分层的可调常量都在 scoring.py（调参只改那一处）。
# 两种导入方式都要能活：`neko/` 在 sys.path 上时是顶层 `scoring`（engine 就是这么导的），
# 以包方式导入（`neko.modes`）时走相对导入。
try:                                        # pragma: no cover - 取决于调用方式
    from scoring import SABOTAGE_MEMORY
except ImportError:                         # pragma: no cover
    from ..scoring import SABOTAGE_MEMORY


class Mode(str, Enum):
    COOP = "coop"
    CLUMSY = "clumsy"
    SABOTAGE = "sabotage"

    @classmethod
    def parse(cls, s) -> "Mode":
        if isinstance(s, Mode):
            return s
        try:
            return cls(str(s).strip().lower())
        except ValueError:
            return cls.COOP


class Mischief(str, Enum):
    """捣蛋/失误的**具体形态**，按强度分档（v1 §5.1）。执行层据此决定怎么演。"""
    # —— 轻：只耽误自己，不破坏局面 ——
    DAZE = "daze"            # 发呆（原地停一拍）
    DETOUR = "detour"        # 绕远路（多走一站再回来）
    SLOW = "slow"            # 磨蹭（动作放慢）
    # —— 中：把自己手上的东西搞乱 ——
    OVER_CHOP = "over_chop"  # 多切几刀
    WRONG_SPOT = "wrong_spot"  # 半成品放到错的台面
    FORGET_PLATE = "forget_plate"  # 忘拿盘子，折返一趟
    SNACK = "snack"          # 把材料丢掉/吃掉（丢进垃圾桶）
    # —— 重：破坏队友/整局 ——
    BIN_TEAMMATE = "bin_teammate"  # 把队友的半成品倒掉
    BURN = "burn"            # 放任灶台烧糊
    BLOCK = "block"          # 堵路 / 抢台子


#: 各模式的参数：失误率、捣蛋率、强度档位分布。
#: 数值是"手感参数"，可在真机上调（v1 §8 M6 就是逐个叠模式实测）。
@dataclass(frozen=True)
class Profile:
    fumble_rate: float          # 每步发生"自然失误"的概率
    mischief_rate: float        # 每步"故意使坏"的基础概率（乘良心修正）
    severity: tuple             # 使坏时按权重抽强度档：轻/中/重
    reaction_delay: float       # 反应延迟（笨拙=慢半拍）
    recover_bias: float         # 被点名后良心回升幅度


PROFILES = {
    # 合作：好好干，但有低概率自然失误（发呆/绕路/多切/忘盘折返）
    Mode.COOP: Profile(
        fumble_rate=0.06,
        mischief_rate=0.0,
        severity=(),
        reaction_delay=0.0,
        recover_bias=0.35,
    ),
    # 失误：想做对但笨手笨脚 —— 高失误率、反应慢、常弄砸（但可恢复、不主动害人）
    Mode.CLUMSY: Profile(
        fumble_rate=0.30,
        mischief_rate=0.0,
        severity=(),
        reaction_delay=0.35,
        recover_bias=0.25,
    ),
    # 捣蛋：抗命 + 主动破坏，频率由良心值驱动
    Mode.SABOTAGE: Profile(
        fumble_rate=0.08,
        mischief_rate=0.55,
        severity=((Mischief.SLOW, 3), (Mischief.DAZE, 3), (Mischief.DETOUR, 2),
                  (Mischief.OVER_CHOP, 3), (Mischief.WRONG_SPOT, 3),
                  (Mischief.FORGET_PLATE, 2), (Mischief.SNACK, 2),
                  (Mischief.BIN_TEAMMATE, 2), (Mischief.BURN, 2), (Mischief.BLOCK, 2)),
        reaction_delay=0.15,
        recover_bias=0.50,
    ),
}

#: 各形态属于哪个强度档（用于"情境收敛"时只允许轻度捣蛋）
SEVERITY_OF = {
    Mischief.DAZE: "light", Mischief.DETOUR: "light", Mischief.SLOW: "light",
    Mischief.OVER_CHOP: "mid", Mischief.WRONG_SPOT: "mid",
    Mischief.FORGET_PLATE: "mid", Mischief.SNACK: "mid",
    Mischief.BIN_TEAMMATE: "heavy", Mischief.BURN: "heavy", Mischief.BLOCK: "heavy",
}


@dataclass
class ModeState:
    """一个**个体**的模式运行时状态。每个厨师各持一份，互不影响（v1 D6）。"""

    mode: Mode = Mode.COOP
    rng: random.Random = field(default_factory=random.Random)

    # —— 捣蛋的"心情/良心"（v1 §5.1）——
    conscience: float = 0.7     # 0=没良心(猛使坏) 1=良心发现(老实)
    drift: float = 0.0          # 良心漂移速度（随机游走）
    strikes: int = 0            # 被主脑记下的次数（容忍度用）
    under_simple_task: bool = False   # 是否处于"被降级派简单任务"状态（T2 之后）

    # —— 统计（日志/调试用）——
    fumbles: int = 0
    mischiefs: int = 0
    reminders_obeyed: int = 0
    reminders_ignored: int = 0

    #: 捣蛋鬼的"重复记忆"：最近做过哪几个动作（`(action, target)`）。
    #: 评分变换用它给"做过的再做一遍"加分 —— 这就是规格说的"做一些重复的、没有意义的"。
    recent: deque = field(default_factory=lambda: deque(maxlen=SABOTAGE_MEMORY))

    # ---------------------------------------------------------------- 模式
    def set_mode(self, m) -> None:
        """热切换（v1 D6）：外部开关/良心发现都走这里，**下一个决策点生效**。"""
        m = Mode.parse(m)
        if m == self.mode:
            return
        self.mode = m
        if m == Mode.COOP:
            # 切回合作：良心直接回到"老实"一侧
            self.conscience = max(self.conscience, 0.85)
            self.under_simple_task = False
        elif m == Mode.SABOTAGE:
            self.conscience = min(self.conscience, 0.45)

    @property
    def profile(self) -> Profile:
        return PROFILES[self.mode]

    # ---------------------------------------------------------------- 漂移
    def tick(self, dt: float) -> None:
        """时间推进：良心随机游走（v1 §5.1「随时间随机游走」）。"""
        if self.mode != Mode.SABOTAGE:
            return
        # 均值回复的随机游走：长期在 0.5 附近晃
        self.drift += self.rng.gauss(0.0, 0.25) * dt
        self.drift *= 0.92
        self.conscience += self.drift * dt
        self.conscience = min(1.0, max(0.0, self.conscience))

    def on_reminded(self) -> bool:
        """被主脑催办（T1）：掷骰决定听劝还是继续捣（v1 §5.2 ③）。

        返回 True = 听劝（良心回升、容忍度该降）。
        """
        p = self.profile
        # 良心越高越容易听劝；被降级派简单任务期间也更容易听
        p_obey = self.conscience * 0.8 + (0.25 if self.under_simple_task else 0.0)
        if self.rng.random() < p_obey:
            self.conscience = min(1.0, self.conscience + p.recover_bias)
            self.reminders_obeyed += 1
            return True
        self.reminders_ignored += 1
        # 抗命一次，良心继续下滑（越捣越上头）
        self.conscience = max(0.0, self.conscience - 0.10)
        return False

    def on_teammate_help(self) -> None:
        """队友求助 / 被主脑点名 → 良心回升（v1 §5.1）。"""
        self.conscience = min(1.0, self.conscience + 0.20)

    def downgrade(self) -> None:
        """T2 改派：被降级派"简单任务"（拿盘子/端菜），此后使坏频率压低（v1 §5.2 ④）。"""
        self.under_simple_task = True

    def restore(self) -> None:
        """恢复正常派单（v1 §5.2 ⑤）。"""
        self.under_simple_task = False
        self.strikes = 0

    # ---------------------------------------------------------------- 决策
    def transform(self, scores: list, sigs: list | None = None) -> list:
        """**把"状态"表达成对评分向量的变换**（交接包 §4.2）。

        规格原话："状态不再是额外撒一层随机捣乱，而是在评分向量上做变换"：

        | 状态 | 变换 | 效果 |
        |---|---|---|
        | `coop` 好帮手 | 原样 | 取最高分 = **高效取最优** |
        | `clumsy` 笨手笨脚 | 加高斯噪声 | **随机评分波动**，常常不是最优，但不主动害人 |
        | `sabotage` 捣蛋鬼 | 取负 + 重复奖励 | **低效**，自然演成"反复做同一件没意义的事" |

        这样捣蛋鬼**不需要单独写一堆捣乱动作** —— 低效本身就是从偏好低分/重复里长出来的。

        真正的实现是 `scoring.transform`（纯函数、脱离游戏可核对）；这里只负责把
        "我是谁"（`self.mode` / `self.rng` / `self.recent`）喂进去。

        ⚠ `-inf`（到不了）在所有模式下都是硬闸门，连噪声都不翻 —— 见 `scoring.transform`。
        """
        try:
            from scoring import transform as _t
        except ImportError:                     # pragma: no cover
            from ..scoring import transform as _t
        return _t(scores, self.mode.value, self.rng, sigs=sigs, recent=self.recent)

    def remember(self, sig: str) -> None:
        """记下刚做过的一个动作（`(action, target)`）—— 供捣蛋鬼的重复偏好用。"""
        self.recent.append(sig)

    def roll(self, urgency: float = 0.0, allowed=None) -> Mischief | None:
        """**每个空闲决策点调一次**：这一步要不要使坏？返回形态或 None。

        urgency: 0..1，局面紧急度（订单剩余时间越少越大）。
                 订单快超时/快崩时**捣蛋概率自动降**（v1 §5.1「情境收敛」）。
        allowed: 允许的形态集合（如"只有轻度能演"时传 light 档）。
        """
        p = self.profile

        # 1) 自然失误（coop/clumsy 都有；捣蛋模式也有少量）
        if p.fumble_rate > 0 and self.rng.random() < p.fumble_rate * (1.0 - 0.7 * urgency):
            self.fumbles += 1
            light = (Mischief.DAZE, Mischief.DETOUR, Mischief.SLOW, Mischief.OVER_CHOP)
            if p.reaction_delay >= 0.3:      # clumsy：更容易弄砸（中档）
                light += (Mischief.WRONG_SPOT, Mischief.FORGET_PLATE, Mischief.SNACK)
            return self._filter(light, allowed) or self.rng.choice(light)

        # 2) 故意使坏（仅 sabotage）
        if self.mode != Mode.SABOTAGE or not p.severity:
            return None

        # 良心修正：良心 1 → 几乎不使坏；良心 0 → 按满额率
        rate = p.mischief_rate * (1.0 - self.conscience)
        if self.under_simple_task:
            rate *= 0.5                      # v1 §5.2 ④：简单任务期间频率压低一半
        rate *= (1.0 - 0.6 * urgency)        # 情境收敛
        if self.rng.random() >= rate:
            return None

        # 抽形态：按权重；紧急时只允许轻度
        pool = list(p.severity)
        if urgency > 0.6:
            pool = [(m, w) for m, w in pool if SEVERITY_OF[m] == "light"] or pool
        if allowed:
            pool = [(m, w) for m, w in pool if m in allowed] or pool
        pick = self.rng.choices([m for m, _ in pool], weights=[w for _, w in pool])[0]
        self.mischiefs += 1
        return pick

    @staticmethod
    def _filter(cands, allowed):
        if not allowed:
            return None
        ok = [c for c in cands if c in allowed]
        return ok[0] if ok else None

    # ---------------------------------------------------------------- 观测
    def summary(self) -> str:
        flag = "  (被降级派简单任务)" if self.under_simple_task else ""
        return (f"模式={self.mode.value} 良心={self.conscience:.2f} "
                f"失误={self.fumbles} 使坏={self.mischiefs} "
                f"听劝={self.reminders_obeyed}/抗命={self.reminders_ignored}{flag}")
