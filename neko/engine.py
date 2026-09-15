"""自动做菜引擎: 以"当前订单"驱动, 按游戏真实机制执行完整流程。

关键机制(全部反编译确认, 不是猜的):
  · 送餐口 = PlateStation —— 把"装了菜的容器"放上去才触发送餐
      ServerPlateStation.OnItemAdded → 盘里有内容 → DeliverCurrentPlate()
  · 容器类型必须匹配订单的 m_platingStep, 否则 ServerOrderControllerBase 判定不匹配
  · 容器来自 CleanPlateStack(干净盘子堆); PlateStation.m_createPlateTime 是废弃字段, 它不发盘子
  · 切菜板 = Workstation(负责 chop); AttachStation 只是普通台面
  · 煮: CookingHandler.GetCookedOrderState —— progress 落在 (cookTime, 2*cookTime] 才是订单要的
      Cooked; 生(Raw)和焦(Burnt)都不匹配 ⇒ 必须盯着实时进度取下
  · 组装顺序无关(CompositeAssembledNode.AssumeTypeMatch 是集合配对)

防御设计: 对局结束即中止 / 异常路径必释放按键 / 卡住即中止让外层重规划 / 每步闭环验证。
"""

from __future__ import annotations

import math
import os
import time

from bridge.keyboard_input import KeyboardPlayer, PLAYER1, PLAYER2, ensure_focus, game_focused, panic_pressed
from map_model import (KitchenMap, Station, is_plate, is_pot, is_extinguisher,
                       teleport_edges, conveyor_edges, wind_cells)
#: 地面传送带的字符。**从 terrain 导进来而不是抄一份** ——
#: "判定和显示只能有一条规则", 抄一份迟早会漂(这一轮已经栽过好几次)。
from terrain import CH_TRAVELATOR

#: 选站位时给**传送带格**加的距离惩罚 —— 只是排到所有非传送带格之后,
#: 不是排除(旁边只有带子时还是得站上去)。取个远大于地图尺寸的数:
#: 格子距离是平方, 一张图最多几百格, 1e6 足够"跨类别"而不影响同类内部的远近排序。
BELT_STAND_PENALTY = 1e6

#: 选站位时给**风区格**加的惩罚 —— 同 `BELT_STAND_PENALTY`, 同样的理由:
#: 风和传送带走的是同一条位移通道(`RigidbodyMotion.Movement` = `MovePosition(pos + v·dt)`,
#: 与按键无关), 站进去人就会一直漂。同样**只排后面、不排除**(旁边只有风区时还得站)。
#: **比传送带的略小**: 传送带是格属性(必然一直推), 风是体积且会被机关关掉(enabled=false)。
WIND_STAND_PENALTY = 1e5

#: **小物件(掉在地上的料 / 传送带上的料)的落位半径**(格)。
#:
#: 病根是**碰撞体大小**, 不是名字。交互判据是"到**碰撞体表面**的距离 < 1.0 且朝向前 180°"
#: (`InteractWithItemHelper.IsColliderInArc`, `InteractWithItemHelper.cs:153-163`)。
#:   台面碰撞体**大** ⇒ 站在**相邻格心**(格距 1.2)就已经贴到表面, 按一下就拿到;
#:   地上/带子上的料是**小东西** ⇒ 站 1.2 格时刚好落在半径**外** ⇒ 游戏报 `抓取='(空)'`。
#: 实测(`s_moonfestival_1_4`): 厨师(2.6,-7.0)、巧克力(2.4,-6.0) 相距 **1.02** 就抓不到,
#:   干等 14 秒后那件东西**被送进了垃圾桶**; 而地上那条路**成功时距 0.73 格**。
#: ⇒ 小物件不再导航到**格心**, 而是落到"**朝物品方向、距物品 `SMALL_STAND` 格**"那个点上
#:   (`_land_near`)。0.7 就是"实测成功过的 0.73"取整。
#:
#: ⚠ **这不是"引擎自己算几何"**(开发约定 规则 2): 这里只是把**起点**从格心挪到贴近点,
#:   "够不够得着"**仍然问游戏**(`_aim_ok` 拿游戏报的 pick/use 名字比)。
#: ⚠ `NEKO_SMALL_STAND=0` **一键关回老路**(`_land_near` 直接返回 False, 后面
#:   "试格心 + 就地微调"两步照跑, 一步不动) —— 和 `NEKO_WIND_COMP=0` 同一个路子。
SMALL_STAND = float(os.environ.get("NEKO_SMALL_STAND") or 0.7)

from pathing import dir_for_step
from cookbook import Knowledge, derive, Op, DishFlow
import lookahead                       # 提前备料: 由订单算"该备多少"(纯函数, 可离线钉死)
import scoring

# 灶台语义(按食材要求的 CookingStationType 映射)
COOK_SEMS = ("hob", "oven", "fryer", "firepit", "barbeque", "floorburner", "flamethrower")

#: 各动作的"**这个材料自己的**前置步骤" —— 同一 target 的这些动作还没做完时, 这一步不算可做。
#:
#: 依据 `derive()`: 每个材料按序产出 `fetch → [chop] → [cook/mix] → assemble`。
#: ☠ 实机教训(2026-09-14 s_balloon_2_3): 拿着**生的** BurritoMeat 时,
#:   `assemble BurritoMeat`(60 分) 压过了 `chop BurritoMeat`(30 分) —— 因为生料和切好的料
#:   **名字同源**, `_held_is` 认不出来是哪一个阶段。`op_assemble` 又不看阶段, 直接把生料
#:   摆上盘, 游戏拒收(实测 `✗ BurritoMeat 没进盘`)。
#:   ⇒ 光比名字不够, 得看**菜谱顺序**: 材料没加工完就不许摆盘。
#: ⚠ 只看**同一个 target** 的前置 —— 取另一份材料不在此列(那正是要的并行),
#:   所以 `fetch` 不设前置。
OP_PREREQ = {
    "cook": ("fetch", "chop", "mix"),
    "mix": ("fetch", "chop"),
    "assemble": ("fetch", "chop", "cook", "mix"),
}

#: **评分是否接管"选哪个动作"**(交接包 §4)。这是评分机制的**总开关**。
#:
#:   True (默认) —— `execute()` 每个决策点给候选 op 打分、取最高(还要过让位判定)。
#:                  旧的"按 `ModeState.roll()` 掷骰演失误/捣蛋"**同时被关掉**,
#:                  因为人格已经改由 `scoring.transform` 在评分向量上表达
#:                  (交接包 §4.2: 状态不再是在评分向量外额外撒一层随机捣乱)。
#:                  **两套同时开 = 同一个捣蛋鬼被扣两次效率**, 强度没法标定。
#:   False      —— 完全退回旧行为(按下标顺序做 op + 掷骰演失误), 用于对照排查。
#:                  用环境变量 `NEKO_SCORE=0` 也能关。
SCORE_DRIVES_MODE = (os.environ.get("NEKO_SCORE") or "1").strip().lower() not in ("0", "off", "no", "false")

#: **导航要不要做迎风补偿**(把摇杆/按键指向"净值方向"而不是"目标方向")。
#:
#: 这是风支持里**最不确定**的一块(符号约定 + 模拟/数字两条路 + 与已有的闭环控制器叠加),
#: 所以留一根可以一键拔掉的保险丝: `set NEKO_WIND_COMP=0` 就退回
#: **只避让不补偿**(选站位仍会躲开风区, 但走过风区时不再修正方向)。
#: 出问题时先用它把变量降到最小, 不用改代码。
WIND_COMP = (os.environ.get("NEKO_WIND_COMP") or "1").strip().lower() not in ("0", "off", "no", "false")

#: **摇杆增益的兜底值** —— `PlayerControls.Movement.RunSpeed = 4f`(`PlayerControls.cs:28`),
#: 实际上是 prefab 上的 `[SerializeField]`(关卡可以改), 所以优先用游戏报的那个
#: (`state.layout.chefs[].run`, 见 `chef_speed`)。这里只是**读不到时**的兜底(旧 DLL)。
#:
#: ☠ **别用 `Engine.speed`(4.0×0.9)当除数**: 那个 0.9 是**按键欠冲**用的常量
#:   (数字键那一路宁可多按几次也别冲过头, 见 `speed` 的注释)。而迎风补偿算的是
#:   "摇杆指向哪" —— 摇杆是**连续量**, 增益就是 `RunSpeed × MovementScale`,
#:   没有欠冲这回事。拿 3.6 去除会把风**高估 11%**, 补偿方向整体偏向下风。
#:   依据(反编译): `vector2 = movementScale * vector * movement.RunSpeed`
#:   (`ClientPlayerControlsImpl_Default.cs:414`)。
RUN_SPEED = 4.0

#: **杂活动作的四个名字** —— 它们和菜谱 op 进**同一张评分表**(阶段二: 全局行为驱动)。
#: 判据/执行都走 `Engine` 里对应的方法, 这里只是一份名单, 给评分侧的两个 dispatch 用
#: (`_op_target_for_score` / `_op_actionable`)。
#: 和菜谱 op 走**同一条评分/重试/日志**的"顺手活"。
#: `rescue`(救快糊的锅)也挂在这一族里 —— 它和杂活共用"手要空、目标在探测阶段解析好"
#: 那两条规则(`_op_actionable` / `_op_target_for_score`), 但**不吃顺路闸门**
#: (见 `_chore_admitted`), 因为它不是"顺手", 是"再不动就废了"。
CHORE_ACTIONS = ("press", "wash", "work", "serve_any", "rescue")

#: **杂活什么时候进候选池**(用户选的"混合: 能插就插"):
#:   `enroute`(默认) —— 菜谱还有能做的动作时, 只有**明显顺路**的杂活能插进来
#:                     (`scoring.CHORE_ENROUTE_CELLS` / `CHORE_NEARBY_CELLS`);
#:                     菜谱全受阻(够不着/没灶台/手空)时, 杂活全进池
#:   `stuck`        —— 只在菜谱全受阻时才看杂活(最保守)
#:   `always`       —— 杂活永远进池(≈"同一池子按分排")
#:   `0`/`off`      —— **完全不进池**, 退回"只做菜谱"(一键拔保险丝)
#: `serve_any`(交菜)**不受这个开关影响** —— 它是临门一脚, 见 `scoring.STEP_VALUE` 的注释。
CHORE_MODE = (os.environ.get("NEKO_CHORES") or "enroute").strip().lower()


#: **"一个动作都做不了"时等多久再判失败**(秒)。`NEKO_IDLE_WAIT` 可调, `0` = 不等(旧行为)。
#:
#: 为什么要等(实机 2026-09-14 `s_summer_1_4`):
#:   那一单的食材**在脚本够不着的另外半个厨房**(可达 36/75, 两个货源都"够不着"),
#:   而这一关此刻**又没有杂活可做** ⇒ 8 个候选全灭 ⇒ 立刻三次指纹**停机**, 整局报废。
#:   但那是**合作游戏**: 食材够不着时该出马的是**人类队友** —— 脚本该做的是**等一会儿再看**,
#:   而不是把整局停掉(用户原话: "可以让人类处理一部分评分不高的行为")。
#:   等的时候世界也可能自己变(平台到位/关卡变形/东西被送上传送带)。
#: ⚠ **必须有限**: 等满还是一场空就照旧报失败 —— 规则 5 "别烧整局" 仍然有效,
#:   只是"没得做"和"卡在一个 bug 里"要分开对待(前者等得起, 后者要立刻停)。
IDLE_WAIT = float(os.environ.get("NEKO_IDLE_WAIT") or 20.0)

#: **知识表重拉的节流间隔**(秒)。见 `_derive_with_retry`:
#: 解析不出货源时说明那张表**看着不全**(传送带关卡开局 prefab 还没实例化),
#: 重拉一次就能补上。节流是为了别让"这关真的没有这种料"变成每 0.5 秒读一次大 JSON。
KNOWLEDGE_RETRY = float(os.environ.get("NEKO_KNOW_RETRY") or 2.0)

#: **预置**(把料丢/放到"下一步要用的地方"附近)的记账保质期(秒)。
#: 用户 2026-09-15: "**鼓励丢食物**" —— 取货时把料朝下一步的方向丢过去, 之后再过去
#: 一趟并行处理好几份(那关有 3 口锅并排)。这块记账就是"我把某份料预置在哪了"。
#: 过期就作废: 东西可能被队友拿走/被传送带送走/被火烧了 —— 记太久会跑回一个空点。
PREPOSE_TTL = float(os.environ.get("NEKO_PREPOSE_TTL") or 60.0)

#: 下一步的处理站**至少这么远**才值得预置(格)。太近就直接抱着走(丢一趟要两次交互)。
TOSS_MIN_CELLS = float(os.environ.get("NEKO_TOSS_MIN") or 4.0)

#: **取货时"多备几份再走"的上限**(份)。`NEKO_BATCH` 可调, `1` = 回到"只备一份"。
#:
#: 用户 2026-09-15: "**他拿食材抛的时候不能多拿一点, 只抛一个那不如不抛。多拿几个抛**"。
#: 为什么单份不值得丢: 丢一份 = 多一次"取+转身+丢" + **回头单独跑一趟去捡**
#:   (那趟是纯多的); 而抱着走本来就只有一趟。**收益全在批量**:
#:   取货点和丢的方向是**同一个点**(站在箱子旁取、就地朝下一步丢) ⇒ 连备 N 份
#:   **几乎不额外走路**, 只是 N 次拾取 + N 次投掷; 之后**一趟**过去并行处理
#:   (那关有 3 口锅并排, 并行吞吐是串行的 3 倍 —— 不预置就没有东西同时下锅)。
TOSS_BATCH_MAX = int(float(os.environ.get("NEKO_BATCH") or 3))

#: **某一步反复失败后, 让它坐多久冷板凳**(秒)。`NEKO_STEP_COOLDOWN` 可调, `0` = 不坐。
#:
#: 用户要求(2026-09-14): "**不允许退出，如果持续失败，就去做其他事**"。
#: 于是 `run()` 里那条"连续 3 次同样失败 → 停机"的**退出**没了, 换成:
#:   **把这一步从候选里拿掉 `STEP_COOLDOWN` 秒** ⇒ 引擎自然会去挑别的步骤/别的菜/杂活。
#: 冷板凳到期后它又会回到候选里(失败往往是暂时的: 东西被队友拿走了、路被 NPC 堵了)。
#: ⚠ 和 `IDLE_WAIT` 的分工: 这个管"这一步做不成", 那个管"一件事都做不成"。
STEP_COOLDOWN = float(os.environ.get("NEKO_STEP_COOLDOWN") or 20.0)

#: **"这一支刚试过、不行" 记多久**(秒)。`NEKO_BRANCH_TTL` 可调, `0` = 不记。
#:
#: 用户 2026-09-15 定的规矩: "**按一次没有得到对应的结果这条路就失败了, 可以返回到其他路**"。
#: ⇒ 失败要记在**具体的台面**上, 不能记在动作上。现在的 `_last_fail_step` 是
#:   `f"{action} {target}"` —— **不含台面** ⇒ 一块板走不到就把**所有**板封掉
#:   (实测日志里 `work board1`/`board3` 走不到, 而 board0/board2 明明好用)。
#:   粒度也不对: 那是"这一步整类别做了", 而这里要的是"这块板这一趟算了, 换一块"。
#:
#: ⚠ **必须有限**: 世界会变(板被腾空、路被让开、东西被队友拿走) ⇒ 划掉的分支要自动回池。
BRANCH_TTL = float(os.environ.get("NEKO_BRANCH_TTL") or 25.0)

#: **一条"传递指令"活多久**(秒)。`NEKO_HANDOFF_TTL` 可调, `0` = 不用传递指令(退回老行为)。
#:
#: 用户 2026-09-15 定的方向: "**对菜单链的每一步做可行性验证, 就可以接入正常的传递逻辑**"。
#: 链条上某一环我这半边做不了、**队友那半边做得了** ⇒ 把料丢给他, 并且**记住这一笔**:
#:   · 记着的时候: `_pass_candidates` 不再重复提议、`_redos` 不再往回捡料
#:     (不记的话就是"丢了 → 手空 → 又去拿一份 → 再丢"的**死循环**);
#:   · 他做完(产物回到我够得着的地方)⇒ 把产物记进 `_preposed`, `assemble` 那一步自己接上。
#:
#: ⚠ **必须有限**: 人类可能一直不做(他在忙别的/没看懂), 世界也可能变(料被烧了/被拿走)。
#:   到期自动回池, 就重新丢一次。
#: ⚠ **必须比 `IDLE_WAIT` 长**: 交出去之后我这边可能一个候选都不剩, 那条路等满
#:   `IDLE_WAIT` 就判死整单 —— 所以"台账活着"要能让它继续等(见 `_execute_scored`)。
HANDOFF_TTL = float(os.environ.get("NEKO_HANDOFF_TTL") or 45.0)

#: **失败的种类** —— 决定"下一步该怎么办", 不是拿来打日志的。
#:
#:   `"branch"` —— **这一支不行**(板是空的 / 站位到不了 / 游戏 `CanHandlePlacement=false`)。
#:                 ⇒ **换支**(别的台面/别的站位), 别原地重试, 更不是整单报废。
#:   `"death"`  —— 摔死到上限, 路本身走不通(原样保留, `op` 里已在用)。
#:   `""`(空)   —— 执行抖动(差 1.0 格、被推开、被抢) ⇒ 值得原地重试。
#:
#: 为什么必须分: 这三类的处置**互相冲突**。混在一起的后果已经实测到了 ——
#: `placeCanHandle=False` 连试 3 次 → `✗ 放弃: assemble` → **整单报废**,
#: 而那条路本来只需要"换个台面再试"。
KIND_BRANCH = "branch"

#: 同一单里"换一支再试"最多几回。
#: ⚠ **必须有上限**: 台面划掉会到期回池(25 秒), 于是"重选→失败→划掉→到期→重选"
#:   会变成一个**慢循环** —— 每一圈都要走一趟路, 一局 150 秒能烧光。
#:   超过这个数就落到原来的"放弃整单"那条路, 让外层重新规划/坐冷板凳(规则 5 的兜底)。
BRANCH_RETRY_MAX = 4

#: **"这个台面放不下东西"** 这条分支的 key 里的"目标"。
#: `assemble` 的失败不是"某个材料不行", 而是"**这块台面**现在接不住放置"
#: (游戏 `CanHandlePlacement=false`) —— 和材料名无关, 换个材料来放一样不行。
#: 所以用一个人造 target 占位, 好让 `pick_assemble_spot` 按台面把它筛掉。
PLACE_SENTINEL = "__place__"

#: **一趟导航里最多允许摔死几次**(超过就放弃这条路)。
#: 为什么单独有这个数: 摔死 → 等重生(实测 6 秒/次) → 接着走 → 再摔死,
#: 是个**看起来在干活、其实在烧整局**的循环。`NEKO_MAX_RESPAWNS` 可调。
MAX_RESPAWNS = int(float(os.environ.get("NEKO_MAX_RESPAWNS") or 2))

#: **"这格物理上过不去"记多久**(秒)。`NEKO_BLOCKED_TTL` 可调, `0` = 不学这一课。
#:
#: 为什么要有这个数(用户: "**寻路…还是有很大的问题**"):
#:   C# 报的物理阻挡(`phys`)是**探针球采样**(`LevelInfo.BlockedBy`), 会漏;
#:   漏掉的格在 A* 眼里是 `.` ⇒ 每次规划都从那儿走、每次都撞住
#:   (`卡住→超时(还差 1.0 格)`, 一轮白烧十几秒, **下一轮还走同一条路**)。
#: ⇒ 撞住就记下来, 并进 A* 的禁行集; 但**必须带 TTL** —— 挡住人的可能是临时的
#:   (另一个厨师、一辆车、传送带上堆的货), 永久封路会把路越走越窄。
BLOCKED_TTL = float(os.environ.get("NEKO_BLOCKED_TTL") or 25.0)

#: **传球**: 走到离人类队友多近才丢(格)。`NEKO_PASS_RANGE` 可调。
#:
#: 用户原话(2026-09-15): "如果脚本控制的地方**没有菜板切菜**, 但是有食物箱,
#: 那么脚本需要**拿到食物箱然后丢给我**(丢的按键和切菜是同一个, 手拿着生食材就能丢)"。
#:
#: 机制(反编译): `ServerAttachmentThrower.CalculateThrowVelocity` = `方向 × m_throwForce`
#: —— **prefab 上的定值, 没有蓄力**, 所以"丢多远"我们控制不了, 只能**走近了再丢**。
#: ⚠ **实测射程 = 平地约 6 格**(用户 2026-09-15 自己数出来的; `m_throwForce` 插件读不到,
#:   所以一直是靠估的 —— 原来写的 3.0 太保守, 会让人多走一倍的路)。
#:   取 5.0 而不是 6.0: 留 1 格余量 —— 丢**不到**队友手里会掉在地上(人类还得去捡),
#:   比多走一格贵。有落差时射程会变(落点越低飞得越远), 这个数只当平地基准。
TOSS_RANGE_CELLS = float(os.environ.get("NEKO_TOSS_RANGE") or 6.0)
PASS_RANGE = float(os.environ.get("NEKO_PASS_RANGE") or 5.0)

#: 传球判据的**余量**(格): 走不到"正好那个点", 也别为 1 格之差把这一丢放弃。
#: ☠ **执行侧和评分侧必须用同一个数** —— `op_pass` 拿它判"够不够近",
#:   `_rank_candidates` 拿它判"这一步能不能拿到分"。两份各写一个数的话,
#:   就会出现"评分说能做 ⇒ 选中 ⇒ 执行时又说太远不丢"的白跑一趟
#:   (这正是"判定只能有一条规则"那件事, 见 `CH_TRAVELATOR` 的注释)。
PASS_SLACK = 1.0


class _GroundItem:
    """把一件**掉在地上的料**包成**和 `Station` 同形状**的取货目标。

    为什么用适配器而不是把 `Item` 直接丢给下游: `op_fetch` / `_op_target_for_score` /
    `_approach` 都只用到 `x / z / name / id`, 包一层就**一个调用点都不用改**,
    也不会把 `map_model.Item` 那个纯数据类污染成"半个台面"。

    `id` 以 `ground_` 开头 —— `op_fetch` 里靠 `id.startswith("conveyor")` 区分"在传送带上",
    前缀不撞车。
    """

    def __init__(self, it, n: int = 0):
        self.id = f"ground{n}_{getattr(it, 'name', '?')}"
        self.name = getattr(it, "name", "") or self.id
        self.x = float(getattr(it, "x", 0.0) or 0.0)
        self.z = float(getattr(it, "z", 0.0) or 0.0)
        self.on = []
        self.kind = "GroundItem"
        # ☠ **这两个字段必须转发** —— 适配器原来只搬 `name/x/z`, 地上那件的
        #   "是生料还是成品"在门口就丢了。而"传递指令"的收回那一腿**只看地上的料**
        #   (队友把切好的丢回来), 判据正是它。少搬一个字段 = 整条闭环瞎掉,
        #   而且**不报错**(`getattr(..., False)` 会安静地答"不是生料")。
        self.tag = getattr(it, "tag", "")
        self.workable = bool(getattr(it, "workable", False))


def chore_key(op) -> tuple:
    """一件杂活的**稳定标识** `(action, target)` —— 给"本轮做过几次"的黑板当 key。

    ⚠ 必须稳定: 探测阶段如果"挑离我最近的那个", key 会随着厨师走动漂移
      ⇒ 上限失效(同一件杂活被反复选中)、旁边那件能做的又看不见。
      所以 `_chore_candidates` **列全**, 距离交给评分层排。
    """
    return (getattr(op, "action", ""), getattr(op, "target", ""))


def wind_aim(ux: float, uz: float, wx: float, wz: float, r: float) -> tuple:
    """迎风补偿的**精确解** —— 纯函数, 脱离游戏就能核对(开发约定 规则 3)。

    返回 `(摇杆方向 x, 摇杆方向 z, 沿目标方向的净速率 t, 说明)`。

    模型(反编译): `净值 = R·û' + W`, 我们要**净值朝目标**:
        `R·û' + W = t·u`, 两边减 W 取模 ⇒ `t² − 2ta + |W|² = R²`
        ⇒ **t = a + √(R² − b²)**  (a = W·u 顺风为正, b² = |W|² − a²)
        ⇒ **û' = (t·u − W)/R**    ← 构造上就是**单位向量**, 不必再归一化
    `t` 同时是"沿目标方向的净速度": 越大越快, `t ≤ 0` = 直线前进不了。

    `说明` 的四种取值各有去处(调用方**只按 t 和它决定走不走**):
      · `精确前馈`  —— 正常解, 摇杆指向 `û'`, 走直线
      · `侧风>R`    —— `b² > R²`: 没有直线解(横向风比厨师还强), 退回**纯追踪**
      · `直线顶不动` —— `t ≤ 0`: 逆风恰好把直线解顶平; 但 `R + a > 0` 说明**螺旋**仍收敛
                        (把摇杆直指目标, 距离按 `R + a` 稳定缩小), 所以**不是**放弃
      · `不可达`    —— `R + a ≤ 0`: **任何**摇杆方向都推不出朝目标的净速度,
                        而任何可用速度的时间平均都落在同一个圆盘里 ⇒ **连绕路也到不了**。
                        只有这一种是真该放弃的(文档 B4 那条)。
    """
    a = wx * ux + wz * uz
    b2 = max(0.0, wx * wx + wz * wz - a * a)
    if r <= 1e-6:
        return ux, uz, a, "R≈0"                    # 被压制/减速到 0: 只服从风
    if r + a <= 0.0:
        return ux, uz, r + a, "不可达"
    u2 = r * r - b2
    if u2 < 0.0:
        return ux, uz, 0.0, "侧风>R"               # 纯追踪: 收敛但走斜线
    t = a + math.sqrt(u2)
    if t <= 1e-3:
        return ux, uz, t, "直线顶不动"             # 纯追踪(同一个 û' = u)
    return (t * ux - wx) / r, (t * uz - wz) / r, t, "精确前馈"


class Engine:
    def __init__(self, bridge, cid=0, bindings=None, log=print, board=None,
                 mode_state=None, world=None, teammate_is_human=None):
        self.bridge = bridge
        self.cid = cid
        self.kb = KeyboardPlayer(bindings or PLAYER1)
        self.log = log
        self.board = board       # 双人时的订单黑板(单人传 None)
        self.mode_state = mode_state   # 三模式的个体状态(neko/modes/); None=纯合作不捣蛋
        # 队友是不是**人**? 只影响日志措辞与将来的调参 —— 四项评分对人和 bot 完全一样
        # (规格: "对方是人类时, 帮助他评距离和可达性, 再和自己的比较")。
        #   `run_engine.py` 只驱动一个厨师、不传黑板 ⇒ 另一只归玩家;
        #   `run_team.py` 两只都是脚本 ⇒ 传了共享黑板。
        self.teammate_is_human = (board is None) if teammate_is_human is None else bool(teammate_is_human)
        #: 队友"多久没动"的追踪 —— 规格: "他也到不了/**没动** → 我做"。
        #: 没有它, 对着一个站着不动的人类会**永远让位**, 脚本干站着。
        #: {cid: (上次位置, 上次动的时间)}
        self._mate_track: dict = {}
        # 共享世界(双人时两个引擎传同一个, 见 neko/world.py):
        #   一张地图 + 两个厨师的实时位置。为 None 时退化成"各自读状态/各自缓存地形",
        #   单人模式行为与以前完全一致。
        self.world = world
        # 交互半径: 反编译实测是 **1.0**(到碰撞体**表面**的距离, 朝向还要在前 180° 内),
        # 见 pathing.INTERACT_RANGE。这里 1.5 只是"导航粗到半径", 落到 1.5 之后还要靠
        # tight 再收紧 + face() 转身, 才真正进入交互范围。
        # (旧值 1.8 已经大于交互半径本身 —— 停在 1.8 处按键是够不着台子的。)
        self.arrive = 1.5          # 导航粗到半径
        self.interact_range = 1.0  # 交互半径(表面距离), 只作参考/日志
        self.step_timeout = 25.0   # 单步超时(秒)
        self.tap_hold = 0.12       # 单次方向键按住时长(保留给固定步长用)
        self.tap_gap = 0.05        # 方向键间隔
        # 精确运动学(反编译标定): PlayerControls.Movement.RunSpeed = 4f (PlayerControls.cs:28),
        # 平地水平速度每帧直接赋值 ⇒ 无加速度/惯性/刹车 ⇒ 位移 = 4 × 按住秒数。
        # 乘 0.9 留余量, 宁可多按几次也别冲过头(冲过头就会在目标两侧来回震)。
        self.speed = 4.0 * 0.9     # 有效推进速度 (u/s)
        self.max_hold = 0.6        # 单次按键最长按住时长(秒) —— 闭环分多次走, 单次别冲太远
        # 焦点策略: 游戏不在前台时**等多久**(秒)。等不到就松手放弃这一步。
        # 默认不抢焦点(见 keyboard_input.FOCUS_POLICY), 这样跑脚本时电脑照样能用。
        self.focus_wait = 0.5
        self.know: Knowledge | None = None
        self.scene = ""
        self.assemble_spot: Station | None = None   # 组装台面(放容器的地方)
        #: 组装台面的 id —— 半成品放在哪个台面上, 整局内不许换。
        #: 见 pick_assemble_spot() 里那段注释(用户实测: 换台面导致同一份材料取了 7 遍)。
        self._assemble_sid: str = ""
        self._stove_used = ""                        # 当前占用的灶台(用完释放)
        self._probed = False                         # 是否已实测过键位归属
        #: 风: 已经说过一次"插件没报 wind 字段, 退回几何投影"了没有(那行日志每帧都会想打)。
        self._wind_fb_told = False
        #: 灭火: 已经说过一次"找不到灭火器/在队友手上"没有(主循环每 2 秒调一次灭火)。
        self._no_ext_told = False
        #: **冷板凳**: `{"fetch Flour": 到什么时候为止}` —— 反复失败的那一步先别选它。
        self._step_bench = {}
        #: **预置记账**: `norm(材料名) -> (x, z, 什么时候预置的)` —— 见 `PREPOSE_TTL`。
        #: "我把某份料丢/放在哪了"。丢出去手上就空了, 所以 `cook/chop/mix/assemble`
        #: 得先靠这张表知道"它躺在附近, 先去捡回来"(见 `_pickup_preposed`)。
        self._preposed = {}
        #: 上一次失败是**哪一类**(见 `KIND_BRANCH` / `"death"`) —— 决定"下一步该怎么办",
        #: 不是拿来打日志的: 摔死不原路重试, 分支不可行**换支**, 其余才值得原地重试。
        self._last_fail_kind = ""
        #: **划掉的分支**: `(归一化 target, 台面 id) -> 过期时刻`。见 `BRANCH_TTL` 的注释。
        self._branch_dead: dict = {}
        #: **"撞住过"的格子** `{(i,j): 到期时间}` —— 地图说能走、物理上过不去的那种
        #: (见 `_note_blocked` / `_dynamic_blocks`)。
        self._blocked = {}
        #: 上一次 `navigate()` 是不是"**卡住、而且没能记下是哪一格**"。
        #: 那种失败下重规划**没有任何信息增量**(同一张图必然同一条路) ⇒
        #: `navigate_smart` 据此直接放弃这一趟, 不烧 `replans` 那几轮。
        #: 每次 `navigate()` 开头都会复位(见那里), 别读残留值。
        self._last_stuck_no_learn = False
        #: **外部命令**(`neko/control.py` 的命令文件): 指定做的下一步 / 暂停 / 收工。
        self._forced = None          # (action, target) —— `do` 命令指定的那一步, 做一次就清
        self._ctrl_paused = False    # `pause` 之后松手等 `resume`
        self._ctrl_stop = False      # `stop` 之后干净退出
        #: 已经报过一次"这个关卡反转了轴"了没有(同上, 每帧都会想打)。
        self._axis_flip_told = False
        #: 已经报过"计划货源够不着, 改用另一个"的 (action, target, id) 集合(每个决策点都会走到)。
        self._src_swap_told = set()
        #: 已经报过"队友也做不了这一环, 不丢"的 (target, 原因) 集合(同上, 别刷屏)。
        self._pass_told = set()
        #: **传递指令台账**: `(被交出去那一步的 action, 归一化 target) -> 过期时刻`。
        #: 见 `HANDOFF_TTL`。形状照 `_step_bench`/`_branch_dead`(值 = 绝对过期时刻, 读时惰性清)。
        self._handoffs: dict = {}
        #: 台账是给**哪张订单**记的 —— 换单就整表清空(见 `_execute_scored` 开头)。
        self._handoff_flow = ""
        #: 已经报过"队友那边做完了"的 (action, target) 集合(同上, 别刷屏)。
        self._handoff_told = set()
        #: **交接那一刻场上已有的同名散件在哪** —— `_handoff_received` 的基线, 见 `mark_handoff`。
        self._handoff_seen: dict = {}
        self._last_fire_check = 0.0                  # 上次查火的时间(节流见 run())
        # ---- 状态出口(见 `neko/status.py`) ----
        #: 上一次写出去的 payload —— 用来**跨引擎重启保留 `result`**(上一局赢没赢
        #: 不该因为脚本重启就消失)。启动时先把文件里已有的那份读回来。
        self._status_prev = None
        self._status_at = 0.0                        # 上次落盘时间(节流 1 秒)
        try:
            import status as _status
            self._status_prev = _status.read()
        except Exception:
            pass
        self._terrain = None                         # 关卡地形(含危险区), 见 terrain()
        self._terrain_scene = ""
        self._terrain_at = 0.0                       # 上面那份是什么时候取的(见 terrain 的 TTL)
        self._terrain_ver = ""                       # 上面那份的版本号(变没变的便宜判据)
        #: 地形最多能用多久(秒) —— 超过就重取一次。
        #: **这是"跳海"的保险丝**。原先地形按场景缓存、**整局不刷新**,
        #: 而限时平台升降/荷叶沉浮/潮水都会改地形, 且**只改高度不改字符** ——
        #: 于是引擎会拿着"平台还升着"的旧图规划, 直接走进海里
        #: (实测 s_wonderland_1_2: 66 格高度在 0.00 ↔ -3.00 之间循环, 字符一格不变)。
        #: ⚠ **2026-09-14 从 1.5 压到 0.5**(用户要求"提高地图更新的频率"):
        #:   实测这一关强制扫一次只要 **32ms**(`s_wonderland_1_5` 41x24,
        #:   逐格 2 条射线 ≈ 2000 次 raycast) ⇒ 0.5 秒的占空比才 6%, 完全付得起。
        #:   1.5 秒的代价是实打实的: 厨师 4 u/s, 1.5 秒走出 **6 格** ——
        #:   拿 6 格前的图规划, 平台/荷叶/潮水早就变了。
        #:   ⚠ **大关卡会线性变慢**(耗时 ∝ 格子数), 所以留了 `NEKO_TERRAIN_TTL`
        #:     覆盖; 真要调就按 `tools/gridwatch.py` 或日志里的实测耗时定。
        self.terrain_ttl = float(os.environ.get("NEKO_TERRAIN_TTL") or 0.5)
        self._last_fail_step = ""                    # 最后失败在哪一步(供主循环判断重复失败)
        #: 台面传送带的"每格往哪传"表 + 速度, 按场景缓存(来自插件 dyn)
        self._belt_dirs_cache = None
        self._belt_speeds_cache = {}

    # ---------------- 状态 ----------------
    def state(self, force: bool = False) -> dict | None:
        """整局状态。

        `force=True` 绕开共享世界的 TTL 缓存 —— **凡是要判断"世界变了没有"的地方
        都必须用它**(比如"按了键之后拿到了没"), 否则读到的是动作之前的旧帧,
        会把成功判成失败 → 重按一次 → 把刚拿到的东西放回去(这个坑踩过)。
        导航这类"只要大致新鲜"的地方用默认(共享缓存), 双人时能省一半桥流量。
        """
        if self.world is not None:
            return self.world.state(force=force)
        try:
            return self.bridge.get_state()
        except Exception as e:
            self.log(f"[状态] 拉取失败: {e}")
            return None

    def world_note(self) -> str:
        """共享世界的诊断行(双人时两个引擎看到的是同一份, 所以只该打一次)。"""
        return self.world.note() if self.world is not None else ""

    def round_active(self) -> bool:
        st = self.state()
        return bool(st and st.get("inRound"))

    def map(self, st: dict) -> KitchenMap | None:
        lay = (st or {}).get("layout") or {}
        if not lay.get("chefs"):
            return None
        return KitchenMap.from_layout(lay)

    def pos(self, st: dict) -> tuple:
        lay = (st or {}).get("layout") or {}
        for c in lay.get("chefs") or []:
            if int(c.get("id", -1)) == self.cid:
                return float(c.get("x", 0)), float(c.get("z", 0)), c.get("held", "")
        return None, None, ""

    def chef_y(self, st: dict) -> float:
        """**厨师当前的高度** —— 多平台关卡判"这格站不站得下"要用它。

        为什么必须是"这只厨师的 y"而不是某个全局地面高度(实测 s_wizard_school_3_4):
          C# 侧原来是拿第一只厨师的当前 y 当全局参考去建整张图, 结果
            ① 随厨师移动而不稳定(同一关两次读出 -1.84 / 0.00);
            ② 只覆盖一层平台, 另一层全判成空洞 —— 厨师站在自己平台上却被判成
               "站在空洞上", 可达格数 = 1, 一步都走不了。
          "站不站得下"是相对量, 所以由引擎把**它自己这只**的 y 传下去。
        """
        try:
            return float(self.chef(st or {}).get("y") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def chef(self, st: dict) -> dict:
        """当前厨师这一帧的完整信息(位置/手持/归属玩家/是否正在重生)。"""
        lay = (st or {}).get("layout") or {}
        for c in lay.get("chefs") or []:
            if int(c.get("id", -1)) == self.cid:
                return c
        return {}

    def is_respawning(self, st: dict) -> bool:
        """正在死亡重生中(PlayerControls.m_bRespawning)。

        这期间游戏接管了角色, 发任何方向键都不会有反应 —— 旧代码不知道这件事,
        于是把它当成"卡住", 一边硬推一边把超时耗光。
        """
        return bool(self.chef(st).get("respawning"))

    def is_impacted(self, st: dict) -> bool:
        """**正被击退/冲量推着吗**(插件读 `ClientPlayerControlsImpl_Default.m_impactTimer > 0`)。

        三类触发(文档 §2.4.2): 撞火 / 被投掷物砸中 / 两名厨师冲刺对撞 —— **都不可预判**。
        冲量期间游戏按 S 曲线把 `m_impactVelocity` 插值进运动
        (`ClientPlayerControlsImpl_Default.cs:426-431`), **按键只起一部分作用**,
        所以这时最该做的是**松手等它衰减**(0.2 秒), 而不是硬顶。

        ⚠ 这是**问游戏**拿到的判据, 不是靠"位移和下令对不上"反推的 ——
          那种反推既难定阈值(0.2 秒的冲量摊到几个 50ms 的循环里, 每帧只多走零点几格),
          又容易写成"看起来在保护、其实永远不会触发"的死代码(交接包 §5.1 的教训)。
        """
        return bool(self.chef(st).get("impacted"))

    def _next_step_dangerous(self, tm, x: float, z: float,
                             dx: float, dz: float, dist: float) -> bool:
        """**下一步要迈进去的那格是危险格吗**(水/空洞) —— 纯判据, 抽出来便于离线核对。

        为什么必须有这条(用户实机指出: "**没有按地图的可达图移动，直接去水里了**"):
          · 水面/空洞**不是障碍格**(水是 `RespawnCollider` 触发器, 不占格子)
            ⇒ 地形上就是 `.`, 只有**危险判据**拦得住;
          · 而 `navigate` 里那道危险检查看的是"**当前格**" —— 那是**人已经在水里了**;
          · 再叠上 `A* 无解 → 直线硬冲` 的兜底, 就成了"从安全格迈进水里 → 淹死 → 重生 →
            接着迈"的死循环(`s_wonderland_1_5` 一路刷了 9 次重生, 整局烧完)。
        ⇒ 判据: **当前格安全、而前方 0.9 格危险 ⇒ 别迈**。
          ⚠ **只在"当前格安全"时管** —— 有的关卡危险区判得很宽(整片被 KillPlane 投影成
            危险), 那时人本来就站在"危险"里, 再拦就一步都走不了(见 `_danger_trust` 的历史教训)。
        """
        if tm is None or not getattr(tm, "ok", False) or dist <= 1e-4:
            return False
        try:
            if tm.is_danger_world(x, z):
                return False                     # 已经在"危险"里 → 这条不适用
            px, pz = x + dx / dist * 0.9, z + dz / dist * 0.9
            return bool(tm.is_danger_world(px, pz))
        except Exception:
            return False

    def chef_speed(self, st: dict) -> float:
        """厨师此刻的**真实推进速度** `R = RunSpeed × MovementScale`(世界单位/秒)。

        这是**控制增益**, 不是 `Engine.speed` —— 后者乘了 0.9 的欠冲余量, 只配给
        "数字键按住多久"用。迎风补偿要除的是这个 R(见 `RUN_SPEED` 那段注释)。

        两个数都来自游戏(`SceneScanner.ReadControl` 反射 `PlayerControls.Movement`
        的 `RunSpeed` 字段与 `MovementScale` 属性), 关卡改了 prefab 也照样对。
        """
        c = self.chef(st) or {}
        try:
            run = float(c.get("run") or 0.0)
        except (TypeError, ValueError):
            run = 0.0
        if run <= 0.0:
            run = RUN_SPEED                       # 旧 DLL / 读不到 → 兜底
        try:
            scale = float(c.get("scale"))
        except (TypeError, ValueError):
            scale = 1.0
        if scale <= 0.0:
            scale = 0.0                           # 喷灭火器/过场压制时游戏自己把它设成 0
        return run * scale

    def axis_signs(self, st: dict) -> tuple:
        """世界方向 → 摇杆值 的**每轴符号** `(sx, sy)`, 取自游戏报的 `alignx`/`aligny`。

        依据 `PlayerControlsHelper.GetControlAxis`(`PlayerControlsHelper.cs:64-71`):
            `x = (±1)·MoveX`, `z = (±1)·(0 − MoveY)` —— **非 `Normal` 即取负**
        ⇒ 关卡把某个轴设成 `Inverted` 时, 同一个摇杆值驱动的是**相反的世界方向**。
        这两个是 prefab 上的 `[SerializeField]`(`PlayerControls.cs:52-54`), 只有游戏知道,
        所以由插件报出来(`SceneScanner.ReadControl` → `state.layout.chefs[].alignx/aligny`)。

        ⚠ 数字键那一路(`_key` / `dir_for_step` / `AXIS_SIGN`)仍按 `Normal` 标定 ——
          真碰上 `Inverted` 的关卡, **键盘注入会整条走反**(那是既有且更深的问题)。
          这里只把默认输入层(模拟量)弄对, 并**大声报一次**。
          枚举默认值是 `Normal`(`PlayerControls.cs:15-19`, 0 值), 所以这是"真碰上了再说"。
        """
        c = self.chef(st) or {}
        ax = str(c.get("alignx") or "")
        ay = str(c.get("aligny") or "")
        sx = -1.0 if (ax and ax != "Normal") else 1.0
        sy = -1.0 if (ay and ay != "Normal") else 1.0
        if (sx < 0.0 or sy < 0.0) and not self._axis_flip_told:
            self._axis_flip_told = True
            self.log(f"[输入] ⚠ 这个关卡反转了轴(alignx={ax!r}, aligny={ay!r}) —— "
                     f"模拟量已按反转发; **数字键那一路仍按 Normal, 会走反**")
        return sx, sy

    def wait_respawn(self, budget: float = 9.0) -> bool:
        """松手等游戏把厨师救回重生点(实测 5s 重生 + 1s 粒子 ≈ 6s)。"""
        self.kb.release_all()
        t0 = time.time()
        while time.time() - t0 < budget:
            time.sleep(0.4)
            st = self.state()
            if not st or not st.get("inRound"):
                return False
            if not self.is_respawning(st):
                self.log(f"[重生] 厨师回来了(等了 {time.time()-t0:.1f}s)")
                return True
        self.log(f"[重生] 等了 {budget}s 还没回来")
        return False

    def wait_idle(self, t: float = 0.3) -> None:
        """**松掉所有键、静止 t 秒**(文档 §5.3 建议的通用原语之二)。

        为什么要它 —— 一次解决四件事, 每一件都有出处:
          · **客户端(非主机)松手瞬间会被服务器坐标吸附**
            (`ClientChefSynchroniser.cs:393`) ⇒ 刚松手读到的位置才是"服务器认的位置"
          · **击退/冲击的衰减**: 冲量持续 0.2 秒、按 S 曲线衰减到 0(§2.4.2)
            ⇒ 在冲量里较劲是白费, 等它自己衰减完
          · **打滑结束判定**: 禁用窗口 `0.38 + m_downTime + 0.42` 秒(§3.2), 停 ≥0.8s 更稳
          · 风/传送带推出来的位移 —— **停手时那一帧才是干净的**, 否则读到的位置
            带着"这一帧被推了多少", 换算成方向就偏

        ⚠ **键盘和模拟量都要松**: 虚拟手柄的 `move()` 设的是 `_axis_override`,
          只调 `kb.release_all()` 不会清掉它(推杆会一直保持) —— 必须也调
          driver 的 `release_all()`。
        """
        self.kb.release_all()
        try:
            drv = get_driver()
            if drv is not None and hasattr(drv, "release_all"):
                drv.release_all()
        except Exception:
            pass
        if t > 0:
            time.sleep(t)

    def checkpoint(self, tm, tx: float, tz: float, tol: float = 1.5) -> tuple:
        """**停手 → 读位置 → 和预期比**(文档 §5.3 建议的通用原语之一)。

        返回 `(对不对, 实际差了多少格)`。

        为什么不能直接拿刚才那帧比: 位置是闭环控制器的输入, 而**外力会改它** ——
        风/传送带每帧 `MovePosition`、击退是 0.2 秒的冲量、客户端松手还会被服务器吸附。
        所以要先 `wait_idle()` 停下来, 再读一帧**没有输入**的位置, 才是可比的真值。

        它同时覆盖: 物理推挤的累计误差、网络修正、带/风漂移(§5.3 原话)。
        """
        self.wait_idle(0.25)
        st = self.state(force=True)
        if not st or not st.get("inRound"):
            return False, float("inf")
        x, z, _ = self.pos(st)
        if x is None:
            return False, float("inf")
        d = ((x - tx) ** 2 + (z - tz) ** 2) ** 0.5
        return d <= tol, d

    def ensure_knowledge(self, st: dict, force: bool = False) -> bool:
        """每场景拉一次食材知识表(切/煮/货源)。

        ☠☠ **`force=True` 是"这张表看着不全"时的重拉口子** —— 见 `_derive_with_retry`。
          快路径(`self.scene == scene` 就直接返回)**必须保留**: 它每次决策都会被调到,
          而 `get_knowledge()` 不是免费读。
        """
        scene = st.get("scene", "")
        if not force and self.know is not None and self.scene == scene:
            return True
        try:
            self.know = Knowledge.from_json(self.bridge.get_knowledge())
            self.scene = scene
            # ☠ **重拉之后必须作废"按订单名缓存"的那份 flows** —— 它是拿**旧**知识表
            #   derive 出来的, 而 `_all_flows` 的 key 只看订单名 ⇒ 不主动清, 就会
            #   一直用着"缺 chop 的那一条"(缓存命中, 悄悄绕过刚修好的重拉)。
            self._flows_key, self._flows_val = None, None
            self.log(f"[引擎] 食材知识表已加载: {len(self.know.items)} 项 (场景 {scene})"
                     + ("  ← **重拉**" if force else ""))
            return True
        except Exception as e:
            self.log(f"[引擎] 食材知识表读取失败: {e}")
            return False

    def _derive_with_retry(self, detail: dict, st: dict):
        """`derive()`, 但**知识表看着不全时会重拉一次**。

        ☠☠ 为什么必须重拉(2026-09-15 实机打回来的"**0 分那一局**"):
          `resolve_leaf` 的 `raw_for`/`ready_for` **只看场景实例**(`not i.prefab`),
          而知识表是**每场景拉一次的备份数据**(`ensure_knowledge` 原来那句
          `self.scene == scene` 让它**永不重拉**)。传送带关卡开局时食材 prefab
          **还没被实例化** ⇒ 表里既没有生鱼的 `next`、也没匹配到箱子 ⇒
          `resolve_leaf` 落到"**没找到货源**"那条兜底 ⇒ `derive()` 只产出
          `fetch + assemble`, **整条链没有 `chop`** ⇒ 脚本拿到生鱼**直接去装盘**,
          游戏 `CanHandlePlacement=false` 拒收 ⇒ 原地折腾一整局。
          ⚠ 这正是用户那条规矩的又一次违例: "**游戏本质上是实时, 我们不能用备份的数据**"——
            地形/状态/盘里内容都改过来了, **知识表是最后一块**。

        **判据是症状, 不是"表里有多少项"** —— "缺哪一条"没法预判, 而
        "**解析不出货源**"(`Op.nosrc`)是现成且准确的信号。

        ⚠ 节流: 最多每 `KNOWLEDGE_RETRY` 秒重拉一次。没有节流的话, "这关真的没有
          这种料"会变成每 0.5 秒读一次大 JSON。
        ⚠ 重拉后**还是一样**就只能认了(不是时机问题), 不再反复试 —— 只打一行日志,
          免得把日志刷满(`日志是省掉测试后的唯一线索`)。
        """
        flow = derive(detail, self.know)
        if not any(getattr(op, "nosrc", False) for op in flow.ops):
            return flow
        now = time.time()
        if now - getattr(self, "_know_retry_at", 0.0) < KNOWLEDGE_RETRY:
            return flow
        self._know_retry_at = now
        if not self.ensure_knowledge(st, force=True):
            return flow
        flow2 = derive(detail, self.know)
        if any(getattr(op, "nosrc", False) for op in flow2.ops):
            self.log(f"[引擎] ⚠ {flow.name}: **重拉知识表后还是找不到货源** —— "
                     f"这关可能真的没有这种料(不是'开局还没扫到')")
            return flow
        self.log(f"[引擎] ↻ {flow.name}: 知识表**重拉**后货源齐了(开局时还没扫到) —— "
                 f"规划已换成**带加工**的版本({len(flow2.ops)} 步)")
        return flow2

    def live_orders(self) -> list:
        """当前挂在订单栏上的订单, 按剩余时间从少到多(最紧急优先)。"""
        try:
            payload = self.bridge.get_live_orders()
        except Exception as e:
            self.log(f"[订单] 读取失败: {e}")
            return []
        orders = [o for o in (payload.get("live") or []) if o.get("name")]
        orders.sort(key=lambda o: float(o.get("t", 1.0)))
        return orders

    def find_detail(self, st: dict, name: str) -> dict | None:
        for d in st.get("details") or []:
            if d.get("name") == name:
                return d
        return None

    # ---------------- 导航 ----------------
    def navigate(self, tx: float, tz: float, arrive: float = None,
                 tight: float = None, tight_timeout: float = 3.0,
                 step_timeout: float = None) -> bool:
        """闭环走到目标交互半径内。

        arrive = 粗到半径(默认 self.arrive); tight = 更近的精到半径(可选)。
        为什么要精到: 相邻台子只隔 1.2 格, 停在 1.8 格处会同时落在两三个台子的交互范围内,
        按交互键就会拿错东西。先粗到保证不卡在障碍上, 再限时收紧到 tight。
        """
        from bridge.keyboard_input import key_down, key_up, ensure_focus, get_driver
        # ⚠ **每次开头复位** —— 调用方(`navigate_smart`)会在失败后读它,
        #   读到上一趟的残留值就会误判"这次也学不到"(同 `_execute_scored` 那条变量不隔离的教训)。
        self._last_stuck_no_learn = False
        # 默认**不抢焦点**: 游戏不在前台就暂停等它回来。
        #
        # ⚠ 这里必须"等", 不能直接 return False —— 这是踩过的坑:
        #   一 return False, navigate_smart 就把它当成"这个路径点到不了"而跳过,
        #   于是用户每看一眼终端, 就把当前路径上的点逐个判死, 整条路径报废。
        #   实测日志: 一连串 "[导航] 游戏不在前台 → 路径点 (2.4,7.2) 到不了 → 继续下一个"。
        _told = False
        while not ensure_focus(wait_s=1.0):
            if not _told:
                self.log("[导航] 游戏不在前台 —— 暂停等它回来(失焦不算导航失败)")
                _told = True
            self.kb.release_all()
            st0 = self.state()
            if not st0 or not st0.get("inRound"):
                return False            # 对局结束了才真的放弃
        if _told:
            self.log("[导航] 游戏回到前台, 继续走")
        tm = self.terrain()
        if tm is not None and tm.ok and tm.is_danger_world(tx, tz):
            # 以前这里没有这道闸: 目标落在水面上, 厨师就一路走进去淹死
            self.log(f"[导航] 目标 ({tx:.1f},{tz:.1f}) 落在危险格上, 拒绝前往")
            return False
        arr = self.arrive if arrive is None else arrive
        # 虚拟手柄才支持连续模拟量（360° 平滑对角走）。纯键盘注入时退化回
        # 原来的"单轴按键 + 按住/松开"离散走法。
        driver = get_driver()
        analog = driver is not None and hasattr(driver, "move") and hasattr(driver, "release_all")
        t0 = time.time()
        limit = self.step_timeout if step_timeout is None else step_timeout
        #: 本趟导航还信不信"危险格"判据。地图和游戏打架时会被关掉 —— 见下面危险那一段。
        _danger_trust = True
        _deaths = 0            # 本趟摔死了几次(见 MAX_RESPAWNS: 摔死→重生→再摔 是死循环)
        reach_t = None
        last_pos = None
        stuck = 0
        prev_dir = None       # 上一轮真正按下的主导轴方向, 用于识别"目标附近来回抖"
        flips = 0             # 目标附近方向翻转次数(受扰动/目标在动的证据)
        _wind_why_told = False  # 本趟是否已报过"补偿退化成纯追踪"(那行日志每 0.03s 都想打)
        dist = float("nan")
        try:
            while True:
                if time.time() - t0 > limit:
                    self.log(f"[导航] 超时 (还差 {dist:.1f} 格)")   # 只看"超时"分不清是没走到还是走错方向
                    return False
                # 位置是闭环控制器的输入，绝不能用 TTL 缓存旧帧：0.10 秒前的位置
                # 换算成方向就偏了，落在目标附近就表现为来回抖（World.state 注释里
                # 明确点名这是病根）。这里每次循环都强制读一帧新鲜的。
                st = self.state(force=True)
                if not st or not st.get("inRound"):
                    self.log("[导航] 对局结束, 中止")
                    return False
                x, z, _ = self.pos(st)
                if x is None:
                    return False
                dx, dz = tx - x, tz - z
                dist = (dx * dx + dz * dz) ** 0.5

                # ---- 风: **先问游戏**(权威), 读不到才退回几何投影(旧 DLL 兜底) ----
                # 权威 = `PlayerControls.WindReceiver.GetVelocity()`, 见 `_wind_of`。
                wnd, _w_trust = self._wind_of(st) if WIND_COMP else (None, True)
                if WIND_COMP and not _w_trust:
                    wnd = self._wind_at(tm, x, z)
                    if wnd is not None and not self._wind_fb_told:
                        self._wind_fb_told = True
                        self.log("[风] ⚠ 插件没报 `wind` 字段(旧 DLL?) —— "
                                 "本局退回几何投影(体积→格), 补偿会偏保守")
                # 厨师此刻的**真实**控制增益 R(= RunSpeed × MovementScale), 不是 self.speed。
                _R = self.chef_speed(st)
                # 把风沿"目标方向"分解: `a` = 纵向分量(正 = 顺风), `b²` = 横向分量²。
                # 依据(反编译): 位移 = **摇杆方向×R + 风**, 两者相加
                #   (`ClientPlayerControlsImpl_Default.cs:414` 的 `movementScale*vector*RunSpeed`
                #    与 `:902-906` 的 `ApplyWindForce` → `MovePosition(pos + v·dt)`)。
                _a = 0.0
                if wnd is not None and dist > 1e-4:
                    _ux, _uz = dx / dist, dz / dist
                    _a = wnd[0] * _ux + wnd[1] * _uz
                    # ☠ **逆风走不到就别硬顶**(文档 B4 的可行性检查)。
                    #   `R + a` 是**最大接近速率**(摇杆正对目标时能达到的沿目标方向分量)。
                    #   ≤ 0 ⇒ **任何**摇杆方向都推不出朝目标的净速度; 而任何可用速度的时间
                    #   平均都落在同一个圆盘里 ⇒ **连绕路也到不了**(不是"难", 是"不可能")。
                    #   ⇒ 只有这一条是"证明出来的不可达"; 硬顶只会把步超时耗光、还被推更远
                    #   (实机事故 s_balloon_2_3: 被推到边缘 → 掉空洞 → 烧掉整局)。
                    #   ⚠ 只看**沿目标方向**的分量 —— 侧风只是让人走斜, 那不叫走不到。
                    if _R + _a <= 0.0:
                        self.kb.release_all()
                        self.log(f"[风] ⚠ 逆风太强 —— 沿目标方向最大净速度只有 "
                                 f"{_R + _a:.2f} u/s(厨师 {_R:.1f}, "
                                 f"风 ({wnd[0]:.2f},{wnd[1]:.2f})), 这个方向到不了, 放弃")
                        return False

                # 死亡重生中: 游戏接管角色, 按键无效。必须松手等, 不能当"卡住"处理 ——
                # 这是之前"按键探针明明能用、导航却一直卡住"的真凶之一。
                #
                # ☠ **但"等回来→接着走→再摔死"是一个死循环**(实机 2026-09-14
                #   `s_wonderland_1_5`: 一路刷了 9 次 `厨师回来了(等了 6.0s)`, 整局就在
                #   这个循环里烧完)。两个成因缺一不可:
                #     ① 这里原来有 `t0 = time.time()` —— **把步超时重置了**,
                #        于是"走两步→摔→等 6 秒→再走"永远撞不到超时(和交接包里记的
                #        "危险格那段死循环"是同一个错法, 那次只修了那一段);
                #     ② 没有"这一趟摔死几次就不再硬走"的上限。
                #   ⇒ 现在: **不重置 t0**(超时仍然算数) + 摔死到上限就放弃这一趟。
                if self.is_respawning(st):
                    _deaths += 1
                    self.log(f"[导航] ⚠ 厨师正在死亡重生(这一趟第 {_deaths} 次), "
                             f"松手等游戏救回来")
                    if not self.wait_respawn():
                        return False
                    if _deaths >= MAX_RESPAWNS:
                        self._last_fail_kind = "death"
                        self.log(f"[导航] ✗ 这一趟已经摔死 {_deaths} 次 —— "
                                 f"**不再硬走这条路**(多半是路上有会沉的平台/危险物), "
                                 f"交给上层换目标")
                        return False
                    last_pos = None
                    stuck = 0
                    continue

                # **正被击退/冲量推着**(撞火 / 被投掷物砸 / 队友冲刺对撞, 文档 §2.4.2):
                # 冲量期间游戏按 S 曲线覆盖速度, **按键只起一部分作用** —— 硬顶是白费,
                # 而且容易被推去不该去的地方(边缘/危险格)。松手等它衰减完再继续。
                # 判据来自游戏(`m_impactTimer`), 不是我们猜的。
                if self.is_impacted(st):
                    self.log("[导航] ⚠ 正在被击退/推开 —— 停手等冲量衰减")
                    self.wait_idle(0.3)
                    last_pos = None
                    stuck = 0
                    continue

                # 掉水里 / 踩空: 这时按键完全无效(游戏接管了角色), 硬按只会白等超时。
                #
                # ☠ **这一段原来是个死循环**(实机 2026-09-14, ChickenNuggetsAndChips 那关):
                #   能走到这里就**一定不在重生态** —— 真在重生的话上面那段已经 `continue` 了。
                #   于是 `wait_respawn(4.0)` 在第一次 0.4s 轮询后立刻返回 True(它判的是
                #   "不在重生中"), 接着 `t0 = time.time()` **把步超时重置了** ⇒ 下一轮同样的
                #   条件再走一遍。步超时永远不触发 ⇒ `navigate` 永不返回 ⇒ `execute` 永远
                #   数不到失败 ⇒ **规则 5 在这一关彻底失效**, 日志里刷了几十遍
                #   `[重生] 厨师回来了(等了 0.4s)`。
                # 根因: `is_danger_world` 是**只看格子的 2D 判据**(不看高度), 而 KillPlane
                #   是 3D 体积 ⇒ 重生点/台面边缘很容易被投影成"危险"; 再加上
                #   `DANGER_CHARS` 把**空洞 V 也算危险**, 这关 832 格里 667 格被判危险。
                # 处置按规则 2(拿不准就问游戏): **游戏不认为他在危险里 → 信游戏。**
                if _danger_trust and tm is not None and tm.ok and tm.is_danger_world(x, z):
                    # 先给游戏 0.6 秒 —— 真掉下去的话 `m_bRespawning` 会翻起来(有个短延迟)
                    _t, _fell = time.time(), False
                    self.kb.release_all()
                    while time.time() - _t < 0.6:
                        time.sleep(0.2)
                        _st2 = self.state()
                        if not _st2 or not _st2.get("inRound"):
                            return False
                        if self.is_respawning(_st2):
                            _fell = True
                            break
                    if _fell:
                        self.log("[导航] ⚠ 厨师掉下去了 —— 松手等游戏救回重生点")
                        if not self.wait_respawn():
                            return False
                        t0 = time.time()        # 人被抓回重生点了, 这一趟重新计时
                    else:
                        # 游戏没重生他 ⇒ 我们的 2D 判据和现实不一致 ⇒ **本次导航不再用它**。
                        # ⚠ 只是不再**预测**; 真掉下去时上面那段 `is_respawning` 照样兜住,
                        #   所以关掉它不会让人淹死。
                        # ⚠ **这里故意不重置 `t0`** —— 重置就等于把步超时作废,
                        #   正是上面那个死循环的成因。
                        _danger_trust = False
                        self.log("[导航] ⚠ 地图判这一格危险, 但游戏没在重生他 —— "
                                 "信游戏, 本次导航不再用危险判据"
                                 "(多半是重生点/台面边缘被投影成了空洞)")
                    last_pos = None
                    stuck = 0
                    continue

                # ---- 防"抽搐": 判定放在下面算完主导轴之后 ----

                if dist <= arr:
                    if tight is None or dist <= tight:
                        self.kb.release_all()
                        return True
                    if reach_t is None:
                        reach_t = time.time()
                    if time.time() - reach_t > tight_timeout:
                        self.kb.release_all()   # 贴不更近(多半被台子挡住), 用当前距离
                        return True

                if last_pos is not None and abs(x - last_pos[0]) < 0.05 \
                        and abs(z - last_pos[1]) < 0.05:
                    stuck += 1
                else:
                    stuck = 0
                last_pos = (x, z)

                if stuck >= 8:
                    # **按了不走 = 这一格物理上过不去**(地图说能走也没用) ⇒ 记下来,
                    # 然后**中止这一趟, 交给外层重规划**。
                    #
                    # ☠ **这里原来写的是"侧移脱困"**(朝垂直于目标的方向按 0.35 秒, 然后
                    #   `continue` 继续朝同一个目标推) —— 那是个**死循环**:
                    #   撞 8 下 → 侧移 → 又撞 8 下 → 又侧移 … 直到 `step_timeout`。
                    #   实测(`s_sushi_1_3` 切鱼那次) 同一格连着 5 遍"卡住, 侧移脱困",
                    #   每遍都记一次"这一格过不去", 然后超时 —— 而**一次重规划都没有**,
                    #   所以学到的那格永远用不上(记了 5 遍同一格就是证据)。
                    # ⇒ 现在: 记下这一格 → `return False` ⇒ `navigate_smart` 看到
                    #   "这一趟学到了新禁行格" ⇒ **带着它重规划**(绕开走)。
                    #   ⚠ 侧移想解决的"被卡住"这类情形, 重规划**也能解决而且更好**:
                    #     新路径的**第一步方向本来就不一样**(这就是一次有依据的侧移),
                    #     而且它是从**当前位置**重算的、还会绕开已记下的格。
                    #   ⚠ 重规划是**有界**的(`replans`), 不会变成另一个死循环。
                    _rec = self._note_blocked(tm, x, z, dx, dz, dist)
                    self.kb.release_all()
                    if _rec:
                        self.log(f"[导航] ✗ 这一格推不过去 (~{x:.1f},{z:.1f}) —— "
                                 f"记下它, 中止这一趟让外层带新禁行格重规划")
                    else:
                        # ☠ **没记下就别再谎报"记下它"** —— 上面那句原来无论记没记都打,
                        #   于是"重规划"看起来有信息增量、其实没有(实机 `s_sushi_4_1`
                        #   读了半天才看出它一次都没记)。改由 `navigate_smart` 决定放弃。
                        self.log(f"[导航] ✗ 这一格推不过去 (~{x:.1f},{z:.1f}) —— "
                                 f"而且**没能记下是哪一格**(跨不出自己那格/方向太小), "
                                 f"重规划也是同一条路")
                    self._last_stuck_no_learn = not _rec
                    return False

                # ☠ **迈步之前先探一下"下一步那格"** —— 这是"直接走进水里"的根因(用户实机指出:
                #   "**没有按地图的可达图移动，直接去水里了**")。
                #   为什么原来拦不住:
                #     · 水面/空洞**不是障碍格**(水是 `RespawnCollider` 触发器, 不占格子)
                #       ⇒ 地形上就是 `.`, 只有**危险判据**能拦住;
                #     · 而危险判据只在"**当前格**危险"时报警 —— 那是**人已经在水里了**;
                #     · 加上 `A* 无解 → 直线硬冲` 那条兜底, 就成了"从安全格径直迈进水里 → 淹死
                #       → 重生 → 接着迈"的死循环(一路刷了 9 次重生)。
                #   ⇒ 判据: **当前格安全、而前方 0.9 格是危险格 ⇒ 停下, 不迈这一步**。
                #     ⚠ **只在"当前格安全"时管** —— 有些关卡的危险区判得很宽(整片都被
                #       KillPlane 投影成危险), 那时人本来就站在"危险"里, 若还拦就等于
                #       一步都走不了(见 `_danger_trust` 那段的历史教训)。
                if self._next_step_dangerous(tm, x, z, dx, dz, dist):
                    self.kb.release_all()
                    _px, _pz = x + dx / dist * 0.9, z + dz / dist * 0.9
                    self.log(f"[导航] ✗ 前方 0.9 格 ({_px:.1f},{_pz:.1f}) 是危险格"
                             f"(水/空洞) —— **不往里迈**, 这一趟到此为止")
                    return False

                # 连续模拟量：直接喂归一化方向，游戏每帧现读 → 平滑对角前进。
                # 旧走法是"单轴按键 + 按住/松开"，既走折线、又一停一走，画面上就是抽搐。
                # 方向换算依据 PlayerControlsHelper.GetControlAxis：MoveX 对应世界 x，
                # MoveY 被取负后才对应世界 z，所以世界 +z 要发 MoveY = -dz。
                if analog:
                    # ☠ **遥感地雷**: 如果此刻在**遥控驾驶会话**里, 这里发的方向键
                    #   驱动的是**被驾驶的平台, 不是厨师** —— 症状是"厨师纹丝不动,
                    #   平台却开到别处去了", 而这里的分支只会判成"厨师卡住→侧移脱困",
                    #   于是把平台越开越远。
                    #   判据: `self.session_station(st) is not None`(见那边的注释);
                    #   调导航前要么先退出会话, 要么改用 `pilot_to()` 直接开平台。
                    inv = 1.0 / dist if dist > 1e-4 else 0.0
                    _ax, _az = dx * inv, dz * inv
                    if wnd is not None and dist > 1e-4:
                        # ☠ **迎风补偿 = 精确前馈**(摇杆指向"净值方向", 不是目标方向)。
                        #   推导、四种退化情形、以及"为什么不能用 `u − W/R` 那种减法"
                        #   (顺风时它会归零 → 脚本在顺风走廊里拒绝往前走)全在 `wind_aim`。
                        # ⚠ 不按分量 `max(-1,min(1,·))` 夹紧: 游戏自己会 `.normalized`
                        #   (`PlayerControlsHelper.cs:70`), 我们只需要控制**方向**;
                        #   而逐分量夹紧在强风时会把向量**转掉**(恰好在最需要它准的时候)。
                        # ⚠ 除数是 **R = RunSpeed × MovementScale**(游戏报的), 不是 `self.speed`
                        #   —— 后者带 0.9 欠冲, 会把风高估 11%。
                        _ax, _az, _t, _why = wind_aim(_ax, _az, wnd[0], wnd[1], _R)
                        if _why not in ("精确前馈", "R≈0") and not _wind_why_told:
                            _wind_why_told = True      # 一次导航报一次就够(每 0.03s 打会刷屏)
                            self.log(f"[风] {_why}(t={_t:.2f}u/s) —— 补偿退化成纯追踪"
                                     f"(风 ({wnd[0]:.2f},{wnd[1]:.2f}), R={_R:.1f})")
                    # 等比缩到 ≤1: 方向**一点不变**(等比), 只是别把几百倍的浮点数丢给桥
                    # (`û'` 在 R 很小时(被压制/减速)会很大; 游戏反正只认方向)。
                    _n = max(1.0, abs(_ax), abs(_az))
                    # 最后一步才换算到**摇杆值**: 关卡反转轴时符号要跟着翻(见 `axis_signs`)。
                    _sx, _sy = self.axis_signs(st)
                    driver.move(_ax / _n * _sx, -_az / _n * _sy)
                    time.sleep(0.03)
                    continue

                # 死区随距离收缩: 远了走大步, 近了走小步, 避免在目标附近来回蹦
                dead = min(0.4, max(0.1, dist / 5.0))
                d = dir_for_step(dx, dz, deadzone=dead)
                if not d:
                    # 两轴都在死区内: 到了就收工, 没到就只推较大的那个轴(细调)
                    if dist <= arr:
                        self.kb.release_all()
                        return True
                    if abs(dx) >= abs(dz):
                        d = "right" if dx > 0 else "left"
                    else:
                        d = "up" if dz > 0 else "down"

                # ---- 防"抽搐"(用户实测: "一开局的疯狂抽搐寻格子") ----
                # 现象: 闭环死区太小, 走近了按住时长也变小, 于是左右反复修正 —— 画面上就是在抖。
                # ⚠ 但"方向翻了"**不等于**"到了": 被别人顶一下、目标在传送带上自己动、
                #   或拐角处两个轴轮流占优, 都会让方向反复。
                #   旧写法只要 dist<1.6 就 return True, 于是"还差 1.5 格"也当到达 ——
                #   实测: 连续 7 次 "就地收工 距 1.0~1.5 格", 人根本没到位, 后面交互全失败。
                #   现在的判据: **只有真到了(arr 阈值内)才算到达**; 没到就把翻转当"受扰动",
                #   继续走; 翻太多次说明这条路走不通 → 返回 False 让调用方重规划(而不是假装成功)。
                if prev_dir is not None and d != prev_dir and dist < 1.6:
                    if dist <= max(arr, 0.9):
                        self.kb.release_all()
                        self.log("[导航] 到目标附近(方向翻转, 距 %.2f 格 ≤ 阈值), 收工" % dist)
                        return True
                    flips += 1
                    if flips >= 4:
                        self.kb.release_all()
                        self.log("[导航] ⚠ 目标附近方向反复 %d 次仍差 %.2f 格 —— "
                                 "多半是被别的厨师顶开/目标在移动, 交给上层重规划" % (flips, dist))
                        return False
                    # 不 return, 下一步照常朝目标走
                prev_dir = d

                key = self._key({"left": "A", "right": "D", "up": "W", "down": "S"}[d])
                # 按住时长直接由运动学算, 不再靠猜。
                # 依据: PlayerControls.Movement.RunSpeed = 4f (PlayerControls.cs:28), 且平地
                # 水平速度是**每帧直接赋值**的(ClientPlayerControlsImpl_Default.cs:414,433-435)
                # —— 没有加速度、没有惯性、没有刹车, 所以 位移 = 4 × 按住秒数, 1 格(1.2u)=0.30s。
                # 旧代码固定 tap_hold=0.12 猜: 远了走不到、近了冲过头, 于是来回震。
                # 一次只推**主导轴**, 所以按主导轴上的距离算。
                step_dist = max(abs(dx), abs(dz))
                # ☠ **迎风补偿(键盘这一路)**: 只按一根轴, 所以只算**这根轴上的风分量**。
                #   顺风 → 有效速度变大 → 按住时间变短; 逆风 → 变长(文档 B4 的
                #   `(目标位移)/(RunSpeed ± wind)`)。
                #   轴的正方向依据 `dir_for_step` 的标定: right=+x / left=−x / up=+z / down=−z。
                _eff = self.speed
                if wnd is not None:
                    _w_axis = {"right": wnd[0], "left": -wnd[0],
                               "up": wnd[1], "down": -wnd[1]}[d]
                    # 闸门用**真实速度** R 判(物理), 按住时长用 `self.speed`(带 0.9 欠冲,
                    # 宁可多按几次也别冲过头 —— 那是数字键这一路一贯的取舍)。
                    # ⇒ 闸门比"按欠冲后的 3.6"更宽松一点(3.6 会把 w_axis<−3.4 当成顶不动,
                    #   而真值要到 w_axis ≤ −4 才真的推不动), 这个保守方向是安全的。
                    if _R + _w_axis <= 0.0:
                        # 这根轴逆风顶不动 —— 换轴是上层的事, 这一步先认输(别把超时耗光)
                        self.kb.release_all()
                        self.log(f"[风] ⚠ 按 {d} 这根轴逆风顶不动"
                                 f"(沿该轴最大净速度 {_R + _w_axis:.2f} u/s, 厨师 {_R:.1f}), "
                                 f"放弃这一步")
                        return False
                    _eff = self.speed + _w_axis
                    if _eff < 0.05:
                        # 欠冲后的除数落进"几乎推不动"区(真值还有效, 只是 0.9 的余量吃掉它了)
                        # ⇒ 按到最长, 让闭环多试几次, 别除出负数/零来。
                        _eff = 0.05
                hold = step_dist / _eff
                hold = max(0.05, min(self.max_hold, hold))
                key_down(key)
                time.sleep(hold)
                key_up(key)
                time.sleep(self.tap_gap)
        finally:
            if analog:
                try:
                    driver.release_all()
                except Exception:
                    pass
            self.kb.release_all()

    # ---------------- 交互 ----------------
    def face(self, tx: float, tz: float, hold: float = 0.10) -> bool:
        """朝目标方向轻点一下方向键, 把厨师**转过去**(顺带贴近一点)。

        为什么非做不可(反编译依据):
          PlayerControls.FindNearbyObjects (PlayerControls.cs:745) 调用
            InteractWithItemHelper.GetCollidersInArc(1f, PI, m_Transform, ...)
          其中 IsColliderInArc (InteractWithItemHelper.cs:153-163) 的判定是
            Dot(_forward, 指向目标的向量) >= cos(arc/2) == cos(PI/2) == 0
          —— **只认"朝向前方 180° 半圆"内的东西**。
        而厨师的面朝方向 = 它**最后一次移动的方向**(位移方向取自输入向量, 与朝向解耦)。
        所以从台子另一侧走过去、或者绕了个弯过来, 面朝很可能是背对的 ——
        这时按键完全没反应, 而日志只会显示"持有物未变", 看起来像交互坏了。

        位移方向与朝向无关 ⇒ 轻点一下就能转头, 不需要大动作。
        """
        from pathing import dir_for_step
        from bridge.keyboard_input import key_down, key_up
        st = self.state(force=True)
        if not st or not st.get("inRound"):
            return False
        x, z, _ = self.pos(st)
        if x is None:
            return False
        dx, dz = tx - x, tz - z
        dist = (dx * dx + dz * dz) ** 0.5
        if dist < 0.05:
            return True

        # 别为了转身把自己送进危险格(转身会实际位移 0.4 格左右)
        tm = self.terrain()
        if tm is not None and tm.ok:
            look = min(0.6, dist)
            nx = x + dx / dist * look
            nz = z + dz / dist * look
            if tm.is_danger_world(nx, nz):
                self.log("[朝向] 目标方向是危险格, 不转身")
                return False

        # ☠ **斜方向要两个轴一起按**（2026-09-15 用户指出"**应该是往左上方丢**"）:
        #   面朝方向 = 它**最后一次移动的方向**, 而 `dir_for_step` 只给**单轴** ⇒
        #   原来这里只能做出正左/正右/正上/正下四种朝向。目标在斜方向时**差 45°** ——
        #   投掷按朝向飞 6 格, 45° 的偏差 = 横向偏出 **4.2 格**, 等于丢到隔壁去了。
        #   (实测: 黄瓜该往左上(菜板)丢, 飞出去的却是另一个方向。)
        #   两个键同时按 ⇒ 虚拟手柄的摇杆落到那个斜角 ⇒ 朝向就是斜的 ✓。
        #   ⚠ 死区按**比例**取: 目标几乎正对着某个轴时只按那一根, 免得为了 2° 之差去抖两个轴。
        dead = max(0.05, 0.12 * dist)
        keys = []
        if abs(dx) > dead:
            keys.append(self._key("D" if dx > 0 else "A"))
        if abs(dz) > dead:
            keys.append(self._key("W" if dz > 0 else "S"))
        if not keys:                      # 两轴都在死区内(几乎在正上方/正右方这种)
            d = dir_for_step(dx, dz, deadzone=0.02)
            if not d:
                return True
            keys = [self._key({"left": "A", "right": "D", "up": "W", "down": "S"}[d])]
        for k in keys:
            key_down(k)
        time.sleep(hold)
        for k in keys:
            key_up(k)
        time.sleep(0.08)
        return True

    def interaction_targets(self, st: dict) -> tuple:
        """游戏自己认为这个厨师**现在按交互键会作用到哪个物体** (pick, use)。

        依据: 插件读 PlayerControls.CurrentInteractionObjects (PlayerControls.cs:394),
        pick = m_TheOriginalHandlePickup(抓取键), use = m_interactable(工位交互键)。
        这是**权威答案** —— 交互判定量的是"到碰撞体表面的距离 < 1.0 + 朝向前 180°"
        (InteractWithItemHelper.cs:119,153-163), 台面有体积、厨师走不到正中间,
        所以脚本自己拿格子中心算距离是没有意义的。
        """
        c = self.chef(st or {})
        return (c.get("pick") or "", c.get("use") or "")

    def placement_target(self, st: dict) -> str:
        """游戏自己认为这个厨师现在按下会**放到哪个物体** (`m_iHandlePlacement` 所在物体名)。

        为什么必须看它(用户实测: "锅的定位不是很好"):
          `InteractDirect` 发 `ReceivePlaceEvent` 时带的**就是**这个物体
          (见 bridge/virtual_pad.py 的 tap → call_direct)。所以 **它指向谁, 东西就真的
          会放到谁那儿**。而站位不对时它会指向旁边一个**无关的台面** ——
          此时直调**照样返回 `ok=True`**, 因为 InteractDirect 只是把游戏算好的目标转交出去,
          它不知道我们要的是锅。结果就是一个"假成功": 东西放上了旁边的柜台,
          引擎却以为进锅了, 后面全是错的。

        返回空串表示游戏此刻没有放置目标(站位太差)。
        """
        c = self.chef(st or {})
        return (c.get("placeh") or "")

    def _place_target_ok(self, stove: Station, pot: str, want_pot: bool) -> tuple:
        """游戏说的放置目标, 是我们想放的那个吗? 返回 (是否OK, 游戏说的目标名)。

        ☠☠ **`want_pot=True` 时只认锅, 不认灶台** —— 这一条原来是错的, 而且错在
          **和本函数自己的用途正相反**的地方(2026-09-15 `s_mine_2_5` 实机打回来的):
            `cand` 里**永远**放着 `stove.name`, 于是"游戏说会放到灶台"也算 OK。
            可**灶台自己接不住食材** —— 锅架在灶上时, 要放进去的是**锅**
            (`ServerCookableContainer`), 灶台那条路走到直调就是
            `placeCanHandle=False`。
          实机账(原话节选, 三次重试一模一样):
            `去 hob1 煮 Rice —— 用灶上那口锅 'equipment_frying_pan_01 (4)'`
            `直调那一趟: ok=True target='workstation_cooker_01 (5)' placeCanHandle=False`
            `[步骤] ⚠ 东西没放上去(锅/灶台没接住)` → `✗ 放弃: cook Rice` → **整单报废**
          ⇒ 本函数的用途本该是"**宁可这一步失败, 也不要放错地方还报成功**"
            (上面调用点的原话), 结果它把"游戏指着灶台"判成了 OK ⇒ 闸门形同虚设。
        ⚠ `want_pot=False`(食材自带 `CookingHandler`, 直接放灶台)**照旧认灶台** ——
          那条路是正的, 别一起改掉。
        ⚠ 游戏没报目标(`""`)⇒ 仍然放行: 判断不了, 交给交互本身去失败。
        """
        tgt = self.placement_target(self.state(force=True))
        if not tgt:
            return True, "(空)"        # 游戏没给目标 —— 判断不了, 交给交互本身去失败
        t = self._norm(tgt)
        # ☠☠ **台面名和锅名都要认** —— 不能再收窄成"只认锅"。
        #
        # 依据(反编译, 规则 1): `ServerAttachStation.cs:107-119`
        #     public bool CanHandlePickup(ICarrier _carrier) {
        #         if (m_item != null) {
        #             IHandlePickup h = m_item.AccessGameObject().RequireInterface<IHandlePickup>();
        #             return h.CanHandlePickup(_carrier);      // ← 转发给 m_item(锅)
        #         } ... }
        #   台面把拾取/放置**转发**给上面架着的那件东西 ⇒
        #   **"游戏说会放到台面"是正确表达**(= 放进这台面上的那口锅), 不是"指错了目标"。
        #
        # ☠ 我 2026-09-15 一度把它收窄成"要锅就**只**认锅"(理由写的是"灶台自己接不住食材")
        #   —— **那是错的, 而且当场把整单打死了**: `s_sushi_1_3` 实机
        #     `⚠ 站位不对: 游戏说会放到 'workstation_cooker_01 (1)', 而不是
        #       'workstation_cooker_01 (1)' / 锅 'utensil_pot_01' —— 不按`
        #   **游戏报的名字和"而不是"里列的一模一样** —— 自己跟自己比还判"不对" ⇒
        #   三次重试 ⇒ `✗ 放弃: cook SushiRice` ⇒ **整单报废**。
        #
        # ⇒ **"该用哪口锅"不归这条判据管** —— 它归 `_pick_stove` 的
        #   `cook_id ∈ cook_steps`(那条才是游戏拒收的权威判据, 见那边的注释)。
        #   这条只回答"游戏此刻的放置目标是不是**我们选中的那个灶台/那口锅**"。
        cand = [self._norm(stove.name)]
        if want_pot and pot:
            cand.append(self._norm(pot))
        return (t in cand), tgt

    def interact(self, kind: str = "pickup", verify_hold_change=True) -> bool:
        # force=True: 这一段的全部意义就是"按键之后世界变了没有", 绝不能用缓存旧帧
        st = self.state(force=True)
        if not st or not st.get("inRound"):
            return False
        _, _, held_before = self.pos(st)
        if kind == "pickup":
            # ☠ **遥感地雷**: 拾取/交互/冲刺 **三个键都是"退出遥控驾驶"的键**
            #   (`ServerSessionInteractable` 的 `SessionBase.Update`: 按下任一个就
            #    `OnSessionEnded`)。所以在遥感会话里调 `interact()` 等于**踩刹车** ——
            #   而它照样返回 True, 看起来像"交互成功"。
            #   要主动退出请用 `pilot_end()`(它就是干这个的, 名字也说得清)。
            self.kb.pickup()
        elif kind == "chop":
            self.kb.chop()
        elif kind == "dash":
            self.kb.dash()
        if not verify_hold_change:
            time.sleep(0.35)
            return True

        # **轮询确认, 而不是只看一眼**。
        # 实测踩的坑: 用户看到"厨师拿了东西又放下"。原因是原来只等 0.35 秒读一次 held,
        # 读到空就判失败 → execute 重试 → **再按一次 pickup 把刚拿到的东西放回去了**。
        # 拿/放是同一个键的开关, 所以"误判失败"的代价不是白跑一趟, 而是把战果毁掉。
        # 这里最多轮询 1.6 秒, 只要中途看到变了就算成功。
        seen = [(held_before or "")]
        t0 = time.time()
        while time.time() - t0 < 1.6:
            time.sleep(0.18)
            st2 = self.state(force=True)
            if not st2 or not st2.get("inRound"):
                return False
            _, _, held_after = self.pos(st2)
            cur = held_after or ""
            seen.append(cur)
            if cur != (held_before or ""):
                return True
        c = self.chef(self.state(force=True) or {})
        self.log("[交互] %s: 持有物始终未变(%s); 游戏说此刻可作用: 抓取=%r 工位=%r 放置=%r; "
                 "手持(服务端)=%r 手持(客户端)=%r 位置=(%.1f,%.1f)%s%s"
                 % (kind, "→".join(repr(s) for s in seen[:3]),
                    (c.get("pick") or "(空)"), (c.get("use") or "(空)"),
                    (c.get("placeh") or "(空)"),
                    (c.get("held") or ""), (c.get("heldc") or ""),
                    float(c.get("x") or 0), float(c.get("z") or 0),
                    self._key_hint(c), self._direct_hint()))
        return False

    @staticmethod
    def _direct_hint() -> str:
        """失败时把"直调那一趟游戏怎么回的"也摆出来。

        为什么必须有: 交互现在走的是**直调游戏的交互入口**(InteractDirect, 见
        `neko/bridge/virtual_pad.py:tap`), 按键只是兜底。所以"没拿起来"有两种完全不同的原因 ——
          · 直调根本没接上(target 为空) → 站位问题
          · 直调接上了但服务端没办成     → 容器判据问题(CanHandlePickup/CanHandlePlacement)
        没有这一行就只能看到"持有物未变", 两种原因长得一模一样。
        """
        try:
            from bridge import keyboard_input as _ki
            d = _ki.get_driver()
            r = getattr(d, "last_direct", None)
            if not r:
                return ""
            # `placeCanHandle` = **游戏自己说**"这个台面此刻收不收手里的东西"
            # (C# 侧 `InteractDirect.PlaceCanHandle`, 复现 `PlaceHeldItem_Server` 里
            # 那次 `CanHandlePlacement`; 拿不到答案就是 None)。有它就能一眼分开:
            #   · `placeCanHandle=false` ⇒ 站位/朝向对、名字也对, 但**游戏就是不收**
            #     (锅在煮/盘子满/台面类型不对) —— 再挪站位也没用
            #   · `placeCanHandle=true` 却仍然失败 ⇒ 问题在**后面**(消息链/容器判据)
            # `placeCanHandleBefore` = **强制刷新那行之前**的同一个判决。两者不同就说明
            # `UpdateNearbyObjects()` 确实在改放置判决(见 `InteractDirect` 里那段悬案注释)。
            return ("\n         直调那一趟: ok=%s method=%s target=%r pick=%r place=%r "
                    "placeCanHandle=%s (刷新前=%s)"
                    % (r.get("ok"), r.get("method"), r.get("target"),
                       r.get("pick"), r.get("place"), r.get("placeCanHandle"),
                       r.get("placeCanHandleBefore")))
        except Exception:
            return ""

    @staticmethod
    def _key_hint(c: dict) -> str:
        """把"游戏说按键有没有用"翻译成人话。

        `canpress` 来自 PlayerControls.CanButtonBePressed()(PlayerControls.cs:453-468):
        它同时检查 窗口在前台 + 角色直接受控 + 没开着对话框/根菜单。
        **停在暂停菜单时按键全部无效、位置也一直不变 —— 从日志上看和"卡住"一模一样**,
        有这一句就能立刻分辨, 不用再猜(实测踩过: 以为寻路坏了, 其实是游戏停着)。
        """
        if "canpress" not in c:
            return ""                       # 老 dll 没有这个字段
        if c.get("canpress"):
            return "  游戏说按键可用"
        return "  ⚠ 游戏说按键此刻**无效** —— 多半是停在暂停菜单/窗口不在前台, 不是寻路坏了"

    # ---------------- 组装台面 ----------------
    def pick_assemble_spot(self, km: KitchenMap, x: float, z: float,
                           claim: bool = True) -> Station | None:
        """挑摆盘位。**优先挑已经有盘子的台面** —— 那样材料放上去就直接进盘,
        不必先跑去拿盘子。双人时通过黑板保证两人不用同一个。

        ⚠ **整局内粘住**(用户实测的 bug): 半成品就放在某个台面上, 换个台面等于从零重来。
          旧行为是"优先挑空台面"(`not s.on`) —— 于是重新规划时, 上一轮放了材料的台面
          因为 `on` 非空而**被排除**, 挑到一个空台面; 从空台面看"什么都没有", 就把
          同一份材料再取一遍。实测一局里同一份海带取了 **7 遍**, 材料还散落在多个台面上
          (用户原话: "菜谱三个食材 1、2 加过了缺少 3, 但是脚本还会拿 1 去补")。

        `claim=False` —— **只读模式**: 算出来但不写 `_assemble_sid`、不在黑板上占位。
        评分层给候选打分时要**替队友**也算一遍目标, 那种调用绝不能以我的 cid 占走台面。
        """
        _raw = [s for s in (km.of("counter") or km.of("board"))
                if not s.spawn and s.kind != "CookingStation"]
        # **放不下东西的台面先别挑**(用户: "返回到其他路")。见 `PLACE_SENTINEL`:
        # 这条记的是"这个台面接不住放置", 和材料名无关。
        # ☠☠ **但全被划掉时必须退回不过滤的那份** —— 返回 None 是"整局卡死"的命门
        #   (见 `_prepare_plate` 那段的注释): 没有摆盘位 ⇒ `assemble` 永远 `-inf`
        #   ⇒ 手里那份料再也放不下去, 一路等到 `IDLE_WAIT`。宁可去试一个已知不好的台面
        #   (规则 5 的老话: 宁可做错也别让死)。
        allc = [s for s in _raw if self._branch_ok(PLACE_SENTINEL, s.id)] or _raw
        if not allc:
            return None
        # **队友正用着的台面先排后面**(用户实机: "这个盘子被我用了, 脚本就会卡住")。
        # 全被占着时不动 `allc` —— 和 `_pick_board` 的兜底同理, 宁可去挤也别没有。
        _mate = self._mate(self.state())
        _free = [s for s in allc if not self._mate_near(s, _mate)]
        if _free:
            allc = _free
        with_plate = [s for s in allc if self._has_plate(s)]

        # 先认上一次用的那个 —— 按 id 到**新鲜的 km** 里取, 保证 `.on` 不是陈旧快照
        #
        # ⚠ 但"台面上有盘子"是**更高优先级**, 不能无条件守旧:
        #   盘子是"材料自动进盘"的前提(PlacementContainer), 没有它材料只能干放在台面上,
        #   后面怎么拼都拼不出菜。而盘子会被送餐消耗掉 —— 上一单送走之后原来那个台面就空了。
        #   所以: 旧台面还有盘子 → 认它; 全场一个带盘子的台面都没有 → 也只能认它;
        #   否则(别处有盘子而它没有) → 让下面的逻辑去挑那个有盘子的。
        if self._assemble_sid:
            prev = km.stations.get(self._assemble_sid)
            if prev is not None:
                # ⚠ **队友正在用那个台面时不许"守旧"** —— 整局粘住 + 人类占用 = 卡死
                #   (用户实机: "这个盘子被我用了, 脚本就会卡住")。让他一下, 去挑别的。
                if not self._mate_near(prev, _mate) and (self._has_plate(prev) or not with_plate):
                    return prev
            elif claim:
                self._assemble_sid = ""    # 台面没了(换关/被拆) → 重新挑

        # ⚠ 这里**必须**有兜底: `with_plate` 为空(全场没有一个台面带盘子)时,
        #   旧写法 `if with_plate: cands = with_plate` 会让 `cands` **未绑定**,
        #   下面两处无条件用它 ⇒ `UnboundLocalError`。触发路径: 换关后刚开局
        #   (`_assemble_sid` 为空) + 台面上还没摆出盘子 —— 而 `_prepare_plate()`
        #   在 execute() 开头就会调这里, 且**没有 try 兜着**。
        #   兜底取 `allc` 正是上面注释本来的意思: "全场一个带盘子的台面都没有 → 也只能认它"。
        cands = with_plate or allc
        if self.board is not None:
            spot = self.board.pick_spot(cands, self.cid, (x, z)) if claim else \
                min(cands, key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2)
        else:
            serve = km.nearest("serve", x, z)
            ax, az = (serve.x, serve.z) if serve else (x, z)
            spot = min(cands, key=lambda s: (s.x - ax) ** 2 + (s.z - az) ** 2)
        if spot is not None and claim:
            self._assemble_sid = spot.id
        return spot

    # ---------------- 键位归属（权威依据） ----------------
    #: 游戏的 Player 枚举 → 键盘半区（v1 §3.2: SplitPadHost=Left=WASD, SplitPadGuest=Right=方向键）
    _PLAYER_TO_KEYS = {"one": "P1", "two": "P2", "three": "P3", "four": "P4"}

    def bind_keys_by_player(self, km: KitchenMap) -> bool:
        """按**厨师归属的玩家**选键盘 —— 权威依据, 不用猜也不用探测。

        为什么需要: `cid` 只是 `FindObjectsOfType(PlayerControls)` 的枚举序号,
        与 `Player.One/Two` **没有必然关系** —— 实测遇到过 `cid=0` 其实是 `Player.Two`,
        于是给它发 WASD 一动不动(四个方向全无反应)。
        依据: `ClientInputTransmitter.Setup()` 里 `iD = GetComponent<PlayerIDProvider>().GetID()`。
        """
        from bridge.keyboard_input import PLAYER1, PLAYER2
        chef = km.chef(self.cid)
        if chef is None:
            return False
        pid = (getattr(chef, "player", "") or "").strip().lower()
        if not pid:
            return False                      # 老 dll 没这个字段 → 交给探测兜底
        want = self._PLAYER_TO_KEYS.get(pid)
        if want is None:
            self.log(f"[键位] 未知的玩家归属 {chef.player!r}")
            return False
        self.kb = KeyboardPlayer(PLAYER1 if want == "P1" else PLAYER2)
        self.log(f"[键位] 厨师#{self.cid} 属于 {chef.player} → 用 {want} 键位")
        return True

    # ---------------- 键位自动探测（兜底） ----------------
    def probe_bindings(self, candidates=None) -> dict | None:
        """**实测**哪一套键位能驱动我这个厨师（cid）。

        为什么必须实测: 厨师 id 来自 `PlayerControls` 的枚举顺序, 与"键盘左半/右半"
        没有必然对应 —— 实测遇到过 `cid=0` 其实归「方向键」那一路、而 WASD 完全
        没绑定到任何玩家的情况。靠假设选错键位, 表现就是"发了一堆按键但人一动不动"。
        """
        from bridge.keyboard_input import PLAYER1, PLAYER2, ensure_focus, key_down, key_up
        cands = candidates or [("P1(WASD)", PLAYER1), ("P2(方向键)", PLAYER2)]
        st = self.state()
        p0 = self.pos(st) if st else (None, None, "")
        if p0[0] is None:
            self.log("[键位] 探测失败: 读不到厨师位置")
            return None
        if not ensure_focus(wait_s=self.focus_wait):
            self.log("[键位] 探测失败: 游戏不在前台(脚本不抢焦点)")
            return None
        for label, b in cands:
            for key in (b["up"], b["down"], b["left"], b["right"]):
                key_down(key)
                time.sleep(0.22)
                key_up(key)
                time.sleep(0.18)
                p1 = self.pos(self.state())
                if p1[0] is None:
                    continue
                if abs(p1[0] - p0[0]) > 0.05 or abs(p1[1] - p0[1]) > 0.05:
                    self.log(f"[键位] 探测到可用键位: {label} (按 {key} 使 "
                             f"P{self.cid + 1} 从 ({p0[0]:.1f},{p0[1]:.1f}) 移到 "
                             f"({p1[0]:.1f},{p1[1]:.1f}))")
                    self.kb = KeyboardPlayer(b)
                    return b
        self.log(f"[键位] ⚠ 两套键位都驱动不了 P{self.cid + 1} —— "
                 f"检查: 该玩家是否已加入? 窗口是否真前台?")
        return None


    def _urgency(self) -> float:
        """局面紧急度 0..1（订单剩余时间越少越大）—— 供"情境收敛"用（v1 §5.1）。"""
        try:
            orders = self.live_orders()
        except Exception:
            return 0.0
        if not orders:
            return 0.0
        left = min(float(o.get("t", 1.0)) for o in orders)
        return max(0.0, min(1.0, 1.0 - left))

    def _apply_mischief(self, m, km, st) -> None:
        """演一次失误/捣蛋。**只做小动作，不改变流程控制**（做完照常继续）。

        形态与强度对齐 v1 §5.1：轻=挡路/慢，中=半成品放错台，重=倒队友菜/烧糊。
        """
        from modes import Mischief
        x, z, held = self.pos(st)
        if x is None:
            return

        if m == Mischief.DAZE:                      # 轻：发呆一拍
            time.sleep(1.2)
        elif m == Mischief.SLOW:                    # 轻：磨蹭
            time.sleep(0.9)
        elif m == Mischief.DETOUR:                  # 轻：绕远路
            far = max(km.stations.values(),
                      key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2)
            self.navigate_smart(km, far.x, far.z, tight=1.2)
        elif m == Mischief.OVER_CHOP:               # 中：多切几刀
            for _ in range(3):
                self.kb.chop()
                time.sleep(0.3)
        elif m == Mischief.WRONG_SPOT:              # 中：手上东西丢到别处
            if held:
                spot = self.pick_assemble_spot(km, x, z)
                if spot is not None:
                    self.navigate_smart(km, spot.x, spot.z, tight=0.6)
                    self.interact("pickup", verify_hold_change=False)
        elif m == Mischief.FORGET_PLATE:            # 中：跑去看一眼盘子又回来
            src = self._find_item_station(km, "Plate", x, z)
            if src is not None:
                self.navigate_smart(km, src.x, src.z, tight=0.8)
                time.sleep(0.6)
        elif m == Mischief.SNACK:                   # 中：把手上的丢垃圾桶
            b = km.nearest("bin", x, z)
            if b is not None and held:
                self.navigate_smart(km, b.x, b.z, tight=0.8)
                self.interact("pickup", verify_hold_change=True)
        elif m == Mischief.BIN_TEAMMATE:            # 重：倒队友台面上的东西
            cands = [s for s in km.stations.values()
                     if s.on and s.id.rstrip("0123456789") not in ("serve", "plates")]
            b = km.nearest("bin", x, z)
            if cands and b is not None:
                t = min(cands, key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2)
                self.navigate_smart(km, t.x, t.z, tight=0.8)
                if self.interact("pickup", verify_hold_change=True):
                    self.navigate_smart(km, b.x, b.z, tight=0.8)
                    self.interact("pickup", verify_hold_change=True)
        elif m == Mischief.BURN:                    # 重：放任灶台烧着
            time.sleep(3.0)
        elif m == Mischief.BLOCK:                   # 重：堵一下路
            time.sleep(2.0)
        time.sleep(0.2)

    def _maybe_mischief(self, km, st) -> None:
        """每个空闲决策点调一次：要不要演一次失误/捣蛋（v1 §4/§5）。"""
        ms = self.mode_state
        if ms is None:
            return
        try:
            m = ms.roll(urgency=self._urgency())
        except Exception as e:
            self.log(f"[模式] 掷骰异常: {e}")
            return
        if m is None:
            return
        self.log(f"[模式] P{self.cid + 1} {ms.mode.value} → 演 {m.value}")
        try:
            self._apply_mischief(m, km, st)
        except Exception as e:
            self.log(f"[模式] 演 {m.value} 失败(忽略): {e}")
        finally:
            self.kb.release_all()

    # ---------------- 各步骤 ----------------
    # WASD 字母 → 逻辑方向(绑定表是按逻辑方向索引的, 不是按字母)
    _WASD2DIR = {"W": "up", "S": "down", "A": "left", "D": "right"}

    def _key(self, wasd: str) -> str:
        """把"逻辑方向"翻译成**这个厨师实际绑定的物理键**。

        为什么必须走这里(实测踩的大坑):
          分屏双人时两个厨师用的是**两套完全不同的键**:
            Player.One → WASD 区(左半键盘)      Player.Two → 方向键区(右半键盘)
          而导航里原先把这个映射**硬编码成 WASD**:
              key = {"left": "A", "right": "D", "up": "W", "down": "S"}[d]
          于是:
            · 上一关厨师是 Player.One → 硬编码碰巧对上, 看着"能用"
            · 这一关厨师是 Player.Two → 导航发出的是 WASD, 而它只听方向键
              ⇒ 厨师**一步都没动**, 日志却只有"卡住/超时(还差 1.0 格)", 极难定位
          交互走的是 self.kb(已按玩家绑定), 所以"取东西"看起来正常 —— 只有移动坏掉。
        现在所有移动键一律过这里, 与 interact 用同一套绑定。
        """
        d = self._WASD2DIR.get((wasd or "").upper())
        if d is None:
            return wasd                    # 不是方向键的原样返回
        return self.kb.b.get(d, wasd)       # 取不到就退回字母本身

    @staticmethod
    def _norm(s: str) -> str:
        """只留字母数字, 用于比物品名。

        先去掉实例编号后缀: 场景里同一类物品的实例叫 "SushiPrawn (2)"、"Plate 5 (3)"
        "utensil_pot_01 (1)" —— 计划里用的是 "SushiPrawn"。不剥掉后缀就只能靠
        "子串包含"兜底, 那会误判(例如 SushiPrawn 与 SushiPrawnCooked 互相包含)。
        """
        import re as _re
        t = (s or "").strip()
        t = _re.sub(r"\s*\(\d+\)\s*$", "", t)      # 去掉结尾的 " (2)"
        t = _re.sub(r"\s+\d+\s*$", "", t)          # 去掉结尾的 " 5"
        return "".join(ch for ch in t.lower() if ch.isalnum())

    def _held_is(self, held: str, want: str) -> bool:
        """手上拿的是不是想要的那个东西。"""
        if not want:
            return True
        h, w = self._norm(held), self._norm(want)
        if not h:
            return False
        return h == w or h.startswith(w)

    def _stand_cell_of(self, tm, tx: float, tz: float, cx: float, cz: float,
                       max_di: int = 2, ortho_only: bool = False, reach=None):
        """`_stand_cell` 的**返回"格子"版** —— 返回 `(i,j)` 或 None。

        单独拆出来是给**评分层**用的: 它既要知道"站不站得到"(可达性闸门),
        又要知道"是哪一格"才能去距离表里查步数。只拿世界坐标是查不到的。

        `reach` 可以传一份**预算好的** `{cell: 步数}`(`TerrainMap.distances_from`)。
        不传就按老样子自己跑一次 BFS —— 语义与拆分前**逐字一致**。
        评分层一次决策传两份(我一份、队友一份), 于是 BFS 次数从 2×候选数 降到 2。
        """
        if tm is None or not tm.ok:
            return None
        if reach is None:
            # ⚠ **`st` / `km` 必须自己取**: 这里原来直接引用了 `st` 和 `km`, 而它们
            #   **既不是参数也不是局部变量** —— 那是必然的 `NameError`, 调用点没有一个
            #   try 兜着, 于是整个"走到台面旁边"的路径全瘫(实测: `_approach` 一进去就炸)。
            #   症状像 §5.2 记的那种"改调用处时误伤": `at_y=` / `extra_edges=` 是后加的,
            #   参数没跟着穿进来。自己取一帧最省事(`state()` 吃 TTL 缓存, 几乎不要钱)。
            st = self.state()
            km = self.map(st) if st else None
            reach = tm.reachable_from(cx, cz, at_y=self.chef_y(st),
                                      extra_edges=self._travel_edges(km, tm))
        i, j = tm.cell_of(tx, tz)
        wind = self._wind_cells()          # 一次算好, 别在双循环里反复取
        best, best_d = None, None
        for dj in range(-max_di, max_di + 1):
            for di in range(-max_di, max_di + 1):
                if di == 0 and dj == 0:
                    continue
                if ortho_only and (di != 0 and dj != 0):
                    continue          # 只要正上下左右: 斜角距 1.70 格, 够不着(交互半径 1.0)
                c = (i + di, j + dj)
                if not tm.walkable(*c):
                    continue
                if c not in reach:
                    continue          # 站得到但过不去, 等于没用
                wx, wz = tm.world_of(*c)
                d = (wx - cx) ** 2 + (wz - cz) ** 2
                # ☠ **别选传送带格当站位**: 站在带子上**输入为 0 也会被推走**
                #   (`RigidbodyMotion.Movement` = `MovePosition(pos + v·dt)`,
                #    和厨师有没有按方向键无关 —— 见 `conveyor_edges` 的注释)。
                #   站在那儿交互 = 人一直在漂, 交互判定时有时无, 表现成"卡住/来回抖"。
                #   用**足够大的惩罚**而不是直接排除: 旁边只有带子时还是得站上去。
                if tm.at(*c) == CH_TRAVELATOR:
                    d += BELT_STAND_PENALTY
                # ☠ 风区格**同理**: 风和传送带走的是同一条位移通道(每帧 `MovePosition`,
                #   不按键也照推), 站进去一样会漂 —— 见 `wind_cells()` 的注释。
                #   实机事故(s_balloon_2_3): 站在风区里被一路推到边缘掉下去。
                if c in wind:
                    d += WIND_STAND_PENALTY
                if best_d is None or d < best_d:
                    best_d, best = d, c
        return best

    def _stand_cell(self, tm, tx: float, tz: float, cx: float, cz: float,
                    max_di: int = 2, ortho_only: bool = False):
        """找一个"能站、够得着目标、且离厨师最近"的格子 —— 该站哪儿去拿东西。

        为什么不能直接朝台面坐标走(用户实测指出的问题):
          台面(含**台面传送带 ConveyorStation**)本身就是**障碍格**, 厨师站不上去。
          直接 navigate(tx,tz) 等于顶着橱柜往里推 —— 表现就是"卡住 + 超时(还差 1.0 格)"。
          s_sushi_4_5 的传送带是**环绕四周一整圈**的, 食材就在这一圈上跑;
          玩家能拿到的只有"这一圈旁边那些没被遮挡的格子"。
        所以正确做法: 从目标的相邻格里挑一个 **可走 + 从厨师出发真的到得了** 的,
        站进那一格中心, 再转身面向目标交互
        (交互半径 1.0, 格距 1.2 —— 站在相邻格刚好够得着)。

        返回 (世界x, 世界z) 或 None。**选择逻辑全在 `_stand_cell_of`**, 这里只转坐标。
        """
        c = self._stand_cell_of(tm, tx, tz, cx, cz, max_di, ortho_only)
        return tm.world_of(*c) if c is not None else None

    def _stand_cells(self, tm, tx: float, tz: float, cx: float, cz: float,
                     max_di: int = 2, avoid=None) -> list:
        """目标的**所有**能站相邻格, 按离厨师远近排序。

        为什么要"所有"而不是"最近那个": 实测拿食材时厨师停在离箱子 2.24 格处
        (交互半径只有 1.0), 一直按 pickup 抓不到 —— 那个方位够不着, 换个方位就行。

        avoid: 队友当前占的格子(双人时由共享世界给出)。**优先避开** ——
        两个人抢同一个站位会互相推挤, 表现为"两个人都卡住"。但只在还有别的选择时
        才避开: 全被占了就照常返回(宁可挤一下, 也不要站在那里什么都不做)。
        """
        if tm is None or not tm.ok:
            return []
        # ⚠ 同 `_stand_cell`: `st`/`km` 原先引用了不存在的名字, 必然 `NameError`。
        st = self.state()
        km = self.map(st) if st else None
        i, j = tm.cell_of(tx, tz)
        reach = tm.reachable_from(cx, cz, at_y=self.chef_y(st),
                                  extra_edges=self._travel_edges(km, tm))
        avoid = avoid or set()
        wind = self._wind_cells()          # 一次算好(同 `_stand_cell_of`)
        out, blocked = [], []
        for dj in range(-max_di, max_di + 1):
            for di in range(-max_di, max_di + 1):
                if di == 0 and dj == 0:
                    continue
                c = (i + di, j + dj)
                if not tm.walkable(*c) or c not in reach:
                    continue
                wx, wz = tm.world_of(*c)
                # ☠ 传送带格排在**所有非传送带格之后**(同 `_stand_cell`):
                #   站上去输入为 0 也会被推走, 交互会时有时无。惩罚足够大 ⇒
                #   只有当旁边**全是**带子时才会退而求其次选它。
                d0 = (wx - cx) ** 2 + (wz - cz) ** 2
                if tm.at(*c) == CH_TRAVELATOR:
                    d0 += BELT_STAND_PENALTY
                if c in wind:
                    d0 += WIND_STAND_PENALTY      # 同 `_stand_cell_of`: 风区也一样会把人推走
                row = (d0, wx, wz)
                (blocked if c in avoid else out).append(row)
        out.sort()
        blocked.sort()
        return [(wx, wz) for _, wx, wz in (out or blocked)]

    def _station_at(self, km: KitchenMap, x: float, z: float, tol: float = 0.25):
        """找坐标落在 (x,z) 上的台面。

        用途: 从**箱子**取料时, 计划给的 (tx,tz) 就是货源的坐标, 但食材并不在
        任何台面上("on" 是空的), 所以 _find_item_station 返回 None。
        这时必须**从坐标反查出那个箱子**, 才能拿到它的名字去核对
        "游戏说抓取=谁"。否则 want 为空 -> _aim_ok 会放行任意可交互物 ->
        站在错的箱子旁也判成功 -> 抓错食材 -> 放回去 -> 死循环
        (用户实测: "订单是寿司, 你去交互虾的食材箱子")。
        """
        best, bd = None, None
        for s in km.stations.values():
            d = (s.x - x) ** 2 + (s.z - z) ** 2
            if d <= tol * tol and (bd is None or d < bd):
                bd, best = d, s
        return best

    def _name_is(self, got: str, want: str) -> bool:
        """判断游戏报的物体名 got 是不是我们要的 want（**用于台面/箱子**）。

        ⚠ 这里不能用 _norm 直接比 —— 实测踩的大坑:
          _norm 会剥掉结尾的 " (N)" 实例编号。这对**物品**是对的
          (计划里写 "SushiFish", 场景实例叫 "SushiFish (2)"), 但对**箱子**是灾难:
              "DispenserCrate 3 (7)" 和 "DispenserCrate 3 (8)"
              归一化后都变成 "dispensercrate3" —— **唯一的区分信息被抹掉了**。
          后果: 站到随便哪个箱子旁边都判"就是它" -> 抓 -> 拿错食材 -> 放回去 -> 死循环
          (用户实测: "拿了就放下拿了就放下")。

        规则: 先精确比; 只有**其中一方没有编号后缀**时才允许归一化比。
              两边都带编号 -> 编号就是身份, 必须精确相等。
        """
        if not got or not want:
            return False
        if got == want:
            return True
        import re as _re
        suf = _re.compile(r"\(\d+\)\s*$")
        if suf.search(got) and suf.search(want):
            return False
        g, w = self._norm(got), self._norm(want)
        return bool(g) and (g == w or w in g or g in w)

    def _fresh_station_name(self, spot) -> str:
        """按 **id** 到**新鲜**的 km 里取这个台面的当前名字(带实例号)。

        ⚠ 不能直接用 `spot.name`: `self.assemble_spot` 可能是几百毫秒前的快照,
          而实例号在物体被销毁/重建后会变 —— 拿旧名字去做**精确比**会永远对不上,
          比"宽松比"更糟(那会把每一步都卡死)。取不到就退回旧名字。
        """
        try:
            st = self.state()
            km = self.map(st) if st else None
            if km is not None:
                now = km.stations.get(getattr(spot, "id", ""))
                if now is not None and getattr(now, "name", ""):
                    return now.name
        except Exception:                                         # noqa: BLE001
            pass
        return getattr(spot, "name", "") or ""

    def _align_for_place(self, spot, tries: int = 8) -> bool:
        """朝 `spot` 挪到**游戏说"放置目标就是它"**为止。返回是否对齐。

        ☠ 为什么必须确认(实测 `s_wonderland_1_5`, 整局报废):
          导航只保证"站到了旁边", `face` 只保证"面朝那边" —— 而**两个台子挨得近时**,
          游戏仍会把 `m_iHandlePlacement` 判成**旁边另一个台子**
          (`placement target='workstation_mixer_01 (2)'` 而期望 `countertop_01 (2)`)。
          原来的代码**读到了 `placeh` 却只打了行日志就照样按下去**,
          连试 3 次全一样 → "同一步连续失败 3 次" → 停机。
          ⇒ 判据用**游戏自己报的 `placeh`**, 和 `_aim_ok` 用 pick/use 同一个道理:
            **别自己编阈值**(交互真实判据是"到碰撞体表面 < 1.0 且朝向前 180°",
             拿格心距离比根本没有可比性)。
        """
        from bridge.keyboard_input import key_down, key_up
        want = self._fresh_station_name(spot)
        if not want:
            return True                      # 不知道期望名字时只能放行(老 dll)
        for k in range(tries):
            st = self.state(force=True)
            if not st or not st.get("inRound"):
                return False
            ph = (self.chef(st) or {}).get("placeh") or ""
            # ☠☠ **这里必须精确比(连实例号一起), 不能用 `_name_is`** —— 实测 `s_sushi_1_3`:
            #   `placeh='countertop_01_standard_wood'`(裸名) 而我们期望 `'(2)'`,
            #   而 `_name_is` 的规则是"**只有一方带编号时**退化成归一化比" —— 于是判"对上了",
            #   按下去 → 游戏 `placeCanHandle=False`(那个台面不是有盘子的那个) →
            #   材料放不进去 → 重试 3 次全一样 → **整单放弃**。
            #   那条宽松规则是为"计划里写裸名"留的; 而**放置目标两侧的名字都来自游戏自己**
            #   (`go.name` 对 `go.name`) ⇒ "一方没编号"只可能是**两个不同的物体**
            #   (同类型台面里第一个叫裸名、第二个叫 `(2)`) ⇒ 编号就是身份。
            if ph and ph == want:
                return True
            cx, cz, _ = self.pos(st)
            if cx is None:
                return False
            dx, dz = spot.x - cx, spot.z - cz
            d = (dx * dx + dz * dz) ** 0.5
            if d < 0.25:
                # 已经贴到台子边上了还判错 → 只能转身换个朝向试试
                self.face(spot.x, spot.z)
                time.sleep(0.15)
                continue
            self.face(spot.x, spot.z)
            dd = dir_for_step(dx, dz, deadzone=0.05)
            if dd:
                key = self._key({"left": "A", "right": "D",
                                 "up": "W", "down": "S"}[dd])
                key_down(key)
                time.sleep(0.15)
                key_up(key)
            time.sleep(0.12)
        st = self.state(force=True)
        self.log("[步骤] ⚠ 挪了 %d 次, 游戏仍说放置目标是 %r(期望 %r) —— **不再按下去**"
                 % (tries, (self.chef(st) or {}).get("placeh") or "", want))
        return False

    def _aim_ok(self, st: dict, want: str) -> bool:
        """**用游戏自己的判定**确认"现在按交互键能作用到目标 want"。

        为什么不能靠比距离(实测教训):
          第一单拿到食材时厨师离目标 1.03 格; 第二单离 1.33 格时游戏说
          "抓取=(空) 工位=(空)" —— 什么都不在范围内。
          我原来用"距离 <= 1.5 就算到位"的阈值是**我自己编的**, 和游戏不一致。
          而交互真实判据是 InteractWithItemHelper.IsColliderInArc
          (InteractWithItemHelper.cs:153-163): 到**碰撞体表面**的距离 < 1.0
          且朝向前 180°。台面有体积, 拿"格子中心距离"比根本没有可比性。
          所以直接比对游戏报的 pick/use 名字 —— 用 _name_is(带编号时精确比)。
        """
        pick, use = self.interaction_targets(st)
        if not want:
            return bool(pick or use)
        return self._name_is(pick, want) or self._name_is(use, want)

    def _land_near(self, km, tm, tx: float, tz: float, cands, want: str,
                   near: float, reach) -> bool:
        """**小物件的精确落位** —— 落到"朝物品方向、距物品 ~`near` 格"那个点上。

        要它的理由见 `SMALL_STAND` 那段注释: 小碰撞体的东西站**格心**(距 1.2)够不着。
        做法: 拿**试站位那同一批候选格** `cands`, 沿"候选格心 → 物品"的连线往物品方向挪,
        在 `near` 附近取几个半径落点, 走过去、`face`、然后**问游戏**(`_aim_ok`)。

        ⚠ **判据是游戏, 不是这里算的几何**(规则 2): 落位只负责把**起点**挪近,
          "够不够得着"永远由 `_aim_ok` 说了算。
        ⚠ **`navigate_smart` 的返回值不看**: 没走到 `tight` 不等于"这一步没用" ——
          厨师已经朝那儿挪过去了, 位置对不对由**游戏**裁。这就是"停手再问游戏"
          那条老规矩(`_approach` 里"先停手再判死"那段)。
        ⚠ 落点必须是**可走且可达**的格, 否则 `navigate_smart` 会白跑 4 轮规划。
          半径往两边各试一个, 因为两种小物件的"能站的那一侧"是**反的**:
            · 地上的料 —— 往**近**挪(0.5)落在**物品自己那格**, 那格本来就能站
              (`_ground_ok` 保证过), 最贴近;
            · 传送带上的料 —— 带子那格**不可走**, 往近挪会挪进带子里, 得用**远**的那个(0.9)。
          `walkable + c in reach` 两道闸把不合适的直接筛掉, 不用分情况写。
        """
        if near <= 0 or not cands:
            return False
        if tm is None or not getattr(tm, "ok", False):
            return False
        landed = 0
        for (sx, sz) in cands[:2]:
            dx, dz = sx - tx, sz - tz
            d = (dx * dx + dz * dz) ** 0.5
            if d <= near + 0.05:
                continue                # 这个候选格本来就在落位圈里, 没可挪的余地
            ux, uz = dx / d, dz / d
            for r in (near, near - 0.2, near + 0.2):
                if r < 0.3 or r >= d:
                    continue
                px, pz = tx + ux * r, tz + uz * r
                c = tm.cell_of(px, pz)
                if not tm.inside(*c) or not tm.walkable(*c) or c not in reach:
                    continue            # 落点踩在障碍/带子格、或过不去 —— 换个半径
                self.navigate_smart(km, px, pz, tight=0.25)
                self.face(tx, tz)
                st2 = self.state(force=True)
                ax, az, _ = self.pos(st2) if st2 else (None, None, "")
                if ax is None:
                    continue
                landed += 1
                df = ((tx - ax) ** 2 + (tz - az) ** 2) ** 0.5
                if self._aim_ok(st2, want):
                    self.log("[接近] 小物件落位 (%.1f,%.1f) 距 %.2f 格"
                             "(候选格心在 %.2f 格), 游戏说可作用 ✓"
                             % (ax, az, df, d))
                    return True
                pick, _use = self.interaction_targets(st2)
                self.log("[接近] 小物件落位 (%.1f,%.1f) 距 %.2f 格, 游戏说: 抓取=%r ✗"
                         % (ax, az, df, pick))
                if landed >= 4:
                    return False        # 试够了, 交回老路(它后面还有"就地微调"兜底)
        return False

    def _approach(self, km: KitchenMap, tx: float, tz: float, attempt: int = 0,
                  tight: float = 0.8, want: str = "", near: float = 0.0) -> bool:
        """接近一个台子并**转身面向它**。

        先站到"能站的相邻格", 再转身 —— 而不是朝台面本身推(那是障碍格)。
        attempt>0(上一次拿错了)时换个方位站: 相邻台子只隔 1.2 格, 游戏靠朝向决定
        交互哪一个, 换个方向最后一步的朝向就不同。

        `near>0` = 这一步取的是**小物件**(地上的料 / 带子上的料): 先走 `_land_near`
        那条**贴近落位**, 落不成再原样走下面的老路(试格心 → 就地微调)。
        默认 0 ⇒ 老路一步不动 —— 十二个老调用点全都不传它。
        """
        st = self.state(force=True)
        cx, cz, _ = self.pos(st) if st else (None, None, "")
        if cx is not None:
            tm = self.terrain()
            gx, gz = tx, tz
            if attempt > 0 and tm is not None and tm.ok:
                import math
                ang = attempt * 2.39996          # 黄金角, 每次方位都不同
                px, pz = tx + 1.3 * math.cos(ang), tz + 1.3 * math.sin(ang)
                if tm.walkable(*tm.cell_of(px, pz)):
                    gx, gz = px, pz
            # 目标的**所有**相邻能站格, 按离厨师远近排序, 逐个试。
            # 只试"最近那个"是不够的 —— 实测 s_sushi_1_1 拿食材时厨师停在
            # 离箱子 2.24 格的地方(交互半径只有 1.0), 一直在按 pickup 却什么也抓不到。
            # 那个方向的相邻格多半被挡住/够不着, 换个方位站就好了。
            avoid = set()
            if self.world is not None:
                avoid = self.world.occupied_by_others(self.cid, tm)
            cands = self._stand_cells(tm, gx, gz, cx, cz, avoid=avoid)
            # ☠ **小物件先试"贴近落位"** —— 理由见 `SMALL_STAND` 那段注释。
            #   放在**试格心之前**: 台面那条路(站格心就贴得到表面)压根不会传 `near` 进来,
            #   而小物件站格心是**注定够不着**的(实测 1.02 格就抓不到) —— 先走过去试一遍纯白跑。
            #   落不成(贴近点全不可站 / 游戏就是不认) ⇒ **原样往下走老路**,
            #   所以这条新路是**加法**: 关掉它(`NEKO_SMALL_STAND=0`)就等于没改过。
            if near > 0 and cands and tm is not None and tm.ok:
                _reach = tm.distances_from(
                    cx, cz, at_y=self.chef_y(st),
                    extra_edges=self._travel_edges(km, tm))
                if self._land_near(km, tm, tx, tz, cands, want, near, _reach):
                    return True
            if not cands:
                self.log("[接近] ⚠ 找不到能站的相邻格(旁边全被占, 或不连通) —— 只能直接朝它走")
            else:
                # **只试最合适的少数几个**。
                # 原来把 ±2 格里十几个候选挨个走过去试, 厨师满厨房乱窜(用户实测:
                # "开局人物就会乱跑一段"), 而且大部分根本到不了(卡住/超时)。
                # 现在: 就近取 3 个; 都不行就**就地微调**, 不再跨半个厨房换位置。
                trial = cands[:3]
                self.log("[接近] 目标 (%.1f,%.1f), 候选站位 %d 个(只试最近 %d 个), 现在距目标 %.2f 格"
                         % (tx, tz, len(cands), len(trial),
                            ((tx - cx) ** 2 + (tz - cz) ** 2) ** 0.5))
                for (sx, sz) in trial:
                    self.navigate_smart(km, sx, sz, tight=min(tight, 0.5))
                    self.face(tx, tz)
                    st2 = self.state(force=True)
                    px, pz, _ = self.pos(st2) if st2 else (None, None, "")
                    if px is None:
                        continue
                    df = ((tx - px) ** 2 + (tz - pz) ** 2) ** 0.5
                    pick, use = self.interaction_targets(st2)
                    if (not want) or self._aim_ok(st2, want):
                        self.log("[接近] (%.1f,%.1f) 距 %.2f 格, 游戏说可作用: 抓取=%r ✓"
                                 % (px, pz, df, pick))
                        return True
                    self.log("[接近] (%.1f,%.1f) 距 %.2f 格, 游戏说: 抓取=%r ✗ 不是它"
                             % (px, pz, df, pick))

                # ---- 就地微调: 不换站位, 只朝目标小步挪 + 转身, 每步问一次游戏 ----
                # 这是"范围交互"的正解: 不必走到某个精确点, 只要进入范围且朝向对。
                if want:
                    self.log("[接近] 就地微调, 朝目标靠近直到游戏说能作用")
                    for k in range(8):
                        st3 = self.state(force=True)
                        px, pz, _ = self.pos(st3) if st3 else (None, None, "")
                        if px is None:
                            break
                        if self._aim_ok(st3, want):
                            self.log("[接近] 微调 %d 次后到位 (%.1f,%.1f) ✓" % (k, px, pz))
                            return True
                        dx, dz = tx - px, tz - pz
                        d = (dx * dx + dz * dz) ** 0.5
                        if d < 0.35:
                            self.face(tx, tz)
                            continue
                        # 只走一小步: 位移 = 速度 × 时长, 用运动学算, 最多 0.25 秒
                        hold = min(0.25, max(0.08, (d - 0.9) / self.speed))
                        key = self._key("D" if abs(dx) >= abs(dz) and dx > 0 else
                                        "A" if abs(dx) >= abs(dz) else
                                        "W" if dz > 0 else "S")
                        from bridge.keyboard_input import key_down, key_up
                        key_down(key)
                        time.sleep(hold)
                        key_up(key)
                        time.sleep(0.08)
                    # ☠ **先停手再判死**(文档 §5.3 的"位置校验点")。位置是**闭环控制器的输入**,
                    #   而外力每帧都在改它 —— 风/传送带每帧 `MovePosition`、击退是 0.2 秒的冲量、
                    #   队友推挤、客户端松手还会被服务器吸附。上面那些 `state(force=True)`
                    #   读到的都是"**正在漂**"的一帧, 拿它判"够不着"会误杀。
                    #   实机(s_balloon_2_3)就是在这里刷 `✗ 微调后游戏仍说作用不到目标`,
                    #   而当时厨师正被风推着走。
                    ok_aim, off = self.checkpoint(tm, tx, tz, tol=2.0)
                    st4 = self.state(force=True)
                    if self._aim_ok(st4, want):
                        self.log(f"[接近] 停手重问游戏: 现在能作用了 ✓ (差 {off:.2f} 格)")
                        return True
                    pick, use = self.interaction_targets(st4)
                    self.log("[接近] ✗ 微调后游戏仍说作用不到目标"
                             "(抓取=%r 工位=%r, 停手重问仍不行, 差 %.2f 格)" % (pick, use, off))

        # 兜底: 地形不可用 / 找不到能站的格子 —— 退回老办法
        if attempt <= 0:
            ok = self.navigate_smart(km, tx, tz, tight=tight)
            if ok:
                self.face(tx, tz)
            # 兜底也要验收：走得到不等于抓得到。箱子坐标错/位置变的时候，走到
            # 旧坐标旁边其实是另一个箱子，这里不确认就返回 True，上层会盲目按下
            # 抓取键，把错的东西拿到手再放回去。
            if ok and want:
                st_now = self.state(force=True)
                ok = self._aim_ok(st_now, want)
            return ok
        import math
        ang = attempt * 2.39996
        px, pz = tx + 1.6 * math.cos(ang), tz + 1.6 * math.sin(ang)
        self.navigate_smart(km, px, pz, tight=0.6)
        ok = self.navigate_smart(km, tx, tz, tight=max(0.45, tight - 0.3))
        if ok:
            self.face(tx, tz)
        if ok and want:
            st_now = self.state(force=True)
            ok = self._aim_ok(st_now, want)
        return ok

    def _find_item_station(self, km: KitchenMap, target: str,
                           x: float = None, z: float = None,
                           exclude_ids=None) -> Station | None:
        """实时找一个"上面正放着 target"的台子, 取**离厨师最近**的那个。

        为什么必须实时: 这一关食材走传送带(ConveyorStation)会自己移动, know 表里的坐标
        一读就过时; 而 state.layout 每秒刷新, 台子的 on 字段是当前真实内容。
        为什么要最近: 同一种食材可能同时躺在传送带的两端(相隔 20 格), 当然取近的。
        """
        if not target:
            return None
        tn = self._norm(target)
        exact, loose = [], []
        for s in km.stations.values():
            if exclude_ids and s.id in exclude_ids:
                continue
            # ⚠ 要**下标**: 下面判"是不是容器"要拿 `tag_of(i)`(tag 是**按格位**给的)
            for i, o in enumerate(s.on or []):
                on = self._norm(o)
                if not on:
                    continue
                if on == tn:
                    exact.append(s)
                    break
                # 子串兜底**只给容器**: 订单说的容器是 "Plate", 场景里的实例却叫 "equipment_plate_01"。
                # ☠☠ **食材绝不许子串匹配** —— 加工阶段改的**正是名字**:
                #   `SushiPrawn` → `ChoppedSushiPrawn`, 而 "sushiprawn" 是它的子串 ⇒
                #   `fetch SushiPrawn`(要**生的**)会把板子上**切好的**认成货源 ⇒
                #   抓到手 → 校验"不是它" → 放回去 → 重试 3 次 → 放弃整单。
                #   (实测 2026-09-15 用户: "**如果板子上已经是切好的，脚本就会卡住**"。)
                if is_plate(o, s.tag_of(i)) or is_pot(o, s.tag_of(i)):
                    if tn in on or on in tn:
                        loose.append(s)
                        break
        best = exact or loose
        if not best:
            return None
        if x is None:
            return best[0]
        return min(best, key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2)

    def _reach_ok_pred(self, tm, reach):
        """造一个 `ok(station) -> bool` 谓词: **"正交邻四格里有可走且可达的"**。

        这是"够不够得着"的**唯一判据** —— 交互几何是 `|Δ| < 1.0` 且只认前 180°
        (`InteractWithItemHelper.cs:153-163`), 所以斜角(1.70 格)不算, 只有正交邻格。
        `op_fetch`(执行) 与 `_op_target_for_score`(评分) **必须用同一个谓词**:
        两边判据一旦漂开, 就会出现"评分判它够不着 ⇒ 永远轮不到执行, 而执行本来够得着"。

        `tm` 不可用 / 拿不到 `reach` 时返回 `None` —— 调用方按"不过滤"处理(旧行为)。
        """
        if tm is None or not getattr(tm, "ok", False) or reach is None:
            return None

        def _ok(s, _tm=tm, _r=reach):
            c = _tm.cell_of(s.x, s.z)
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (c[0] + di, c[1] + dj)
                if _tm.inside(*n) and _tm.walkable(*n) and n in _r:
                    return True
            return False

        return _ok

    def _ground_ok(self, it, tm, reach) -> bool:
        """那件**地上的料**够不够得着 —— 它自己那格能站, 或正交邻格能站且可达。

        (交互半径 1.0 ⇒ 站在旁边也能捡; 站在它自己那格最好, 因为料不挡路。)
        `tm`/`reach` 拿不到时**放行**(调用方没有地形信息, 别把候选全毙了)。
        """
        if tm is None or not getattr(tm, "ok", False) or reach is None:
            return True
        c = tm.cell_of(it.x, it.z)
        if tm.inside(*c) and tm.walkable(*c) and c in reach:
            return True
        pred = self._reach_ok_pred(tm, reach)
        return bool(pred(it)) if pred is not None else True

    def _unclaimed_items(self, km) -> list:
        """**没主的**料 —— 掉地上的/被丢出来的那些(不在任何台面上、也不在任何厨师手上)。

        ☠ 判据**只有一份**: 就是 `map_model.KitchenMap.unseen_items()`
          (2026-09-15 从这儿挪过去的 —— 原来这儿和 `mapview` 各写一份, 结果
           `mapview` 那份是**按名字**算的: 台面上有一块 `SushiFish`、地上另外躺着三条时,
           名字出现过 ⇒ 地上那三条被一起吞掉 ⇒ 报告说"看不见 0 件"(假的),
           而用户正是靠那个报告判断"脚本看不见地上的料")。
          挪过去之后两边同源, 谁也不会再各错各的。
        """
        return km.unseen_items()

    def _find_ground_item(self, km, target: str, x: float, z: float,
                          tm=None, reach=None):
        """**掉在地上/台面外**的那件料 —— 最近的同名一个, 包成"取货目标"; 没有就 None。

        数据源是插件按 tag **全场景**扫的 `km.items`(`SceneScanner.ScanItems`)。
        ☠ **引擎以前从来没读过它**: `_find_item_station` 只认"挂在台面上的",
          `find_source` 只认箱子 —— 于是**掉在地上的料在引擎眼里根本不存在**
          (用户原话: "不会拿地上的食物啊")。`km.unseen_items()` 已经把"在台面上/
          在手上"的排掉了, 剩下的正是掉地上的、被丢出来的、在移动平台上的。
        """
        tn = self._norm(target)
        if not tn:
            return None
        exact, loose = [], []
        for it in self._unclaimed_items(km):
            n = self._norm(getattr(it, "name", ""))
            if not n:
                continue
            if not self._ground_ok(it, tm, reach):
                continue
            if n == tn:
                exact.append(it)
            elif tn in n or n in tn:
                loose.append(it)
        best = exact or loose
        if not best:
            return None
        if x is None:
            it = best[0]
        else:
            it = min(best, key=lambda t: (t.x - x) ** 2 + (t.z - z) ** 2)
        return _GroundItem(it)

    def _fetch_source_live(self, km, st, op, x: float, z: float,
                           tm=None, reach=None) -> Station | None:
        """**实时**解析"这一步该去哪儿取 `op.target`" —— 三个来源里**最近的、够得着的**那个。

        三个来源(**规则只有这一份**, 评分和执行都走它):
          ① **台面上正放着**的(传送带上的东西会动, 计划坐标一读就过时)
          ② **掉在地上/台面外**的(`km.items` 里没主的那件 —— 引擎以前整类漏掉)
          ③ **够得着的箱子**(`_reach_ok_pred`: 正交邻格里有可走且可达的)

        用户原话: "**当前订单需要的料…只需要集齐需要的食材**" ⇒ **脚边就有的别跑去开箱子**,
        所以这里按**距离**挑, 而不是"先台面、再箱子"地分等级。

        ⚠ **评分层必须调这个, 不能只看计划里的 `op.at_x/at_z`** —— 计划坐标是
          `find_source` 按距离挑的(见那边的注释), 可能挑中隔着墙/对面厨房那个;
          而且它**看不见台面上/地上的现货**(实测被用户抓到: 地上躺着一块要用的料,
          脚本却径直去了箱子)。实测 `s_summer_1_4` 更狠: 计划给的两个货源**都够不着**
          ⇒ 评分把两行 `fetch` 全判死 ⇒ 8 个候选一个不剩 ⇒ 停机烧整局。
        """
        ok = self._reach_ok_pred(tm, reach)
        cands, fallback = [], []
        live = self._find_item_station(km, op.target, x, z)
        if live is not None:
            (cands if (ok is None or ok(live)) else fallback).append(live)
        ground = self._find_ground_item(km, op.target, x, z, tm=tm, reach=reach)
        if ground is not None:
            cands.append(ground)          # `_find_ground_item` 内部已经过了可达性
        crate = km.find_source(op.target, x, z, ok=ok)
        if crate is not None:
            cands.append(crate)
        if not cands:
            # 一个够得着的都没有 —— 把够不着的那个报回去(日志里要看得见"它在哪")
            if not fallback and ok is not None:
                fallback = [s for s in (live,) if s is not None] or \
                           [s for s in (km.find_source(op.target, x, z),) if s is not None]
            cands = fallback
        if not cands:
            return None
        if x is None:
            return cands[0]
        # ☠ **"最近"要按"走得到的步数", 不是直线距离**(用户 2026-09-15:
        #   "地上的食材和箱子里的、台面上的**需要判断距离, 去距离最近的拿取**")。
        #   直线 3 格但隔着一道墙 = 得绕 10 格, 而脚边那件可能只 2 步 —— 按直线比就会
        #   挑中那个"看起来近、其实绕远"的, 表现成"**脚边有料却跑去开箱子**"。
        #   `reach` 就是**从厨师出发的那张 BFS 步数表**(调用方已经算好了, 这里不再跑 BFS);
        #   站位格和评分层用同一个 `_stand_cell_of` —— 两处判据必须是同一条。
        # ⚠ 没有地形/步数表时**退回直线**: 那正是老行为, 不能因为拿不到数据就把候选全弄成 inf。
        def _walk(s):
            # ☠☠ **`reach` 可能是 set, 也可能是 dict —— 必须分开对待**。
            #   `TerrainMap.reachable_from()` 返回 **set**(到不到得了),
            #   `TerrainMap.distances_from()` 返回 **dict**(到那一格几步)。
            #   两者在"够不够得着"那个判据里**通用**(都是 `c in reach`), 所以历史上
            #   调用方各传各的、一直没出事 —— 直到这里要**排序**, 用了 `reach.get`
            #   ⇒ `'set' object has no attribute 'get'` ⇒ **每次 fetch 都抛异常**,
            #   重试 3 次 → 放弃 → 冷板凳 → 整局空转(2026-09-15 实机整整一局就这么废了)。
            #   ⇒ 拿到 set 就**退回直线距离**: 它只答得了"到不到得了", 答不了"多远"。
            if not isinstance(reach, dict) or tm is None or not getattr(tm, "ok", False):
                return (s.x - x) ** 2 + (s.z - z) ** 2
            cell = self._stand_cell_of(tm, s.x, s.z, x, z, ortho_only=True, reach=reach)
            if cell is None:
                return float("inf")
            return reach.get(cell, float("inf"))
        return min(cands, key=_walk)

    def _wait_for_item(self, target: str, x: float, z: float,
                       timeout: float = 20.0) -> Station | None:
        """等目标东西出现。传送带会把食材送过来, 来得晚了就等一会。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if not self.round_active():
                return None
            st = self.state()
            km = self.map(st) if st else None
            if km is None:
                time.sleep(0.4)
                continue
            s = self._find_item_station(km, target, x, z)
            if s is not None:
                return s
            time.sleep(0.4)
        return None

    def op_fetch(self, km, x, z, op: Op, st: dict, attempt: int = 0) -> bool:
        """去货源拿东西(传送带/台面/箱子), 并校验拿到的是不是目标。"""
        # 手上还有别的东西: 先送去组装台面腾出手(一次只能拿一个)
        _, _, held = self.pos(st)
        if held:
            # ☠ **手上就是这一步要取的那个东西 ⇒ 已经拿到了, 别再跑一趟**
            #   (实机 2026-09-14 `DLC13_MoonPie_Watermelon`)。这一条必须排在"腾出手"**前面**:
            #   那一轮 `fetch DLC13_Egg` 是**成功的**(拿到了蛋), 但**这一单剩下的步骤全都
            #   不可达** ⇒ `_rank_candidates` 没候选 ⇒ 整轮作废 → `run()` 重新规划 →
            #   `pending` 重置 ⇒ **又选中同一个 `fetch DLC13_Egg`**。
            #   于是走到下面那段"腾出手": 把**刚拿到的蛋**当成"别的东西"送去摆盘位 →
            #   生蛋放进盘子**被游戏拒收**(`✗ DLC13_Egg 没进盘([] 没变)`) → 连试 3 次 →
            #   规则 5 停机, 整局报废。**错的是"再拿一次"这个动作, 不是那一局。**
            #
            # ⚠ 判据用**归一化后精确相等**, 不用 `_held_is`(它是前缀匹配, 见 `_norm` 的注释):
            #   前缀匹配会把 `SushiPrawnCooked` 当成 `SushiPrawn` 的前置已满足 ——
            #   而 `fetch` 的语义是"去拿**生料**", 拿着的却是熟的 ⇒ 后面 `cook` 一定炸。
            if self._norm(held) and self._norm(held) == self._norm(op.target):
                self.log(f"[步骤] 手上已经是 {held!r} —— 这一步不用再跑一趟")
                return True
            # ☠ **腾手: 加工好的进盘, 没加工完的丢脚下**(用户 2026-09-15 定的规矩):
            #   > "**没有处理过的食材无法放在盘子上, 直接丢地上就好了**"
            #   实测(`s_sushi_1_3`): 生 `SushiRice` 往"有盘子的组装台面"上放, 游戏判
            #   `CanHandlePlacement=false` → `OnFailedToPlace` → **东西留在手上**;
            #   而那次我们把"盘子内容变了"(其实是上一步的 seaweed)误判成成功 ⇒
            #   后面 `fetch` 发现"腾不出手"→ 失败 → 重试 3 次 → 重新规划 → **同一个循环**,
            #   一整局就这么烧掉(直调统计还是 hits=N fails=0, 因为直调**确实命中了**,
            #   是游戏自己拒绝了 —— 判据在 `placeCanHandle`)。
            #   ⇒ 先按老路试一次(加工好的料本来就该进盘, 那条路是通的),
            #     **游戏不收就地丢下**: `direct("take")` → `ReceiveTakeEvent(target=null)`
            #     → 游戏的 `carrier.TakeItem()` = **丢在脚下**。那份料留在原地, 之后要它再回来拿。
            self.log(f"[步骤] 手上还有 {held}, 先腾出手")
            # ☠ **"还没加工完"的料**别往盘子上试 —— 游戏一定拒(`CanHandlePlacement=false`),
            #   白走一趟还把 `__place__ @ counterN` 划掉 **25 秒**(实测 2026-09-15
            #   `s_sushi_1_2`: `把 SushiRice 放到摆盘位 counter2` → `还在手上` →
            #   `这一支先划掉 25 秒` → 最后还是 `直接丢脚下`)。既然必输就直接丢。
            #   ⚠ 判据是 `_needs_work`(**能不能切**)。**能煮的**它认不出来(同名、`next` 空),
            #     那种仍会白试一趟 —— 根治要让插件报"**手上这件还能不能加工**"
            #     (和 `heldhas` 同一个位置加一个 `heldwork`), 那一层**还没做**。
            if self._needs_work(held):
                self.log(f"[步骤] {held} 还没加工完 ⇒ 不往盘子上试(游戏必拒), "
                         f"按用户要求**直接丢脚下**")
                if not self._drop_held(st, held):
                    return False
            elif not self.op_assemble(km, x, z, Op("assemble", held), st):
                self.log(f"[步骤] {held} 进不了盘(多半是**没加工完**的料, 进不了盘子) "
                         f"—— 按用户要求**直接丢脚下**")
                if not self._drop_held(st, held):
                    return False
            _, _, held = self.pos(self.state(force=True) or {})
            # 组装台面那边"并盘"之后手上会剩一个空盘 —— 拿着它去货源台面按键,
            # 会把盘子放到货源上(实测: 盘子被丢到食材箱上, 然后一直在两个台面之间来回).
            if held and self._is_plate(held):
                self._put_down_plate(km, x, z)
                _, _, held = self.pos(self.state() or {})
            if held:
                self.log(f"[步骤] ⚠ 手上还有 {held}, 腾不出手来取料")
                return False

        # 1) 实时找"正放着目标"的台子(传送带上的食材会移动, 取离自己最近的)
        cx, cz, _ = self.pos(self.state() or {})
        live = self._find_item_station(km, op.target, cx or x, cz or z)
        live_src = None
        if live is not None:
            tx, tz = live.x, live.z
            self.log(f"[步骤] 取 {op.target} @{live.id}({tx:.1f},{tz:.1f}) 实时")
        else:
            # 1.5) 实时找"能出这个食材的箱子"。know 表只读一次、坐标可能过时,
            #   而 state.layout 每秒刷新 —— 箱子换位/换内容后必须信实时的, 否则
            #   会走到旧坐标抓到旁边别的箱子(日志里"拿到 SushiFish 不是 Seaweed")。
            # ☠ **挑箱子必须先看"够不够得着"**(实测 s_wonderland_1_5)。
            #   那关两个厨房被一道墙切开(连通块 28 格 vs 108 格, **交集 0**),
            #   而 `find_source` 只按距离挑 ⇒ 挑中对面厨房的箱子 ⇒
            #   `_stand_cell` 找不到可达的相邻格(唯一那个在对面) ⇒ 退到 2.4 格 >
            #   交互半径 1.0 ⇒ 够不着 ⇒ **整步失败**。
            #   判据和交互几何对齐: **正交相邻格里有"可走且可达"的**才算够得着
            #   (斜角 1.70 格 > 半径 1.0, 不算)。
            _tm0 = self.terrain()
            _r0 = None
            if _tm0 is not None and _tm0.ok and cx is not None:
                # ⚠ **这里传 `distances_from`(dict), 不是 `reachable_from`(set)** ——
                #   `_fetch_source_live` 现在要靠它**按步数挑最近**(用户要求"去距离最近的拿取"),
                #   而 set 只答得了"到不到得了"。两者都是**同一次 BFS**、代价一样, 所以
                #   这里换成更信息量的那个; `c in reach` 那几处判据对 dict 同样成立(查键) ✓。
                _r0 = _tm0.distances_from(
                    cx, cz, at_y=self.chef_y(st),
                    extra_edges=self._travel_edges(km, _tm0))
            # ☠ **规则只有 `_fetch_source_live` 一份**(评分层调的是同一个):
            #   "台面上的现货 / 地上的料 / 够得着的箱子 —— **谁近去谁**"。
            #   以前这段自己抄了一遍"先箱子后地上比距离", 迟早会和评分层漂开。
            live_src = self._fetch_source_live(km, st, op, cx or x, cz or z,
                                               tm=_tm0, reach=_r0)
            if live_src is None and _r0 is not None:
                # 区分"这关没有这个箱子"和"有, 但都够不着" —— 后者是**地图/订单**的问题,
                # 不是导航的问题, 日志必须说清楚(否则下一个人会去查寻路)。
                _all = km.find_source(op.target, cx or x, cz or z)
                if _all is not None:
                    self.log(f"[步骤] ⚠ 有出 {op.target} 的箱子({_all.id} "
                             f"@{_all.x:.1f},{_all.z:.1f}), 但**这只厨师走不到它旁边** —— "
                             f"多半是两个厨房(要传接球), 不是寻路问题")
            if live_src is not None:
                tx, tz = live_src.x, live_src.z
                _id = str(live_src.id)
                if _id.startswith("ground"):
                    _what = "地上"
                elif any(_id == c.id for c in km.of("crate")):
                    _what = "实时箱子"
                else:
                    _what = "台面上"
                self.log(f"[步骤] 取 {op.target} @{_what} {_id}({tx:.1f},{tz:.1f})")
            else:
                # 2) 计划里已经知道货源坐标(know 表给的箱子/静置台面) → 直接去。
                #    **这一步必须在"等传送带"之前** —— 实测 s_sushi_1_3 这关
                #    `台面传送带0`(压根没有传送带), 却先傻等 20 秒, 三步重试白烧掉 60 秒,
                #    一局只有 150 秒。箱子就在那儿, 直接去拿就行。
                has_belt = bool(km.of("conveyor"))   # 语义编码在 id 前缀里, 用 of() 查, Station 上没有 sem 字段
                if op.at_x or op.at_z:
                    tx, tz = op.at_x, op.at_z
                    self.log(f"[步骤] 取 {op.target} @已知货源({tx:.1f},{tz:.1f})")
                elif has_belt:
                    # 3) 这关真有传送带, 才值得等它把食材送过来(等短一点, 别烧掉整局)
                    self.log(f"[步骤] 台面上暂时没有 {op.target}, 等传送带送来(最多 8 秒)...")
                    live = self._wait_for_item(op.target, x, z, timeout=8.0)
                    if live is not None:
                        tx, tz = live.x, live.z
                        self.log(f"[步骤] {op.target} 到了 @{live.id}({tx:.1f},{tz:.1f})")
                    else:
                        tx, tz = x, z
                        self.log(f"[步骤] 等不到 {op.target} 送过来")
                        return False
                else:
                    # 4) 没有传送带又没有已知货源 → 找不到就放弃
                    self.log(f"[步骤] 找不到 {op.target} 的货源(这关没有传送带, 也没有已知箱子)")
                    return False

        # **食材在台面传送带上**: 会自己移动, 追不上 —— 站旁边等它漂过来。
        # (实测三次重试的目标每次只差一格: 26.4 → 25.2 → 24.0, 就是它在跑)
        if live is not None and live.id.startswith("conveyor"):
            self.log(f"[步骤] {op.target} 在传送带上 @{live.id}({tx:.1f},{tz:.1f}), 站旁边等它过来")
            if self._grab_from_belt(km, op, x, z):
                _g = self.pos(self.state(force=True) or {})[2]
                self.log(f"[步骤] ✓ fetch 走**传送带**这条路成功, 手上={_g!r}")
                return True
            return False

        # 先粗到再收紧: 相邻台子太近, 站远了会拿错。
        # want=**货源台面自己的名字** —— 让游戏确认"现在按抓取键能作用到它"。
        # 注意从箱子取料时 live 是 None(食材不在任何台面上), 必须从坐标反查那个箱子;
        # 否则 want 为空 -> _aim_ok 放行任意可交互物 -> 站在错箱子旁也判成功。
        src_station = live if live is not None else live_src
        if src_station is None:
            src_station = self._station_at(km, tx, tz)
        want_name = src_station.name if src_station is not None else ""
        if not want_name:
            self.log("[步骤] ⚠ 查不到货源台面名(坐标 %.1f,%.1f) —— 只能抓到什么算什么"
                     % (tx, tz))
        # ☠ **地上的料走"贴近落位"**(`near`), 台面不走 —— 见 `SMALL_STAND` 那段注释:
        #   台面碰撞体大, 站格心就贴得到表面, 那条路是通的, **别去动它**;
        #   地上的料是小东西, 站格心(1.2)刚好在交互半径外 ⇒ 游戏报 `抓取='(空)'`。
        # ⚠ 判据用 `isinstance`(是不是 `_GroundItem` 适配器), **不是名字前缀** ——
        #   `ground_`/`conveyor` 前缀是字符串约定, 而这里是"这类目标的几何性质"。
        _near = SMALL_STAND if isinstance(src_station, _GroundItem) else 0.0
        if not self._approach(km, tx, tz, attempt, want=want_name, near=_near):
            return False
        if not self.interact("pickup", verify_hold_change=True):
            return False
        st2 = self.state()
        _, _, got = self.pos(st2)
        if not self._held_is(got, op.target):
            self.log(f"[步骤] 拿到的是 {got!r}, 不是 {op.target!r} → 放回去")
            self.interact("pickup", verify_hold_change=False)
            return False
        # ⚠ **报成功时必须说清手上到底是什么** —— 实机 2026-09-15 出现过
        #   `✓ fetch SushiFish` 而下一步 `手持''` 的**假成功**, 整条链就此断掉。
        #   `op_fetch` 有三个 `return True`(手上已经是它 / 传送带 / 这里), 不打这一行
        #   就分不清是**哪条路**报的成功 —— 和当初钉 `op_chop` 假 ✓ 是同一招。
        self.log(f"[步骤] ✓ fetch {op.target} 成功, 手上={got!r}")
        return True

    # ---------------- 传送带拦截 ----------------
    #: 传送带上物品的格子速度(格/秒)。插件 dyn 里每条都带 speed, 这里只是兜底值。
    BELT_SPEED = 0.5
    #: 厨师速度(u/s)与冲刺时的估算速度。依据 PlayerControls.Movement.RunSpeed = 4f,
    #: Dash 是 1 秒的 S 曲线加速(8 → 4 u/s, 全程约 6 单位)。
    CHEF_SPEED = 4.0
    CHEF_SPEED_DASH = 6.0

    def _belt_dirs(self):
        """台面传送带的"每格往哪传"表: {(i,j): (stepx, stepz)}。按场景缓存一次。

        这是"提前量拦截"必需的数据 —— 不知道它往哪走, 就只能傻追。
        来自插件的 dyn 命令(InteractiveScan 已经把 ConveyorStation 的
        m_conveyanceDirectionXZ + transform.right 算成了轴向步长)。
        """
        if self._belt_dirs_cache is not None:
            return self._belt_dirs_cache
        m = {}
        speeds = {}
        dyn = {}
        try:
            dyn = self.bridge.get_dyn()
            tm = self.terrain()
            for c in dyn.get("conveyors") or []:
                if c.get("type") == "Travelator":
                    continue                    # 推人的地面传送带是另一套, 不管
                try:
                    x, z = float(c.get("x") or 0), float(c.get("z") or 0)
                    sx = int(round(float(c.get("stepx") or 0)))
                    sz = int(round(float(c.get("stepz") or 0)))
                    sp = float(c.get("speed") or 0)
                except (TypeError, ValueError):
                    continue
                if tm is None or not tm.ok:
                    break
                cell = tm.cell_of(x, z)
                if sx or sz:
                    m[cell] = (sx, sz)
                if sp > 0:
                    speeds[cell] = sp
        except Exception as e:
            self.log(f"[传送带] 取方向失败: {e}")
        self._belt_dirs_cache = m
        self._belt_speeds_cache = speeds
        if not m:
            self.log(f"[传送带] dyn 没给出可用的台面传送带方向"
                     f"(收到 {len(dyn.get('conveyors') or [])} 条传送带记录, 可用方向格 {len(m)} 个)")
        return m

    def _plan_intercept(self, tm, live, cx: float, cz: float):
        """算出"该提前去哪个格子等它" —— 真正的拦截, 而不是追。

        为什么这样对(用户指出的): 厨师 4 u/s(冲刺 ~6), 食材只有 0.5~0.6 格/秒,
        **厨师快 6~13 倍** —— 只要去它**下游**等着, 一定能截住。
        原来的做法是朝食材"当前位置"走, 每步都有开销, 到了它又走了一格, 看起来像"追不上"。

        做法: 沿传送带往下游扫 1..9 格, 找第一个"厨师赶得到、且食材还没过去"的点。

        返回 (站位世界坐标, 拦截格世界坐标, 预计等待秒数) 或 None。
        """
        dirs = self._belt_dirs()
        cell = tm.cell_of(live.x, live.z)
        step = dirs.get(cell)
        if step is None:
            return None
        ci, cj = cell
        belt_speed = max(0.1, self._belt_speeds_cache.get(cell, self.BELT_SPEED))

        for k in range(1, 10):
            ti, tj = ci + step[0] * k, cj + step[1] * k
            if not tm.inside(ti, tj):
                break
            wx, wz = tm.world_of(ti, tj)
            # 拦截点必须是**传送带路径上**的格子(还在传), 否则等不到
            if tm.cell_of(wx, wz) not in dirs and not tm.walkable(ti, tj):
                # 该格可能已经出了传送带(到垃圾桶了) —— 那就在它之前截
                break
            # 拦截点必须**紧邻**拦截格(max_di=1) —— 站位离拦截点太远就够不着了
            # (交互半径 1.0; 站到 ±2 格那样 2.4 格开外, 食材经过时抓不到)
            spot = self._stand_cell(tm, wx, wz, cx, cz, max_di=1, ortho_only=True)
            if spot is None:
                continue
            dist = ((spot[0] - cx) ** 2 + (spot[1] - cz) ** 2) ** 0.5
            t_chef = dist / self.CHEF_SPEED_DASH        # 用冲刺速度估, 偏乐观一点
            # 依据(`ConveyorStation.m_conveySpeed` 的单位是**格/秒**, 不是世界单位/秒):
            #   · `ServerConveyorStation.cs:192` `m_arriveTime = now + 1f / GetConveySpeed()`
            #     ⇒ 传一格耗时 `1/speed` 秒
            #   · 插件侧也是按这个单位报的: `InteractiveScan.cs:231-233,276`
            #     (`cellsPerSecond = true`, 取的字段就是 `m_conveySpeed`)
            # ⇒ 走 k 格 = `k / speed` 秒。**不能再乘格长** —— 那是把它当"世界单位/秒"了,
            #   会把 t_item 放大 1.2 倍(cellx), 把其实赶不上的格子判成"赶得上",
            #   拦截点选到厨师到不了的地方。
            t_item = k / belt_speed
            if t_chef <= t_item + 1.2:                  # 留 1.2 秒余量
                return (spot, (wx, wz), max(0.0, t_item - t_chef))
        return None

    def _grab_from_belt(self, km: KitchenMap, op: Op, x: float, z: float,
                        budget: float = 14.0) -> bool:
        """提前去下游拦截传送带上的食材, 然后等它到手边再抓。

        用户指出的两条都用上了:
          · 远距离先**冲刺**(Dash 键, 1 秒 S 曲线加速)缩短赶路时间
          · 去**下一个能拿到的地方**等, 而不是追它当前位置
        兜底: 算不出拦截点(没有方向数据)时, 退化成"站到它旁边等它漂过来"。
        """
        tm = self.terrain()
        t_end = time.time() + budget
        grabs = 0
        #: **拿到的是别的东西**的次数 —— 和 `grabs` 分开计(见下面那段注释)。
        wrong = 0
        dashed = False
        while time.time() < t_end:
            if not self.round_active():
                return False
            st = self.state()
            cx, cz, held = self.pos(st) if st else (None, None, "")
            if cx is None:
                return False
            if self._held_is(held, op.target):
                return True

            live = self._find_item_station(km, op.target, cx, cz)
            if live is None:
                time.sleep(0.4)
                continue

            d = ((live.x - cx) ** 2 + (live.z - cz) ** 2) ** 0.5
            if d <= 1.5:
                # ☠☠ **不能只 `face` 一下就按 —— 要先"就地微调"到游戏确认够得着**
                #   (2026-09-15 用户: "**我发现脚本不会拿地上和传送带上的食材**")。
                #   小东西(地上的/带子上的)碰撞体小: 站在**相邻格心**时(格距 1.2),
                #   到**碰撞体表面**的距离可能刚好在半径 1.0 之外, 或者落在
                #   "前方 180°"那个判据的边界上 ⇒ 游戏直接报 `抓取='(空)'`。
                #   台面能拿是因为**柜台碰撞体大**(站相邻格就贴到表面) —— 这一条差别
                #   就是"台面拿得到、地上的/带子上的拿不到"的根源。
                #   实测(`s_moonfestival_1_4`): 厨师(2.6,-7.0) 巧克力(2.4,-6.0) 距 1.02,
                #   游戏报 `抓取='(空)'`, 干等 14 秒后那件东西**被送进了垃圾桶**。
                #   地上/箱子那两条路本来就过 `_approach`(它的微调会一直挪到游戏说能作用
                #   为止), 所以地上那次成功是"距 0.73 格"—— **比一格还近**。只有传送带
                #   这条绕过了它, 这里补上同一套。
                # ☠☠ **`want` 在传送带上不能用食材名** —— 这是"拿不到传送带上的东西"
                #   的**根**, 2026-09-15 `s_sushi_4_1` 实测:
                #     游戏报的抓取目标是**传送带台面本身**(`ConveyorStation (15)`),
                #     **不是带子上那件食材**。而 `_aim_ok` 是拿游戏报的名字和 `want` 比的
                #     (`_name_is(pick, want)`) ⇒ `want='SushiRice'` **恒为假** ⇒
                #     微调循环一直转到 `tight_timeout`, 日志里就是
                #     `✗ 微调后游戏仍说作用不到目标(抓取='ConveyorStation (15)' …, 差 1.34 格)`。
                #   ⇒ 这里要的是"**站到能对带子按抓取键的地方**" —— 传 `""`
                #     (`_aim_ok`: 空 `want` = 范围里**有任意可交互物**就算到位)。
                #   ⚠ 别改成 `live.name`(某一个带段的名字): **东西在带子上一直换段**
                #     (日志里同一次里就出现过 `ConveyorStation (4)` 和 `(15)`),
                #     要求"必须是那一段"会在东西漂走时把微调卡死。
                #   ⚠ "抓没抓对"不靠 `want` 管 —— 按完之后有**独立**的一次核对(见下)。
                # ☠ **和地上的料共用同一套落位** —— 这里传 `near=SMALL_STAND`,
                #   走的是 `_approach` → `_land_near` 那**同一条**路(别在这儿再抄一份:
                #   两处各写一份, 迟早一处修了另一处没修)。
                #   带子上的料和地上的一样是**小碰撞体** ⇒ 站相邻格心(1.2)够不着,
                #   上面那段 ☠☠ 记的 `s_moonfestival_1_4` 就是这条账。
                #   ⚠ `want=""` 不变(带子上游戏报的是 `ConveyorStation (N)`, 见上) ——
                #     `_land_near` 里 `_aim_ok(st, "")` = "范围里有任意可交互物"。
                self._approach(km, live.x, live.z, tight=0.5, want="",
                               near=SMALL_STAND)
                if self.interact("pickup", verify_hold_change=True):
                    # interact 只判"手上有没有变"，不判"是不是目标"。传送带上一格
                    # 一格连着好几个食材，手伸过去很可能抓到旁边那一个(SushiPrawn
                    # 不是 SushiFish)。必须再验一次，拿错了就放回原地继续等。
                    st2 = self.state(force=True)
                    _, _, got = self.pos(st2)
                    if self._held_is(got, op.target):
                        return True
                    self.log(f"[步骤] 拿到 {got!r}，不是 {op.target!r}，放回继续等")
                    self.interact("pickup", verify_hold_change=False)
                    # ☠☠ **"拿错了"是进展, 不是失败** —— 它恰好证明**站位是对的**
                    #   (够得着带子), 只是带子上**这一刻**过来的不是那件。
                    #   它不该和"按不上"共用一个上限: 要等几次是**关卡决定的**
                    #   (带子上排了几件东西), 不是 5 —— 实机就见过排得多时白放弃。
                    #   ⇒ 分开计; 真正管住它的是上面的时间预算(`budget`)。
                    #   ⚠ 留一个**远大于任何关卡排布**的硬顶只是防病态死循环。
                    wrong += 1
                    if wrong >= 30:
                        self.log(f"[步骤] ⚠ 带子上拿到 {wrong} 次都不是 {op.target!r} —— "
                                 f"这件多半根本不在这条带子上, 收工")
                        return False
                    time.sleep(0.2)
                    continue
                # 按不上(够不着/没目标)才是真的"这一轮没做成" —— 只有它计数。
                grabs += 1
                if grabs >= 5:
                    return False
                time.sleep(0.2)
                continue

            plan = self._plan_intercept(tm, live, cx, cz) if (tm is not None and tm.ok) else None
            if plan is not None:
                spot, aim, wait_s = plan
                dist = ((spot[0] - cx) ** 2 + (spot[1] - cz) ** 2) ** 0.5
                if dist > 3.0 and not dashed:
                    # 距离远, 先冲刺一次再把路走完 —— 冲刺是 1 秒的加速, 别一直按
                    self.kb.dash()
                    dashed = True
                    time.sleep(0.2)
                self.log(f"[步骤] {op.target} 在下游 {aim[0]:.1f},{aim[1]:.1f} 会经过, "
                         f"提前去等(约 {wait_s:.1f}s)")
                self.navigate_smart(km, spot[0], spot[1], tight=0.5)
                self.face(aim[0], aim[1])
                time.sleep(min(2.0, wait_s + 0.4))
                continue

            # 兜底: 没有方向数据 —— 站到它旁边等它漂过来
            if tm is not None and tm.ok:
                spot = self._stand_cell(tm, live.x, live.z, cx, cz)
                if spot is not None:
                    self.navigate_smart(km, spot[0], spot[1], tight=0.5)
                    self.face(live.x, live.z)
            time.sleep(0.15)
        self.log(f"[步骤] 等了 {budget:.0f} 秒 {op.target} 也没到手边")
        return False

    def op_take_plate(self, km, x, z, op: Op, flow: DishFlow) -> bool:
        """摆盘: 准备好"装着菜的容器"。

        游戏机制: 食材是对着"已经有盘子的台面"放下就自动进盘 ——
        PlacementContainer + IngredientToContainerBehaviour.TransferToContainer。
        所以台面上本来就有盘子时, 直接拿那个台面当摆盘位即可,
        根本不用先把盘子搬来搬去(那样既慢又最容易失败)。
        """
        existing = self._find_item_station(km, "Plate", x, z)
        if existing is not None:
            self.assemble_spot = existing
            self.log(f"[步骤] {existing.id} 上已有盘子, 直接用它摆盘")
            return True
        # 台面上没有现成盘子 → 才去盘子堆/别处取一个
        pick = (self._find_item_station(km, "Plate", x, z)
                or self._find_item_station(km, flow.plate, x, z))
        if pick is None:
            spots = km.of("plates")
            for s in spots:
                if flow.plate and s.plate and s.plate == flow.plate:
                    pick = s
                    break
            if pick is None and spots:
                pick = min(spots, key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2)
        if pick is None:
            self.log("[步骤] 找不到盘子(既没有盘子堆, 台面上也没有)")
            return False
        self.log(f"[步骤] 去 {pick.id}({pick.x:.1f},{pick.z:.1f}) 拿容器 {flow.plate or '(任意)'}")
        if not self.navigate_smart(km, pick.x, pick.z, tight=0.8):
            return False
        if not self.interact("pickup", verify_hold_change=True):
            return False
        # 放到组装台面
        spot = self._spot_now(km) or self.pick_assemble_spot(km, x, z)
        if spot is None:
            self.log("[步骤] 找不到组装台面")
            return False
        self.assemble_spot = spot
        self.log(f"[步骤] 把容器放到组装台面 {spot.id}")
        if not self.navigate_smart(km, spot.x, spot.z, tight=0.6):
            return False
        return self.interact("pickup", verify_hold_change=True)

    def _obstacles(self, km: KitchenMap) -> set:
        """台子所占的网格 = 障碍。台子间距实测 1.2, 正好是游戏网格。"""
        from pathing import to_grid
        return set(to_grid(s.x, s.z) for s in km.stations.values())

    # ------------------------------------------------------------ 传送门边

    # ------------------------------------------------------------ 遥感驾驶
    #
    # 机制(反编译): 走到 `Terminal` 控制台按交互 → 开始一个 session:
    #   厨师自己的 `PlayerControls.enabled = false` + 刚体转 kinematic(**人定住**),
    #   控制权交给被驾驶的物体(`ServerPilotMovement.AssignPlayer`)。
    #   ⇒ **此后发的移动键驱动的是那块平台, 不是厨师**。
    #   会话在按下 拾取/交互/冲刺 任意一个键时结束。
    # 依据: `Terminal.cs` / `ServerTerminal.cs` / `ServerPilotMovement.cs` /
    #       `ClientSessionInteractable.cs`(`HasSession` 就是那个可读信号)。
    def session_station(self, st: dict) -> dict:
        """**我(本地这只厨师)现在在遥感驾驶吗?** 是则返回那个控制台工位, 否则 None。

        为什么非知道不可: 不知道就会**把平台当厨师开** ——
          `navigate()` 发的移动键全落到平台上; 卡住检测判"厨师卡住"→ 中止→重规划 → 更乱。
          而且**退出用的正是交互键**, 所以会话中调 `interact()` 等于踩刹车。
        """
        for s in ((st or {}).get("layout") or {}).get("stations") or []:
            if s.get("session"):
                return s
        return None

    def pilot_pose(self) -> dict | None:
        """被驾驶物体的**当前位置**(来自插件 dyn 的 platforms 表)。"""
        try:
            dyn = self.bridge.get_dyn()
        except Exception as e:
            self.log(f"[遥感] 读机关表失败: {e}")
            return None
        plats = dyn.get("platforms") or []
        if not plats:
            return None
        # 优先按控制台说的名字找, 找不到就用第一个
        want = (self.session_station(self.state()) or {}).get("pilots") or ""
        for p in plats:
            if want and (p.get("name") or "") == want:
                return p
        return plats[0]

    def pilot_to(self, tm, cell: tuple, budget: float = 20.0,
                 arrive: float = 0.35) -> bool:
        """把**被驾驶的平台**开到格子 `cell`。**调用前必须已经在会话里**(见 `session_station`)。

        闭环: 每轮读平台**当前位置** → 算方向 → 发移动键 → 再读。
        为什么要闭环而不是"按住 N 秒": 平台是**逐格移动且只往空闲格走**
        (`ServerPilotMovement.Update_Movement` 里先做 0.3 的 BoxCast,
        目标格被占就退化成只走 x 或只走 z) —— 所以它会被墙挡、会沿边走,
        按时间开环必然停错地方。
        """
        from pathing import dir_for_step
        from bridge.keyboard_input import ensure_focus, get_driver
        tx, tz = tm.world_of(*cell)
        t0 = time.time()
        last = None
        while time.time() - t0 < budget:
            if not ensure_focus(wait_s=1.0):
                self.kb.release_all()
                continue
            p = self.pilot_pose()
            if p is None:
                self.log("[遥感] 读不到平台位置(会话结束了吗?)")
                return False
            px, pz = float(p.get("x") or 0), float(p.get("z") or 0)
            dx, dz = tx - px, tz - pz
            dist = (dx * dx + dz * dz) ** 0.5
            if dist <= arrive:
                self.kb.release_all()
                self.log(f"[遥感] 平台已到位 格({cell[0]},{cell[1]}) 世界({px:.1f},{pz:.1f})")
                return True
            driver = get_driver()
            analog = driver is not None and hasattr(driver, "move")
            if analog:
                inv = 1.0 / dist if dist > 1e-4 else 0.0
                driver.move(dx * inv, -dz * inv)
                time.sleep(0.05)
            else:
                d = dir_for_step(dx, dz, deadzone=0.05)
                if not d:
                    self.kb.release_all()
                    continue
                key = self._key({"left": "A", "right": "D",
                                 "up": "W", "down": "S"}[d])
                from bridge.keyboard_input import key_down, key_up
                key_down(key)
                time.sleep(min(0.25, max(0.06, max(abs(dx), abs(dz)) / self.speed)))
                key_up(key)
                time.sleep(0.05)
            # 没动 = 被挡住/顶住, 交给上层决定(别在这儿死等)
            if last is not None and abs(px - last[0]) < 0.05 and abs(pz - last[1]) < 0.05:
                self._pilot_stuck = getattr(self, "_pilot_stuck", 0) + 1
                if self._pilot_stuck >= 12:
                    self.kb.release_all()
                    self.log(f"[遥感] 平台卡在 ({px:.1f},{pz:.1f}), 距目标还 {dist:.1f}")
                    self._pilot_stuck = 0
                    return False
            else:
                self._pilot_stuck = 0
            last = (px, pz)
        self.kb.release_all()
        self.log(f"[遥感] 超时({budget}s)还没把平台开到 格({cell[0]},{cell[1]})")
        return False

    def pilot_end(self) -> bool:
        """退出遥感会话(按交互键 —— 拾取/交互/冲刺任一即可)。

        ⚠ 这也是为什么会话中**不能随手调 `interact()`**: 那正是退出键。
        """
        self.kb.release_all()
        time.sleep(0.1)
        self.kb.pickup()
        time.sleep(0.35)
        st = self.state(force=True)
        s = self.session_station(st) if st else None
        if s is None:
            self.log("[遥感] 已退出驾驶")
            return True
        self.log("[遥感] 按了交互但还在会话里")
        return False

    def _travel_edges(self, km, tm):
        """本帧的**额外边** = 传送门 + **地面传送带**。按地形对象缓存。

        为什么合在一起: 泛洪和 A* 都只认**一个** `extra_edges` 参数 ——
        分成两处传的话迟早有一处漏传, 那就又回到 §5.1 那个
        "工具静默地什么都没做"(实测: 漏传传送门边让可达少算 15 格)。

        缓存键用**对象身份**而不是版本号: `Engine.terrain()` 没变时返回的是
        **同一个 `TerrainMap` 对象**, 变了才换新的 —— 身份就是最准的"变了没有"。
        """
        c = getattr(self, "_travel_cache", None)
        if c is not None and c[0] is tm:
            return c[1]
        ed = {}
        try:
            for k, vs in (teleport_edges(km, tm) or {}).items():
                ed.setdefault(k, []).extend(vs)
        except Exception as e:
            self.log(f"[边] 传送门边构造失败: {e}")
        try:
            dyn = self.bridge.get_dyn() or {}
            for k, vs in conveyor_edges(tm, dyn).items():
                lst = ed.setdefault(k, [])
                for t in vs:
                    if t not in lst:
                        lst.append(t)
        except Exception as e:
            self.log(f"[边] 传送带边构造失败: {e}")
        self._travel_cache = (tm, ed)
        return ed

    def _dyn(self, ttl: float = 1.0) -> dict:
        """机关表(`dyn`), 带短 TTL —— 一帧里好几个地方要用, 别各取各的。"""
        now = time.time()
        c = getattr(self, "_dyn_cache", None)
        if c is not None and now - c[0] < ttl:
            return c[1]
        try:
            d = self.bridge.get_dyn() or {}
        except Exception as e:
            self.log(f"[机关] 取 dyn 失败: {e}")
            d = {}
        self._dyn_cache = (now, d)
        return d

    def _wind_cells(self) -> dict:
        """本帧的**风区格表** `{(i,j): (vx, vz)}`(世界单位/秒)。没有风就返回空表。

        ⚠ **按 `dyn` 那个缓存对象的身份失效**, 不是按 `TerrainMap` 身份(那是 `_travel_edges`
          的做法)。理由: 风是**动态**的 —— `enabled` / `m_windSpeed` 会被机关改,
          而地形可以一动不动。`_dyn()` 本身带 1 秒 TTL, 所以这张表最多每秒重算一次。
        """
        dyn = self._dyn()
        c = getattr(self, "_wind_cache", None)
        if c is not None and c[0] is dyn:
            return c[1]
        try:
            w = wind_cells(self.terrain(), dyn)
        except Exception as e:
            self.log(f"[风] 风区格表构造失败: {e}")
            w = {}
        self._wind_cache = (dyn, w)
        return w

    def _wind_at(self, tm, x: float, z: float):
        """厨师**站的这一格**有没有风 —— 返回 `(vx, vz)`(世界单位/秒)或 None。

        按格查(`wind_cells` 已经把体积投到格上了)。风区通常横跨好几格,
        厨师走进去的**那一刻**就开始被推, 所以判据要用**当前位置**、每轮重取。
        """
        if tm is None or not getattr(tm, "ok", False) or x is None:
            return None
        w = self._wind_cells()
        if not w:
            return None
        return w.get(tm.cell_of(x, z))

    def _wind_of(self, st: dict) -> tuple:
        """**游戏自己报的**这个厨师此刻身上的风力 —— 返回 `((vx, vz) | None, 可不可信)`。

        权威来源(反编译): `PlayerControls.WindReceiver.GetVelocity()`
        (`PlayerControls.cs:408,576` → `WindAccumulator.cs:71-74` = `m_totalForce`)
        —— `ClientPlayerControlsImpl_Default.cs:902-906` 的 `ApplyWindForce` **用的就是它**。

        ⇒ 它一次答掉三个几何推不出来的问题:
          · 多股风**已经求和**(`WindAccumulator.cs:44-51`: 重叠体积/风箱喷雾都算进去了)
          · `m_windFilter` 层掩码**已经过掉**(`WindVolume.cs:8-9`: 体积可以被配成"不吹厨师")
          · 碰撞体的真实形状/旋转/在哪个子物体上, 全不用管
        ⇒ 所以**只要它在, 就优先用它**; 几何投影(`_wind_at`)只作旧 DLL 的兜底
          —— 而且它现在只喂"规划"(哪几格会吹), 不再喂"补偿"。

        返回的第二个值是**"这个读数可不可信"**:
          · `True`  + `(0.0, 0.0)` ⇒ 游戏说此刻**没有风**, 这时**不要**退回几何投影
            (投影说"你在风里"而游戏说没风, 那是层掩码/求和抵消 —— 游戏才对)
          · `False` + `None`        ⇒ 插件没报这个字段(旧 DLL)或反射失败 → 调用方自己兜底
        """
        c = self.chef(st) or {}
        w = c.get("wind")
        if not isinstance(w, dict) or not w.get("ok"):
            return None, False
        try:
            vx = float(w.get("vx") or 0.0)
            vz = float(w.get("vz") or 0.0)
        except (TypeError, ValueError):
            return None, False
        if abs(vx) < 1e-3 and abs(vz) < 1e-3:
            return None, True                     # 权威: 此刻没风
        return (vx, vz), True

    def world_transitioning(self) -> list:
        """**关卡此刻正在变形吗** —— 返回正在变的构件列表(空 = 没在变)。

        依据(反编译 `InteractiveScan.Snapshot`): `transitions` 里**只上报
        有变形标志为真**的构件 —— `IsTransitioning` / `IsTideTransitioning` /
        `IsArtInMotion` / `InScene` 任一为真(`InteractiveScan.cs:161-162`:
        "没有任何变形标志为真 = 这一关此刻没在变形, 不必上报")。
        ⇒ **`transitions` 非空就是"关卡正在动"。**

        ⚠ 为什么要当**导航闸门**: 变形期间地形正在改(潮水涨/画动/场景切换),
          地形图上"现在能走"的格子可能正变成水或空洞。**硬走过去就是掉下去** ——
          而且那和"寻路算错了"长得一模一样, 事后极难查。
        """
        return self._dyn().get("transitions") or []

    def _log_triggers(self, km) -> None:
        """把**触发机器**打出来 —— 它们不"操作", 而是**解释地形为什么会变**。

        `TriggerIgniteArea`(会点火) / `TriggerCreateHazard`(会造危险区) /
        `TriggerMoveSpawnPoints`(会挪出餐点) 之类, 存在就说明这一格附近有会变的玩意。
        只在一关打一次。
        """
        if getattr(self, "_trig_logged_scene", "") == (self.scene or ""):
            return
        self._trig_logged_scene = self.scene or ""
        tg = self._dyn().get("triggers") or []
        if not tg:
            return
        from collections import Counter
        c = Counter((t.get("type") or "?") for t in tg)
        self.log("[机关] 触发机器 %d 个(它们会让地形/危险区变, 不是用来按的): %s"
                 % (len(tg), ", ".join("%s×%d" % kv for kv in c.most_common())))
        for t in tg[:6]:
            self.log("        %-24s (%.1f,%.1f) 开=%s"
                     % (t.get("type"), float(t.get("x") or 0),
                        float(t.get("z") or 0), t.get("on")))

    def _pilot_probe(self, term) -> bool:
        """**"现在动的是平台还是厨师?"** —— 进/退会话的判据都靠它。

        ⚠ **不能靠 `Station.session` 判**: 它是 C# 建**场景缓存**时算一次的
          静态值(`SceneScanner.DescribeStatic`, 最多 5 秒兜底重扫),
          按完交互键**立刻读反映不了**。所以直接发一小段方向键看**谁动了**:

             厨师动了            → 没进会话(控制权还在厨师手上)
             厨师没动、平台动了  → **进了会话**(控制权已经交给平台)
             两边都没动          → 判不了(多半被挡住), 当没进
        """
        from bridge.keyboard_input import key_down, key_up

        def snap():
            st = self.state(force=True)
            cx, cz, _ = self.pos(st) if st else (None, None, "")
            p = self.pilot_pose() or {}
            return cx, cz, float(p.get("x") or 0), float(p.get("z") or 0)

        cx0, cz0, px0, pz0 = snap()
        if cx0 is None:
            return False
        # 朝**远离控制台**的方向推 —— 那个方向基本不会被台子挡住
        d = dir_for_step(cx0 - term.x, cz0 - term.z, deadzone=0.05) or "down"
        key = self._key({"left": "A", "right": "D", "up": "W", "down": "S"}[d])
        key_down(key)
        time.sleep(0.35)
        key_up(key)
        time.sleep(0.20)
        cx1, cz1, px1, pz1 = snap()
        if cx1 is None:
            return False
        chef_moved = abs(cx1 - cx0) > 0.15 or abs(cz1 - cz0) > 0.15
        plat_moved = abs(px1 - px0) > 0.05 or abs(pz1 - pz0) > 0.05
        if not chef_moved and plat_moved:
            self.log("[遥感] ✓ 已在会话里(厨师没动、平台动了 %.2f 格)"
                     % (((px1 - px0) ** 2 + (pz1 - pz0) ** 2) ** 0.5))
            return True
        if chef_moved:
            self.log("[遥感] ✗ 厨师还在动 —— 没进会话")
            return False
        self.log("[遥感] ? 厨师和平台都没动 —— 判不了")
        return False

    def pilot_enter(self, km, st, tm) -> bool:
        """走到最近的 `Terminal` 控制台按交互, 并**验证真的进了会话**。

        进入方式: 走到控制台旁边 → 按拾取键(`interact` 会等 `held` 变化, 这里没东西可拿,
        所以直接 `kb.pickup()`)。进门之后厨师的 `PlayerControls` 被停用,
        移动键改驱动平台 —— `navigate` 里那段 ☠ 注释说的就是这个。
        """
        terms = km.of("terminal") if km is not None else []
        if not terms:
            self.log("[遥感] 这一关没有 Terminal 控制台")
            return False
        x, z, _ = self.pos(st) if st else (None, None, "")
        if x is None:
            return False
        t = min(terms, key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2)
        self.log("[遥感] 去控制台 %s (%.1f,%.1f), 它驾驶的是 %r"
                 % (t.id, t.x, t.z, getattr(t, "pilots", "")))
        if not self.navigate_smart(km, t.x, t.z, tight=0.8):
            self.log("[遥感] 走不到控制台")
            return False
        self.kb.release_all()
        time.sleep(0.15)
        self.kb.pickup()
        time.sleep(0.45)
        return self._pilot_probe(t)

    def pilot_bridge(self, km, st, tm, goal_xz, tries: int = 3) -> bool:
        """目标**走不到** → 用**可开动的平台**搭桥过去。

        串联四步(§4.2 的"串联"): `bridge_cells` 算桥位 → 走到控制台进会话 →
        把平台开到桥位 → 退出 → **重取地形、重算可达** → 通了就导航过去。

        ⚠ 两条硬约束:
          · `pilot_to` 假设**已经在会话里** —— 所以先 `pilot_enter`
          · 会话中**严禁调 `interact()`**(那正是退出键, 见 `pilot_end` 的注释)
        """
        x, z, _ = self.pos(st) if st else (None, None, "")
        if x is None or tm is None or not tm.ok:
            return False
        cands = tm.bridge_cells((x, z), goal_xz)
        if not cands:
            # `bridge_cells` 是**按单格**算的: "只把这一格变可走, 目标就通了"。
            # 多个格子才通的情况它返回空 —— 那种交给上层换别的办法。
            self.log("[搭桥] `bridge_cells` 说单格当桥救不了 (目标 %.1f,%.1f)"
                     % goal_xz)
            return False
        self.log("[搭桥] 桥位候选 %d 个(按离厨师近排序), 试前 %d 个: %s"
                 % (len(cands), tries, cands[:tries]))
        # ☠ **重入闸**: `pilot_enter` 里"走到控制台"用的是 `navigate_smart`,
        #   而控制台够不着时那条路又会进 `pilot_bridge` —— 不加这道闸就无限套娃
        #   (`navigate_smart → pilot_bridge → pilot_enter → navigate_smart → …`)。
        self._in_bridge = True
        entered = False
        try:
            for c in cands[:tries]:
                if not entered:
                    if not self.pilot_enter(km, st, tm):
                        return False
                    entered = True
                # 单次别等太久: 三个候选 × 20s 会把整局的节奏耗光
                if not self.pilot_to(tm, c, budget=8.0):
                    self.log("[搭桥] 平台开不到 %s, 换下一个候选" % (c,))
                    continue
                self.pilot_end()
                entered = False
                # 退出后**必须重取地形**: 判据是**可达性**而不是字符 ——
                # 平台不占格子(`MovingPlatform5` 实测报 `平台0`), 它停下了
                # `walkable` 也不会变, 变的只有"从厨师出发能不能到"。
                tm2 = self.terrain(force=True)
                st2 = self.state(force=True)
                if tm2 is None or not tm2.ok or not st2:
                    continue
                x2, z2, _ = self.pos(st2)
                if x2 is None:
                    continue
                g = tm2.cell_of(goal_xz[0], goal_xz[1])
                reach = tm2.reachable_from(
                    x2, z2, at_y=self.chef_y(st2),
                    extra_edges=teleport_edges(km, tm2))
                if g in reach:
                    self.log("[搭桥] ✓ 平台停在 %s 之后目标格 %s 进可达集了" % (c, g))
                    # ⚠ **不在这里调 `navigate_smart`** —— 那会递归
                    #   (`navigate_smart → pilot_bridge → navigate_smart`)。
                    #   返 True 让调用方重取地形、重新规划就行。
                    return True
                self.log("[搭桥] 平台停在 %s, 目标格 %s 还是到不了" % (c, g))
        finally:
            if entered:
                self.pilot_end()
            self._in_bridge = False
        self.log("[搭桥] 试完 %d 个候选都没通" % min(len(cands), tries))
        return False

    def navigate_teleport(self, tx: float, tz: float, exits,
                          budget: float = 6.0) -> bool:
        """朝**传送门自己那一格**挤进去, 直到人被送到对端。

        为什么不能用 `navigate()`:
          · 门那一格在地形上是**障碍**(`teleport_edges` 的注释写着"边必须经过门自己那格"),
            所以"走到目标附近"这个判据**永远不成立** —— 人顶在门上被判 `stuck>=8`,
            一次次被判"卡住"→中止, 把 `step_timeout` 白耗光
          · 更糟的是 `navigate()` 只判"没动"(`abs(x-last)<0.05`), **不判位置跳变** ——
            被传到对岸之后它还在朝原方向走, 可能掉头走回出口那扇门**被再传回来**

        所以**成功判据换掉了**: 不是"走到 (tx,tz)", 而是
        **厨师所在格落进 `exits` 的 Chebyshev 1 邻域**(`exits` = 该格在
        `teleport_edges` 里的出边目标, 也就是"出口旁边那几格")。
        """
        ex = [tuple(c) for c in (exits or ())]
        if not ex:
            return False
        from bridge.keyboard_input import key_down, key_up, ensure_focus, get_driver
        tm = self.terrain()
        driver = get_driver()
        analog = driver is not None and hasattr(driver, "move") \
            and hasattr(driver, "release_all")
        t0 = time.time()
        try:
            while time.time() - t0 < budget:
                if not ensure_focus(wait_s=1.0):
                    self.kb.release_all()
                    continue
                st = self.state(force=True)
                if not st or not st.get("inRound"):
                    self.log("[传送] 对局结束, 中止")
                    return False
                x, z, _ = self.pos(st)
                if x is None:
                    return False
                if self.is_respawning(st):
                    self.log("[传送] 厨师正在重生, 松手等")
                    if not self.wait_respawn():
                        return False
                    t0 = time.time()
                    continue
                if tm is not None and tm.ok:
                    cc = tm.cell_of(x, z)
                    if any(abs(cc[0] - e[0]) <= 1 and abs(cc[1] - e[1]) <= 1
                           for e in ex):
                        self.kb.release_all()
                        self.log("[传送] ✓ 已到对端 格%s (世界 %.1f,%.1f)" % (cc, x, z))
                        return True
                dx, dz = tx - x, tz - z
                dist = (dx * dx + dz * dz) ** 0.5
                if dist < 1e-4:
                    dx, dz = 0.0, 1.0          # 正好压在门上, 随便推一下
                if analog:
                    inv = 1.0 / max(dist, 1e-4)
                    driver.move(dx * inv, -dz * inv)
                    time.sleep(0.03)
                else:
                    d = dir_for_step(dx, dz, deadzone=0.02)
                    if not d:
                        # 两轴都在死区 = 已经贴到门上了, 直接顶最后一下
                        d = ("right" if dx > 0 else "left") if abs(dx) >= abs(dz) \
                            else ("up" if dz >= 0 else "down")
                    key = self._key({"left": "A", "right": "D",
                                     "up": "W", "down": "S"}[d])
                    key_down(key)
                    time.sleep(0.10)
                    key_up(key)
                    time.sleep(0.04)
            self.kb.release_all()
            self.log("[传送] 挤了 %.0fs 还没过去 (门格世界 %.1f,%.1f)" % (budget, tx, tz))
            return False
        finally:
            if analog:
                try:
                    driver.release_all()
                except Exception:
                    pass
            self.kb.release_all()

    # ------------------------------------------------------------ 关卡地形
    def terrain(self, force: bool = False):
        """拿整张关卡网格(含危险区)。**有保质期, 不是整局只用一份。**

        这张图是**寻路的唯一真相来源**: 它同时知道"哪里被占住"和"哪里会淹死/掉下去",
        而游戏原生 FindPath 只知道前者。
        双人时走共享世界 —— 同一关**只拉一次、只解一次**, 两个人看同一份。

        ⚠ **为什么必须带保质期**(用户实测指出的"跳海"):
          这里原来是"按场景缓存, 关卡不变就不重取", 而地形**真的会变** ——
          限时平台升降、荷叶沉浮、潮水、火。要命的是这类变化**常常只改高度不改字符**
          (`s_wonderland_1_2` 实测: 66 格高度在 `0.00 ↔ -3.00` 循环, 字符一格不变),
          所以拿着开局那张图, 引擎会以为"平台还升着" → **直接走进海里**。
          现在: 超过 `terrain_ttl` 就重取, 再用 C# 给的**版本号**判断到底变没变 ——
          没变就沿用**旧对象**(不打扰别处的引用, 也不刷日志), 变了才换。
        """
        if self.world is not None:
            return self.world.terrain(force=force)
        from terrain import TerrainMap
        st = self.state()
        scene = (st or {}).get("scene") or ""
        now = time.time()
        if (not force and self._terrain is not None
                and self._terrain_scene == scene and self._terrain.ok
                and (now - self._terrain_at) < self.terrain_ttl):
            return self._terrain
        try:
            # 只要"最多这么旧"的数据: 不强制重建(C# 每格一次射线, 很重),
            # 但也不能拿 C# 默认那 5 秒的老图 —— 5 秒够厨师走出 20 格。
            data = self.bridge.get_map(force=force, max_age=self.terrain_ttl)
        except Exception as e:
            self.log(f"[地形] 取图失败: {e}")
            return self._terrain
        tm = TerrainMap(data)
        if tm.error:
            self.log(f"[地形] 报错: {tm.error}")
            return self._terrain
        if not tm.ok:
            self.log("[地形] 网格数据不完整, 退回旧寻路")
            return self._terrain
        if self._terrain is not None and self._terrain.ok and self._terrain_scene == scene:
            if tm.ver:
                same = (tm.ver == self._terrain_ver)      # 权威判据
            else:
                # 老 dll 没有版本号 → 退化成"比计数"(和改之前一样), 免得刷日志
                same = (tm.counts == self._terrain.counts)
            self._terrain_at = now
            if not force and same:
                return self._terrain
        self._terrain = tm
        self._terrain_scene = scene
        self._terrain_ver = tm.ver
        self._terrain_at = now
        # **只有真的变了才记日志** —— 否则每 1.5 秒刷一行, 日志会被淹掉
        self.log(f"[地形] 更新 ver={tm.ver or 'n/a'}  {tm.w}x{tm.h} 格 "
                 f"步长({tm.cellx:.2f},{tm.cellz:.2f}) " + tm.describe_dangers())
        return tm

    def _note_blocked(self, tm, x: float, z: float, dx: float, dz: float,
                      dist: float) -> None:
        """**记下"这一格物理上过不去"** —— 地图说能走、人却撞住的那种(带 TTL)。

        为什么让引擎"学这一课": C# 的物理阻挡是**探针球采样**出来的(`LevelInfo.BlockedBy`),
        会漏格; 漏掉的格在 A* 眼里是 `.` ⇒ 每次规划都从那儿走、每次都撞住
        (`卡住→超时(还差 1.0 格)`), **而且下一轮还走同一条路**。
        用户原话: "寻路…还是有很大的问题" —— 这就是其中一类:
        **地图和物理打架时, 引擎以前只会硬撞, 不会记住。**

        记的是**按方向相邻的那一格**(厨师站在好格子里、往坏格子里推)。

        **返回值 = 到底记下没有** —— 调用方靠它区分"学到了"和"白撞一趟"
        (见 `navigate_smart` 里那条"学不到就别重规划")。

        ☠☠ **不能用"前方 0.7 格采样"**(2026-09-15 实机 `s_sushi_4_1`, 用户:
          "会站在传送门前发呆"): 格子步长 **1.20**, 而采样只往前 **0.7** ——
          厨师只要站得离格心近一点就**跨不出边界**, 采到的正是**自己那格**,
          于是被下面那条"别记自己站的格"的守卫吞掉 ⇒ **什么也没记**。
          而"卡住"恰恰就是"**没离开自己的格子**" ⇒ 这个写法**专挑真卡住的时候失效**。
          闭环: 卡住 → 学不到 → 整趟重规划 → 同一张图同一条路 → 又卡住 →
          `replans` 四轮跑满、**厨师一步没动**, 日志读起来就是"站着发呆"。
          ⇒ 改成**按方向取相邻格**: 学不学得到从此和"站得离格心多远"无关。
        """
        # ⚠ `dist` 太小的时候**方向是没有意义的**(人在目标上原地蹭) —— 那时记下的
        #   会是一个随机邻居。只有"真的在往那儿推"才值得记。
        if BLOCKED_TTL <= 0 or tm is None or not getattr(tm, "ok", False) or dist <= 0.5:
            return False
        here = tm.cell_of(x, z)
        c = (here[0] + (1 if dx > 1e-6 else (-1 if dx < -1e-6 else 0)),
             here[1] + (1 if dz > 1e-6 else (-1 if dz < -1e-6 else 0)))
        if not tm.inside(*c) or c == here:
            return False
        self._blocked[c] = time.time() + BLOCKED_TTL
        self.log(f"[导航] ⚠ 记下「这一格过不去」{c}(地图说能走, 人撞住了) —— "
                 f"接下来 {BLOCKED_TTL:.0f} 秒规划时绕开它")
        return True

    def _dynamic_blocks(self, km, tm) -> set:
        """**当前不能走的格子** = 会动的东西占住的 + **撞住过的**(学来的)。

        为什么要单独算(用户指出: "对于路人和车辆完全没有建模"):
          地形是**整局一次的静态快照**, 而车会开、路人会走。快照把它们冻在
          第一次扫到的位置 —— 于是"地图说安全的地方"可能是车当前的位置。
          **这比不知道更危险: 它让人放心地走进去。**
          所以每次规划都拿 movers 的**当前位置**重新禁一遍。

        另一半是 `_blocked`: **地图和物理打架时学到的**(见 `_note_blocked`)。
        合并在这里, 于是 `navigate_smart` 的 A* 会自动绕开它们;
        真绕不开时那边有"退回不避让"的兜底(不会因此把路彻底封死)。
        """
        if km is None or tm is None or not getattr(tm, "ok", False):
            return set()
        now = time.time()
        # 顺手清过期项(字典别无限长)
        if len(self._blocked) > 48:
            self._blocked = {c: t for c, t in self._blocked.items() if t > now}
        learned = {c for c, t in self._blocked.items() if t > now}
        try:
            out = set(km.blocked_by_movers(tm))
        except Exception as e:
            self.log(f"[导航] 动态禁行格算失败: {e}")
            out = set()
        return out | learned

    def _native_path_safe(self, tm, pts: list, blocked: set = None) -> list:
        """把游戏原生路径里"会淹死人的点"和"被会动的东西占住的点"剔掉。

        原生寻路不知道水面, 所以它给的路径可能直接横穿池塘; 也不知道车开到哪了。
        这里逐点检查: 一旦某个点落在危险格或动态禁行格上, 就把这条路径整条作废
        (返回空), 让调用方改用地形 A* —— 半条原生路径比没有路径更危险。
        """
        if not pts or tm is None or not tm.ok:
            return pts
        blk = blocked or ()
        for (px, pz) in pts:
            if tm.is_danger_world(px, pz):
                return []
            if blk and tm.cell_of(px, pz) in blk:
                return []
        return pts

    def navigate_smart(self, km: KitchenMap, tx: float, tz: float,
                       tight: float = 0.8, replans: int = 3) -> bool:
        """带寻路的导航。

        优先级(实测排出来的):
          1) **地形 A*** —— 用游戏自己的网格(占用物=障碍), 再额外避开水面/空洞。
             这是唯一既不会撞墙、也不会淹死的方案。
          2) 游戏原生 GridNavSpace.FindPath —— 兜底。但必须先过滤掉危险点,
             因为它的可走判定 `GetGridOccupant()==null` 根本看不见水面。
          3) 拿台子列表当障碍的 Python A* —— 最后兜底(会漏掉边界与橱柜)。
        每段走完位置会变, 所以失败就重新规划。

        ⚠ **遥感相关的两件事, 这张导航图都不知道**:
          · **可开动的平台不占格子**(实测 `MovingPlatform5` 关报 `平台0`),
            所以它停在哪、能不能当桥, 在地形图上**完全看不见** ——
            "某几格到不了"有可能是"平台没开过去", 不是地形问题。
            要判这个用 `TerrainMap.bridge_cells(起点, 目标)`(它会告诉你停在哪几格有用)。
          · **会话开着时移动键驱动的是平台而不是厨师** ——
            本函数一路都在发移动键, 所以进这里之前必须先确认不在会话里
            (`self.session_station(st) is None`), 否则厨师原地不动、
            平台却被开跑(见 navigate 里那段 ☠ 注释)。
        """
        from pathing import plan_path
        tm = self.terrain()
        for attempt in range(replans + 1):
            st = self.state()
            if not st or not st.get("inRound"):
                return False

            # 地图是**会变**的: 荷叶踩过会消失、按钮会改传送带、火会占格、潮水会吞台面。
            # 前一轮没走通就重取一次 —— 插件读的是实时的 GetGridOccupant, 而游戏自己的
            # m_nodeMap 只在 Start 建一次永不刷新, 所以只有重新取图才能看到变化。
            if attempt > 0:
                tm = self.terrain(force=True)

            x, z, _ = self.pos(st)
            if x is None:
                return False
            if (tx - x) ** 2 + (tz - z) ** 2 <= (tight or self.arrive) ** 2:
                return True

            # 会动的东西(路人/车)当前占住的格子 —— 每次都重算, 因为它们在动
            blk = self._dynamic_blocks(km, tm)
            # **额外边**(传送门 + 地面传送带): 到这一格就也能到那一格。
            tedges = self._travel_edges(km, tm)

            def _plan(blocked):
                """三条路依次试。blocked 传空 = 不避让会动的东西。"""
                if tm is not None and tm.ok:
                    p = tm.find_path(x, z, tx, tz, blocked=blocked,
                                     at_y=self.chef_y(st), extra_edges=tedges)
                    if p:
                        return p
                p = self._native_path_safe(tm, self._game_path(tx, tz), blocked=blocked)
                if p:
                    return p
                return plan_path(x, z, tx, tz, self._obstacles(km) | (blocked or set()))

            pts = _plan(blk)
            if not pts and blk:
                # ⚠ **绕不开就只能不绕**。路人/车把唯一的路堵死时, 硬撑着"必须避让"
                #   会让整个厨师原地卡住 —— 那比"冒着撞上去的风险走"更糟(整局报废)。
                #   所以这里退回不避让, 但**大声记下来**: 这条日志就是"这局有东西挡路"的证据。
                self.log(f"[导航] ⚠ 动态禁行({len(blk)} 格)导致无路可走 —— "
                         f"退回不避让会动的东西(路人/车), 冒着撞上去的风险")
                pts = _plan(set())
            if not pts:
                self.log(f"[导航] 地形 A* 无解 → ({tx:.1f},{tz:.1f}), 试原生寻路")
            if not pts:
                # 三条路都规划不出来 —— **先试"用可开动的平台搭桥"**(只在第一轮试,
                # 否则外层 replans 会把整局的节奏耗光)。
                # 为什么放这儿: 目标到不了常常不是"没有路", 而是**某几格缺一块地板**,
                # 而移动平台本质就是**一块可挪的地板**(`bridge_cells` 就是算这个的)。
                if attempt == 0 and not getattr(self, "_in_bridge", False) \
                        and tm is not None and tm.ok \
                        and self.session_station(st) is None:
                    if self.pilot_bridge(km, st, tm, (tx, tz)):
                        tm = self.terrain(force=True)   # 桥搭好了, 重取图重规划
                        continue
                # 都不行 → 退回直线冲一次
                return self.navigate(tx, tz, tight=tight)

            # 逐格走。关键: **某个路径点走不到不该让整条路径失败** ——
            # 实测 GridNavSpace 的末点常落在台子碰撞体边缘(如 (-1.2,3.6) 紧贴 serve0),
            # 人物理上过不去, 但那时通常已经站在目标旁边了(距台子 1 格, 交互半径 1.8 够得着)。
            for (px, pz) in pts:
                if (px - x) ** 2 + (pz - z) ** 2 < 0.09:
                    continue                      # 起点附近的点不用专门走
                if tm is not None and tm.ok and tm.is_danger_world(px, pz):
                    self.log(f"[导航] 路径点 ({px:.1f},{pz:.1f}) 是危险格, 跳过")
                    continue
                # **传送门那格要"挤进去"而不是"走到"** —— 它是障碍格,
                # `navigate` 的"到目标附近"永远不成立(见 `navigate_teleport`)。
                _cell = tm.cell_of(px, pz) if (tm is not None and tm.ok) else None
                _exits = tedges.get(_cell) if _cell is not None else None
                if _exits and not tm.walkable(*_cell):
                    if not self.navigate_teleport(px, pz, _exits):
                        self.log(f"[导航] 路径点 ({px:.1f},{pz:.1f}) 是传送门, 没能挤过去")
                        continue
                    x, z = px, pz
                    continue
                # ☠ **超时必须随距离走**。以前每个路点只有相邻一格(1.2u ≈ 0.33s),
                #   写死 6 秒是 18 倍余量; 而 `find_path` 现在会**拉直路径**
                #   (视线裁剪), 一个路点可能跨十几格 —— 17u 按 `self.speed`(3.6 u/s)
                #   要走 4.7 秒, 6 秒只剩 1.3 倍, 路上再被顶一下/等个门就判"到不了",
                #   而那个点往往**就是唯一那个点**, 跳过去等于这条路白规划。
                #   按运动学给: 位移/有效速度 × 2.5 倍余量 + 2 秒(起步/转身/状态读取的开销)。
                _seg = ((px - x) ** 2 + (pz - z) ** 2) ** 0.5
                _budget = max(6.0, _seg / max(1.0, self.speed) * 2.5 + 2.0)
                _blk0 = len(self._blocked)
                if not self.navigate(px, pz, arrive=0.9, step_timeout=_budget):
                    # ☠☠ **这一趟里学到新的"过不去"格子 ⇒ 立刻重规划, 不要"继续下一个"**
                    #   (2026-09-15 实机: 一次导航把同一面墙撞了 **5 遍**, 每遍都记一次
                    #    `记下「这一格过不去」(19,9)`, 然后超时 —— 一共白烧十几秒)。
                    #   为什么"继续下一个"救不了: 剩下那些路点是**同一条路**上的点,
                    #   撞墙的那格还在它们中间; 而学到的禁行格只有**规划时**才会被
                    #   `_dynamic_blocks` 并进 A* 的禁行集(见 `_note_blocked`)。
                    #   ⇒ 地图知识变了, 剩下那些路点就都不可信了 —— 跳出内层, 让外层
                    #     `for attempt in range(replans+1)` 拿着**新禁行格**重新规划。
                    #   ⚠ 没学到新格子时仍然"继续下一个"(老行为): 那种失败多半是
                    #     路点本身不好(原生路径的末点卡在台子碰撞体边缘), 跳过去是对的。
                    if len(self._blocked) > _blk0:
                        self.log(f"[导航] 路径点 ({px:.1f},{pz:.1f}) 撞到地图上看不见的障碍 "
                                 f"—— **带上新禁行格重规划**(不再沿原路硬撞)")
                        break
                    # ☠☠ **卡住 + 没学到新格 ⇒ 重规划也是同一条路, 直接放弃这一趟**
                    #   (2026-09-15 实机 `s_sushi_4_1`): 那种情形下 `_blocked` 不变,
                    #   于是外层 `for attempt in range(replans+1)` 拿着**同一张图**
                    #   又规划出**同一条路径**、同样这一批路点、同样在**同一格**卡住 ——
                    #   实测四轮跑满而**厨师一步没动**; 而这里原来的 `continue` 只是
                    #   把六颗路点跳完, 照样进下一轮。用户看到的就是"站在传送门前发呆"。
                    #   ⇒ 没信息增量就别重试, `return False` 让上层换支/换目标
                    #     (那才是真正会改变局面的动作)。见 `navigate` 里的 `_last_stuck_no_learn`。
                    if getattr(self, "_last_stuck_no_learn", False):
                        self.log("[导航] ⚠ 卡住且**没学到新格** —— 重规划还是同一条路, "
                                 "这一趟到此为止(交给上层换支/换目标)")
                        return False
                    self.log(f"[导航] 路径点 ({px:.1f},{pz:.1f}) 到不了, 继续下一个")
                    continue                       # 跳过去, 别把整条路径判死
                x, z = px, pz
            # 最后一步: **别朝台面本身推**。
            # 台面(含台面传送带)是障碍格, 厨师站不上去 —— 朝它走就是顶着橱柜推,
            # 表现成"卡住 + 超时(还差 1.0 格)"。目标是障碍格时改成站到旁边能站的格,
            # 然后转身面对它(交互半径 1.0、格距 1.2, 站相邻格刚好够得着)。
            goal_walk = (tm is not None and tm.ok and tm.walkable(*tm.cell_of(tx, tz)))
            if goal_walk:
                ok = self.navigate(tx, tz, arrive=1.4, tight=tight)
            else:
                spot = None
                st3 = self.state()
                cx3, cz3, _ = self.pos(st3) if st3 else (None, None, "")
                if tm is not None and tm.ok and cx3 is not None:
                    spot = self._stand_cell(tm, tx, tz, cx3, cz3)
                if spot is not None:
                    ok = self.navigate(spot[0], spot[1], arrive=1.2, tight=tight)
                else:
                    ok = self.navigate(tx, tz, arrive=1.4, tight=tight)
            if ok:
                self.face(tx, tz)
                return True
            # ☠☠ **失败要真的重规划, 不能直接判死** —— 这个循环本来就叫 `replans`,
            #   docstring 也写着"失败就重新规划", 可这里原来写的是 `return ok`:
            #   **第一次就返回**, 于是 `replans=3` 从来没生效过(循环体只跑一次, 那个
            #   `for attempt` 形同虚设)。实测(`s_sushi_1_3` 那次切鱼): 一趟导航把同一面墙
            #   撞了 5 遍、超时, 然后就放弃了 —— 没有任何一次"带着新知识重新规划"。
            #   ⇒ 现在失败就再来一轮; 而**重规划是有信息增量的**(不是盲目重试):
            #     这一趟里撞到过的格子已经记进 `_blocked`(见 `_note_blocked`),
            #     下一轮的 `_dynamic_blocks` 会把它并进 A* 的禁行集 ⇒ 绕开它走。
            #     `attempt > 0` 时还会 `terrain(force=True)` 重取地图(荷叶/按钮/火会改地形)。
            if attempt < replans:
                self.log(f"[导航] 这一趟没到 ({tx:.1f},{tz:.1f}) —— "
                         f"带上新禁行格重规划(第 {attempt + 2}/{replans + 1} 次)")
        return False

    def _game_path(self, tx: float, tz: float) -> list:
        """问游戏自己的寻路网格。失败返回空(由调用方退到 Python A*)。"""
        try:
            res = self.bridge.get_path(tx, tz, self.cid)
        except Exception as e:
            self.log(f"[寻路] 游戏寻路不可用: {e}")
            return []
        if res.get("error"):
            self.log(f"[寻路] 游戏寻路报错: {res['error']}")
            return []
        pts = []
        for p in res.get("path") or []:
            try:
                pts.append((float(p["x"]), float(p["z"])))
            except (KeyError, TypeError, ValueError):
                continue
        return pts

    def _board_item(self, sid: str) -> str:
        """读切菜板上现在放着的东西(名字)。"""
        st = self.state()
        km = self.map(st) if st else None
        s = km.stations.get(sid) if km else None
        return (s.on[0] if (s and s.on) else "") or ""

    #: 搅拌台把东西搅好要多久(秒)。反编译 `MixingHandler.m_mixingTime = 10f`
    #: —— **prefab 上的 `[SerializeField]`, 关卡可以改**, 所以留个环境变量兜底。
    #: ⚠ 过搅会**毁菜**: `ServerMixingHandler.SetMixingProgress` 里 `>1.3×` 报 OverDoing、
    #:   `MixingHandler.GetMixedOrderState` 里 `>2×` 直接 OverMixed(ruined)
    #:   ⇒ 取的时候要贴着 `m_mixingTime`, 不能拖。
    MIX_TIME = float(os.environ.get("NEKO_MIX_TIME") or 10.0)

    def op_mix(self, km, x, z, op: Op, st: dict) -> bool:
        """把材料放进**搅拌台**搅好, 再取回来。

        ☠ 机制和"切菜"**完全不是一回事**(反编译, 三处串起来):
          · `ServerMixingStation.CanAddItem` 要求放上去的东西有 `IMixable`
            ⇒ 搅拌台上那个**容器**(实测 `DLC03_utensil_mixer`)才是本体,
              材料是**放进容器里**的 —— 所以"放"这一步按的是拾取键(通用的放置)。
          · `OnOrderCompositionChanged` → `SetMixerOn(true)`
            ⇒ **容器里一有东西就自动开搅, 不需要按键**(和切菜/洗盘子完全不同,
              所以这里没有"连按交互键"那一段)。
          · 时长是 `MixingHandler.m_mixingTime`(默认 10s), 过搅会毁菜。
          ⇒ 动作就是三步: **放进去 → 等 → 取回来**。

        ⚠ 现在只能**按时长等**(游戏没有上报搅拌进度)。要像"切菜看名字变没变"那样
          精确判定, 得让插件多报一个字段(见交接包"待补的 C# 上报")。
        """
        import time as _t
        mk = km.nearest("mix", x, z)
        if mk is None:
            self.log("[步骤] 这关没有搅拌台(MixingStation)")
            return False
        _, _, held = self.pos(st)
        self.log(f"[步骤] 去 {mk.id} 搅拌 → {op.target} (站旁边/料放进去后自动开搅)")
        if not self._approach(km, mk.x, mk.z, tight=0.8):
            return False
        if held:
            # 放进容器(游戏把它塞进搅拌台上的那个 IMixable 容器里)
            if not self.interact("pickup", verify_hold_change=True):
                self.log(f"[步骤] ✗ {held!r} 放不进搅拌台(是容器不接受它, 还是站位不对?)")
                return False
        before = self._station_items(mk.id)
        # **等**: 搅拌是"放上去自动开始 + 计时"的, 没有按键可连打(见 docstring)。
        t0 = _t.time()
        while _t.time() - t0 < self.MIX_TIME:
            if not self.round_active():
                return False
            _t.sleep(0.5)
        self.log(f"[步骤] 搅了 {self.MIX_TIME:.0f} 秒(容器: {before} → {self._station_items(mk.id)})")
        # 取回来(没搅好也先取 —— 留在上面只会过搅毁掉)
        if not self.interact("pickup", verify_hold_change=True):
            self.log("[步骤] ✗ 搅拌好的东西取不回来")
            return False
        _, _, got = self.pos(self.state() or {})
        self.log(f"[步骤] ✓ 搅拌完成, 手上是 {got!r}")
        return True

    def op_pass(self, km, x, z, op: Op, st: dict) -> bool:
        """**把手上的这份料丢给人类队友** —— 他那半边能做我这边做不了的那一环。

        用户原话: "如果脚本控制的地方**没有菜板切菜**, 但是有食物箱,
        那么脚本需要**拿到食物箱然后丢给我**(丢的按键和切菜是同一个, 手拿着生食材就能丢)"。

        机制(反编译, 三处):
          · `ClientPlayerControlsImpl_Default.Update_Throw`: **松开使用键**那一刻投,
            按住期间是瞄准(`Update_Aim: m_aimingThrow = isUsePressed`)
            ⇒ **"丢"和"用/切"是同一个键** ✓(用户说的就是这个)
          · `ServerPlayerControlsImpl_Default.ReceiveThrowEvent(_target)`:
            按**厨师朝向** `transform.forward` 算,**不读任何蓄力值**
          · `ServerAttachmentThrower.CalculateThrowVelocity`: `方向 × m_throwForce`
            —— **prefab 定值 ⇒ 没有蓄力, 每次丢一样远**; `AlertPotentialCatchers`
            再沿投掷方向 **10 格**球扫一遍, 命中 `players` 层就通知接球。
          ⇒ 动作: **走近到 `PASS_RANGE` 内 → 转身对准他 → 丢**(`direct("throw")` 直调服务端入口)。

        ⚠ 走不到他身边是**常态**(两个厨房被墙隔开) —— 所以不要求"站到旁边",
          只要求"离他 ≤ `PASS_RANGE`"; 到不了就**认输**(别把料丢进水里)。
        """
        mate = self._mate(st)
        if mate is None:
            self.log("[传球] 没有队友 —— 丢给谁? (单人局不该走这条路)")
            return False
        _, _, held = self.pos(st)
        if not held:
            self.log("[传球] 手上没东西, 没法丢")
            return False
        mx, mz = mate[0], mate[1]
        cx, cz, _ = self.pos(st)
        self.log(f"[传球] 把 {held!r} 丢给队友(他在 {mx:.1f},{mz:.1f}, 现距 "
                 f"{((mx-(cx or 0))**2 + (mz-(cz or 0))**2) ** 0.5:.1f} 格)")
        # 先按"走到他旁边"走; 走不到(隔墙)就**尽量靠近**, 靠到射程内也算数
        if not self._approach(km, mx, mz, tight=PASS_RANGE):
            self.navigate_smart(km, mx, mz, tight=PASS_RANGE, replans=1)
        st2 = self.state(force=True)
        x2, z2, held2 = self.pos(st2) if st2 else (None, None, "")
        if x2 is None:
            return False
        d = ((mx - x2) ** 2 + (mz - z2) ** 2) ** 0.5
        if d > PASS_RANGE + PASS_SLACK:  # 留 1 格余量(走不到正好那个点)
            self.log(f"[传球] ✗ 离队友还有 {d:.1f} 格(要 ≤ {PASS_RANGE:.1f}) —— "
                     f"太远丢不到, 这次不丢(免得丢进水里)")
            return False
        self.face(mx, mz)               # 投掷方向 = 厨师朝向, 必须正对
        time.sleep(0.1)
        try:
            r = self.bridge.direct("throw", player=self._my_player_index(st2 or st))
        except Exception as e:
            self.log(f"[传球] ✗ 投掷直调失败: {e}")
            return False
        if not r.get("ok"):
            self.log(f"[传球] ✗ 投掷被拒: {r.get('error')} —— 手上是不是没有可丢的东西?")
            return False
        self.log(f"[传球] ✓ 丢出去了({d:.1f} 格)")
        # ☠ **丢成功才记台账** —— 记的是"**这一环交出去了**"(键用 `op.handoff` 那个
        #   被替代的菜谱动作, 不是 `"pass"`)。记下之后: 不再重复提议、不再回头捡料,
        #   产物回来时下游那一步自动接上。见 `HANDOFF_TTL` / `mark_handoff`。
        self.mark_handoff(getattr(op, "handoff", ""), op.target)
        return True

    def _station_items(self, sid: str) -> str:
        """读某个台面上现在放着的东西(**含容器里的内容** `onhas`)—— 给日志用。"""
        st = self.state()
        km = self.map(st) if st else None
        s = km.stations.get(sid) if km else None
        if s is None:
            return "?"
        on = list(s.on or [])
        has = [h for h in (s.onhas or []) if h]
        return "%s%s" % (on, ("〔%s〕" % ",".join(has)) if has else "") or "(空)"

    def _pick_board(self, km, x: float, z: float, target: str = ""):
        """挑一块**"这块料放得上去"**的切菜板(离厨师最近的优先)。

        ☠ 原来两处都写 `km.nearest("board", …)` —— **只看最近那块**。它被别人的东西占着时
          `chop` 就判"别白切", 可场上**不止一块板** ⇒ 整条链卡死
          (实测 2026-09-15 用户: "**如果板子上已经是切好的，脚本就会卡住**")。

        优先级: ① 已经放着 `target` 的(走过去就能切) → ② **空板**(把料放上去切) → ③ None。
        ⚠ **③ 就是"没有"**, 不要退而求其次挑一块被占着的 —— 那样只会白跑一趟再失败
          (见 `op_chop` 的"别白切"注释: 按十几刀、板上一点没变)。
        """
        mate = self._mate(self.state())      # 队友的坐标也在这张图上(见 `_mate_near`)
        best, best_key = None, None
        fallback, fallback_key = None, None  # 被队友占着的那块 —— 兜底用
        for s in (km.of("board") or []):
            on = list(getattr(s, "on", None) or [])
            if target and any(self._held_is(o, target) for o in on):
                pri = 0
            elif not on:
                pri = 1
            else:
                continue                  # 占着别的东西 → 这块用不了
            # **这一支刚试过、不行** → 换一块(用户: "按一次没结果这条路就失败了,
            # 可以**返回到其他路**")。⚠ 只在**板**这一维上划掉 —— 见 `BRANCH_TTL`:
            # 记在动作上会让一块板走不到就把所有板封掉。
            if not self._branch_ok(target, s.id):
                continue
            d = (s.x - x) ** 2 + (s.z - z) ** 2
            key = (pri, d)
            # ☠ **队友正站在这块板旁边 ⇒ 他正在用它, 别去排队**(用户实机指出的)。
            #   记下来当兜底, 但先不挑 —— "换一块板"比"走过去挤"便宜得多。
            if self._mate_near(s, mate):
                if fallback_key is None or key < fallback_key:
                    fallback, fallback_key = s, key
                continue
            if best_key is None or key < best_key:
                best, best_key = s, key
        # ☠ **全被队友占着时退回兜底** —— 否则"唯一一块板有人正在用"会变成
        #   `chop` 不可达 ⇒ 整条链卡死, **比排队更糟**(规则 5)。
        #   和 `_branch_ok` 那个"全划掉就退回不过滤"是同一个道理。
        return best if best is not None else fallback

    def op_chop(self, km, x, z, op: Op, st: dict) -> bool:
        """在切菜板上把东西切到完成。

        刀数依据 ClientWorkableItem: HasFinished() = (m_progress == m_stages-1),
        每 chopsPerSlice 刀推进一片; 合作模式 2 人时 chopsPerSlice=1, 单人时=5。
          ⇒ 刀数 = (m_stages - 1) * chopsPerSlice
        完成判定优先看"板上的东西名字变了"(完成时 GameObject 会被 m_nextPrefab 替换),
        比字符串猜名字可靠 —— 生料名往往就包含成品名(CucumberWhole ⊃ Cucumber)。
        """
        board = self._pick_board(km, x, z, op.target)
        if board is None:
            # ⚠ 说清是"没有"还是"都被占着" —— 后者是"等腾出来/换一块"的问题, 不是关卡没板
            _allb = km.of("board") or []
            self.log("[步骤] 没有**能用的**切菜板" +
                     ("(场上 %d 块都被别人的东西占着)" % len(_allb) if _allb
                      else "(这关没有 Workstation)"))
            return False
        _, _, held = self.pos(st)
        self.log(f"[步骤] 去 {board.id} 切 → {op.target}")
        # ⚠ **走不到 / 对不齐都算"这一支不行"** —— 用户 2026-09-15: "按一次没得到对应的
        #   结果这条路就失败了, 可以**返回到其他路**"。实测日志里那两条正是这样:
        #     `[导航] ✗ 这一格推不过去 (~20.4,-6.2)` → `走不到 ChoppingBoard_Side 5 (4) 旁边`
        #   处置不是原地重试(还是撞同一面墙), 而是把**这块板**划掉、换一块再选。
        if not self.navigate_smart(km, board.x, board.z, tight=0.8):
            self._last_fail_kind = KIND_BRANCH
            self.mark_branch_dead(op.target, board.id, "走不到旁边")
            return False
        self.face(board.x, board.z)
        if not self._align_for_place(board):     # 见 _align_for_place: 挨得近会判到旁边台子
            self._last_fail_kind = KIND_BRANCH
            self.mark_branch_dead(op.target, board.id, "对不齐台面")
            return False
        if held:
            self.interact("pickup", verify_hold_change=False)   # 先放上板
            time.sleep(0.25)

        n_players = len((st.get("layout") or {}).get("chefs") or [])
        per_slice = 1 if n_players >= 2 else 5     # GameConfig.SingleplayerChopTimeMultiplier
        stages = op.chop_stages or 0
        max_chops = max(1, stages - 1) * per_slice if stages else 10
        base = self._board_item(board.id)
        # ☠ **板上放着别的料时, 别切** —— 这一刀按下去只会"板上物品没有变化",
        #   却要白按十几次(实机日志: `需切 7 刀 (板上: ChoppedDriedFruit)` →
        #   `直调 use 失败: 身边没有可互动的东西` ×10 → `✗ 切完但板上物品没有变化`)。
        #   走到这一步说明**评分层的判据也不该放行** —— 两处判据现在一致(见 `_op_actionable`)。
        if base and not self._held_is(base, op.target):
            self.log(f"[步骤] ✗ {board.id} 上是 {base!r}, 不是要切的 {op.target!r} —— "
                     f"别白切(先把它处理掉/换个板)")
            self._last_fail_kind = KIND_BRANCH
            self.mark_branch_dead(op.target, board.id, f"板上是 {base}")
            return False
        self.log(f"[步骤] 需切 {max_chops} 刀" + (f" (板上: {base})" if base else ""))

        # ☠☠ **"按下去"必须有回执, 不能只数次数** —— 实机 2026-09-15 打回来的一整局:
        #   `base` 为空时下面那条兜底会"按满刀数就当切完" ⇒
        #     `需切 7 刀`(没有板名那一截) → `直调 use 失败: 身边没有可互动的东西` ×7 → `✓ chop`
        #   那 7 下**一下都没落到板上**: 板是**空的** ⇒ 游戏的 `m_interactable` 是 null
        #   ⇒ `use` 没有目标 ⇒ 纯空按。
        #   ⚠ `_align_for_place` 通过**不代表**有活可干 —— 它看的是 `placeh`("**能往这儿放**"),
        #     和"这上面**有东西可切**"是两件事; 实测那两次它都通过了。
        #   代价不是白按三秒: `chop` 这一步**销了号**(`_execute_scored` 把下标从 `pending` 去掉),
        #   手里的料也一块丢了 ⇒ 后面的 `assemble` 永远 `不可达(手空, ...)` ⇒ **整局空转**
        #   (第一份日志里"只剩 assemble/deliver 两个都手空"就是这么来的)。
        #   ⇒ 判据改成"要有回执", 二选一:
        #     ① 板上的东西**换了名字**(`base` 非空时走上面的分支);
        #     ② 游戏自己说 `use` **作用得到这块板**(`m_interactable` 非空且就是它)。
        #   一个都没有 ⇒ 这一刀根本没落下去, **不许报成功**。
        _want_board = self._norm(getattr(board, "name", ""))

        def _board_workable() -> bool:
            """游戏自己说「`use` 作用得到**这块板**」吗 —— 这是"这一刀落得下去"的唯一回执。"""
            _u = self.interaction_targets(self.state(force=True))[1]
            return bool(_u) and (not _want_board or self._norm(_u) == _want_board)

        # ⚠ **读不到板名时, 那条"按刀数收工"的兜底必须有个前提**: 游戏说这板上有活可干。
        #   不是的话它就是块**空板**, 按下去是纯空按 —— 所以**先问再按**, 别把十来下
        #   烧在这儿(实机那次就是这么白按了 7 下)。先停手重问一次(同 `_approach` 的
        #   做法: 按键的按下状态会影响 `m_interactable`), 还不行就立刻收手。
        if not base and not _board_workable():
            self.kb.release_all()
            time.sleep(0.25)
            if not _board_workable():
                self.log(f"[步骤] ✗ {board.id} 上读不到要切的 {op.target!r}, 而游戏说 "
                         f"`use` 作用不到它 —— 这块板是空的, 这一趟**没活可干**(不白按)")
                self._last_fail_kind = KIND_BRANCH
                self.mark_branch_dead(op.target, board.id, "板是空的")
                return False

        landed = 0
        done = False
        for i in range(max_chops + 3):
            if not self.round_active():
                return False
            if _board_workable():
                landed += 1
            self.kb.chop()
            time.sleep(0.35)
            cur = self._board_item(board.id)
            if base and cur and cur != base:
                self.log(f"[步骤] 切好了({i+1} 刀): {base} → {cur}")
                done = True
                break
            if not base and landed and i + 1 >= max_chops:
                done = True   # 读不到板上的名字, 按刀数收工(**但要有回执**)
                break
        if not done:
            if not landed:
                # ☠☠ **两种情况必须分开报**(实测 2026-09-15, board0/1/2 三块板连着犯):
                #     · 板上**没东西** ⇒ 这块板真的是空的, 换一块就对
                #     · 板上**有东西**, 而 `use` 还是没有目标 ⇒ **不是空板**,
                #       是**站位/朝向**的问题 —— 拿到的正是 `(板上: SushiFish)`
                #       紧接着 `直调 use 失败: 身边没有可互动的东西` ×10。
                #   ⚠ 根因: `_align_for_place` 验的是 `placeh`(**放置目标**),
                #     而 `use` 是**另一个字段**(`m_interactable`) —— **放置对得上 ≠
                #     交互够得着**。所以"放上去了、却按不动"是完全可能的状态。
                #   ⚠ 原来这里一律写"多半是这块板空的", 在第二种情况下**是错的**,
                #     会把人引去查台面而不是查站位。
                _on = self._board_item(board.id)
                _, _, _h = self.pos(self.state() or {})
                #: 这一趟是**手上的锅**还是**板的锅** —— 决定要不要把这块板划掉(见下面)。
                _blame_hand = False
                if _on:
                    # ☠☠ **别急着写"站位/朝向"** —— 还有第三种成因, 而且更常见:
                    #   上来之前**手上拿着别的东西**, 而 `op_chop` 的头一件事是
                    #   `if held: interact("pickup")`("先放上板") ⇒ 那件废料被放上去,
                    #   板子当场失效、`use` 全部打空。实测(2026-09-15): 手持**生 SushiRice**
                    #   去切板上那块 `SushiFish`, 上面这条日志把排查引去查脚下,
                    #   而真正的原因在**手上**(见 `_op_actionable` 的 `chop` 分支)。
                    #   ⇒ 手上有东西就把 **`手上={_h!r}`** 一起打出来, 一眼能分开这两种。
                    _who = (f" —— ⚠ 而**手上还拿着 {_h!r}**(上板前那一下会把它放上去, "
                            f"板子就废了)" if _h else
                            " —— **不是空板, 是站位/朝向**(`placeh` 对得上但 "
                            "`m_interactable` 是空的)")
                    self.log(f"[步骤] ✗ 按了但**一下都没落到 {board.id} 上**, 而板上"
                             f"**明明有 {_on!r}**{_who}")
                    _blame_hand = bool(_h)
                else:
                    self.log(f"[步骤] ✗ 按了但**一下都没落到 {board.id} 上**"
                             f"(游戏说 use 作用不到它) —— 这块板**确实是空的**; "
                             f"不把「原地空按」当成切好了")
            else:
                # ClientWorkableItem 完成时会替换板上的物件。若名字仍未变化，说明
                # use 没有送到游戏；把生料拿回手里不能算切菜完成。
                self.log("[步骤] ✗ 切完但板上物品没有变化，拒绝把生料当成成品")
            # 这一类 = **这一趟办不成** ⇒ 换一支, 别原地重试(用户: "返回到其他路")。
            self._last_fail_kind = KIND_BRANCH
            if _blame_hand:
                # ☠ **不是这块板的错, 别把它划掉 25 秒** —— 错在手上(上来之前拿着别的料),
                #   板子是好的。实测那局 `board1`/`board2` 各被冤枉划掉一次(每次 25 秒),
                #   最后只剩 board0 能用。⇒ 板子留着, 让上层**换一份手**再来。
                self.log(f"[步骤]    这一趟是**手上拿着东西**的锅 —— {board.id} 不划掉"
                         f"(它是好的, 别让它背 25 秒冷板凳)")
            else:
                self.mark_branch_dead(op.target, board.id, "切不动")
            return False
        # ⚠ **收尾那一下必须是「拿起」, 不能是「放下」** —— `verify_hold_change` 的判据是
        #   "持有物变了没有", 而**放下也会让它变** ⇒ 原来对着空板按一下就把手里的料
        #   丢在那儿, 却报 `✓`(实机日志里 `✓ chop` 的**下一格**就是 `assemble 手空`)。
        if not self.interact("pickup", verify_hold_change=True):
            return False
        _, _, _held_after = self.pos(self.state(force=True) or {})
        if not _held_after:
            self.log("[步骤] ✗ 切完收尾那一下把手里的料**放下去了**(收工时手是空的) —— "
                     "这不是「拿起」, 判失败")
            return False
        return True

    def op_cook(self, km, x, z, op: Op, st: dict, flow: DishFlow = None) -> bool:
        """把东西放上灶台, 盯到"刚熟"立刻取下(生和焦都不算)。

        两种煮法, 由 op.in_pot 决定(见 cookbook.derive):
          · in_pot=False: 食材自带 CookingHandler → 直接放灶台, 熟了**用手拿走**。
          · in_pot=True : 食材自己不带 CookingHandler, 煮它的是**锅** → 装进锅,
                          熟了**手拿盘对锅按交互取菜, 锅留在灶上不动**(用户要求)。
        """
        try:
            return self._cook(km, x, z, op, st, flow)
        except Exception:
            raise
        finally:
            if self.board is not None and self._stove_used:
                self.board.release_stove(self._stove_used, self.cid)
                self._stove_used = ""

    # ---------------- 灭火 ----------------
    #
    # 为什么整块是新的(用户实测提出): **引擎原来对火一无所知** ——
    # `get_dyn()` 只用来读传送带方向, `op_cook` 里"着了火就失败"是**放弃**而不是**处理**。
    # 而 s_balloon_5_2 那种关卡开局就 5 处着火, 还有燃烧器持续点火, 不灭就没法做菜。
    #
    # 机制(全部来自反编译, 见 InteractDirect.SprayAction 的注释):
    #   · 触发: `ServerSprayingUtensil.OnTrigger("StartSpray"/"StopSpray")`
    #   · 命中: 以**厨师**为原点、用厨师 forward, **15° 半锥 + 4 射程 + 0.6 半径**
    #           ⇒ **必须正对**, 斜一点就浇不到
    #   · 效果: `FightFire(0.5s, dt)` ⇒ 持续喷 **0.5 秒**灭掉一个满强度的火
    #   · 副作用: 喷的时候 `MovementScale=0` —— 原地定住, 但**可以转向**
    #            拿着灭火器的人不会着火
    def fires(self) -> list:
        """场上**正在烧**的地方(世界坐标)。数据来自插件 dyn 命令。"""
        try:
            dyn = self.bridge.get_dyn()
        except Exception as e:
            self.log(f"[灭火] 读火失败: {e}")
            return []
        out = []
        for f in (dyn or {}).get("fires") or []:
            try:
                out.append((float(f.get("x") or 0), float(f.get("z") or 0)))
            except (TypeError, ValueError):
                continue
        return out

    def _fire_targets(self) -> list:
        """场上正在烧的东西 —— **带名字/类型**(灭火时要打出来)。

        为什么非要有名字(实机 2026-09-14 `s_summer_1_4`): `dyn.fires` 里既有
        **真的台面着火**(可以灭), 也有**关卡自带的火焰/烟花危险物**
        (那一关 11 处里 8 处是 `FireWorkHazard`)——
        只报坐标的话, "喷了没反应"分不清是"没对准"还是"这东西根本灭不掉",
        而这两种情形的处置完全相反(前者要调站位, 后者要换目标)。
        """
        try:
            dyn = self.bridge.get_dyn()
        except Exception as e:
            self.log(f"[灭火] 读火失败: {e}")
            return []
        out = []
        for f in (dyn or {}).get("fires") or []:
            try:
                out.append({"x": float(f.get("x") or 0), "z": float(f.get("z") or 0),
                            "name": f.get("name") or "", "type": f.get("type") or ""})
            except (TypeError, ValueError):
                continue
        return out

    def _my_player_index(self, st: dict) -> int:
        """这个厨师归属的玩家号(0=One), 给 `direct` 用。"""
        from bridge.virtual_pad import PLAYER_INDEX
        key = str(self.chef(st or {}).get("player") or "").strip().lower()
        return PLAYER_INDEX.get(key.replace("player.", ""), self.cid)

    def _find_extinguisher(self, km: KitchenMap):
        """场上的灭火器在哪 —— (持有者cid, 坐标) 或 (None, None)。"""
        for c in km.chefs:
            if is_extinguisher(c.held or ""):
                return c.id, (c.x, c.z)
        for s in km.stations.values():
            for i, o in enumerate(s.on or []):
                if is_extinguisher(o, s.tag_of(i)):
                    return None, (s.x, s.z)
        return None, None

    def extinguish(self, km: KitchenMap, st: dict, budget: float = 25.0) -> int:
        """有火就灭。返回**灭掉了几处**(0 = 没火或灭不掉)。

        流程: 拿灭火器 → 走到火**正前方** → 面向 → 喷 ~0.9s → 确认灭了。
        为什么要"正前方": 那 15° 半锥很窄, 站在斜角上按了也浇不到,
        而日志只会显示"没反应"。
        """
        import time as _t
        fires = self.fires()
        if not fires:
            self._no_ext_told = False       # 火灭了 → 下次再着火时可以重新抱怨一次
            return 0

        # ---- 1) 先弄到灭火器(拿着它自己也不会着火) ----
        _, _, held = self.pos(st)
        if not is_extinguisher(held or ""):
            owner, pos = self._find_extinguisher(km)
            if pos is None:
                # ⚠ **每次着火只抱怨一次**: 主循环每 2 秒调一次灭火, 不节流就是刷屏
                #   (而"这关没有灭火器"这件事 2 秒内不会变)。
                if not self._no_ext_told:
                    self._no_ext_told = True
                    self.log(f"[灭火] ⚠ 场上有 {len(fires)} 处着火, 但**找不到灭火器**")
                return 0
            if owner is not None and owner != self.cid:
                # 同上一句: 灭火器在人类队友手上时, 该出马的是他 —— 我们只提醒一次,
                # 然后回去做菜(反复刷"拿不到"没有任何用处)。
                if not self._no_ext_told:
                    self._no_ext_told = True
                    self.log(f"[灭火] ⚠ 灭火器在队友(P{owner + 1})手上, 拿不到 —— "
                             f"这火得他灭; 我们继续做菜")
                return 0
            if not self.navigate_smart(km, pos[0], pos[1], tight=0.8):
                self.log("[灭火] 走不到灭火器那儿")
                return 0
            if not self.interact("pickup", verify_hold_change=True):
                self.log("[灭火] 拿不起灭火器")
                return 0
            self.log("[灭火] ✓ 拿到灭火器(拿着它自己不会着火)")

        # ---- 2) 逐个灭 ----
        player = self._my_player_index(st)
        done = 0
        tried = set()          # 喷过没反应的那些(坐标取整), 免得在它身上反复耗时间
        t0 = _t.time()
        while _t.time() - t0 < budget:
            fires = self._fire_targets()
            if not fires:
                break
            cx, cz, _ = self.pos(self.state() or {})
            if cx is None:
                break
            # 从**最近**那处开始灭(走得少, 也最快止住扩散)。
            # ☠ **喷了没反应就换下一处, 不要 break**(实机 2026-09-14 `s_summer_1_4`:
            #   11 处火里有 8 处是 `FireWorkHazard`(烟花), 最近那处正好是它 ——
            #   喷一次没反应就整个放弃, 留下**真正在烧的灶台**不管, 日志还会
            #   甩一句"多半是没正对", 把方向完全带偏)。
            cands = sorted(fires, key=lambda f: (f["x"] - cx) ** 2 + (f["z"] - cz) ** 2)
            cur = next((f for f in cands
                        if (round(f["x"], 1), round(f["z"], 1)) not in tried), None)
            if cur is None:
                self.log(f"[灭火] ⚠ 场上还剩 {len(fires)} 处火, 但**都喷过没反应** —— "
                         f"多半不是灭火器能灭的(关卡自带的火焰/烟花危险物), 不再耗时间")
                break
            fx, fz = float(cur["x"]), float(cur["z"])
            fname = cur.get("name") or cur.get("type") or "?"
            # ☠ **不能直接朝火的坐标走** —— 烧着的大多是**台面**(`countertop` / `workstation_cooker`),
            #   那是**障碍格**, 朝它推就是顶着橱柜走。实机第一次测试(2026-09-14 `s_summer_1_4`)
            #   就是这么卡住的: `地形 A* 无解 → 试原生寻路` → 沿着一排台面蹭 → `超时(还差 8.0 格)`,
            #   **一次喷雾都没发出去**, 40 秒预算全烧在一个火上面。
            #   ⇒ 用 `_approach`: 先站到**能站的相邻格**(试最近 3 个), 再转身面向它 ——
            #     和"去台面旁边拿东西"走的是同一条路(它还会挑开被队友占住的格子)。
            # ⚠ **不传 `want=`**: 喷雾不是"交互键作用到某个物体", 火对象不在
            #   `CurrentInteractionObjects` 里 —— 传了会永远判 `✗ 不是它`。
            if not self._approach(km, fx, fz, tight=1.4):
                self.log(f"[灭火] ✗ 走不到火 {fname} ({fx:.1f},{fz:.1f}) 旁边 —— 换下一处")
                tried.add((round(fx, 1), round(fz, 1)))
                continue
            # `_approach` 最后已经转过头了(那 15° 锥要求必须正对); 再补一次不亏。
            self.face(fx, fz)
            self.log(f"[灭火] 对 {fname} ({fx:.1f},{fz:.1f}) 喷 0.9s "
                     f"(需 0.5s 灭一个满强度的火)")
            try:
                # 发**小写** `spray`。曾经这里发的是全大写 `SPRAY` —— 那是为了绕开
                # 当年诊断命令也叫 `spray` 的子串撞车(诊断现已改名 `sprayinfo`, 不需要了)。
                # ⚠ 更糟的是: C# 那边分派用 `ToLowerInvariant()` 而比较用大小写敏感的
                #   `== "spray"`, 所以**大写 SPRAY 实际执行的是"停喷"** ——
                #   于是这个"绕过"把灭火变成了"两次停喷"(已修, 见 InteractDirect)。
                r = self.bridge.direct("spray", player=player)
            except Exception as e:
                self.log(f"[灭火] ✗ 开喷失败: {e}")
                break
            if not r.get("ok"):
                self.log(f"[灭火] ✗ 开喷被拒: {r.get('error')} —— 手上是不是没有灭火器?")
                break
            _t.sleep(0.9)
            try:
                self.bridge.direct("unspray", player=player)
            except Exception:
                pass
            left = len(self.fires())
            if left < len(fires):
                done += 1
                # ⚠ **不清 `tried`**: 试过没反应的(烟花那类)再试一次还是没反应,
                #   而每次都要走过去 + 喷 0.9s。真想重试的场合是**下一次调用**
                #   ——`run()` 每 2 秒调一次 `extinguish`, 那时 `tried` 是空的。
                self.log(f"[灭火] ✓ {fname} 灭了(还剩 {left})")
            else:
                self.log(f"[灭火] ⚠ {fname} 喷了没反应(火数没变) —— "
                         f"记下它, 换下一处(喷不灭的多半是关卡自带的火焰/烟花)")
                tried.add((round(fx, 1), round(fz, 1)))

        # ---- 3) 手上有灭火器的话保持拿着(它能防火), 不主动放下 ----
        return done

    # ---------------- 锅 ----------------
    def _pick_stove(self, km: KitchenMap, x: float, z: float, want_pot: bool,
                    claim: bool = True, ing: str = ""):
        """挑一个灶台。want_pot=True 只挑"灶上已经有锅"的; False 只挑灶上没有锅的。

        `claim=False` —— **只读模式**: 算出来但不在黑板上占位(评分层替队友评估时用,
        那种调用绝不能以我的 cid 把队友本来能用的灶台抢走)。

        ☠☠ `ing` —— **要煮的这个材料名**。它把"**按菜谱选灶台**"接进来了:
          只用 `want_pot` + "离我最近"挑, 会挑中"**别人的菜该用的那口锅**"。
          实测(2026-09-15 `s_mine_2_5`): 米被拿到**煮切过的肉**那口平底锅上,
          游戏当场拒收(`placeCanHandle=false`), 三次重试后整单报废。
          用户原话: "**灶台是煮切过之后的肉的, 但是脚本并没有按菜谱来**"。

          权威判据(**问游戏**, 规则 1/2 —— 不是我们猜的规矩):
            `CookableContainer.cs:46-47`
                var cp = _object.RequestComponent&lt;CookableProperties&gt;();
                if (cp == null || !cp.AllowsCookingStep(_handler.AccessCookingType)) return false;
            `CookableProperties.cs:11-13` `AllowedCookingSteps` 比的是 `CookingStepData.m_uID`
          ⇒ ↔ **食材的 `cook_steps` 里有没有这个容器的 `cook_id`**。
            `SceneScanner.ScanCooking` 报 `cookId`(`CookingHandler.m_cookingType.m_uID`),
            `ItemKnowledge.One` 报食材的 `cookSteps`(见那两处的注释)。
          ⚠ **不知道就放行**(老 dll 没这两个字段 / 这食材没有 `CookableProperties`)
            ⇒ 退回"离我最近"那条老路 —— 绝不能把"读不到"变成"哪口锅都不能用"。
          ⚠ 判据用**这个食材自己的** `cook_steps`, 所以**必须传 `ing`**;
            不传 = 老行为(向后兼容, 离线桩和手工调都还走得通)。

        为什么要分开 want_pot(用户实测要求: "不要把锅拿走"):
          · 米饭这类"容器煮"的菜只能进**锅**; 手拿米饭对空灶台按交互是放不上去的 ——
            表现就是"按了没反应"。
          · 反过来, 自带 CookingHandler 的食材直接放灶台, 塞进锅反而被拒。
        占用判据必须用 Cooking.busy: 锅架在灶上时**空锅也一直有 CookingHandler**(进度 0),
        旧代码用 `cooking_on(s) is not None` 判占用, 会把所有带锅的灶台都当成"正在煮",
        一个都用不了。
        """
        #: 这个材料允许的加热方式; 空 = 不知道 ⇒ 一律放行
        _allow = None
        if ing and self.know is not None:
            for it in getattr(self.know, "items", []) or []:
                if self._norm(getattr(it, "ing", "")) == self._norm(ing):
                    _allow = list(getattr(it, "cook_steps", []) or [])
                    break
        for sem in COOK_SEMS:
            for s in km.sorted_by_dist(sem, x, z):
                pot = s.pot_name()
                if want_pot and not pot:
                    continue
                if (not want_pot) and pot:
                    continue
                ck = km.cooking_on(s)
                if ck is not None and ck.busy:
                    continue
                # ☠ **"这口锅是给这道菜用的吗"** —— 只有两边都知道答案时才拦。
                #   不知道(id 为 0 / 食材没报 cook_steps)⇒ 放行, 退回老行为。
                if want_pot and _allow and ck is not None and ck.cook_id:
                    if ck.cook_id not in _allow:
                        continue
                if self.board is not None:
                    # claim=False: 只读地问"这灶台我用得了吗", 不占位
                    owner = self.board.stove_owner(s.id)
                    if owner is not None and owner != self.cid:
                        continue
                    if claim:
                        self.board.claim_stove(s.id, self.cid)
                return s, pot, ck
        return None, "", None

    def _find_pot_with(self, km: KitchenMap, x: float, z: float, target: str,
                       claim: bool = True):
        """找一口**锅里已经有目标食材**的锅(自己上一步放的, 或别人/上一轮留下的)。

        为什么必须有这条: "把米饭放进锅"和"等它熟"之间隔着一次取盘子。如果那一步失败,
        重试时锅已经是"有东西"的状态 —— 只认空闲锅的 _pick_stove 会认为"没有灶台了",
        于是整单卡死, 而锅里的米饭继续煮到焦。正确做法: 认出"锅里就是我刚才放的东西",
        接着用这口锅往下走(熟了就直接取, 没熟就继续等)。
        """
        want = self._norm(target)
        for sem in COOK_SEMS:
            for s in km.sorted_by_dist(sem, x, z):
                ck = km.cooking_on(s)
                if ck is None or not ck.is_pot or not ck.inside:
                    continue
                if want and want not in self._norm(ck.inside):
                    continue
                if self.board is not None:
                    owner = self.board.stove_owner(s.id)
                    if owner is not None and owner != self.cid:
                        continue
                    if claim:
                        self.board.claim_stove(s.id, self.cid)
                return s, ck
        return None, None

    def _pot_now(self, stove: Station):
        """重新读一次这口锅的实时状态(Cooking 或 None)。"""
        st = self.state()
        km = self.map(st) if st else None
        return km.cooking_on(stove) if km else None

    def _empty_plate_source(self, km: KitchenMap, x: float, z: float, want_type: str):
        """找"空盘子"的来源。

        优先级: ① 盘子堆里对得上订单容器类型的(干净, 类型必对)
                ② 台面上放着的**空**盘(最近)
                ③ 任意盘子堆
        为什么必须空: 装了菜的盘子拿到锅边按交互, 走的是"把手上容器的内容倒进目标"
        那条分支(ServerPlacementContainer 反向分支), 结果是把菜倒进锅里, 正好反了。
        """
        t = self._norm(want_type) if want_type else ""
        cand = []          # (优先级, 距离, 台子)
        for s in km.of("plates"):
            ok = bool(t) and self._norm(s.plate) == t
            cand.append((0 if ok else 2, (s.x - x) ** 2 + (s.z - z) ** 2, s))
        for s in km.stations.values():
            # ☠☠ **脏盘子堆不是"空盘来源"**(2026-09-15 `s_sushi_1_3` 实机):
            #   原来这里只判 `empty_plate_names()` 非空, 而脏盘堆那条 `on` 里的
            #   整叠脏盘既"看着像盘子"又"内容是空的" ⇒ 被选成空盘来源。
            #   日志: 组装台面那盘已经有米 ⇒ "改找空盘" ⇒ **去了 `dirty_plates0`**
            #   ⇒ 手上攥着 `DirtyPlateStack` ⇒ 进不了盘 ⇒ 丢脚下 ⇒ `盘里=空`
            #   ⇒ `assemble`/`deliver` 全灭 ⇒ 锅里那份米**烧糊**。
            #   用户原话: "**脏盘不会识别并拿去洗**"。
            #   ⇒ 脏盘归**洗盘子**那条路(`_chore_candidates` ② 认 `dirty_plates`), 不归这条。
            if (s.id or "").startswith("dirty_plates"):
                continue
            if s.empty_plate_names():
                cand.append((1, (s.x - x) ** 2 + (s.z - z) ** 2, s))
        if not cand:
            return None
        cand.sort(key=lambda r: (r[0], r[1]))
        return cand[0][2]

    def _get_plate_for_pot(self, km: KitchenMap, x: float, z: float, want_type: str,
                           taking: str = "") -> bool:
        """准备一个"去锅里取菜"用的盘子。

        顺序: 手上已有盘子 → 组装台面那个已经装菜的盘子(首选) → 台面上的空盘。

        为什么首选组装台面那盘: 海带这类前面已经摆进盘里的食材, 必须和煮好的米饭
        进**同一个**盘子。拿一个空盘去锅里取, 要么另成一盘拼不起来, 要么交互直接
        不生效(实测: 手拿空盘对锅按交互, 饭没出来)。

        `taking` —— 这一步**要从锅里取出来的那份**(如 `'SushiRice'`)。
        ☠☠ **盘里已经有它了就不能再用这只**(用户 2026-09-15 指出:
          > "已装菜的那盘, 但是**已经装过米的盘子没有过滤掉**?!")
          上面那条"首选已装菜那盘"讲的是**海带**——别的组件进同一个盘子是对的;
          可"**同一份料的重复**"完全是另一回事: 拿一只已经装着米的盘子再去锅里
          装米, 盘里会变成两份米。
          ⚠ 这一条**不能靠游戏拦**: `ServerPlate.CanAddIngredient` 恒返回 true
            (`ServerPlate.cs:23-26`) ⇒ **盘子那一侧从不拒绝**, 拦不拦得住得看
            `CanCombineWithContents` 按容器能力分派的那条路 —— 不赌它。
          ⇒ 我们这边先过滤掉: **盘里已经有 `taking` 的那只, 不选**。
        ☠ **手上那只也要查**(2026-09-15 补): 原来这里写着"手上那只读不到内容, 只能照旧放行"
          —— **那句已经过期**。插件从 2026-09-15 起报 `heldhas`(手上容器里装了什么),
          `_held_contents` 就是读它的, `_deliver_plate` 一直在用同一条核对。
          漏掉的后果实测过: 拿一只**装着米**的盘子去锅里接米, 按 3 次锅一动不动, 整步判失败
          (用户原话: "主要是脚本用带米的盘子取接米, 那确实接不下来")。
        """
        _st = self.state() or {}
        _, _, held = self.pos(_st)
        if held and self._is_plate(held) and taking \
                and self._norm(taking) in self._held_contents(_st):
            # ☠☠ **手上这只盘里已经有这份料了 ⇒ 拿它去接同一份, 游戏不会接**。
            #   用户 2026-09-15 实机指出的就是这一条:
            #     > "主要是脚本用**带米的盘子**取接米, 那确实接不下来"
            #   和上面"组装台面那盘已经有它了"是**同一条规矩**, 只是那次查的是
            #   **台面上**的盘(`_plate_contents_on`), 这次查**手上**的盘(`heldhas`)——
            #   原来注释写"手上那只读不到内容, 只能照旧放行", 那句**已经过期**了:
            #   `_held_contents`(插件 `heldhas`)从 2026-09-15 起就能读, `_deliver_plate`
            #   一直在用它核对"手上这盘是不是这道菜"。这里漏了同一道核对。
            #   ⚠ 代价不是白按一下: 实测那局连着按 3 次锅**一动不动**, 然后整步判失败。
            #   ⇒ 处置也不是认输: **先把这盘放到空台面**(盘里有菜, 别丢地上),
            #     腾出手再按正常流程去拿一只干净的。
            self.log(f"[步骤] ⚠ 手上这只盘里**已经有 {taking}** 了"
                     f"(盘里={sorted(self._held_contents(_st))}) —— 拿它去锅边接同一份, "
                     f"游戏不会接; 先把它放到空台面, 再去拿一只干净的")
            if not self._put_down_plate(km, x, z):
                return False
            _, _, held = self.pos(self.state() or {})
        if held:
            if self._is_plate(held):
                self.log(f"[步骤] 手上已经端着盘子 {held!r}, 直接用它去锅里取")
                return True
            self.log(f"[步骤] ⚠ 手上有 {held!r} 不是盘子, 没法去锅里取菜")
            return False
        spot = self._spot_now(km) or self.pick_assemble_spot(km, x, z)
        # ☠☠ **按 id 到新鲜的 `km` 里重取一次**(`_spot_now` 就是干这个的) —— `self.assemble_spot` 是**几百毫秒前的
        #   快照**(同文件 `:1698` 与 `_top_up_plate` 的 `:4923` 都记着这条;
        #   那边写的就是"用新鲜的, 别用陈旧快照"), 而这一格上那盘**随时可能没了**。
        #   实机 2026-09-15 `s_sushi_1_3` 的**整条死因链**:
        #     `去组装台面 counter4 拿已装菜的盘子` → 走到那儿
        #     → `直调 pickup 失败: 没有可取的目标(站位不对: 身边没有可拾取物/可放置面)`
        #     → `⚠ 没有盘子可取菜 —— 停在这里, 锅不动` ×3 → `✗ 放弃: cook SushiRice`
        #     → **锅里的饭就一直烧着** → 后面 `rescue` 连着 4 轮**赢分**(113/120)
        #       却`走不到 utensil_pot_01 旁边` → 冷板凳 20 秒 → 回来还是死路 → **整单报废**。
        #   ⇒ 快照说"那盘在", 走到跟前游戏说"身边没有可拾取物" —— 就是这一条。
        #   复核不过就退回"去盘子堆拿一只干净的"(`_empty_plate_source`), 别硬按。
        if spot is not None:
            spot = km.stations.get(getattr(spot, "id", "")) or spot
        src = None
        used_assemble = False
        if spot is not None and self._has_plate(spot):
            _have = self._plate_contents_on(spot)
            if taking and self._norm(taking) in _have:
                # 盘里已经有这份料了 ⇒ 再用它去取同一份 = 装两份米(见 docstring)。
                self.log(f"[步骤] ⚠ 组装台面 {spot.id} 那盘里**已经有 {taking}** 了"
                         f"(盘里={sorted(_have)}) —— 不能再用它取同一份, 改找空盘")
            else:
                self.log(f"[步骤] 去组装台面 {spot.id} 拿已装菜的盘子, 用它从锅里取菜")
                src = spot
                used_assemble = True
        if src is None:
            src = self._empty_plate_source(km, x, z, want_type)
        if src is None:
            self.log("[步骤] 全场找不到可用的盘子(取菜必须用盘子)")
            return False
        if used_assemble:
            how = "取组装台面那盘"
        elif src.id.startswith("plates"):
            how = "从盘子堆取一个干净空盘"
        else:
            how = "取台面上那个空盘"
        self.log(f"[步骤] 去 {src.id}({how}), 用它从锅里取菜")
        if not self.navigate_smart(km, src.x, src.z, tight=0.8):
            return False
        if not self.interact("pickup", verify_hold_change=True):
            return False
        _, _, got = self.pos(self.state() or {})
        if not self._is_plate(got):
            self.log(f"[步骤] ⚠ 拿到的不是盘子({got!r})")
            return False
        return True

    def _cook(self, km, x, z, op: Op, st: dict, flow: DishFlow = None) -> bool:
        # ☠ **手上有什么必须现读** —— 传进来的 `st` 可能是上一拍的快照。
        #   实测(2026-09-15 `s_mine_2_5`): 日志上一行刚 `[预置] ✓ 捡回来了 Rice`,
        #   下一行就是 `[步骤] 手上没东西可煮` ⇒ **白费一次重试**(而重试次数有限,
        #   三次就 `✗ 放弃` 整单)。用户那条规矩在这儿同样适用:
        #   "每次都用最新的……手上的盘、盘里的内容"。
        #   ⚠ `state(force=True)` 不吃 TTL —— 这一段判的就是"此刻手上有没有"。
        _stn = self.state(force=True)
        _, _, held = self.pos(_stn or st)
        need = op.wait or 0.0
        want_pot = bool(getattr(op, "in_pot", False))
        plate_type = (flow.plate if flow is not None else "") or ""
        already = False        # 锅里已经有我要煮的东西了(自己上一步放的)

        # 0) 锅里已经有目标食材 → 接着用它(熟了就直接取; 没熟就继续等)。
        #    这条同时兜住"上一步取盘子失败"的重试: 锅不会因为"有东西"而被判成不可用。
        stove = pot = None
        if want_pot:
            stove, ck = self._find_pot_with(km, x, z, op.target)
            if stove is not None:
                already = True
                pot = stove.pot_name()

        # 1) 选灶台
        if stove is None:
            stove, pot, _ = self._pick_stove(km, x, z, want_pot, ing=op.target)
        if stove is None and want_pot:
            self.log("[步骤] ⚠ 没有『灶上放着锅』的灶台, 退回直接放灶台(可能放不上去)")
            want_pot = False
            stove, pot, _ = self._pick_stove(km, x, z, False, ing=op.target)
        if stove is None:
            self.log("[步骤] 没有找到可用的灶台")
            return False
        self._stove_used = stove.id

        if already:
            self.log(f"[步骤] 灶台 {stove.id} 上的锅 {pot!r} 里已经有 {op.target}(接着用它)")
        elif want_pot:
            self.log(f"[步骤] 去 {stove.id} 煮 {op.target} —— 用灶上那口锅 {pot!r}"
                     + (f" (需 {need:.0f}s)" if need else "")
                     + "; 取菜时用盘子, 锅不动")
        else:
            self.log(f"[步骤] 去 {stove.id} 煮 {op.target}"
                     + (f" (需 {need:.0f}s)" if need else ""))

        # 2) 走过去, 把手上的生料放进锅 / 放上灶台
        if not already:
            if not held:
                self.log("[步骤] 手上没东西可煮")
                return False
            if not self.navigate_smart(km, stove.x, stove.z, tight=0.8):
                return False
            # ⚠ **按之前先问游戏"你会放到哪"**(用户实测: "锅的定位不是很好")。
            #   站位不对时 m_iHandlePlacement 会指向旁边一个无关台面, 而直调照样回
            #   ok=True —— 东西放上了柜台, 引擎却以为进锅了, 后面全错。
            #   宁可这一步失败(execute 会重试, 每次重新导航 = 再给一次机会),
            #   也不要放错地方还报成功。
            ok_place, who = self._place_target_ok(stove, pot, want_pot)
            if not ok_place:
                self.log(f"[步骤] ⚠ 站位不对: 游戏说会放到 {who!r}, 而不是 "
                         f"{stove.name!r}" + (f" / 锅 {pot!r}" if (want_pot and pot) else "")
                         + " —— 不按, 免得放错地方还报成功")
                return False
            if not self.interact("pickup", verify_hold_change=True):   # 手上的东西必须脱手
                self.log("[步骤] ⚠ 东西没放上去(锅/灶台没接住)")
                return False
            time.sleep(0.4)

        # 3) 要锅的菜: 趁煮的时候去拿盘子(手空了才拿得动), 回来正好取菜。
        #    必须在"煮好之前"拿到 —— 焦了就上不了盘。
        if want_pot:
            if not self._get_plate_for_pot(km, x, z, plate_type, taking=op.target):
                self.log("[步骤] ⚠ 没有盘子可取菜 —— 停在这里, 锅不动")
                return False

        # 4) 盯着进度: 直到状态变 Cooked(刚熟) 立刻取下; 着了火就失败
        t0 = time.time()
        limit = (2.0 * need + 6.0) if need else 60.0
        while time.time() - t0 < limit:
            if not self.round_active():
                return False
            st2 = self.state()
            km2 = self.map(st2) if st2 else None
            ck = km2.cooking_on(stove) if km2 else None
            if ck is not None:
                what = ck.inside or ck.ing or ck.name
                if ck.burning:
                    self.log(f"[步骤] {op.target} 烧起来了! ({what})")
                    break
                if ck.ready:
                    self.log(f"[步骤] {op.target} 刚熟({what} prog={ck.prog:.1f}/{ck.need:.1f}), 立刻取下")
                    break
                self.log(f"[步骤] 煮中 {what} {ck.state} {ck.prog:.1f}/{ck.need:.1f}")
            time.sleep(0.5)
        else:
            self.log(f"[步骤] 煮超时({limit:.0f}s), 放弃")
            return False

        # 5) 取下来
        if want_pot:
            # 手拿盘对锅按交互 → 锅里的东西进盘子, **锅留在灶上**
            return self._take_from_pot(stove, op)
        if not self.navigate_smart(km, stove.x, stove.z, tight=0.8):
            return False
        return self.interact("pickup", verify_hold_change=True)

    def _take_from_pot(self, stove: Station, op: Op) -> bool:
        """用盘子从锅里取菜。**锅不动、不拿走**。

        依据(反编译 + s_sushi_1_3 实测组件清单确认):
          · 锅身上只有 ServerCookableContainer 一个 IContainerTransferBehaviour
            (实测组件: CookableContainer+CookingHandler+ServerCookableContainer,
             没有 PreparationContainer/ServerPreparationContainer —— 所以锅既不会
             "把自己倒出去然后消失", 也不会被消耗)。
          · 手拿盘对着锅(或锅所在的灶台)按交互 →
            ServerAttachStation.HandlePlacement → PlacementType.OntoOccupant
            → 锅的 ServerPlacementContainer.HandlePlacement:
                 CanCombine(盘子)=false → 反向分支
                 containerTransferBehaviour2 = ServerCookableContainer
                 → CanTransferToContainer(盘子的容器)
                    = AssembledNodeTransfer.CanTransferFromContainer(锅, 盘子)
                      要求进度 == 0 或 **>= AccessCookingTime(必须全熟)**
                 → TransferToContainer(null, 盘子容器, _dontRemove:false)
                    → 锅的 ServerIngredientContainer.Empty()  ← 锅留下, 内容清空
        """
        before = self._pot_now(stove)
        if before is not None and not before.busy:
            self.log(f"[步骤] 锅 {before.name!r} 已经是空的, 没什么可取")
            return False
        for k in range(3):
            st = self.state()
            km = self.map(st) if st else None
            if km is None:
                return False
            if not self.navigate_smart(km, stove.x, stove.z, tight=0.8):
                return False
            # 朝向**锅**的实时位置, 而不是灶台中心 —— 锅架在灶台某个挂点上,
            # 朝灶台中心可能正好背对锅, m_iHandlePlacement 就判不到锅。
            fx = before.x if (before is not None and before.x) else stove.x
            fz = before.z if (before is not None and before.z) else stove.z
            self.face(fx, fz)
            if not self._align_for_place(stove):   # 手拿盘对锅取菜: 别判到旁边台子
                continue
            st0 = self.state(force=True)
            placeh = (self.chef(st0) or {}).get("placeh") or ""
            # ⚠ **把"手上这盘里装了什么"一起打出来** —— 实机那局连着 3 次"锅一动不动"
            #   却看不出盘里有没有东西(用户是从**屏幕上**看到那只盘里已经有米饭的)。
            #   这一行是唯一能离线定案的证据, 别省。
            self.log(f"[步骤] 手拿盘子对 {stove.id} 按交互取菜(第 {k+1} 次, "
                     f"游戏放置目标={placeh!r}, 手上这盘里="
                     f"{sorted(self._held_contents(st0))}) —— 锅不动")
            # ⚠ 这里**不能**用 verify_hold_change: 取菜前后手上都是那个盘子(名字没变),
            #   靠"持有物变了"判成功只会误判成失败然后重按 —— 而重按可能把菜放回去。
            self.interact("pickup", verify_hold_change=False)
            time.sleep(0.5)
            after = self._pot_now(stove)
            if after is None or not after.busy:
                _, _, held = self.pos(self.state() or {})
                if not self._is_plate(held):
                    self.log(f"[步骤] ⚠ 取完菜手上却不是盘子({held!r}) —— 检查是否误拿了锅")
                    return False
                self.log(f"[步骤] ✓ 锅里的菜已经到盘子上, 锅仍在 {stove.id} 上")
                return True
            self.log(f"[步骤] 锅里还有 {after.inside or after.name!r}({after.state}), 再试")
        self.log("[步骤] ✗ 从锅里取菜失败(锅还满着) —— 没拿走锅是对的, 但菜没出来")
        return False

    def _is_plate(self, name: str) -> bool:
        return "plate" in self._norm(name or "")

    def _free_counter(self, km: KitchenMap, x: float, z: float):
        """找一个**空台面**(能放下多余盘子的普通台面)。"""
        free = [s for s in km.of("counter")
                if not s.on and not s.spawn and s.kind != "CookingStation"]
        if not free:
            return None
        return min(free, key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2)

    def _put_down_plate(self, km: KitchenMap, x: float, z: float) -> bool:
        """手上多出一个盘子(比如从锅里取完菜、菜已经并进台面那盘)时, 先把手腾出来。

        为什么需要: 摆盘位那个台面上已经有一个盘子时, 手上这盘菜会**并进它**
        (ServerPlate.TransferToContainer → CombineWithContents, 然后把自己 Empty),
        手上于是剩下一个**空盘**; 端着空盘去送餐口是送不出东西的。
        """
        spot = self._free_counter(km, x, z)
        if spot is None:
            self.log("[步骤] 找不到空台面放多余的盘子")
            return False
        self.log(f"[步骤] 手上还端着盘子, 先放到空台面 {spot.id} 上, 把两手腾出来")
        if not self.navigate_smart(km, spot.x, spot.z, tight=0.6):
            return False
        return self.interact("pickup", verify_hold_change=True)

    def _drop_held(self, st, held: str) -> bool:
        """**把手上的东西丢在脚下** —— 并且**记下它掉在哪**(`_preposed`)。

        ☠☠ **为什么要记**(2026-09-15 `s_sushi_1_2` 实机打回来的一条**断链**):
          `chop X` 做完 ⇒ `_drop_subsumed_fetches` 把同名的 `fetch X` **销了号**
          (它的前提是"做完了, 料就在手上") ⇒ 紧接着腾手把那份料**丢在脚下** ⇒
          **手上空了, 而 `fetch X` 已经不存在了** ⇒ 这一单的 `pending` 里再也没有 `fetch`
          ⇒ `_chain_dead` 判"**链子断了**" ⇒ **整单作废**。
          `_redos` 也救不了: 它只看 pending 里的 `chop`/`cook`/`mix`, 而 `chop X` 已经做完了。
          ⇒ 记进 `_preposed` 之后, 下游那一步靠 `_preposed_ok` 的捷径变回**可做**,
            `do_op` 会自己走过去把它捡回来 —— **这正是 `_preposed` 存在的意义**
            ("我把某份料放在哪了"), 以前只有"主动丢给下一步"(`_prepose`)才写它,
            而"腾手丢下"这条**漏了**。
        ⚠ 落点是**厨师脚下**(`ReceiveTakeEvent(target=null)` → `carrier.TakeItem()`),
          所以拿当前位置记就够 —— 不用真去读地上的东西。
        """
        cx, cz, _ = self.pos(self.state(force=True) or {})
        try:
            self.bridge.direct("take", player=self._my_player_index(st))
        except Exception as e:                                       # noqa: BLE001
            self.log(f"[步骤] 丢下失败: {e!r}")
            return False
        time.sleep(0.35)
        if cx is not None and held:
            k = self._norm(held)
            if k:
                self._preposed[k] = (cx, cz, time.time())
                self.log(f"[步骤] 丢下的 {held} 记在脚下 ({cx:.1f},{cz:.1f}) "
                         f"—— 回头要它时按预置去捡")
        return True

    def _has_plate(self, s: Station) -> bool:
        """台面上有没有盘子。优先用游戏自己的 Unity Tag(Plate), 名字只做兜底。"""
        for i, o in enumerate(s.on or []):
            if is_plate(o, s.tag_of(i)):
                return True
        return any("plate" in self._norm(o) for o in (s.on or []))

    def _held_contents(self, st) -> set:
        """**手上那件容器里装了什么** —— 插件读的 `heldhas`(见 `SceneScanner.ReadHeldItems`)。

        ☠☠ 2026-09-15 之前**根本读不到**: 插件只报 `held`(名字), 于是"空盘"和"装好的菜"
          在引擎眼里**一模一样**。用户实机描述的现象正是这个盲区的后果:
            > "脚本拿着食材去盘子那, 放上盘子**被我拿着并且重新放一个空盘子**,
            >  脚本会拿着**空盘子**去提交。"
          ⇒ 现在 `_deliver_plate` 能核对"手上这盘里是不是这道菜"了。

        ⚠ 归一化用**和 `_plate_contents_on` 同一套**(`+` 分隔 + `_norm`) ——
          同一个 `ContentsNames` 产出, 别在这里换一种切法。
        ⚠ **空串有两种含义**(内容为空 / 读不到), 所以调用方要判
          **键在不在**(旧 dll 没有这个字段 ⇒ 放行), 别用"空不空"当判据。
        """
        h = (self.chef(st) or {}).get("heldhas") or ""
        return {self._norm(p) for p in h.split("+") if p.strip()}

    def _plate_contents_on(self, s: Station) -> set:
        """台面上那个盘子里装了什么(插件读的 onhas)。用来判断"并盘到底成功没有"。"""
        out = set()
        for i, o in enumerate(s.on or []):
            if not is_plate(o, s.tag_of(i)):
                continue
            for part in (s.has_of(i) or "").split("+"):
                if part.strip():
                    out.add(self._norm(part))
        return out

    def _ensure_plate(self, km: KitchenMap, x: float, z: float, spot: Station) -> bool:
        """摆盘位上一个盘子都没有时, 才真去拿一个放上来(正常情况台面上本来就有)。

        ⚠ **来源有三处, 别只数两处**(2026-09-15): 原来是"台面上放着的" + "干净盘子堆",
           **漏了"地上的盘子"** —— 而**地上的盘子是我们自己造出来的**(腾手时把东西
           丢在脚下、放置失败掉在台面/地上)。漏掉它 ⇒ 一盘躺在地上、却没有一个"台面上的",
           就报"全场找不到盘子" ✗。
          `_find_ground_item` 返回的本来就是个**可导航的取货目标**(它自己有 id/坐标),
          所以三种来源可以直接按距离比 —— 不必分等级。
        """
        from collections import OrderedDict as _OD
        cands = _OD()
        for s in (self._find_item_station(km, "Plate", x, z),
                  self._find_ground_item(km, "Plate", x, z,
                                         tm=self.terrain(), reach=None),
                  min(km.of("plates") or [], key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2,
                      default=None)):
            if s is not None:
                cands[getattr(s, "id", id(s))] = s
        if not cands:
            self.log("[步骤] 全场找不到盘子(台面上/地上/盘子堆都没有)")
            return False
        src = min(cands.values(), key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2)
        self.log(f"[步骤] 摆盘位 {spot.id} 没盘子, 去 {src.id} 拿一个")
        if not self.navigate_smart(km, src.x, src.z, tight=0.8):
            return False
        if not self.interact("pickup", verify_hold_change=True):
            return False
        if not self.navigate_smart(km, spot.x, spot.z, tight=0.6):
            return False
        return self.interact("pickup", verify_hold_change=True)

    def op_press(self, km, st, op: Op = None) -> bool:
        """按一个**此刻可按**的机关按钮。

        ⚠ 按钮的权威来源是 **`dyn.buttons`**, 不是台面表:
          `InteractiveScan` 扫的是 `SwitchStation` / `ToggleSwitch` / `PressureSwitch`
          三种组件(`InteractiveScan.cs:33`), 而 `map_model._KIND_SEM` **只映射了
          `switchstation`** —— 另外两种在台面表里**压根查不到**。
          **查不到不等于没有**, 所以这里直接读 dyn。
          `pressable` = 那一刻游戏说它可交互(`Interactable.enabled`)。

        按键用 `chop`(切/交互): 游戏那边它对应"使用" —— `InteractDirect` 的 `use`
        分支会**同时**发 `ReceiveInteractEvent` + `ReceiveTriggerInteractEvent`,
        而电锯/上菜铃那类靠的正是后者。
        """
        btns = [b for b in (self._dyn().get("buttons") or []) if b.get("pressable")]
        if not btns:
            return False
        # **按 op 指的那颗按**(评分阶段探测的就是它) —— 否则会"按 A 台算的分、
        # 走到 B 台去干"。探测到的那颗现在按不动了就直接不做, **不要换别的**:
        # 换一颗等于执行了一个评分时没算过的动作。
        if op is not None and op.at_name:
            same = [b for b in btns if (b.get("name") or "") == op.at_name]
            if not same:
                self.log(f"[机关] 探测时那颗按钮({op.at_name})现在按不动了 —— 不做")
                return False
            btns = same
        cx, cz, _ = self.pos(st)
        if cx is None:
            return False
        b = min(btns, key=lambda t: (float(t.get("x") or 0) - cx) ** 2
                + (float(t.get("z") or 0) - cz) ** 2)
        bx, bz = float(b.get("x") or 0), float(b.get("z") or 0)
        self.log("[机关] 去按 %s @(%.1f,%.1f)" % (b.get("type") or "?", bx, bz))
        if not self._approach(km, bx, bz, want=(b.get("name") or "")):
            self.log("[机关] 走不到按钮旁边")
            return False
        if not self.interact("chop", verify_hold_change=False):
            return False
        self.log("[机关] ✓ 按了 %s" % (b.get("type") or "?"))
        return True

    def op_wash(self, km, st, budget: float = 20.0, op: Op = None) -> bool:
        """洗盘子 —— **两步机制**(反编译 `WashingStation` + `ServerWashingStation`):

          ① **把整叠脏盘放到洗手池上**: `WashingStation.CanHandlePlacement` 要求
             手上是 `DirtyPlateStack`; 放下后**盘子叠被销毁**、`m_plateCount += size`
             (`ServerWashingStation.HandlePlacement`)
          ② **在洗手池按住交互键**: `UpdateSynchronising` 里每
             `m_cleanPlateTime`(2 秒)洗好**一个**, 洗好的走
             `m_plateReturnStation.ReturnPlate()` ⇒ 出现在**干燥台**(PlateReturnStation)

        ⇒ 可观测的终点是"**干燥台上的盘子变多**" —— 洗手池自己看不到进度。
        """
        from bridge.keyboard_input import key_down, key_up
        _, _, held = self.pos(st)
        if held:
            self.log(f"[洗盘] 手上有 {held}, 先腾出手")
            return False
        # ① 端一叠脏盘子
        stacks = [s for s in km.of("dirty_plates") if int(getattr(s, "n", 0) or 0) > 0]
        if not stacks:
            return False                       # 没有脏盘子可洗 → 这件杂活不成立
        # **按 op 指的那一叠端**(评分阶段探测的就是它), 同 `op_press`;
        # 那一叠空了就返回 False, **不要换一叠**(换了等于执行没算过的动作)。
        if op is not None and op.at_name:
            same = [s for s in stacks if s.name == op.at_name]
            if not same:
                self.log(f"[洗盘] 探测时那叠脏盘子({op.at_name})现在没了 —— 不做")
                return False
            stacks = same
        cx, cz, _ = self.pos(st)
        s0 = min(stacks, key=lambda s: (s.x - (cx or 0)) ** 2 + (s.z - (cz or 0)) ** 2)
        self.log("[洗盘] 去端脏盘子 %s(%d 个) @(%.1f,%.1f)"
                 % (s0.id, int(s0.n), s0.x, s0.z))
        if not self._approach(km, s0.x, s0.z, want=s0.name):
            self.log("[洗盘] 走不到脏盘堆")
            return False
        if not self.interact("pickup", verify_hold_change=True):
            self.log("[洗盘] 拿不起脏盘子")
            return False
        # ② 放到洗手池
        sinks = km.of("wash")
        if not sinks:
            self.log("[洗盘] 这关没有洗手池")
            return False
        sk = sink = min(sinks, key=lambda s: (s.x - s0.x) ** 2 + (s.z - s0.z) ** 2)
        before = sum(int(getattr(d, "n", 0) or 0) for d in km.of("return_plates"))
        self.log("[洗盘] 送到洗手池 %s @(%.1f,%.1f)" % (sk.id, sk.x, sk.z))
        if not self._approach(km, sk.x, sk.z, want=sk.name):
            self.log("[洗盘] 走不到洗手池")
            return False
        self.interact("pickup", verify_hold_change=True)   # 放下盘子叠
        # ③ **按住**交互键洗 —— 每 2 秒一个, 用"干燥台的盘子数"当进度条
        key = self.kb.b.get("pickup")
        if not key:
            return False
        t0 = time.time()
        try:
            key_down(key)
            while time.time() - t0 < budget:
                time.sleep(0.5)
                km2 = self.map(self.state(force=True) or {}) or km
                now = sum(int(getattr(d, "n", 0) or 0) for d in km2.of("return_plates"))
                if now > before:
                    self.log("[洗盘] ✓ 洗好 %d 个(干燥台上 %d → %d)"
                             % (now - before, before, now))
                    return True
        finally:
            key_up(key)
        self.log("[洗盘] 按了 %.0fs, 干燥台没见新的干净盘子" % (time.time() - t0))
        return False

    #: `op_assemble` 里"**干放**(台面没盘子)算不算这一步做成"。
    #:
    #: 默认 **0 = 不算** —— `assemble` 的语义是**把材料放进盘子**, 台面上没盘子时
    #: 材料只会落在光台面上, 根本没进任何盘子; 报成功就是**假 ✓**, 会让这一步销号,
    #: 紧接着 `_execute_scored` 的"链子断了"判据成立 ⇒ **整单作废**
    #: (实机 `s_sushi_1_1` 两单都死在这儿, 第二单吃掉剩下九成时间)。
    #: 现在改成"换一个摆着盘子的台面"。
    #:
    #: `NEKO_DRY_PLACE=1` 回到旧行为(干放照旧报 ✓) —— 留这个开关是因为
    #: "裸台面放置时真时假"曾经是**未解**(旧交接文档 §3.5 记的就是它, 那份文档已删),
    #: 我没能在离线把游戏侧那条判据钉死(依据只有实机日志的反证);
    #: 要是发现干放**确实**能进盘, 翻这个开关即可。
    DRY_PLACE_OK = (os.environ.get("NEKO_DRY_PLACE") or "0") not in ("0", "", "false", "False")

    def op_assemble(self, km, x, z, op: Op, st: dict) -> bool:
        """把手上的材料放到摆盘位 —— 台面上有盘子时, 这一步本身就是"摆盘"。

        特殊情况(用锅煮的菜): 上一步"从锅里取菜"结束时手上**已经端着那个盘子**了:
          · 不能再"给摆盘位补一个盘子"(_ensure_plate 会把手上这盘菜放到别处去);
          · 直接放到摆盘位 —— 那儿本来就有盘子时, 手上这盘会并进它。
        """
        _, _, held = self.pos(st)
        if not held:
            # ⚠ **手空不能无条件当成功**。手空有两种来路:
            #   ① 这一步本来就没什么可做(极少);
            #   ② **上一次放置失败了, 材料已经被游戏丢掉/放到了别处** —— 这时报"跳过"
            #      等于假装成功, 后面会端着空盘去煮饭/送餐, 整条链子白跑一整局。
            # 所以判据是: **台面上那个盘子里确实已经有这一步要放的东西** 才允许跳过。
            # 判据用 `_plate_contents_on`(插件读的 `onhas`), 和"并盘成功没有"用的是同一个
            # 来源 —— 不猜、不按名字猜阶段。
            want = self._norm(op.target or "")
            spot0 = self._spot_now(km) or self.pick_assemble_spot(km, x, z)
            contents = self._plate_contents_on(spot0) if spot0 is not None else set()
            if want and spot0 is not None and self._has_plate(spot0) and want in contents:
                self.log(f"[步骤] 组装: 手上空, 但 {spot0.id} 那盘里已有 {op.target!r} → 跳过")
                return True
            if want:
                self.log(f"[步骤] 组装: 手上空, 且盘里没有 {op.target!r}"
                         f"(摆盘位={getattr(spot0, 'id', None)!r}, 盘里={sorted(contents)})"
                         f" —— 判失败, 不假装成功")
            else:
                self.log("[步骤] 组装: 手上空, 且这一步没报材料名 —— 无从核对, 判失败")
            return False
        holding_plate = self._is_plate(held)
        if holding_plate:
            # ☠☠ **空盘不算"组装好了"** —— 上面那道闸门只管**被选中**这一步;
            #   `op_fetch` 的腾手会**直接调** `op_assemble`(绕过闸门), 而
            #   "端着空盘放到空台面"那条路**没有任何核对**(`holding_plate and _has_plate`
            #   那条有 `onhas` 比对, `holding_plate and not _has_plate` 这条**没有**)。
            #   ⇒ 这里先把它拦掉: 把空盘放下**不等于**"把 {op.target} 放进盘里"。
            _c = self.chef(st) or {}
            if "heldhas" in _c and not self._held_contents(st):
                self.log(f"[步骤] ✗ 手上端着的是**空盘** —— 放到台面上什么也没组装, "
                         f"不是「把 {op.target} 放进盘里」, 判失败(不假装成功)")
                return False
        spot = self.assemble_spot
        if spot is None:
            spot = self.pick_assemble_spot(km, x, z)
            if spot is None:
                self.log("[步骤] 找不到摆盘位")
                return False
            self.assemble_spot = spot
        # ☠☠ **按 id 到新鲜的 km 里重取一次** —— `self.assemble_spot` 是
        #   **整局粘住的那个快照对象**(见 `pick_assemble_spot` "整局内粘住"那段),
        #   它的 `.on`/`.onhas` **永远不会自己更新**: 盘子被端走之后读它还是"上面有盘子"
        #   ⇒ 对着**空台子**按放置 ⇒ 游戏 `CanHandlePlacement=False` ⇒ 东西放不下去
        #   (实测 2026-09-15 用户: "**这个地方的盘子我已经用过了, 但是没有更新掉,
        #    还是在对着空台子使用**")。
        #   ⚠ 同一个坑 `_skip_already_on_spot` / `_top_up_plate` / `_align_for_place`
        #     都已经各自绕过一次了 —— 这里是最该有却一直没有的那一处。
        _fresh = km.stations.get(spot.id) if km is not None else None
        if _fresh is not None:
            spot = _fresh
            self.assemble_spot = spot
        _dry = False        # 走了"干放"那条路吗 —— 见下面收尾处的判据
        if holding_plate:
            self.log(f"[步骤] 手上端着盘子 {held!r} —— 直接放到 {spot.id}(不再另取盘子)")
        elif not self._has_plate(spot):
            # ⚠ 这里**不能**去补盘子: 手上正拿着材料, 对着盘子堆按交互只会把材料
            #   放到盘子堆上(实测踩过)。补盘子在 execute() 开头做 —— 那时手是空的。
            #
            # ☠☠ **台面上已经有别的东西时, "干放"必然被游戏拒, 那就别按了**
            #   (用户 2026-09-15 给的判据: "干放**行**的时候是台子上没有其他东西")。
            #   反编译对得上: `AttachStation.CalculatePlacementType`(`AttachStation.cs:42-70`)
            #   `_item != null` 时走 `placementHandler.CanHandlePlacement`, 手上这份料
            #   既不是盘子也没法并进已有容器 ⇒ `NotValid` ⇒ 游戏回 `placeCanHandle=false`。
            #   ⇒ **按下去只会白按一下再失败**(实机一局里四次都栽在这儿), 直接换台面。
            #   ⚠ 空台面那条**保留**, 但**它不算 `assemble` 做成** —— 见收尾处。
            #   ☠☠☠ **2026-09-15 更正**: 我上一轮在这里写过"实测它**能成**", 依据是
            #     `⚠ 摆盘位 counter10 上没有盘子…` 之后紧跟 `✓ assemble SushiFish`。
            #     那是**把假 ✓ 读成了真成功** —— 同一对日志在 `s_sushi_1_1` 又出现两次,
            #     而这一次它后面紧跟着引擎自己的判决:
            #       `✓ assemble SushiFish` → `⛔ **链子断了**: 手上是空的, 而这一单
            #        剩下的 1 步里**没有 fetch**(deliver)`
            #     ⇒ **材料掉在光台面上, 根本没进盘子**, 而 `✓` 让这一步销了号,
            #       链子再也填不满 ⇒ 整单作废。这一局两单就是这么死的
            #       (第二单还吃掉了剩下的九成时间)。
            if list(getattr(spot, "on", None) or []):
                self.log(f"[步骤] ⚠ 摆盘位 {spot.id} 没盘子, 而且台面上已经有 "
                         f"{list(spot.on)!r} —— **干放会被游戏拒**(用户规则: "
                         f"干放只在台面空着时才行), 不按了, 换一个台面")
                self._last_fail_kind = KIND_BRANCH
                self.mark_branch_dead(PLACE_SENTINEL, spot.id, "没盘子且台面非空")
                self.assemble_spot = None
                self._assemble_sid = ""
                return False
            _dry = True
            self.log(f"[步骤] ⚠ 摆盘位 {spot.id} 上没有盘子, 材料只能先干放在台面上"
                     f"(**干放 ≠ 进盘**, 见收尾的判据)")
        self.log(f"[步骤] 把 {held} 放到摆盘位 {spot.id}"
                 + ("(上面有盘子)" if self._has_plate(spot) else ""))
        if not self.navigate_smart(km, spot.x, spot.z, tight=0.6):
            return False
        # 导航只保证站到了旁边, 朝向还是"最后一次移动的方向"。放置必须面向台面,
        # 否则游戏会把 m_iHandlePlacement 判成旁边别的台面, 材料就放错地方。
        self.face(spot.x, spot.z)
        # ⚠ **光转身还不够** —— 两个台子挨得近时游戏照样判到旁边那个(实测
        #   `s_wonderland_1_5`: 期望 `countertop_01 (2)` 却报 `workstation_mixer_01 (2)`)。
        #   这里挪到"游戏说放置目标就是它"为止, 对不上就**不按**。
        if not self._align_for_place(spot):
            return False
        # 手上端着盘子放到"已经有盘子"的台面 → 游戏做的其实是**两盘合并**:
        # ServerPlate.TransferToContainer → CombineWithContents, 然后把**自己清空**
        # (ServerPlate.cs:131-150), 盘子还在手上 —— 名字没变, 所以不能用
        # verify_hold_change 判成功, 只能看"台面那盘的内容变多了没有"(onhas)。
        if holding_plate and self._has_plate(spot):
            before = self._plate_contents_on(spot)
            self.interact("pickup", verify_hold_change=False)
            time.sleep(0.4)
            st2 = self.state()
            km2 = self.map(st2) if st2 else None
            if km2 is None:
                return True
            spot2 = km2.stations.get(spot.id) or spot
            cx, cz, held2 = self.pos(st2)
            after = self._plate_contents_on(spot2)
            if after != before:
                self.log(f"[步骤] ✓ 手上的菜并进了 {spot.id} 那盘({sorted(before)} → {sorted(after)})")
                if self._is_plate(held2):
                    # 手上现在只剩一个空盘: 端着它去拿下一个材料会到处乱放, 先放空台面
                    self._put_down_plate(km2, cx, cz)
                return True
            self.log(f"[步骤] ⚠ 并盘没生效({sorted(before)} 没变), 手上还是 {held2!r}")
            return False
        if not holding_plate and self._has_plate(spot):
            # 生料/成品放到"已经有盘子"的台面时, 游戏应自动把它并进盘子
            # (PlacementContainer + IngredientToContainerBehaviour)。
            #
            # ☠☠ **"进了没"要看两件事, 而且两次读必须是同一时刻的**(2026-09-15 实机打回来的):
            #   这一段原来只判"盘子内容变了没", 而 `before` 是从**可能陈旧的 `self.assemble_spot`**
            #   读的、`after` 是从**刚取的新快照**读的 —— 前后差着几百毫秒。于是只要期间有
            #   **任何别的变化**(上一步放的料还没反映进旧快照), `after != before` 就成立。
            #   实测 `s_sushi_1_3` 的日志:
            #       `✓ SushiRice 进了 counter12 那盘([] → ['seaweed'])`   ← 变化的是**上一步的 seaweed**
            #       `⚠ 手上还有 SushiRice, 腾不出手来取料`                 ← 我们手上**根本还拿着它**
            #   ⇒ **假成功**, 然后 `fetch` 那步发现手没空 → 失败 → 重试 → 整局循环烧掉。
            #   这正是本项目记过两次的那类: "假'能'的代价是执行一个错的物理动作"。
            #   ⇒ 现在两条都要: ① 我这份**离手了**; ② 盘子里**也确实变了**。
            #      · 只判①(手空) → 漏掉"东西丢在台面上、没进盘"(最早那个 bug);
            #      · 只判②(盘变) → 漏掉"东西还在手上"(这次这个)。
            st0 = self.state(force=True)
            placeh = (self.chef(st0) or {}).get("placeh") or ""
            self.log(f"[步骤] 放置目标游戏报={placeh!r} (期望 {spot.name!r} 或它的盘子)")
            km0 = self.map(st0) if st0 else None
            spot0 = (km0.stations.get(spot.id) if km0 is not None else None) or spot
            before = self._plate_contents_on(spot0)          # ← 和下面那次读同一时刻
            _, _, held_before = self.pos(st0) if st0 else (None, None, held)
            if not self.interact("pickup", verify_hold_change=False):
                return False
            time.sleep(0.4)
            st2 = self.state(force=True)
            km2 = self.map(st2) if st2 else None
            if km2 is not None:
                spot2 = km2.stations.get(spot.id) or spot
                after = self._plate_contents_on(spot2)
                _, _, held2 = self.pos(st2) if st2 else (None, None, held)
                if held2 != held_before and after != before:
                    self.log(f"[步骤] ✓ {held} 进了 {spot.id} 那盘"
                             f"({sorted(before)} → {sorted(after)})")
                    return True
                if held2 == held_before:
                    # ⚠ **这条路上必须打 `_direct_hint()`**: 直调命中了(`hits` 在涨)却还在手上,
                    #   那说明游戏收到了 Place 事件但自己判了 `CanHandlePlacement=false`
                    #   (`PlaceHeldItem_Server` 的反分支 → `OnFailedToPlace`, 东西留在手上)。
                    #   探针里的 `placeCanHandle` 就是那个判决 —— 不打出来只能靠猜。
                    self.log(f"[步骤] ✗ {held} **还在手上** —— 没放下去"
                             f"(游戏报的放置目标={placeh!r}, 盘子里是 {sorted(after)})"
                             + self._direct_hint())
                else:
                    self.log(f"[步骤] ✗ {held} 离手了但没进盘"
                             f"({sorted(before)} → {sorted(after)}) —— 多半丢在台面上了")
                # ☠ **这是"这一块台面放不下", 不是"这一单完了"**(用户: "返回到其他路")。
                #   实测代价: `placeCanHandle=false` 连试 3 次 → `✗ 放弃: assemble`
                #   → **整单报废** —— 而它只需要换个台面。所以:
                #     ① 记 `PLACE_SENTINEL`(和材料名无关, 见那边的注释) ⇒ `pick_assemble_spot`
                #        会把这块筛掉; ② 把**整局粘住的** `assemble_spot` 松开, 否则下一轮
                #        还是认回它(`pick_assemble_spot` 的守旧分支)。
                self._last_fail_kind = KIND_BRANCH
                self.mark_branch_dead(PLACE_SENTINEL, spot.id, "游戏 placeCanHandle=false")
                self.assemble_spot = None
                self._assemble_sid = ""
                return False
        # ☠☠ **收尾这一下失败了, 也要换台面 —— 和上面"并盘失败"完全对称**
        #   (2026-09-15 实机打回来的: 一整局四次栽在同一处)。
        #   上面那条只盖住了 `holding_plate and _has_plate(spot)` 那一条路(并盘);
        #   而**更常见的**是这一条: 摆盘位上**没有盘子**, 材料只能"干放"上去,
        #   游戏一律回 `placeCanHandle=false`(`AttachStation.CouldAttachToSelfIfEmpty`
        #   要求 carried item 有 `IAttachment`), 于是:
        #     `把 Seaweed 放到摆盘位 counter3` → `持有物始终未变` → 重试 3 次
        #     → `✗ 放弃: assemble` → **整单报废**。
        #   而它只需要**换一个台面** —— 下一个台面说不定本来就有盘子
        #   (`pick_assemble_spot` 的 `with_plate` 那一路优先挑"本来就摆着盘子的")。
        #   ⚠ 日志里那四次全都**没有** `[分支]` 行, 就是因为这里以前什么都不做。
        if not self.interact("pickup", verify_hold_change=True):
            self._last_fail_kind = KIND_BRANCH
            self.mark_branch_dead(PLACE_SENTINEL, spot.id, "放置被游戏拒了")
            self.assemble_spot = None
            self._assemble_sid = ""
            return False
        if _dry and not self._is_plate(op.target) and not self.DRY_PLACE_OK:
            # ☠☠☠ **干放只证明了"离手", 没有证明"进盘" —— 所以它不算 `assemble` 做成。**
            #   `assemble` 的语义是**把材料放进那个盘子**(候选理由写的就是"把这个材料
            #   放到组装台面"); 台面上**没有盘子**, 材料落地也进不了任何盘子。
            #   以前这里直接 `return True`, 于是报出**假 ✓** ⇒ 这一步销了号 ⇒
            #   `_execute_scored` 的"链子断了"判据(手空 + 剩余步骤里没有 `fetch`)
            #   立刻成立 ⇒ **整单作废**。实机 `s_sushi_1_1` 两单都死在这儿, 第二单
            #   吃掉了剩下的九成时间。
            #   ⇒ 和上面那条"台面非空"完全对称: **换一个摆着盘子的台面**。
            #     (`pick_assemble_spot` 的 `with_plate` 那一路优先挑本来就摆着盘子的;
            #      实在一个都没有时, `execute()` 开头的补盘子会先把手空出来去拿。)
            #   ⚠ 盘子自己作为 `assemble` 目标时不算 —— 那本来就是"把盘子放上台面"。
            self.log(f"[步骤] ✗ {held} **干放**在 {spot.id} 台面上了 —— 它**没进盘子**, "
                     f"这一步不算做完(assemble 的判据是**进盘**)。换一个摆着盘子的台面")
            self._last_fail_kind = KIND_BRANCH
            self.mark_branch_dead(PLACE_SENTINEL, spot.id, "台面没盘子, 干放不算进盘")
            self.assemble_spot = None
            self._assemble_sid = ""
            return False
        return True

    def op_deliver(self, km, x, z, op: Op, st: dict, flow=None) -> bool:
        """端起容器送到送餐口。"""
        _, _, held = self.pos(st)
        # 手上还端着盘子(多半是"并进台面那盘"之后剩下的空盘): 先放下腾出手,
        # 否则会端着空盘去送餐口 —— 空盘送不出东西(ServerPlateStation 判空)。
        # ☠☠ **这里原来是 `self.assemble_spot`(陈旧对象)被喂给下面的复核函数** ——
        #   而复核函数读的正是那个对象的 `.onhas` ⇒ **它核的是一份备份数据**
        #   (用户: "游戏本质上是实时, 我们不能用备份的数据了")。
        #   走 `_spot_now(km)` 拿到当下那一份。
        _sp = self._spot_now(km)
        if held and self._is_plate(held) and _sp is not None:
            if self._put_down_plate(km, x, z):
                st = self.state()
                _, _, held = self.pos(st)
        _sp = self._spot_now(km)
        if not held and _sp is not None:
            self.log(f"[步骤] 去组装台面 {_sp.id} 端容器")
            if not self.navigate_smart(km, _sp.x, _sp.z, tight=0.6):
                return False
            # ☠☠ **端起来之前复核那盘还是本单的菜**(见 `_spot_still_ready`)。
            #   用户描述的现象就是这一条: 走到摆盘位时, 拼好的那盘已经被端走、
            #   换成了一个**空盘** ⇒ 端起空盘送到送餐口 ⇒ `✗ 送餐后订单仍在`。
            #   放在 `navigate_smart` **之后**: 走过去那几秒正是被换盘的窗口。
            if not self._spot_still_ready(km, flow, self._spot_now(km)):
                self.log(f"[步骤] ⚠ 走到组装台面 {_sp.id} 时,**那盘已经"
                         f"不是这道菜了**(多半被队友端走/换成了空盘) —— 不端了")
                self._last_fail_kind = KIND_BRANCH
                return False
            if not self.interact("pickup", verify_hold_change=True):
                return False
            st = self.state()
        # 后半程("送到送餐口 + 确认是我们交的")抽成了 `_deliver_plate` ——
        # `op_serve_any`(交台面上现成的那盘)复用的是**同一段**, 判据只留一份。
        return self._deliver_plate(km, x, z, op, st, flow)

    def _deliver_plate(self, km, x, z, op: Op, st: dict, flow=None) -> bool:
        """**端着盘子送到送餐口** —— `op_deliver` 的后半段, 逐字抽出来给 `serve_any` 复用。

        三处判据都是踩过坑的, 别删也别简化:
          · 手上不是盘子就别按(`ClientPlateStation.CanAddItem` 只收 `ClientPlate`)
          · 订单不在订单栏上了 ⇒ 不是我们能交的
          · **盘子离手**才算"我们交的"(订单消失而盘子还在手上 = 队友交的, 不冒领)
        """
        serve = km.nearest("serve", x, z)
        if serve is None:
            self.log("[步骤] 找不到送餐口(PlateStation)")
            return False
        self.log(f"[步骤] 送到 {serve.id}")
        if not self.navigate_smart(km, serve.x, serve.z, tight=0.8):
            return False
        self.face(serve.x, serve.z)
        if not self._align_for_place(serve):     # 见 _align_for_place: 挨得近会判到旁边台子
            return False
        # ServerPlateStation 接到放置事件并不代表订单完成：错误菜品、空盘或
        # 交互没命中都不应标记成功。只接受目标订单从 live 列表消失。
        before = sum(1 for order in self.live_orders() if order.get("name") == op.target)
        if before <= 0:
            self.log(f"[步骤] ✗ 找不到待交付订单 {op.target!r}，无法确认送餐")
            return False

        # ☠ **手上不是盘子就别按**。依据(反编译):
        #   `ClientPlateStation.CanAddItem(_object, _ctx)` = `_object.GetComponent<ClientPlate>() != null`
        #   —— 送餐口**只收盘子**, 拿别的东西按下去游戏的放置事件**根本不会发生**,
        #   我们却会在下面看到"订单没了"而**冒领功劳**(实机就是这么错的:
        #   手持生肉 BurritoMeat 走到送餐口, 一局 150 秒全耗在"送餐→失败→重新规划→再送餐")。
        _st0 = self.state()
        _, _, held0 = self.pos(_st0) if _st0 else (None, None, "")
        if not self._is_plate(held0):
            self.log(f"[步骤] ✗ 手上不是盘子({held0!r}) —— 送餐口只收盘子, 按了也不会发生放置, 不去空跑")
            return False
        # ☠☠ **手上这盘里是不是这道菜** —— 上面那条只证明了"手上是个盘子"。
        #   空盘、别人那道的菜、上一单的残料, 全都满足 `_is_plate`。
        #   用户实机描述(2026-09-15): "放上盘子被我拿着并且重新放一个空盘子,
        #   脚本会拿着**空盘子**去提交" ⇒ 走到送餐口空按一下 ⇒
        #   `✗ 送餐后订单 'X' 仍在, 判为交付失败`。
        #   依据: `heldhas`(插件读的**手上**容器内容, 见 `SceneScanner.ReadHeldItems`)。
        #   ⚠ 判据复用 `_dish_matches`(**只此一份**, 和 `_find_ready_dish`/`_spot_still_ready` 同源)。
        #   ⚠ 用**键在不在**判断"插件支不支持这个字段": 旧 dll 没有 `heldhas`
        #     ⇒ 放行(退回旧行为), 而不是把整条送餐路堵死。
        _c0 = self.chef(_st0) if _st0 else {}
        _req, _opt = self._dish_sets(flow) if flow is not None else (set(), set())
        if _req and "heldhas" in (_c0 or {}):
            _have = self._held_contents(_st0)
            if not self._dish_matches(_have, _req, _opt):
                self.log(f"[步骤] ✗ 手上这盘里**不是 {op.target} 要的菜**"
                         f"(盘里={sorted(_have) or '空'}, 本单要={sorted(_req)})"
                         f" —— 送到送餐口也交不掉, 不白跑")
                self._last_fail_kind = KIND_BRANCH
                return False

        if not self.interact("pickup", verify_hold_change=False):
            return False
        deadline = time.time() + 2.5
        while time.time() < deadline:
            time.sleep(0.15)
            remaining = sum(1 for order in self.live_orders() if order.get("name") == op.target)
            if remaining < before:
                # ---- 用户要求: "提交菜谱的前提是这一单已经完成**并且被需要**" ----
                # 再确认**是不是我们交的**。因果链(反编译):
                #   我们放手 → `AttachStation` 收下 → `ServerPlateStation.DeliverCurrentPlate()`
                #   → `FoodDelivered(...)` → 服务端回 `PlateStationMessage{m_success}`
                #   → `ClientPlateStation.DeliverPlate()` → `DeliverySequence` **销毁那个盘子**
                # ⇒ **盘子离开手**才是"我们交的"证据。订单消失而盘子还在手上
                #   ⇒ 那是**别人**交掉的(人类队友), 不能记在自己头上。
                _st1 = self.state(force=True)
                _, _, held1 = self.pos(_st1) if _st1 else (None, None, "")
                if self._is_plate(held1):
                    self.log(f"[步骤] ⚠ 订单 {op.target} 消失了, 但盘子还在手上({held1!r}) —— "
                             f"不是我们交的(多半是队友交的), 不冒领")
                    return False
                self.log(f"[步骤] ✓ 已交付 {op.target}(盘子已出手)")
                return True
        self.log(f"[步骤] ✗ 送餐后订单 {op.target!r} 仍在，判为交付失败")
        return False

    def _top_up_plate(self) -> None:
        """手空着 + 摆盘位缺盘子 → 现在就去补一个。**每个 op 边界都试一次**。

        为什么不能只在 `execute()` 开头补一次(实测踩的坑, 上一局 9 次失败都是这个):
          `_prepare_plate()` 里有 `if held: return True` —— **手上有东西就整个跳过**。
          而 `execute()` 开头手上经常有东西(上一轮失败留下的材料, 日志里就是
          "手上还有 SushiRice, 先放到组装台面")。那一次跳过之后**再没有任何重试**,
          于是一整轮里每次 assemble 都在"没有盘子的台面"上干放, 材料永远拼不成菜,
          日志里只看到反复的 `⚠ 摆盘位 counterNN 上没有盘子`。

        放在 op 边界是因为那一刻手经常是空的(上一个材料刚放下去), 补盘正好做得了。
        成本: 一次 state 读(共享缓存) + 一次 `_has_plate`; 大多数时候直接返回。
        """
        if self.assemble_spot is None:
            return
        st = self.state()
        if not st or not st.get("inRound"):
            return
        _, _, held = self.pos(st)
        if held:
            return                                  # 手上有东西 → 现在补不了
        km = self.map(st)
        if km is None:
            return
        spot = self._spot_now(km)                       # 用新鲜的, 别用陈旧快照
        if spot is None or self._has_plate(spot):
            return
        self.log(f"[步骤] 摆盘位 {spot.id} 还缺盘子 —— 趁手空补一个")
        if not self._ensure_plate(km, *self.pos(st)[:2], spot):
            # ☠ **补不上就换台面, 别在这儿反复补** —— 用户实测原话:
            #   "**还有盘子, 这个盘子被我用了, 脚本就会卡住**"。
            #   盘子被人类端走/正拿在手上时, `_ensure_plate` 的三个来源(台面上/地上/盘子堆)
            #   一个都找不到, 于是下一轮又跑一遍 `去 plates0 拿一个`, **原地打转**。
            #   ⇒ 把这个台面划掉 + 松开粘住的 `_assemble_sid`, 下一轮 `pick_assemble_spot`
            #     会挑**另一个**台面 —— 而 `with_plate` 那一路优先挑"本来就摆着盘子的",
            #     多半直接就找着了。
            #   ⚠ **有限期**(`BRANCH_TTL`): 人类会把盘子还回来, 到点这个台面自动回池。
            #   ⚠ 这正是"地图驱动"的做法(用户 2026-09-15: "**人类的影响已经作用在地图上了**")
            #     —— 不建模队友, 只按"这一步办不成"记账换路。
            self.mark_branch_dead(PLACE_SENTINEL, spot.id, "补不上盘子")
            self.assemble_spot = None
            self._assemble_sid = ""

    def _prepare_plate(self, flow: DishFlow) -> bool:
        """开局(手还空着)先把摆盘位那个盘子备好。

        为什么必须放在这里: 摆盘位的"盘子"是**材料自动进盘**的前提
        (PlacementContainer + IngredientToContainerBehaviour.TransferToContainer),
        台面上没盘子的时候, 材料只会干放在台面上, 后面怎么拼都拼不出菜。
        而"去拿盘子"必须**手是空的**才做得了 —— 手上拿着材料去盘子堆按交互,
        只会把材料放到盘子堆上(实测踩过)。
        拿不到不算致命: 大多数关卡台面上本来就摆着盘子, 这里只是兜底。

        ☠☠ **摆盘位必须无条件挑出来 —— 这一句是整局的命门**(2026-09-15 实机打回来的一条)。
           `0d100ec` 把这里的条件**写反了**(原来 `if not flow.plate:`, 被翻成 `if flow.plate:`),
           后果不是"少补一个盘子", 而是**整局卡死**:
             · `_op_target_for_score("assemble")` **只读** `self.assemble_spot`, 没挑出来就报
               `不可达(还没挑摆盘位)` ⇒ 评分表里 assemble 永远是 `-inf`;
             · `op_assemble` 里那段惰性补挑**得先被选中**才轮得到它 —— **鸡生蛋**;
             · `_top_up_plate()` 开头又是 `if self.assemble_spot is None: return`, **一样救不了**。
           实测(`s_sushi_1_3`): 虾切好之后连续 5 轮评分全是"assemble 不可达 + deliver 不可达",
           手里那盘切好的虾放不下去, 一路等到 `IDLE_WAIT` 20 秒 —— **一整局白烧**。
           ⇒ 所以下面**先挑位、先记下**, 再谈要不要补盘子。
        """
        st = self.state()
        km = self.map(st) if st else None
        if km is None:
            return False
        _, _, held = self.pos(st)
        if held:
            # ⚠ 手上有东西就补不了盘子(见 docstring), 但**摆盘位还是得挑出来** ——
            #   否则这一整轮 assemble 又是不可达(上面的命门)。`_top_up_plate` 会在
            #   每个 op 边界趁手空时补上。
            if self.assemble_spot is None:
                sp = self.pick_assemble_spot(km, *self.pos(st)[:2])
                if sp is not None:
                    self.assemble_spot = sp
            return True
        # ☠☠ **换单要换盘** —— 摆盘位是**整局粘住**的(`pick_assemble_spot` 的注释写着),
        #   而它上面那个盘子**可能还是上一单的菜** ⇒ 新单的料会并进旧菜里。
        #   实机 2026-09-15 `s_sushi_1_3`(**两单一起废**):
        #     `✓ ChoppedSushiPrawn 进了 counter15 那盘`
        #     `(['cucumber','seaweed','sushirice'] → ['cucumber','seaweed','sushiprawn','sushirice'])`
        #   —— `Sushi_PlainPrawn` 的虾被倒进了**装着黄瓜那道菜**的盘子。
        #   ⇒ 开工前先看一眼那个盘子: 里面若有**本单不要**的东西, 这个台面这一单就不能用。
        #   ⚠ 用**现成的**排除机制: `mark_branch_dead(PLACE_SENTINEL, …)` ——
        #     `pick_assemble_spot` 本来就会滤掉那里被划掉的台面, 不用新造判据。
        #   ⚠ 判据和 `_deliver_plate`/`_spot_still_ready` **同一份**(`_dish_sets`)。
        #   ⚠ **只赶"有本单不要的东西"** 的那种: 半成品(全是本单的材料)必须**留在原台面**
        #     (`pick_assemble_spot` 的"整局粘住"就是为它写的, 别把它误伤了)。
        if self.assemble_spot is not None:
            _cur = self._spot_now(km)
            _have = self._plate_contents_on(_cur)
            _foreign = self._dish_foreign(_have, flow)
            if _foreign:
                self.log(f"[步骤] ⚠ 摆盘位 {_cur.id} 那盘里是 {sorted(_have)}"
                         f" —— {sorted(_foreign)} **不是这一单的**, 换一个台面, "
                         f"别把新料倒进旧菜里")
                self.mark_branch_dead(PLACE_SENTINEL, _cur.id, "那盘是别的单的菜")
                self.assemble_spot = None
                self._assemble_sid = ""
        spot = self.pick_assemble_spot(km, *self.pos(st)[:2])
        if spot is None:
            return False
        self.assemble_spot = spot
        if self._has_plate(spot):
            return True
        if not flow.plate:
            return True                 # 这道菜不要求盘子 ⇒ 不补(摆盘位**已经记下了**)
        self.log(f"[步骤] 开局: 摆盘位 {spot.id} 上没有盘子, 先补一个")
        if not self._ensure_plate(km, *self.pos(st)[:2], spot):
            # 同 `_top_up_plate`: 补不上盘子 ⇒ 这个台面现在对我们没用, 换一个。
            # (两处判据**必须一致** —— 一处换一处不换会来回抖。)
            self.mark_branch_dead(PLACE_SENTINEL, spot.id, "补不上盘子")
            self.assemble_spot = None
            self._assemble_sid = ""
            return False
        return True

    def _toss_budget(self, km, st, target: str):
        """这个材料**还缺几份**(由订单栏算) —— 连备的额度。

        返回 `int`(可能是 0 = 够了, 一份都不多拿);
        **`None` = 算不出来**(订单栏读不到/出错) ⇒ 调用方**退回老行为**,
        别把"读不到"静默当成"不需要备" —— 那会把用户当初要的"多拿几个抛"悄悄关掉。
        """
        try:
            gap = lookahead.demand(self._all_flows(st), self._inventory(km, st))
            return int(gap.get(self._norm(target), 0))
        except Exception:                                          # noqa: BLE001
            return None

    def _prep_and_toss(self, km, st, flow, op) -> None:
        """**取货那一趟里连备几份** —— 每份都"就地朝下一步丢", 然后才离开箱子。

        用户 2026-09-15: "**他拿食材抛的时候不能多拿一点, 只抛一个那不如不抛。多拿几个抛**"。

        为什么单份不值得丢: 丢一份 = 多一次"取+转身+丢" **再加回头单独跑一趟去捡**;
          而抱着走本来就只有一趟 ⇒ 单份时丢是**净亏**。
        收益全在批量: 取货点和丢的方向是**同一个点**(站在箱子旁取、就地朝下一步丢),
          所以连备 N 份**几乎不额外走路**, 只是 N 次拾取 + N 次投掷; 之后**一趟**过去
          并行处理(`s_sushi_1_3` 有 3 口锅并排, 并行吞吐是串行的 3 倍)。

        ☠ **只在"确实丢了"的时候连备**: 下一步的处理站就在旁边时 `_toss_to_next` 不会丢
          (抱着走更省) —— 那时"连备"没有意义(手只有一只, 抱不了两份)。
        ⚠ 每一份都**重新探一次货源**(`op_fetch` 自己会解析"现在离我最近的") —— 箱子的
          余量、传送带上的位置都会变, 不能拿第一次的坐标硬套。
        ☠☠ **连备几份现在由订单算**(2026-09-15 用户):
          原来这里是 `for k in range(TOSS_BATCH_MAX)` —— 一个**和订单、配方都无关的
          写死值**(默认 3)。而用户对备料的要求是"份数得从'当前场上有几张单、
          每张要几条'推出来"、"**别把 3 写进代码**"。
          ⇒ 先拿 `lookahead.demand(订单栏, 现有)` 算这个材料**还缺几份**, 连备到那个数;
            `TOSS_BATCH_MAX` **退成刹车**(上限), 不再是份数的来源。
          ⚠ 缺口是**取到手之后**重算的 —— 那时手上那份已经进了库存, 所以 `want`
            正好是"还要再拿几份", 不会多备一份。
          ⚠ 算不出来(订单栏读不到/出错)⇒ `None` ⇒ **退回老行为**, 不静默改成"不备"。

        ⚠ 每轮都查 `round_active()` —— 别为一局末尾的备料把时间烧光。
        """
        want = self._toss_budget(km, st, op.target)
        if want is not None and want <= 0:
            return                       # 订单(含余量)已经够了 ⇒ 一份都不多拿
        if TOSS_BATCH_MAX <= 1:
            self._toss_to_next(km, st, flow, op.target)
            return
        budget = TOSS_BATCH_MAX if want is None else min(TOSS_BATCH_MAX, want)
        tossed = 0
        for k in range(budget):
            if not self.round_active():
                break
            if not self._toss_to_next(km, st, flow, op.target):
                break                      # 没丢成(太近/游戏不收) → 不连备
            tossed += 1
            if k + 1 >= budget:            # ⚠ 比的是**这次算出来的额度**, 不是那个上限
                break
            st = self.state(force=True)
            if not st or not st.get("inRound"):
                break
            km = self.map(st) or km
            _, _, held = self.pos(st)
            if held:
                break                      # 手上还有东西(丢失败?) → 别再堆
            # 回去再拿一份(就在同一个箱子旁, 不额外走路)
            if not self.op_fetch(km, *self.pos(st)[:2], op, st, 0):
                break
        if tossed > 1:
            self.log(f"[预置] 这一趟**连备了 {tossed} 份 {op.target}** —— "
                     f"回头一趟过去一起处理")

    def _item_spots(self, km, name: str) -> set:
        """这个东西此刻都在哪些位置(地上 + 台面上) —— 只用来算"丢出去落在哪"。"""
        n = self._norm(name)
        out = set()
        if not n:
            return out
        for it in (getattr(km, "items", None) or []):
            if n in (self._norm(getattr(it, "ing", "")), self._norm(getattr(it, "name", ""))):
                out.add((round(it.x, 2), round(it.z, 2)))
        for s in (getattr(km, "stations", None) or {}).values():
            for o in (s.on or []):
                if self._norm(o) == n:
                    out.add((round(s.x, 2), round(s.z, 2)))
        return out

    def _toss_to_next(self, km, st, flow, target: str) -> bool:
        """把刚拿到的 `target` **朝它下一步的处理站丢过去**, 并把**真实落点**记下来。

        用户 2026-09-15:
        > "可以取货时, 拿出这个食材**往下一步处理的方向丢**... SushiRice 在左侧, 下一步处理是锅,
        >  在右上侧... 我们就拿出 SushiRice 往右侧丢, 后续过去处理的时候**可以处理出很多份米备用**"

        为什么值得: 一次只能拿一个 ⇒ 抱着走 12 格, 这一趟路上什么都干不了; 丢出去是一瞬间,
        于是可以连着 `取→丢` 几轮, 先把料堆到加工区附近, 再**一趟过去并行处理**
        (`s_sushi_1_3` 有 3 口锅并排, 并行吞吐是串行的 3 倍)。

        ☠ **目的地在射程外时要认清: 这是"丢一段", 不是"送到位"**。
          实测射程 = 平地约 6 格(`TOSS_RANGE_CELLS`, 用户自己数的), 而典型的目标
          (米箱 (-1.2,7.2) → 锅 (10.8,10.8)) 是 **12 格** ⇒ **一次丢不到**。
          所以这条路是 **"丢一段(6) → 走一段 → 再丢(6)"** 的两跳, 不是一次到位。
        ☠ **落点不假设、靠读** —— `m_throwForce` 插件读不到, 且**有落差时射程会变**
          (落点地面越低飞得越远, 用户提醒过)。所以丢完**读它真的落在哪**,
          把**实测落点**记进预置表(而不是我们想要的落点)。
        """
        if not target:
            return False
        nxt = None
        for o in (getattr(flow, "ops", None) or []):
            if getattr(o, "target", "") == target and o.action in ("cook", "chop", "mix"):
                nxt = o
                break
        if nxt is None:
            return                                  # 它没有"下一步处理站"(比如直接摆盘的料)
        try:
            tgt, _why = self._op_target_for_score(km, st, nxt, *self.pos(st)[:2])
        except Exception:                                            # noqa: BLE001
            return False
        if not tgt:
            return False
        cx, cz, _ = self.pos(st)
        dist = ((tgt[0] - cx) ** 2 + (tgt[1] - cz) ** 2) ** 0.5
        if dist < TOSS_MIN_CELLS:
            return                                  # 就在旁边, 抱着走更省事(丢要两次交互)
        before = self._item_spots(km, target)
        _hop = "" if dist <= TOSS_RANGE_CELLS else \
            f" (射程约 {TOSS_RANGE_CELLS:.0f} 格 ⇒ 这是**丢一段**, 不是送到位)"
        self.log(f"[预置] {target} 的下一步在 {nxt.action}({tgt[0]:.1f},{tgt[1]:.1f}) "
                 f"距 {dist:.1f} 格 —— 朝那边丢过去(腾出手, 回头一趟并行处理){_hop}")
        self.face(tgt[0], tgt[1])
        try:
            r = self.bridge.direct("throw", player=self._my_player_index(st))
        except Exception as e:                                       # noqa: BLE001
            self.log(f"[预置] ✗ 丢失败: {e!r}")
            return False
        if not isinstance(r, dict) or not r.get("ok"):
            self.log(f"[预置] ✗ 游戏没收下这个投掷: {r!r}")
            return False
        time.sleep(0.5)
        st2 = self.state(force=True)
        km2 = self.map(st2) if st2 else None
        if km2 is None:
            return False
        new = self._item_spots(km2, target) - before
        if not new:
            self.log("[预置] ⚠ 没看到新落点(可能被传走/被吃了) —— 这条预置不记")
            return False
        bx, bz = min(new, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cz) ** 2)
        flew = ((bx - cx) ** 2 + (bz - cz) ** 2) ** 0.5
        self._preposed[self._norm(target)] = (bx, bz, time.time())
        # ☠ **方向偏差也要打出来** —— 投掷是按**朝向**飞的, 而朝向只能靠"按哪个键"去控;
        #   实测有一次该往左上(菜板)丢、飞出去却是另一个方向。只看"飞了几格"看不出这个错,
        #   所以把**意图方向与实际位移的夹角**一起报 —— 偏差大就是瞄准/朝向的问题, 不是射程的问题。
        import math as _m
        ang = None
        try:
            a_i = _m.atan2(tgt[1] - cz, tgt[0] - cx)
            a_a = _m.atan2(bz - cz, bx - cx)
            ang = abs(_m.degrees(a_i - a_a))
            ang = min(ang, 360.0 - ang)
        except Exception:                                        # noqa: BLE001
            pass
        self.log(f"[预置] ✓ 落在 ({bx:.1f},{bz:.1f}) —— **实测射程 {flew:.1f} 格**"
                 f"(意图点 {tgt[0]:.1f},{tgt[1]:.1f}"
                 + (f", 方向偏差 {ang:.0f}°" if ang is not None else "")
                 + "); 记进预置表, 回头去捡")
        return True
        if ang is not None and ang > 45.0:
            self.log("[预置] ⚠ **方向偏了** —— 瞄的是意图点, 飞出去的却是别的方向; "
                     "看 `face()` 有没有把朝向转到位(斜方向要两个轴一起按)")

    def _preposed_ok(self, km, target: str) -> bool:
        """这个材料**预置在附近**吗 —— TTL 内 **而且现在真的还在那儿**。

        ☠☠ 用户 2026-09-15 定的原则:
          > "**游戏本质上是实时, 我们不能用备份的数据了**, 应该积极更新数据, 包括脚本的订单、
          >  手上的盘、盘里的内容、**盘的位置**、厨师的位置等等"

        原来这里**只判 TTL**(60 秒内就当"还在那儿") ⇒ `assemble X` 因此被判**可做**
        ⇒ 步骤被选中 ⇒ `_pickup_preposed` 走过去 ⇒ `✗ 走不到预置点 —— 这条预置作废`
        (实机日志里这行反复出现, 每次白跑一趟还要重选)。
        ⇒ **记录是意图, 地图才是事实** —— 用 TTL 筛完, 再拿**当下这一帧**核一遍。

        ⚠ 核对力度是**便宜的那一档**: 只看"那一带(1.5 格内)还有没有一件叫它的东西",
          不做精确定位 —— 定位的活留给 `_pickup_preposed`(它走不到会自己作废)。
        ⚠ 核对不过就**就地撤销这条预置**(并且只报一次: 撤销之后下一轮直接查不到表项)。
        ⚠ 拿不到地图/接口出错 ⇒ **放行**(退回旧行为): 这是"提前判死"的捷径,
          判错的方向该偏松, 别把正常路堵了。
        """
        k = self._norm(target) or "\0"
        ent = self._preposed.get(k)
        if not ent or (time.time() - ent[2]) >= PREPOSE_TTL:
            return False
        if km is None:
            return True
        px, pz = float(ent[0] or 0.0), float(ent[1] or 0.0)
        try:
            loose = km.unseen_items() or []
        except Exception:                                            # noqa: BLE001
            return True
        for it in loose:
            if self._norm(getattr(it, "name", "")) != k:
                continue
            dx = float(getattr(it, "x", 0.0) or 0.0) - px
            dz = float(getattr(it, "z", 0.0) or 0.0) - pz
            if dx * dx + dz * dz <= 1.5 ** 2:
                return True
        self._preposed.pop(k, None)        # 撤销: 下一轮直接查不到 ⇒ 这行日志只打一次
        self.log(f"[预置] ⚠ {target} 已经不在预置点 ({px:.1f},{pz:.1f}) 那一带了"
                 f"(地图上找不着) —— 这条预置作废, 别再拿它当'在附近'")
        return False

    def _pickup_preposed(self, km, st, target: str) -> bool:
        """这一步要用的东西**不在手上、而是躺在预置点** → 先去捡回来。

        用户 2026-09-15: "**鼓励丢食物**" —— 取货时把料朝下一步的方向丢过去,
        之后再过去一趟并行处理好几份。丢出去手上就没它了, 所以 `cook/chop/mix/assemble`
        这几步必须先"去捡回来", 否则会判成"手上没有可煮的"而白跑一趟。

        没预置记录、或东西已经在手上 → 直接 `True`(什么都不做)。
        """
        key = self._norm(target)
        if not key or key not in self._preposed:
            return True
        px, pz, _t = self._preposed[key]
        _, _, held = self.pos(st)
        if self._held_is(held, target):
            self._preposed.pop(key, None)             # 已经在手上 → 这条预置作废
            return True
        if time.time() - _t > PREPOSE_TTL:
            self._preposed.pop(key, None)
            return True
        self.log(f"[预置] 手上没有 {target}, 它预置在 ({px:.1f},{pz:.1f}) —— 先去捡回来")
        if not self._approach(km, px, pz, tight=0.6, want=target):
            self.log(f"[预置] ✗ 走不到预置点 ({px:.1f},{pz:.1f}) —— 这条预置作废")
            self._preposed.pop(key, None)
            return False
        if not self.interact("pickup", verify_hold_change=True):
            self.log("[预置] ✗ 没捡起来 —— 这条预置作废")
            self._preposed.pop(key, None)
            return False
        self._preposed.pop(key, None)
        self.log(f"[预置] ✓ 捡回来了 {target}")
        return True

    def do_op(self, km: KitchenMap, st: dict, op: Op, flow: DishFlow,
              attempt: int = 0) -> bool:
        if op.optional:
            self.log(f"[步骤] {op.action} {op.target} 是可选材料, 跳过")
            return True
        x, z, _ = self.pos(st)
        if x is None:
            return False
        # **预置**: 这一步要用的料不在手上、而是躺在预置点 → 先捡回来(见 `_pickup_preposed`)。
        #   放在派发**之前**、且四个动作共用一处 —— 否则"手上没有可煮的"会各自白跑一趟。
        # ☠ **但 `cook` 的"取菜"那一半不能捡**(见 `_cook_takeout`): 它要的是**空手/
        #   盘子**, 而 `_pickup_preposed` 恰好会把那份生料**塞回手上** —— 于是"锅里
        #   已经有了"这个本来正确的状态, 被脚本自己废掉了去拿盘子的手。
        if op.action in ("chop", "cook", "mix", "assemble"):
            if not self._cook_takeout(km, x, z, op) \
                    and not self._pickup_preposed(km, st, op.target):
                return False
        if op.action == "fetch":
            ok = self.op_fetch(km, x, z, op, st, attempt)
            if ok and flow is not None:
                # 拿到了就**顺手朝它的下一步丢过去**(用户要求"鼓励丢食物", 见 `_toss_to_next`)。
                # 放在"拿成功"之后: 失败了还丢什么。
                self._prep_and_toss(km, st, flow, op)
            return ok
        if op.action == "plate":
            # 容器这步"尽力而为": 拿不到不该把整条流程卡死 ——
            # 后面的取料/切/煮照样要跑, 不然连问题出在哪都看不出来。
            if not self.op_take_plate(km, x, z, op, flow):
                self.log("[步骤] ⚠ 取容器没成, 先跳过(继续取料/切/煮)")
                self.assemble_spot = self._spot_now(km) or self.pick_assemble_spot(km, x, z)
            return True
        if op.action == "chop":
            return self.op_chop(km, x, z, op, st)
        if op.action == "mix":
            return self.op_mix(km, x, z, op, st)
        if op.action == "cook":
            return self.op_cook(km, x, z, op, st, flow)
        if op.action == "assemble":
            return self.op_assemble(km, x, z, op, st)
        if op.action == "deliver":
            return self.op_deliver(km, x, z, op, st, flow)
        # ---- 杂活(阶段二): 和菜谱 op 走**同一个** `do_op`, 于是重试/异常/日志全都一致 ----
        if op.action == "press":
            return self.op_press(km, st, op)      # 按 op 指的那颗(探测和执行必须同一个)
        if op.action == "wash":
            return self.op_wash(km, st, op=op)
        if op.action == "work":
            return self.op_work(km, x, z, op, st)
        if op.action == "serve_any":
            return self.op_serve_any(km, x, z, op, st, flow)
        if op.action == "pass":
            return self.op_pass(km, x, z, op, st)
        if op.action == "rescue":
            return self.op_rescue(km, x, z, op, st)
        # tool 暂不处理(订单要求的器皿/成品物件, 由摆盘那条路自己管)
        return True

    def op_work(self, km, x, z, op: Op, st: dict) -> bool:
        """加工台面上没加工完的料 —— 切菜/搅拌/烘培**同一条路**: 站旁边按交互键。

        为什么目标从 `op` 上取(而不是像 `_chores` 那样自己再扫一遍最近的台面):
        评分阶段探测的就是这个台面, 执行时必须是**同一个** —— 否则会
        "按 A 台算的分、走到 B 台去干"。`at_name` 给 `_approach` 的 `want`
        (让游戏确认"现在按交互键能作用到它", 见 `_aim_ok`)。
        """
        if not (op.at_x or op.at_z):
            return False
        self.log(f"[杂活] 去加工 {op.at_name or op.target}({op.note or ''})")
        if not self._approach(km, op.at_x, op.at_z, want=op.at_name):
            self.log(f"[杂活] 走不到 {op.at_name or op.target} 旁边")
            return False
        # 用 `chop`(使用键): 游戏那边它同时发 Interact + TriggerInteract,
        # 切/搅/烘靠的正是后者 —— 与 `_chores` 第③段、`op_press` 同一个键。
        return self.interact("chop", verify_hold_change=False)

    def op_serve_any(self, km, x, z, op: Op, st: dict, flow=None) -> bool:
        """**交菜**: 台面上已经拼好的一盘(不管谁拼的), 端去送餐口。

        用户点名的行为("切菜, 洗盘子, **交菜**, 灭火, 控制机关…"), 之前**没有**:
        `op_deliver` 的取盘来源写死 `self.assemble_spot`, 台面上别人拼好的那盘没人管。

        ⚠ `op.target` 是**订单名**(不是台面名) —— 后半程那段"订单剩余量变少才算交成"
          拿它和订单列表比, 所以这里必须传订单名, 台面只在 `at_x/at_z` 里。
        ⚠ **手上已经有盘子时不要再端一盘**: 直接送去(多半是刚并完盘剩下的那盘)。
        """
        _, _, held = self.pos(st)
        if not self._is_plate(held):
            if held:
                self.log(f"[杂活] 手上有 {held}, 先腾手再交菜")
                return False
            self.log(f"[杂活] 去端台面上那盘菜 {op.at_name or ''}"
                     f"@({op.at_x:.1f},{op.at_z:.1f})")
            if not self._approach(km, op.at_x, op.at_z, want=op.at_name):
                self.log("[杂活] 走不到那盘菜旁边")
                return False
            # ☠☠ **走过来之后、端起来之前, 重新核对那盘还是那道菜**(见 `_spot_still_ready`)。
            #   探测时它是拼好的菜; 走到这儿可能已经被人类队友端走、换成了一个**空盘**
            #   —— 而 `want=op.at_name` 只对**台面名字**, 换盘它照样通过。
            #   ⚠ 放在 `_approach` **之后**才有意义: 走过去本身要好几秒, 那正是被换盘的窗口。
            _spot = self._station_named(km, op.at_name)
            if not self._spot_still_ready(km, flow, _spot):
                self.log(f"[杂活] ⚠ 走到 {op.at_name or '那盘菜'} 时,**盘里已经不是这道菜了**"
                         f"(多半被队友端走/换成了空盘) —— 不端了, 换别的")
                self._last_fail_kind = KIND_BRANCH
                return False
            if not self.interact("pickup", verify_hold_change=True):
                self.log("[杂活] 端不起那盘菜")
                return False
            st = self.state() or st
        return self._deliver_plate(km, x, z, op, st, flow)

    def _skip_already_on_spot(self, ops: list) -> list:
        """组装台面上已经有某个材料了 → 把它那一组(fetch/chop/cook/mix/assemble)整组跳过。

        `derive()` 是**纯静态**的: 每次都从订单定义从头推一遍, 完全不看台面上已经放了
        什么。叠加"失败 → 重新规划 → 从头执行", 就会反复取同一份材料。
        (用户实测: 三个食材, 1、2 已经加过、缺 3, 脚本却回头又拿 1 去补。)

        判定故意**保守**: 归一化后**精确相等**才算命中, 认不出来就不跳。
        漏跳只是回到旧行为(多取一次), 误跳却会让这单缺料做不出来 —— 那是更糟的失败。
        (不用子串: `_norm` 的注释里说过 `SushiPrawn` 与 `SushiPrawnCooked` 会互相包含。)

        分组依据: `derive()` 对每个材料按顺序产出
        `fetch(raw) → [chop] → [cook/mix] → assemble(name)`, **以 assemble 收尾**。

        ☠ **已知的交错链路风险(2026-09-15 用户点名要求检查, 确认存在)**:
          这段判据是**按材料名、跨订单**的, 而"归属"这件事根本没有被表达 ——
            ① `assemble_spot` **整局粘住同一个台面**(见 `pick_assemble_spot`), 所有订单都用它;
            ② 订单过期/失败后, 它留在台面上的**半成品没有任何人清**(全仓没有"丢弃/清台面"逻辑,
               唯一的垃圾桶用法是捣蛋鬼的 SNACK 和"传送带送到垃圾桶");
            ③ 于是下一张订单开跑时, `have` 里混着**上一张订单的残料**, 而判据只看名字。
          两个方向的错都会发生, 且都**看不出来**:
            · **误跳** —— 别的菜的残料让本单的 `fetch→assemble` 整组被跳 ⇒ 本单永远缺那份
              自己的料; 极端情况下 A 的残料 + B 的料会**凑成第三道菜**(游戏按集合匹配,
              多一样少一样都不认);
            · **看不见** —— 料散在**别的**台面上时这里看不到(只看 `self.assemble_spot`)⇒
              重复取料。memory 里那条"同一份海带取了 7 遍"就是这个方向。
          而且这是**会累积**的: 垃圾不消失 ⇒ 之后每张订单只要材料名撞上那堆垃圾就被误跳,
          失败面随时间扩大 —— 这正是"行为像黑盒"的成因(同一段代码, 行为取决于不可见的累积状态)。
          ⇒ 真要修, 判据应从"**这个名字在不在台面上**"换成"**这一盘是否已经匹配本单**"
            (`_ready_plates` 已经在算 `req <= have <= req|opt`, 那才是集合级、跨订单安全的)。
            在那之前, 下面那行日志把"台面上到底有什么 / 本单要什么"打全, 让实机能分辨复用还是污染。
        """
        spot = self.assemble_spot
        if spot is None or not ops:
            return ops

        # 用**当下**的台面快照, 而不是可能陈旧的那一份(布局每秒才重扫一次)
        st = self.state()
        km = self.map(st) if st else None
        if km is not None:
            fresh = km.stations.get(spot.id)
            if fresh is not None:
                spot = fresh

        have = set(self._norm(o) for o in (spot.on or []))
        have |= self._plate_contents_on(spot)
        if not have:
            return ops

        out, group = [], []
        # 本单**要哪些**材料(必需/可选分开) —— 只用来打日志, 好让实机一眼看出
        # "台面上那个东西到底是本单的、还是上一张订单的残料"(见函数注释的交错风险)。
        want_req = sorted(set(self._norm(o.target) for o in ops
                              if o.action == "assemble" and not getattr(o, "optional", False)))
        want_opt = sorted(set(self._norm(o.target) for o in ops
                              if o.action == "assemble" and getattr(o, "optional", False)))
        for op in ops:
            group.append(op)
            if op.action != "assemble":
                continue
            mat = self._norm(op.target)
            if mat and mat in have:
                _extra = sorted(have - set(want_req) - set(want_opt))
                self.log(f"[步骤] 台面 {spot.id} 上已有 {op.target} —— "
                         f"跳过这 {len(group)} 步({group[0].action} {group[0].target} 起)"
                         + (f" | ⚠ 台面上还有本单不要的: {_extra}(可能是别的订单的残料)"
                            if _extra else "")
                         + f" | 台面全部: {sorted(have)} | 本单要: {want_req}")
            else:
                out.extend(group)
            group = []
        out.extend(group)          # 尾部不以 assemble 收尾的(例如 deliver)
        return out

    # ---------------- 评分: 选"下一步做哪个 op" ----------------
    def _mate(self, st):
        """队友的 `(x, z, held, 静止秒数, 是不是人)`; 没有队友就返回 None。

        规格: "对方是人类时, 帮助他评距离和可达性, 再和自己的比较"。

        **不需要任何新通信**(交接包 §4.1): 共享世界里本来就有两只厨师的实时位置,
        "他去不去得了"我这边用**同一张地形图**再 BFS 一次就算出来了 —— 和算自己的完全一样。

        "静止秒数"就是规格里那句"他也到不了/**没动** → 我做"的"没动":
        没有它, 对着一个发呆的人会**永远让位**, 脚本干站着一步不动。
        ⚠ 判据里带上 `held`: 人站在锅边等熟 / 在板前切菜, 坐标长时间不动但**那是正常操作**,
          只看坐标会把人家正在干的活抢了(所以 AFK_SECONDS 也取得大, 见 scoring)。
        """
        if self.world is None:
            return None
        try:
            others = self.world.others(self.cid)
        except Exception:
            return None
        if not others:
            return None
        o = others[0]
        ox, oz = float(o.get("x", 0.0)), float(o.get("z", 0.0))
        held = str(o.get("held") or "")
        key = int(o.get("id", -1))
        now = time.time()

        prev = self._mate_track.get(key)
        if prev is None:
            self._mate_track[key] = (ox, oz, held, now)
            still = 0.0
        else:
            px, pz, pheld, pt = prev
            moved = ((ox - px) ** 2 + (oz - pz) ** 2) ** 0.5
            if moved >= scoring.MATE_MOVE_EPS or held != pheld:
                self._mate_track[key] = (ox, oz, held, now)
                still = 0.0
            else:
                still = now - pt
        return ox, oz, held, still, bool(self.teammate_is_human)

    def _op_target_for_score(self, km, st, op, x: float, z: float,
                             tm=None, reach=None):
        """给评分用: 这一步"要去哪儿" —— 返回 `((tx,tz), label)` 或 `(None, 原因)`。

        ⚠ **只调 `do_op` 用的那些同一个 helper**, 一行判定规则都不抄。
          抄一份迟早会漂, 而漂了以后的后果是**排序不对**(不是动作不对) ——
          这个函数**只用来决定"下一步做哪个", 永远不执行任何动作**:
          `do_op` 到时候仍会自己解析一次真正的目标。
          所以就算镜像错了, 最坏也只是选错顺序, **不会走错地方**。这是它敢叫"镜像"的前提。
        ⚠ **绝不改状态**: 两个选灶台的都要传 `claim=False`(它们默认会在黑板上占位);
          `assemble` 只读 `self.assemble_spot`, **绝不调 `pick_assemble_spot`**
          (那个会写 `_assemble_sid` 和黑板 —— 正是"同一份材料取了 7 遍"那个 bug 的现场)。
        """
        a = op.action
        if a == "fetch":
            # ☠ **计划坐标只按距离挑过**(`find_source`), 可能挑中隔着墙/对面厨房那个。
            #   实测 `s_summer_1_4`: 计划给的两个货源**都够不着** ⇒ 两行 `fetch` 被判死
            #   ⇒ 8 个候选一个不剩 ⇒ 停机烧整局。而执行侧 `op_fetch` **本来就会**按可达性
            #   重挑一个(`_fetch_source_live` / `_reach_ok_pred`) —— 评分这侧漏了同一条规则,
            #   于是"能做的事"被判成"不可达", 永远轮不到执行。
            #   ⇒ 计划坐标够不着时, 用**执行侧同一个** `_fetch_source_live` 现场重挑。
            # ☠ **先现场看一眼, 再退回计划坐标**(用户原话: "当前订单需要的料…只需要集齐
            #   需要的食材" ⇒ **脚边就有的别跑去开箱子**)。计划坐标是 `find_source`
            #   按距离挑的, 它**看不见台面上/地上的现货** —— 于是"地上躺着一块要用的料,
            #   脚本径直去了箱子"(实机被用户抓到)。
            _live = self._fetch_source_live(km, st, op, x, z, tm=tm, reach=reach)
            if _live is not None:
                return (_live.x, _live.z), (getattr(_live, "id", "") or op.at_name or "?")
            tgt = (op.at_x, op.at_z) if (op.at_x or op.at_z) else None
            if tgt is not None and tm is not None and reach is not None:
                if self._stand_cell_of(tm, tgt[0], tgt[1], x, z,
                                       ortho_only=True, reach=reach) is not None:
                    return tgt, (op.at_name or "?")
                s = self._fetch_source_live(km, st, op, x, z, tm=tm, reach=reach)
                if s is not None:
                    _k = (op.action, op.target, s.id)
                    if _k not in self._src_swap_told:       # 每个决策点都会走到这里, 别刷屏
                        self._src_swap_told.add(_k)
                        self.log(f"[评分] ⚠ 计划货源 {op.at_name or '?'}"
                                 f"@{tgt[0]:.1f},{tgt[1]:.1f} 够不着 → "
                                 f"改用够得着的 {s.id}@{s.x:.1f},{s.z:.1f}")
                    return (s.x, s.z), s.id
                return None, (f"够不着: 计划货源@{tgt[0]:.1f},{tgt[1]:.1f}, "
                              f"也没有别的够得着的货源")
            if tgt is None:
                # 计划里压根没有货源坐标 —— 再现场找一次(箱子可能刚刷出来/知识表没认出来)
                s = self._fetch_source_live(km, st, op, x, z, tm=tm, reach=reach)
                if s is not None:
                    return (s.x, s.z), s.id
                return None, "无货源坐标(现场也没找到能出它的箱子/台面)"
            return tgt, (op.at_name or "?")
        if a == "chop":
            # ⚠ **和 `op_chop` 用同一个 `_pick_board`** —— 不能一个挑"最近那块"、
            #   另一个挑"收得下的那块": 判据一旦漂开, 就会出现"评分算的是这块板、
            #   执行跑去了那块板"(这项目已经栽过好几次)。
            b = self._pick_board(km, x, z, op.target)
            return ((b.x, b.z), b.id) if b else (None, "没有能用的切菜板(都被占着?)")
        if a == "mix":
            # ⚠ **搅拌台不是切菜板**: 去的是 `MixingStation`(`sem == "mix"`)。
            #   原来这两类共用一行"最近的切菜板", 于是 mix 永远指着一个板子 ——
            #   而 `op_mix` 去了那儿只会把料放板上(切成片而不是搅匀)。
            mk = km.nearest("mix", x, z)               # 与 op_mix 同一行
            return ((mk.x, mk.z), mk.id) if mk else (None, "没有搅拌台")
        if a == "cook":
            want_pot = bool(getattr(op, "in_pot", False))
            s, _ck = self._find_pot_with(km, x, z, op.target, claim=False)
            if s is None:
                s, _p, _c = self._pick_stove(km, x, z, want_pot, claim=False, ing=op.target)
            if s is None and want_pot:
                s, _p, _c = self._pick_stove(km, x, z, False, claim=False, ing=op.target)
            return ((s.x, s.z), s.id) if s else (None, "没有能用的灶台")
        if a == "assemble":
            sp = self.assemble_spot                     # 只读 —— 见上面那段警告
            return ((sp.x, sp.z), sp.id) if sp else (None, "还没挑摆盘位")
        if a == "deliver":
            sv = km.nearest("serve", x, z)              # 与 618 / 2911 同一惯用法
            return ((sv.x, sv.z), sv.id) if sv else (None, "没有送餐口")
        # **传球**: 目标是**队友**(探测阶段写进 `at_x/at_z`)。
        if a == "pass":
            if op.at_x or op.at_z:
                return (op.at_x, op.at_z), (op.at_name or "队友")
            return None, "传球: 没解析到队友位置"
        # **杂活**: 目标在**探测阶段**就解析好了(`_chore_candidates` 写进 `at_x/at_z`),
        # 这里直接拿 —— 和 `fetch` 的"已知货源"同一条路。
        # ⚠ 探测和执行必须看同一个台面, 否则会"按 A 台算的分、走到 B 台去干"。
        if a in CHORE_ACTIONS:
            if op.at_x or op.at_z:
                return (op.at_x, op.at_z), (op.at_name or a)
            return None, f"{a}: 没解析到目标"
        return None, f"空操作({a})"

    def _cook_takeout(self, km, x, z, op) -> bool:
        """这一步 `cook` 是**"从锅里取菜"**那一半吗(锅里**已经**有要煮的那份了)。

        ☠☠ **为什么非要有这个判据**(2026-09-15 实机打回来: 一局里 `✗ 放弃: cook SushiRice`
          出现 **3 次**, 每次 3 个重试 ⇒ **9 次白按**, 还把整单打进冷板凳一次):
          `cook` 一个动作名底下其实是**两个前置完全相反**的阶段(依据在 `_cook`):
            · **下锅**(锅里还没有): 走 `_cook` 第 2 步 —— `if not held: return False`
              (`neko/engine.py:3938-3940`) ⇒ 要求**手上有生料**, 走过去放进去;
            · **取菜**(锅里已经有了): 跳过第 2 步, 直接 `_get_plate_for_pot`
              (`neko/engine.py:3856-3862`), 而它第一句就是
              `if held: 不是盘子 ⇒ return False` ⇒ 要求**手空或者端着盘子**。
          而"锅里已经有 SushiRice、手上又攥着一份 SushiRice"这个状态**同时满足了两边**:
          `_op_actionable` 判它**能做**, `do_op` 又把那份预置的料**塞回手上**,
          选中后一头撞进 `_get_plate_for_pot` 的硬前置 →
            `⚠ 手上有 'SushiRice' 不是盘子, 没法去锅里取菜` → `⚠ 没有盘子可取菜`
            → 重试 3 次 → `✗ 放弃: cook` → **锅还在灶上糊着没人救**。
          ⇒ 判据只此一份, `_op_actionable`(能不能做) 和 `do_op`(去不去捡预置)都调它。

        只对 `in_pot=True` 成立 —— `in_pot=False` 的煮法没有"取菜"这一半(熟了用手拿)。
        """
        if op.action != "cook" or not getattr(op, "in_pot", False):
            return False
        if x is None or z is None:
            return False
        try:
            return self._find_pot_with(km, x, z, op.target, claim=False)[0] is not None
        except Exception:
            return False

    def _op_actionable(self, km, st, op, x: float, z: float, held: str,
                       strict: bool = True, idx: int = -1, ops=None, pending=None,
                       flow=None, steps=None):
        """到了那儿**有活可干**吗 —— 返回 `(能不能做, 原因)`。

        可达性说的是"我到得了", 这里说的是"到了以后干得了" —— **两件事**。

        ☠ **必须看"手上拿的是不是这一步要的那个东西", 不能只看"手上有东西"** ——
          这是实机实测打回来的(2026-09-14, s_balloon_2_3):
          当时写的是 `if held: return True`, 于是拿着生肉 BurritoMeat 时
          `assemble Pasta`(60 分) 被判"能做"并选中, `op_assemble` **不核对材料**
          直接把生肉放上了盘子 —— 不是"选错顺序", 是**做错动作**, 然后三步全废。
          同时 `deliver`(100 分) 也因为"手上有东西"一直拿最高分, 拿着生肉走到送餐口,
          一局 150 秒全耗在"送餐→失败→重新规划→再送餐"上。
          ⇒ **假"能"的代价是错的物理动作, 不是排序**, 所以这里要严。

        `strict=False` —— 放宽档(老行为: 手上有东西就算能做)。
        由 `_rank_candidates` 在"严格档一个候选都不剩"时兜底, 见那里的注释。

        ☠☠ `steps` —— **还没做完的全部菜谱步骤**(见 `_rank_candidates` 的同名参数)。
        下面**两处**"上游/别的步骤还没做完吗"的扫描都用它, **不能用 `pending`** ——
        `pending` 是候选集(冷板凳上/已交出去的那两类已经被剔掉了), 拿它问等于把
        那两类**当成不存在** ⇒ 下游的闸门整个失效。不传 ⇒ 退回 `pending`(老调用逐字不变)。
        """
        a = op.action
        # ☠ **这一份是"还没做完的步骤全集"** —— 两处扫描共用, 别各写一份
        #   (`_rank_candidates` 的 `steps` 注释里有实测账)。
        _all = pending if steps is None else steps
        # **杂活**: 一次只能拿一个东西 ⇒ 手上有东西时一件都做不了(会和主流程抢手)。
        # ⚠ 这里**不看 `strict`**: 杂活的前置判据是硬的(手空 + 探测阶段已经验过台面在不在),
        #   放宽档是给"名字认不准"的菜谱材料用的, 与杂活无关。
        if a in CHORE_ACTIONS:
            if held:
                return False, f"手上有 {held}, 杂活要先腾手"
            if not (op.at_x or op.at_z):
                return False, f"{a}: 没解析到目标"
            return True, ""
        if a == "fetch":
            # ☠ **这里不再判"有没有货源"** —— 那个判据现在只有一处: `_op_target_for_score`
            #   (它按 计划坐标 → 实时台面 → 可达箱子 → **地上的料** 依次解析, 解析不到才返回 None)。
            #   原来这里写的是 `(True,"") if (op.at_x or op.at_z) else (False,"无货源坐标")`,
            #   于是**计划里没有货源的料**, 哪怕就躺在脚边, 也会在走到 `_op_target_for_score`
            #   之前被毙掉 —— 实机日志里那条 `不可达(无货源坐标)` 就是这么来的(用户: "不会拿地上的食物啊")。
            return True, ""
        # **菜谱顺序闸门**: 这个材料自己的前置步骤还没做完 → 现在做它没意义。
        # 见 OP_PREREQ 的注释(生料和切好的料名字同源, 光比名字分不出阶段)。
        #
        # ☠ **这一条不受 `strict` 影响** —— 和下面 `deliver` 那三处同理:
        #   它不是"名字猜不准"的兜底项, 而是**正确性前提**(生料上盘游戏直接拒收)。
        #   原来这里写的是 `if not strict: break`(放宽档就跳过), 而**放宽档恰恰是在
        #   "严格档一个候选都不剩"时触发的** —— 也就是说:**链子越堵, 这道闸门越不设防**:
        #     拿着的生料要煮/要搅(灶台/搅拌机不可用) ⇒ 严格档全灭 ⇒ 退放宽档
        #     ⇒ `assemble`(60分!) 不再被闸门拦 ⇒ **径直把生料放上盘子**。
        #   用户实机原话: "**傻傻的把食材放盘子上，但是这种食材可能需要切完，
        #   和其他食材混合搅拌，放到烤箱**" —— 就是这一条。
        _need = OP_PREREQ.get(a)
        if _need and ops is not None and _all is not None:
            _tn = self._norm(op.target)
            # ☠☠ **只认"链上紧邻的那一步", 不是"全表里任何一个同名前置"**。
            #
            #   为什么(2026-09-15 另一台机器跑当前版本打回来的):
            #     同名料出现两次时(两张单都要鱼 / 一张单要两条), `derive()` 会产出
            #     `fetch X, chop X, assemble X, fetch X, chop X, assemble X, …`。
            #     原来那句"扫全表找同名前置"于是会拿**第二份还没取的 `fetch X`**
            #     去拦**第一份的 `chop/assemble X`** ⇒ 实测症状就是
            #     "**拿到食材后原地折腾, 一直不加工**"(把'下一份备料'误当成当前
            #     这份的前置步骤)。
            #   ⇒ 判据改成: 取**我前面最近的那一个**同名前置(下标最大的 `_j < idx`),
            #     只有**它**还没做完才拦。`derive()` 给每个叶子产出的是**连续一段**
            #     (`fetch → [chop] → [cook|mix] → assemble`), 所以"最近的那个"
            #     必然就是**我自己这一份**的前置, 不会串到别的份上。
            #   ⚠ **不能只看 `_all` 里的** —— 要先把"最近的那个"找出来(它可能**已经做完了**),
            #     再问它做没做完。只扫 `_all` 会让"我的前置做完了、但更早那份的还没做"
            #     这种局面**误拦**(而那时正解是让我继续加工)。
            #   ⚠ 下标从 `idx-1` 往前走, 天然不会跨过 `idx` 去碰后面那份。
            _near_j = None
            for _j in range(idx - 1, -1, -1):
                _oj = ops[_j]
                if _oj.action in _need and self._norm(_oj.target) == _tn:
                    _near_j = _j
                    break
            if _near_j is not None and _near_j in _all:
                _oj = ops[_near_j]
                # ☠ **"还没轮到"和"这一轮根本没人做"要分开报** —— 两者的下一步
                #   **完全不同**: 前者等着就行, 后者是**链子断在这里**(要么等队友
                #   做完、要么得靠 `_redos` 回溯/换单)。日志是省掉测试后的唯一线索,
                #   混成一句话就只能靠猜(交接包 §5.1: 算了就要用)。
                #   ⚠ 判据和 `_execute_scored` 那边**同一份**(`step_benched` 的 key
                #     是 `"{action} {target}"`, `handoff_live` 是 `(action, target)`)。
                _why = f"还有前置步骤没做({_oj.action} {_oj.target})"
                if self.step_benched(f"{_oj.action} {_oj.target}"):
                    _why += " —— 它**在冷板凳上**, 这一轮不会有人做它"
                elif self.handoff_live(_oj.action, _oj.target):
                    _why += " —— 它**已经交给队友了**, 等他做完才轮到这一步"
                return False, _why
        if a == "pass":
            # **传球**: 手上得拿着要丢的那份料(丢的是"手上的东西"), 且得有队友接。
            if not self._held_is(held, op.target):
                return False, (f"手上是 {held!r} 不是 {op.target}" if held
                               else f"手空 —— 要先把 {op.target} 拿在手上才能丢")
            return True, ""
        if a == "mix":
            # **搅拌**: 手上必须拿着要搅的那个(搅拌台是"把料放进去自动搅")。
            # ⚠ 和 `chop` 不同: **不看搅拌台上有什么** —— 台上那个是**容器**,
            #   容器里有东西 ≠ 里面有"我要搅的这个"(容器可以装着别的料在搅)。
            #   ⇒ 判据只能看手, 否则会"拿着 A 跑去搅别人正在搅的 B"。
            if self._held_is(held, op.target):
                if km.nearest("mix", x, z) is None:
                    return False, "这关没有搅拌台"
                return True, ""
            if not strict and held:
                return True, ""
            return False, (f"手上是 {held!r} 不是 {op.target}" if held
                           else "手空(搅拌要先把料拿在手上)")
        # ☠☠ **没加工完的料上不了盘 —— 游戏自己的判据, 不是我们的偏好。**
        #   反编译链(规则 1):
        #     台面上**有盘子** → `ClientPlacementContainer.CanHandlePlacement`
        #                        (`ClientPlacementContainer.cs:19`)
        #       → `PlacementContainer.CanCombine`(`PlacementContainer.cs:7`)
        #       → 要求 `CanTransferToContainer` **且** `CanAddIngredient(composition)`
        #     台面**是空的**   → `AttachStation.CouldAttachToSelfIfEmpty`
        #                        (`AttachStation.cs:89`) 要求 carried item 有 `IAttachment`
        #   生料两处都过不去 ⇒ 游戏回 `placeCanHandle=False`, **放置根本不会发生**。
        #   用户原话(已记在 `_advance_bonus` 的 docstring 里, 只是从没变成判据):
        #     "比如 SushiRice, 想要放在盘子上需要**先煮熟**"。
        #   ⇒ 拿着生料去 `assemble` 不是"排序不好", 是**执行一个错的物理动作**:
        #     实机日志里连试 3 次 `placeCanHandle=False` → `✗ 放弃: assemble`
        #     → **整单报废**(`Sushi_PlainPrawn` 就是这么死的)。
        #
        #   ⚠⚠ **必须排在这一段(`_preposed_ok` 捷径)之前** —— 那条捷径会在
        #     "料被预置在附近"时直接 `return True`。写在 `assemble` 分支里会被它绕过去。
        #   ⚠ 与 `deliver` 那三处同理: **不受 `strict` 影响** —— 它不是"名字猜不准"的
        #     兜底项, 而是**正确性前提**; 放宽档也不许绕过去, 否则恰好是
        #     "链子越堵这道闸门越不设防"(那条注释里的老教训)。
        #   ⚠ 判据用 `_needs_work`(知识表里还有 `next` = 还能变成别的东西),
        #     **不是比名字** —— 生料和切好的料名字同源, 比名字认不出阶段。
        #     对盘子/锅/成品料它自然为 False(知识表里查不到 / 没有 next)。
        if a == "assemble" and held and self._needs_work(held):
            return False, (f"手上的 {held!r} **还没加工完**, 上不了盘"
                           f"(游戏 CanHandlePlacement=false)")

        # ☠☠ **`cook` 的取菜闸门必须排在这一段 `_preposed_ok` 捷径之前** ——
        #   和上面 `assemble` 的阶段闸门同一条道理。实测 2026-09-15: 那 3 次
        #   `✗ 放弃: cook` 正是**两条路各死掉一部分** —— 有的走这个 cook 分支
        #   (`_held_is` 命中), 有的走的是下面那条捷径(`选(预置在附近(执行时先去捡))`)
        #   —— 只堵分支不堵捷径, 等于没堵。
        if a == "cook" and self._cook_takeout(km, x, z, op):
            # **取菜**那一半: 只剩"拿盘子去锅里取", 手必须空着或者已经端着盘子。
            # ⚠ 这一条**不受 `strict` 影响**: 它不是"名字猜不准"的兜底项, 而是
            #   `_get_plate_for_pot`(`neko/engine.py:3856-3862`) 的**硬前置**。
            #   放宽档绕过去, 恰好会在"链子最堵"的时候把最堵的那一步放出来
            #   (与上面 `deliver`/`assemble` 两道闸门同理)。
            #   手上有料时正解是**先腾手**(空手回退那条路), 不是硬按。
            if not held:
                return True, ""
            if self._is_plate(held):
                # ☠☠ **盘里已经有这份料了 ⇒ 这一步**现在**做不了**(2026-09-15 实机打回来的)。
                #   用户原话: "**主要是脚本用带米的盘子取接米, 那确实接不下来**"。
                #   原来这道闸门问的是"**是不是盘子**"就放行 —— "盘子"和"空盘子"之间的
                #   差别**从来没进过可行性层** ⇒ 判据说能做、评分选它、走到锅边按 3 次
                #   锅一动不动, 整步判失败。`_get_plate_for_pot` 那边我上一轮补了同一条核对,
                #   但**那只是执行层**: 步骤已经被选中了。可行性验证的意义就是**别选它**。
                #   ⚠ 判据和执行侧**同一份**(`_held_contents` = 插件 `heldhas`);
                #     旧 dll 没这个键 ⇒ 放行(退回旧行为), 见 `_held_contents` 的约定。
                _c = self.chef(st) or {}
                if "heldhas" in _c:
                    _have = self._held_contents(st)
                    if self._norm(op.target) in _have:
                        return False, (f"手上这盘里**已经有 {op.target}** 了"
                                       f"(盘里={sorted(_have)}) —— 拿它去锅里接同一份, "
                                       f"游戏不会接(实测按 3 次锅一动不动); "
                                       f"先把这盘摆上去/放下, 再取")
                return True, ""
            return False, (f"锅里已经有 {op.target} 了, 取菜要**空手或端盘子**"
                           f"(现在手上有 {held!r}) —— 先腾手再取")

        # **预置在附近也能做** —— 那就跟"手上拿着"一样成立: 执行时 `do_op` 会先去捡
        # (`_pickup_preposed`)。不加这一条, 候选会被打成 `-inf`, 永远轮不到执行。
        # 依据: 用户 2026-09-15 "鼓励丢食物" —— 丢出去手上就空了, 而流程还得能接着走。
        if a in ("chop", "cook", "mix", "assemble") and self._preposed_ok(km, op.target) \
                and not self._is_plate(op.target):
            return True, "预置在附近(执行时先去捡)"
        if a == "chop":
            if self._held_is(held, op.target):
                return True, ""                        # 手上就是它 → 上板切
            if held:
                # ☠☠ **手上拿着**别的东西**时, 不许去切**(2026-09-15 用户点出来的:
                #   "生米无法和切板交互这条链就应该直接裁掉")。
                #   根因在**执行层**: `op_chop` 到了板前第一件事是
                #     `if held: interact("pickup", verify_hold_change=False)` —— "先放上板"。
                #   手上是别的东西 ⇒ 那一下**就是把那件废料放到板上**。实测那局:
                #   手持**生 `SushiRice`** 去切板上那块 `SushiFish` ⇒ 板子被塞进米
                #   ⇒ 十下 `use` 全失败 ⇒ 而日志把它归因成**"站位/朝向"**
                #   (`placeh` 对得上但 `m_interactable` 是空的) —— 一查就是半个钟头,
                #   因为真正的原因在**手上**, 不在脚下(见 `neko-placeh-vs-interactable`)。
                #   ⇒ 判据: 上板之前手必须是**空的**或者**拿着要切的那个**。
                #   ⚠ **不受 `strict` 影响** —— 它不是"名字猜不准", 而是**会做错物理动作**
                #     (把不相干的料放上板, 板子当场废掉 + 冷板凳 25 秒)。
                #     所以放在"板上有它 ⇒ 直接切"那条**前面**。
                return False, (f"手上是 {held!r}, 不是 {op.target} —— 上板前那一下会把它"
                               f"放到板上, 板子就废了; 先腾手再来切")
            b = self._pick_board(km, x, z, op.target)
            # ☠ **板上有东西 ≠ 板上有"要切的这个"**。原来这里只判 `b.on` 非空, 于是
            #   `chop Chocolate` 在"板上躺着别人切剩的 ChoppedDriedFruit"时照样成立
            #   ⇒ 白走过去、白按十几刀、最后"板上物品没有变化"再失败 ——
            #   正是"假'能'的代价是**执行一个错的物理动作**"那一类(实机 2026-09-14)。
            # ⚠ `_pick_board` 已经把"被占着的板"排除掉了, 所以这里 `b is None` 就是
            #   **真的没有能用的板** ⇒ 直接判不可做, 别退放宽档(见 `_pick_board`)。
            if b is None:
                return False, "没有能用的切菜板(都不存在或被别人的东西占着)"
            if any(self._held_is(o, op.target) for o in (b.on or [])):
                return True, ""                        # 板上就是要切的它 → 直接切
            if not strict and held:
                return True, ""
            _bon = (b.on or [None])[0] if b is not None else None
            return False, (f"手上是 {held!r} 不是 {op.target}" if held
                           else (f"板上是 {_bon!r} 不是 {op.target}" if _bon
                                 else "手空且板上没东西"))
        if a == "cook":
            # **下锅**那一半(取菜那一半已在上面闸门处理掉, 见 `_cook_takeout`):
            # 手上的生料要放进去 —— 或者锅里已经"等着我下锅"的那份就是手上这个。
            if self._held_is(held, op.target):
                return True, ""
            if not strict and held:
                return True, ""
            return False, (f"手上是 {held!r} 不是 {op.target}" if held
                           else "手上没有可煮的")
        if a == "assemble":
            # 手上端着盘子也算 —— "用锅煮"的菜取出来时手上已经端着那盘菜了,
            # 见 op_assemble 的 docstring。
            # (⚠ 阶段闸门不在这儿 —— 它必须排在下面 `_preposed_ok` 那条捷径**之前**,
            #  见那一段的注释。)
            if self._held_is(held, op.target):
                return True, ""
            if self._is_plate(held):
                # ☠☠ **"端着盘子"≠"摆好了"** —— 这个豁免本来是为一条**正路**开的
                #   ("用锅煮的菜取出来时手上已经端着那盘菜了"), 但它**不查那盘里是什么**。
                #   ⇒ 空盘也放行 ⇒ 走到台面前**把空盘一放**就被当成 `assemble` 做完。
                #   实机 2026-09-15 `s_sushi_1_3`(**两单一起废**)的账:
                #     `手上端着盘子 'equipment_plate_01 (3)' —— 直接放到 counter15`
                #     → `✓ assemble Cucumber`   ← 中间**没有**"进了那盘"那一行(真进盘才有)
                #     → 下一步 `deliver Sushi_Cucumber`(100 分)被选中
                #     → `⚠ 走到组装台面 counter15 时,**那盘已经不是这道菜了**` → 交不掉。
                #   ⚠ 判据用 `heldhas`(`_held_contents`), 读不到(旧 dll)**放行** —— 退回旧行为。
                _c = self.chef(st) or {}
                if "heldhas" in _c:
                    _have = self._held_contents(st)
                    if not _have:
                        return False, ("手上端着的是**空盘** —— 放上去什么也没组装, "
                                       "不是「把材料放进盘里」; 别把它当成 assemble 做完")
                    # ☠ **还不能是别的单的菜**: 那盘里若有**本单不要**的东西, 放上去就是
                    #   **两单的菜串在一起**(实测: `ChoppedSushiPrawn 进了 counter15 那盘
                    #   (['cucumber','seaweed','sushirice'] → 加了 sushiprawn)`)。
                    #   判据复用 `_dish_sets`(`_deliver_plate`/`_spot_still_ready` 同一份)。
                    _foreign = self._dish_foreign(_have, flow)
                    if _foreign:
                        return False, (f"手上这盘里 {', '.join(sorted(_foreign))} "
                                       f"**不是本单要的** —— 放上去会串进别人的菜"
                                       f"(`_dish_foreign`, 和挑摆盘位同一份判据)")
                return True, ""
            if not strict and held and self._same_material(held, op.target):
                # ☠☠ **放宽档不等于"手上有东西就行"**(2026-09-15 实机 `s_sushi_1_2` 打回来的)。
                #   原来这一句是 `if not strict and held: return True` —— 于是**手上拿着
                #   完全不相干的料**也放行: 那一局手里攥着**生的 SushiRice**, 却选中了
                #   `assemble SushiFish`(严格档恰好全灭 ⇒ 退放宽档 ⇒ 这句放行)。
                #   后果是**一条链**: 走到盘子前 `placeCanHandle=False`(生米上不了盘)
                #   → 这一支划掉 → **`assemble_spot` 被清成 None** → 评分表里所有
                #   `assemble` 全 `-inf` → 候选全灭 → 把米**丢在地上**腾手
                #   → 回头还得去捡那粒米(而它就贴在台面边上, 抓取目标被判成台面)…整单报废。
                #   放宽档**本来**要救的是"名字认不准"(如 `ChoppedSushiPrawn` vs `SushiPrawn`),
                #   所以这里要的是"**同一族的料**", 不是"手里有东西"。
                #   ⚠ 判据用 `_same_material`(**只有这一份**, 和 `_find_item_station` 的
                #     loose 档、`cookbook` 的 `_same_family` 同一套"同族"意思)。
                return True, ""
            return False, (f"手上是 {held!r}, 不是 {op.target}/盘子" if held
                           else f"手空, 没东西可摆(料不在手上 —— 可能在台面/板上)")
        if a == "deliver":
            # ⚠⚠ **这一段不受 `strict` 影响** —— 它**不是**"名字猜不准"的兜底项,
            #    而是用户给的**正确性前提**, 放宽档**也不许**把它绕过去。
            #    (实测: 一开始把 ①② 也写成 strict-only, 结果严格档全灭 → 退回放宽档
            #     → deliver 又被放回来了, 前提形同虚设。)
            #
            # 只有端着**盘子**才是真能送。拿着生料送餐 = 走到送餐口干站着,
            # 而 deliver 步骤价 100 ⇒ 它会一直压过所有正常步骤(实测就是这么烧掉一局的)。
            if not self._is_plate(held):
                return False, (f"手上是 {held!r}, 不是盘子" if held else "手空, 没东西可送")
            # ☠☠ **"是盘子"≠"是本单那道菜"** —— 同一个洞(2026-09-15 一并补上):
            #   空盘、别人那道的菜、上一单的残料, 全都满足上面那条 `_is_plate`。
            #   而 `deliver` 步骤价 100, 一旦被判"能做"就会**压过所有正常步骤** ——
            #   实测就是这么烧掉一局的。判据复用 `_dish_matches`(`_deliver_plate` 用的
            #   **同一份**, 见那里"只此一份, 和 `_find_ready_dish`/`_spot_still_ready` 同源")。
            #   ⚠ `flow` 缺省 None(离线探针/手工调) ⇒ 跳过这一条, 退回旧行为;
            #     生产路径上 `_execute_scored` **一定**传(只有一个调用点)。
            if flow is not None:
                _c0 = self.chef(st) or {}
                _req, _opt = self._dish_sets(flow)
                if _req and "heldhas" in _c0:
                    _hav = self._held_contents(st)
                    if not self._dish_matches(_hav, _req, _opt):
                        return False, (f"手上这盘里**不是 {op.target} 要的菜**"
                                       f"(盘里={sorted(_hav) or '空'}, 本单要={sorted(_req)})"
                                       f" —— 送到送餐口也交不掉, 不白跑")
            # ---- 用户原话: "提交菜谱的前提是**这一单已经完成并且被需要**" ----
            if ops is not None and _all is not None:
                # ① **已经完成**: 还有 required 的 `assemble` 没做 ⇒ 菜根本没拼出来。
                #    ⚠ 只看**非 optional** 的 assemble —— 可选材料本来就允许不做,
                #      拿它拦住送餐会让整单永远交不出去。
                # ☠☠ **这里必须扫 `_all`(全集), 不是候选集** —— `deliver` 是 100 分,
                #   而"剩下的 `assemble` 在冷板凳上/已经交出去"时它**照旧放行** ⇒
                #   端着一盘没拼完的菜走去送餐口。这正是"上游做不了、下游还在争分"
                #   那一族里**代价最大**的一个(送不掉还占着一趟路)。
                _todo = [ops[j] for j in _all
                         if j != idx and ops[j].action == "assemble"
                         and not getattr(ops[j], "optional", False)]
                if _todo:
                    return False, f"菜还没拼完(还有 {len(_todo)} 个 assemble 没做)"
                # ② **被需要**: 订单已经不在订单栏上了 ⇒ 别去了。
                #    它是"别人已经把这单交了"的直接判据 —— 实机那次假 `✓ 已交付`
                #    就是订单在别处消失、而脚本正好站在送餐口, 把这个当成了自己的功劳。
                if not self._order_live(op.target):
                    return False, f"订单 {op.target} 已经不在订单栏上了"
            return True, ""
        return False, f"空操作({a})"                    # tool / mix: do_op 里就是 return True

    def _order_live(self, name: str) -> bool:
        """这一单还挂在订单栏上吗 —— 用户说的"**被需要**"。

        ⚠ **读不到就返回 True**(放行): 桥抽风/订单接口报错时宁可去试一次,
          也不能因为读不到订单把整条流程卡死在送餐这一步。
        """
        if not name:
            return True
        try:
            return any((o.get("name") or "") == name for o in self.live_orders())
        except Exception:
            return True

    def _rank_candidates(self, km, st, ops, pending: list, n_recipe=None, used=None,
                         flow=None, steps=None):
        """给候选 op 打分 → 按状态变换 → 降序。同时把整张表打进日志。

        一次决策只跑 **2 次 BFS**(我一份、队友一份), 然后每个候选查表 ——
        不是每个候选跑一次。

        返回**选中那个 op 的下标**(ops 里的下标), 一个都做不了时返回 None。

        `n_recipe` —— `ops` 里**前多少个是菜谱 op**(其余是杂活, 由阶段二拼在尾部)。
        默认全部是菜谱(老调用方式**逐字不变**)。这条分界线有三处用途, 少一处就会静默改行为:
          ① **严格档的兜底只看菜谱** —— 否则只要有一件杂活可达, "全灭就退回放宽档"
             这条保险丝就永不触发(它是 `_held_is` 认不出名字时唯一的救生圈);
          ② **`follow`(顺路)只跟菜谱比** —— 否则多几个杂活就会改掉菜谱候选的分数,
             阶段一验过的排序会**悄悄漂**(不报错, 只是顺序变了);
          ③ **杂活闸门**(`_chore_admitted`)要拿"菜谱候选的步数"当参照。
        `used` —— 本轮"这件杂活做过几次"的黑板, 只给闸门用。

        ☠☠ `steps` —— **还没做完的全部菜谱步骤**(不传 ⇒ 退回 `pending`, 老调用方式逐字不变)。

        为什么要跟 `pending` 分开: `pending` 是**候选集** —— `_execute_scored` 把
        "在冷板凳上"和"已经交给队友"的那两类**剔掉了**(`_fresh`)。而"这一步的上游
        还没做完吗"必须拿**全集**问, 拿候选集问会把那两类**当成不存在**:

          · `fetch X` 在冷板凳上 ⇒ `cook X`/`assemble X` 的 `OP_PREREQ` 扫描一个都找不到
            ⇒ **闸门整个失效**, 它们带着 `assemble` 的 60 分**回到表里**跟别的候选争分,
            而**唯一能喂饱它们的那一步这一轮根本没在跑**;
          · 同理 `deliver`(100 分)的"菜还没拼完"也是同一份扫描 ⇒ 剩下的 `assemble`
            一在冷板凳上它就放行 ⇒ 端着一盘没拼完的菜走去送餐口。

        ⇒ **"这一步现在可行"和"这一步现在该做"是两件事**: 前者只看这一帧的世界,
          后者还要看**链子**(上游会不会有人做)。`_feasible` 每轮都把"接下来哪些东西
          可行"算一遍, 这里补的是另一半 —— **不可行的上游, 连同它的下游一起剪**。

        ⚠ **剪枝的两条边, 缺一条都不全**(别以为 `OP_PREREQ` 已经盖住了):
          · **上游算出来不可行**(`(够不着)`/`(没有能用的灶台)`) —— 这条**本来就盖到了**,
            因为 `OP_PREREQ` 判的是"上游**还在 `steps` 里**"(不看它可不可行);
          · ☠ **上游连候选都不是**(在冷板凳上 / 已经交给队友) —— **就是缺这一条**,
            也是 `steps` 存在的唯一理由。上面那段实测账说的就是它。

        用户 2026-09-15 点名的形状(原话):
          > "可信验证不是说这个可信就去做, **需要剪枝的**, 用**菜谱需求校准**,
          >  保持更新的时候**都去计算一下接下来哪些东西是可行的**"
          > "…它得…**按本单菜谱来**" —— 所以剪枝的判据是**本单的步骤表**, 不是别的单。
        """
        if steps is None:
            steps = pending
        tm = self.terrain()
        cx, cz, held = self.pos(st)
        if cx is None or tm is None or not tm.ok:
            # 拿不到地形/位置 → **返回 None 而不是空列表**: 调用方判的是 `is None`,
            # 返回 `[]` 会让它去解包 `i, fin = []` 直接 ValueError。
            return None
        n_recipe = len(ops) if n_recipe is None else n_recipe
        recipe = [j for j in pending if j < n_recipe]
        chores = [j for j in pending if j >= n_recipe]
        edges = self._travel_edges(km, tm)
        my_reach = tm.distances_from(cx, cz, at_y=self.chef_y(st), extra_edges=edges)

        mate = self._mate(st)
        mate_reach = None
        if mate is not None:
            mate_reach = tm.distances_from(mate[0], mate[1], extra_edges=edges)

        # 1) 每个候选: 解析目标 → 可达性(拿站位格) → 可做性
        def evaluate(strict: bool, idx_list=None):
            out = {}
            for i in (pending if idx_list is None else idx_list):
                out[i] = self._feasible(km, st, ops[i], i, cx, cz, held, tm, my_reach,
                                        ops, pending, flow, mate=mate, strict=strict,
                                        steps=steps)
            return out

        info = evaluate(True)
        # ⚠ **兜底**: 严格档(要求"手上就是那东西")把候选筛空了, 就退回放宽档。
        #   闸门是用来**排序**的, 不是用来把活干没的(规则 5) ——
        #   万一某个场景的物品名和计划里的对不上(`_held_is` 认不出), 宁可回到老行为,
        #   也不能让脚本"一个能做的都没有"然后停机烧掉整局。
        # ☠ **判据只看菜谱那一段**(`recipe`) —— 杂活可达不算"菜谱还有得做",
        #   否则这条保险丝就被杂活静默拔掉了。
        if recipe and all(info[j]["cell"] is None for j in recipe):
            self.log("[评分] ⚠ 严格判据下一个候选都不剩 → 退回放宽档(只看手上有东西)")
            info.update(evaluate(False, recipe))

        # 2) 顺路: 到"其他还要做的目标"里最近那个的**步数差**。
        #    用手里这张距离表算, 不再跑 BFS —— 这是三角不等式给出的下界, 够当tiebreaker。
        #    ⚠ 分母**只含菜谱候选**: 菜谱 op 的分数不能被杂活的增减改掉(见 docstring ②)。
        flow_ds = [my_reach[info[j]["cell"]] for j in recipe
                   if info[j]["cell"] is not None]

        # 2.5) **杂活入池闸门**(用户选的"混合: 能插就插") —— 在距离算完之后、打分之前。
        #      被挡掉的把 `cell` 置空 ⇒ `dist=None` ⇒ 分数自动 `-inf`(不用另设标志),
        #      而且"为什么没进池"会进日志的判定列。
        for j in chores:
            r = info[j]
            if r["why"]:
                continue        # 已经被 `_op_actionable` 用**更具体**的原因挡了(手上有东西…)
            d = my_reach.get(r["cell"]) if r["cell"] is not None else None
            ok, why = self._chore_admitted(r["op"], d, flow_ds, used or {})
            if not ok:
                r["cell"] = None
                r["why"] = f"闸门: {why}" if why else "闸门"

        raw, sigs = [], []
        for i in pending:
            r = info[i]
            d = my_reach.get(r["cell"]) if r["cell"] is not None else None
            follow = 0.0
            if d is not None:
                others = [my_reach[info[j]["cell"]] for j in recipe
                          if j != i and info[j]["cell"] is not None]
                if others:
                    follow = float(min(abs(d - o) for o in others))
            r["dist"], r["follow"] = d, follow
            # 紧迫度(目前只有"锅快糊了"非 0): `scoring.burn_urgency` 算好的加成分。
            # 推进(手上这份先做完): 见 `_advance_bonus` —— 用**我手上**的那份算。
            raw.append(scoring.score(r["op"].action, d, follow,
                                     urgency=getattr(r["op"], "urgency", 0.0),
                                     advance=self._advance_bonus(r["op"], held)))
            sigs.append(f"{r['op'].action} {r['op'].target}")

        # 3) 队友的原始分: 只换位置相关的项 —— 步骤价对两个人都一样。
        #    ⚠ 拿**队友的位置**去解析目标, 且一律 claim=False。
        other_raw = []
        for i in pending:
            r = info[i]
            o = scoring.NEG_INF
            # ☠ **传球不给队友评分** —— 丢的是**我手上**那份料, 队友替不了。
            #   不这么写的话: 拿队友的位置去解析 `pass` 的 target(= 队友自己)必然取到
            #   "紧挨着他那格" ⇒ `od≈1` ⇒ 他那一栏凭空 ~36 分 ⇒ 我这边分低时(远距离传球
            #   只有个位数)**一直让位给人类**, 而人类根本做不了这个动作 ⇒ 传球被让掉。
            #   `choose` 的第 2 条正好是我要的: "队友 -inf ⇒ 队友也做不了 ⇒ 我做"。
            #
            # ☠☠ **原来这里还多一个 `r["cell"] is not None`(`r["cell"]` 是"**我**的站位格")
            #   —— 那是一个静默的错答**: 我够不着 ⇒ 整段跳过 ⇒ 他那一栏填 -inf ⇒
            #   于是"**我做不了 —— 那他能不能做?**"这个问题**永远答"他也不能"**,
            #   而那是传递逻辑唯一要问的问题(用户 2026-09-15 的方向就是它)。
            #   离线钉死: `runtime/_splitkitchen_probe.py` 第 6 节 —— 队友就站在砧板边
            #   (`_stand_cell_of` 取得到他的站位格), 而 `other_raw` 照样是 `-inf`。
            #   ⚠ 删掉它对**选谁**零影响: `scoring.choose` 第 1 条先把"我 `-inf`"剔掉,
            #     之后才读 `other_raw` —— 所以这一栏变准只改数据(和日志), 不改决策。
            if mate_reach is not None and r["op"].action != "pass":
                can, _oc, od, _why = self._mate_can(km, st, r["op"], mate, tm, mate_reach)
                if can:
                    # 紧迫度是**位置无关**的(锅不会因为谁去看它而少烧一秒) ⇒ 两边都加,
                    # 否则"我 vs 队友"就不是在比同一件事, 让位判定会失真。
                    # ⚠ 但"推进"要按**队友自己手上**的那份算 —— 谁拿着才算谁的。
                    o = scoring.score(r["op"].action, od, r["follow"],
                                      urgency=getattr(r["op"], "urgency", 0.0),
                                      advance=self._advance_bonus(r["op"], mate[2] if mate else ""))
            other_raw.append(o)

        # 4) 状态 = 对评分向量的变换(coop 原样 / clumsy 加噪声 / sabotage 取负+重复)
        final = self.mode_state.transform(raw, sigs) if self.mode_state is not None else list(raw)

        ok_idx = [n for n, i in enumerate(pending) if raw[n] != scoring.NEG_INF]
        if not ok_idx:
            self.log("[评分] ⛔ 候选全部不可达/不可做:")
            self._log_score_table(pending, info, raw, final, other_raw, None, mate,
                                  reach_n=len(my_reach), reach_total=self._walk_total(tm))
            return None

        # 让位**只对人类的队友开**(规格那段讲的就是人类; 双脚本局面比不了,
        # 见 scoring.choose 的注释)。
        yield_on = mate is not None and self.teammate_is_human
        chosen, verdict = scoring.choose(final, other_raw,
                                         mate[3] if mate is not None else None,
                                         yield_on=yield_on)
        self._log_score_table(pending, info, raw, final, other_raw, chosen, mate, verdict,
                              reach_n=len(my_reach), reach_total=self._walk_total(tm))
        if chosen is None:
            return None
        return pending[chosen], final[chosen]

    def apply_commands(self) -> None:
        """读一次外部命令文件并执行(`neko/control.py`)—— `run()` 每轮调一次。

        ⚠ **每一条都要打日志**: 下命令的人不在这个终端里, 日志是唯一的回执。
        """
        try:
            import control
        except ImportError:
            try:
                from . import control
            except ImportError:
                return
        for line in control.take():
            cmd, arg, rest = control.parse(line)
            if cmd == "mode":
                if self.mode_state is None:
                    self.log(f"[控制] mode {arg} —— 这一局没有模式状态(roster 没给), 忽略")
                    continue
                # ☠ **先验名字再切**: `Mode.parse` 认不出来时**兜底返回 coop** ——
                #   打错一个字母 (`mode clumsy` 写成 `mode clumzy`) 会**静默变回好帮手**,
                #   而你以为它在演笨手笨脚(测试时最坑的就是这种)。
                if arg.lower() not in ("coop", "clumsy", "sabotage"):
                    self.log(f"[控制] ⚠ {arg!r} 不是人格名 —— 只认 coop / clumsy / sabotage"
                             f"(当前仍是 {self.mode_state.mode.value})")
                    continue
                before = self.mode_state.mode.value
                self.mode_state.set_mode(arg)
                self.log(f"[控制] 人格 {before} → {self.mode_state.mode.value}"
                         f"(下一个决策点生效)")
            elif cmd == "do":
                if not arg:
                    self.log("[控制] do 缺动作名(例: do wash / do fetch Flour)")
                    continue
                self._forced = (arg.lower(), rest)
                self.log(f"[控制] 指定下一步: {arg} {rest}".rstrip())
            elif cmd == "pause":
                self._ctrl_paused = True
                self.kb.release_all()
                self.log("[控制] ⏸ 暂停(松手; 下 `resume` 继续)")
            elif cmd == "resume":
                self._ctrl_paused = False
                # 复位"暂停只报一次"的闸门 —— 否则下一次暂停在日志里就**看不见了**
                # (而日志是省掉测试后的唯一线索: "我按了暂停它到底停没停")。
                self._ctrl_paused_told = False
                self.log("[控制] ▶ 继续")
            elif cmd == "stop":
                self._ctrl_stop = True
                self.log("[控制] ⏹ 收到 stop —— 这一轮跑完就收工")
            else:
                self.log(f"[控制] ⚠ 不认识这条命令: {line!r}"
                         f"(可用: mode/do/pause/resume/stop)")

    def _forced_pick(self, pool, n_recipe: int):
        """`do <action> [target]` 指定的那一步在池子里的下标; 没指定/找不到就 None。

        匹配规则(宽到窄): `action + target` 全等 → `action` 相等 → `target` 相等。
        没对象时打清楚"现在池子里有什么", 免得下命令的人以为命令没生效。
        """
        if not self._forced:
            return None
        act, tgt = self._forced
        cands = []
        for i, op in enumerate(pool):
            a = (op.action or "").lower()
            t = (getattr(op, "target", "") or "")
            if a == act and (not tgt or t.lower() == tgt.lower()):
                return i
            if a == act:
                cands.append(i)
            elif tgt and t.lower() == tgt.lower():
                cands.append(i)
        if cands:
            return cands[0]
        have = ", ".join(sorted({f"{o.action} {o.target}".strip() for o in pool}))
        self.log(f"[控制] ⚠ 现在没有可做的「{act} {tgt}」—— 池子里是: {have}")
        return None

    def step_benched(self, key: str) -> bool:
        """这一步现在是不是在**冷板凳**上(刚反复失败过) —— 是就先别选它, 去做别的。"""
        until = self._step_bench.get(key)
        if until is None:
            return False
        if time.time() >= until:
            self._step_bench.pop(key, None)     # 到期自动回池
            return False
        return True

    # ---------------- 分支: "这一支不行" → 返回到其他路 ----------------

    def mark_branch_dead(self, target: str, sid: str, why: str = "") -> None:
        """把「`target` 在这块台面上办不成」记下来, `BRANCH_TTL` 秒内不再挑它。

        用户 2026-09-15 的原话: "**按一次没有得到对应的结果这条路就失败了, 可以返回到
        其他路**"。所以下次挑台面时换一块, 而不是原地重试、更不是把整单废掉。

        ⚠ 粒度是**台面**, 不是动作 —— 见 `BRANCH_TTL` 的注释: 记在动作上会让一块板
        走不到就把所有板封掉(实测日志里 board1/board3 走不到, 而 board0/board2 好用)。
        """
        if not sid or BRANCH_TTL <= 0:
            return
        self._branch_dead[(self._norm(target), sid)] = time.time() + BRANCH_TTL
        self.log(f"[分支] {target} @ {sid} 这一支先划掉 {BRANCH_TTL:.0f} 秒"
                 f"{('(' + why + ')') if why else ''} —— 改走别的")

    #: 判定"队友正在用某个台面"的距离(格)。交互半径是 1.0、量的是到**碰撞体表面**,
    #: 所以站得够近才用得了它 —— 取 1.6 是留一点余量(队友位置读出来有抖动、
    #: 他可能正在微调站位)。**别调大**: 太大会把"他只是路过"也当成"他在用"。
    MATE_OCCUPY_CELLS = 1.6

    def _mate_near(self, s, mate) -> bool:
        """队友是不是**正站在这个台面旁边** —— 那就是"他正在用它"。

        用户 2026-09-15 指出的真问题(实机):
          > "**他不会计算人类玩家的占用**，比如切菜板，这个位置已经被人类用了，
          >   脚本会**排队**，而不是用其他菜板。"
        而队友的坐标**本来就在地图上**(`state.layout.chefs` —— 日志里那行
        `队友@20.6,-6.2 持'SushiPrawn' 静止0.0s(人)`), `_mate()` 每次决策都取了,
        **只是从来只喂给评分让位, 没喂给选台面** ⇒ 挑中人家正在用的那块, 走过去挤。
        用户的总结: "**人类的影响已经作用在地图上了**" —— 对, 位置就在那张图里。

        ⚠ **这是"他在用", 不是"他会来用"** —— 意图读不出来, 也不该猜。
        ⚠ 调用方**必须留兜底**: 全被排除时退回不过滤的那份, 否则会出现
          "唯一一块板被队友占着 ⇒ chop 不可达 ⇒ 卡死"。见 `_pick_board`。
        """
        if mate is None or s is None:
            return False
        dx, dz = s.x - mate[0], s.z - mate[1]
        return dx * dx + dz * dz <= self.MATE_OCCUPY_CELLS ** 2

    def _branch_ok(self, target: str, sid: str) -> bool:
        """这一支还在划掉期里吗。到期自动回池 —— 世界会变(板被腾空/东西被拿走)。"""
        k = (self._norm(target), sid)
        exp = self._branch_dead.get(k)
        if exp is None:
            return True
        if time.time() >= exp:
            self._branch_dead.pop(k, None)
            return True
        return False

    # ---------------- 传递指令(台账) ----------------
    def handoff_key(self, action: str, target: str) -> tuple:
        """传递指令的账键 —— `(action, 归一化 target)`。**只有这一处拼键**。"""
        return (str(action or ""), self._norm(target))

    def mark_handoff(self, action: str, target: str) -> None:
        """记下"**这一环我交出去了**" —— `HANDOFF_TTL` 秒内不再重复交、也不再往回捡料。

        ⚠ 只在 `op_pass` **真的丢出去之后**调(失败不能记, 否则这一步就再也没人做了)。
        """
        k = self.handoff_key(action, target)
        if not k[0] or not k[1] or HANDOFF_TTL <= 0:
            return
        # 值 = `(过期时刻, 原始的名字)` —— 带上原名**只为日志**: 台账的键是归一化的,
        # 直接拿键打日志会印出 `sushifish` 这种小写串, 而日志是省掉测试后的唯一线索。
        self._handoffs[k] = (time.time() + HANDOFF_TTL, target)
        # ☠☠ **拍一张快照: 交出去的**那一刻**, 场上已经躺着的同名散件在哪**。
        #   为什么必须拍(实机 2026-09-15 `s_sushi_1_3`): 同一次决策里就报了
        #     `[传球] ✓ 丢出去了` → `[传球] 队友那边 SushiRice 已经是加工好的了`
        #   —— 人类不可能在那一瞬间把米煮好, 它匹配到的是**早就躺在地上的另一粒生米**。
        #   根因: **生米和熟米同名**, 而 C# 的 `work` 字段只覆盖"能不能切"、不覆盖"能不能煮"
        #   (`SushiRice` 两头都是 `workable=false`) ⇒ **名字分不出阶段**。
        #   ⇒ 判据换成"**跟交接那一刻的记录比**: 新出现的才算产物", 而不是"看到同名就算"。
        self._handoff_seen[k] = self._loose_spots(target)
        self._handoff_told.discard(k)          # 新的一笔, 允许再报一次"他做完了"
        self.log(f"[传球] ✓ 把 {target} 交给队友做 {action} —— 记下这一笔, "
                 f"{HANDOFF_TTL:.0f} 秒内不重复交、也不回头捡料")

    def handoff_live(self, action: str, target: str) -> bool:
        """这一环现在是不是"已经交出去了"(台账活着)。到期自动回池。"""
        k = self.handoff_key(action, target)
        ent = self._handoffs.get(k)
        if ent is None:
            return False
        if time.time() >= ent[0]:
            self._handoffs.pop(k, None)
            self.log(f"[传球] ⏳ 交给队友的 {ent[1]}({k[0]}) 等了 {HANDOFF_TTL:.0f} 秒还没回来"
                     f" —— 这一笔作废, 需要的话重新交一次")
            return False
        return True

    def _handoffs_live(self) -> list:
        """现在**还活着**的传递指令 `[(action, 归一化 target, 原始名), …]`(顺手清掉过期的)。"""
        now = time.time()
        for k in [k for k, ent in self._handoffs.items() if now >= ent[0]]:
            self._handoffs.pop(k, None)
        return [(k[0], k[1], ent[1]) for k, ent in self._handoffs.items()]

    def _loose_spots(self, target: str) -> set:
        """**现在场上**没主的、叫 `target` 的东西**在哪**(按 0.1 格量化)。

        给 `_handoff_received` 当"交接前的基线"用 —— 判"新出现的才算产物"。
        ⚠ 用**当下这一帧**(`state()` 默认不吃 TTL) —— 用户 2026-09-15:
          "游戏本质上是实时, 我们不能用备份的数据了"。
        """
        n = self._norm(target)
        if not n:
            return set()
        st = self.state()
        km = self.map(st) if st else None
        if km is None:
            return set()
        out = set()
        for it in (km.unseen_items() or []):
            if self._norm(getattr(it, "name", "")) != n:
                continue
            out.add((round(float(getattr(it, "x", 0.0) or 0.0), 1),
                     round(float(getattr(it, "z", 0.0) or 0.0), 1)))
        return out

    def _handoff_received(self, km) -> None:
        """**产物回来了没有** —— 回来了就把它记进 `_preposed`, 让下游那一步自己接上。

        判据(**只有这一份**): 世界里有一件**名字对得上**(归一化后精确相等)而且
        **已经不是生料**的东西 —— 那正是队友做完丢回来的成品
        (`_is_unprocessed` 说明为什么"是不是生料"必须问实例, 不能问名字/tag)。

        ☠ **不新增执行路径**: `_preposed` + `_preposed_ok` + `_pickup_preposed`
          本来就是"我把料放在哪了, 执行那一步之前先去捡回来", 而且 `do_op` 已经在
          `chop`/`cook`/`mix`/`assemble` 四条路上调它。所以这里只要**把产物记进那张表**,
          下一轮 `assemble Y` 就自动变成可做、`do_op` 会自己走过去捡 —— 一行就够了。
        """
        if not self._handoffs:
            return
        for (act, tn, orig) in self._handoffs_live():
            if not tn:
                continue
            found = None
            # ① 只认**没主的**那件(`unseen_items`: 既不在任何台面上、也不在任何厨师手上)
            #   —— "丢回来"就是掉在地上/台面外, 而**摆在他那半边台面上**的不算:
            #   那种我多半走不到, 记进 `_preposed` 会让 `assemble` 白跑一趟还判失败。
            #   (他在手上拿着的时候也看不见 —— 那就继续等, 他放下/丢出来就看见了。)
            # ② ☠☠ **还得是"新出现的"**: 交出去那一刻场上已经躺着的同名散件(位置在
            #   `_handoff_seen` 基线里)**不算产物** —— 否则就是把早就有的东西当成
            #   "他做完了"(实测那局当场就误报了一次, 见 `mark_handoff` 的注释)。
            _base = self._handoff_seen.get((act, tn), set())
            for it in (km.unseen_items() or []):
                if self._norm(getattr(it, "name", "")) != tn:
                    continue
                if self._is_unprocessed(it):
                    continue              # 还是生料 ⇒ 那不是成品(可能只是我丢出去那份没接住)
                _p = (round(float(getattr(it, "x", 0.0) or 0.0), 1),
                      round(float(getattr(it, "z", 0.0) or 0.0), 1))
                if _p in _base:
                    continue              # 交接**之前**就在那儿 ⇒ 不是他做的
                found = _p
                break
            if found is None:
                continue
            if (act, tn) not in self._handoff_told:
                self._handoff_told.add((act, tn))
                self.log(f"[传球] 队友那边 {orig} 已经是加工好的了 —— 记下位置, 下一步自己去捡")
            self._preposed[tn] = (found[0], found[1], time.time())

    def bench_step(self, key: str, why: str = "") -> None:
        """把一步放冷板凳 `STEP_COOLDOWN` 秒 —— 用户要求: 持续失败就去**做其他事**, 不是退出。"""
        if not key or STEP_COOLDOWN <= 0:
            return
        self._step_bench[key] = time.time() + STEP_COOLDOWN
        self.log(f"[引擎] ⏸ {key} 先放冷板凳 {STEP_COOLDOWN:.0f} 秒{('(' + why + ')') if why else ''}"
                 f" —— 这段时间去做别的(别的步骤/别的菜/杂活), 到期自动回池")

    def _walk_total(self, tm) -> int:
        """这一关的**可走格总数** —— 地形扫描时已经数好了, 直接从 `counts` 取, 不重扫。

        只给日志用: 和"我的可达格数"一比, 就知道"全部候选不可达"是
        **厨师被隔在小口袋里**(可达 ≈ 1) 还是**台面本身的问题**(可达 ≈ 总数)。
        """
        try:
            return int((tm.counts or {}).get("free") or 0)
        except Exception:
            return 0

    def _log_score_table(self, pending, info, raw, final, other_raw, chosen, mate,
                         verdict=None, reach_n=None, reach_total=None):
        """把评分表打进日志 —— **这是省掉离线测试的替代品**(交接包 §8: 一局日志要能定位)。

        四项原始值 / 变换后分数 / 为什么选它, 全摊开; 出问题时照常数直接调参。
        """
        rows = []
        for n, i in enumerate(pending):
            r = info[i]
            d = r["dist"]
            v = verdict[n] if verdict else ("不可达" if d is None else "候选")
            # 让位**必须带上数字**, 否则"为什么让"在日志里看不出来 ——
            # 而这正是协作出问题时唯一要看的东西。
            if v.startswith("让位") and other_raw and n < len(other_raw):
                v += f"(他{scoring.fmt(other_raw[n])} vs 我{scoring.fmt(final[n])})"
            # ☠ **`why` 必须打出来** —— 那是 `_op_actionable` 给的**具体原因**
            #   ("没有能用的灶台" / "手上是 X 不是 Y" / "还有前置步骤没做(...)")。
            #   原来算完就扔, 日志里只剩一句"不可达", **分不清是"走不到"还是"没灶台"** ——
            #   而这正是决定"下一步该干什么"的唯一线索(交接包 §5.1: 算了就要用)。
            if r.get("why"):
                v += f"({r['why']})"
            elif d is None:
                # `_op_actionable` 放行了, 但**站不到台面旁边**(没有可走且可达的正交邻格)
                v += "(够不着)"
            _act = f"{r['op'].action} {r['op'].target}"
            # ☠ **回溯必须看得出来**。它的 `action` 是**现成的**(`fetch`/`chop`/`cook`),
            #   光看动作名和"菜谱里那一步"**一模一样** —— 而"为什么又去取一次料"
            #   正是调参时要看的东西。见 `_redos`。
            if getattr(r["op"], "redo", False):
                _act += " ↺回溯"
            # ☠ **备料也一样** —— 它的 `action` 是现成的 `fetch`/`chop`,
            #   光看动作名和菜谱那一步一模一样; 而"这是为**后面的单**提前备的"
            #   正是要一眼看出来的东西(用户 2026-09-15 的"延迟收益")。
            if getattr(r["op"], "prep", False):
                _act += " ⊕备料"
            rows.append(scoring.row(n + 1, _act,
                                    d is not None, d if d is not None else 0.0,
                                    scoring.step_value(r["op"].action), r["follow"],
                                    raw[n], final[n], v,
                                    urg=getattr(r["op"], "urgency", 0.0)))
        if chosen is not None:
            # ⚠ **不要在这里重写判定** —— `choose` 已经把选中那项标成"选"/"让位收回"了。
            #   这里再盖一次"选"会把"让位收回"吃掉, 而那正是排查协作问题时最该看见的一行。
            hi = max(range(len(pending)), key=lambda n: raw[n])
            if raw[hi] != final[hi] and pending[hi] != pending[chosen]:
                self.log(f"[评分] ⚠ 变换改变了选择: 原始最高是 "
                         f"{info[pending[hi]]['op'].action} {info[pending[hi]]['op'].target}"
                         f"({scoring.fmt(raw[hi])}) → 实际选了 "
                         f"{info[pending[chosen]]['op'].action} {info[pending[chosen]]['op'].target}"
                         f"({scoring.fmt(final[chosen])}, 模式="
                         f"{self.mode_state.mode.value if self.mode_state else '?'})")
        m = ""
        if mate is not None:
            m = (f" 队友@{mate[0]:.1f},{mate[1]:.1f} 持{mate[2]!r} 静止{mate[3]:.1f}s"
                 f"{'(人)' if mate[4] else '(bot)'}")
        # ☠ **"我从这儿能到几格"必须打出来** —— 这是"全部候选不可达"时**第一个要看的数**:
        #   若只有 1~2 格, 说明厨师被隔在一个小口袋里(站在非可走格上/掉到平台外),
        #   **一个机制就能解释整张表全灭**, 不用去逐个查台面;
        #   若接近总可走格数, 那才是"这台面/这灶台本身的问题"(逐个看右边的 `(原因)`)。
        #   (实机 2026-09-14 `s_summer_1_4`: 8 个候选全灭, 而当时**没有这个数**, 只能靠猜。)
        rn = ""
        if reach_n is not None:
            rn = f"  我的可达格 {reach_n}" + (f"/{reach_total}" if reach_total else "")
        self.log(f"[评分] 决策点 {len(pending)} 个候选{rn}{m}")
        self.log(scoring.table(rows))

    def _execute_scored(self, flow: DishFlow, ops: list, retries: int, total: int) -> bool:
        """**阶段一 + 阶段二**: 用四项评分挑"下一步做哪个", 而不是按下标顺序走。

        `do_op` 与所有 `op_*` **一行不动** —— 换掉的只是"选哪个"。

        阶段二把**杂活**也放进同一张表(用户: "如果拿不到食材, 就检查能否做其他事")。
        实现上用**合成下标空间**: 杂活挂在 `ops` 尾部, 传给 `_rank_candidates` 的是
        `ops + chores` 和 `pending + [杂活下标]` —— 于是那边一行都不用改。
        两条记账规则必须分清:
          · **菜谱 op 才从 `pending` 销号**(它是一步, 做完就没了);
          · **杂活不销号**, 靠 `used` 黑板限次 —— 它每轮重新生成,
            否则 `while pending` 永不收敛(同一件杂活被反复填回来)。
        """
        pending = list(range(len(ops)))
        n_recipe = len(ops)                 # 分界线: < n_recipe 是菜谱, >= 是杂活
        used = {}                           # {(action,target): 本轮做过几次}
        _idle_since = None                  # "连续一个动作都做不了"从什么时候开始(见 IDLE_WAIT)
        _branch_n = 0                       # 本单里"换一支再试"已经用掉几回(见 BRANCH_RETRY_MAX)
        # **换单就清传递指令台账** —— 见 `HANDOFF_TTL` 的注释。
        # ⚠ 判据是**订单名变了没有**, 不是"进了这个函数就清": 同一单会被外层
        #   "连续失败 → 重新规划"**再送进来一次**, 那时清掉就等于把刚交出去的活忘了,
        #   会**再丢一份**给人类。
        if self._handoff_flow != flow.name:
            self._handoffs.clear()
            self._handoff_told.clear()
            self._handoff_seen.clear()
            self._handoff_flow = flow.name
        while pending:
            # ☠☠ **外部命令必须在这里也读一次**(`pause`/`resume`/`stop`/`do`/`mode`)。
            #   原来 `apply_commands()` 只在**外层**(`run()` 里, 每单一次)跑, 而一单
            #   可能跑好几分钟 ⇒ 实测打回来的症状是"**暂停命令只在整单结束时才生效**"
            #   (想停停不下来, 想改 `do` 也得等一整单)。
            #   ⚠ **`pause` 在原地等, 不 return** —— return 会被外层记成"这一单失败"
            #     (冷板凳/指纹都会跟着动), 而用户按暂停**不是**这一单做错了。
            #   ⚠ 等待期间**松手**并**继续读命令**, 否则 `resume` 永远读不到(死锁)。
            self.apply_commands()
            if self._ctrl_stop:
                self.kb.release_all()
                self.log("[控制] ⏹ 收到 stop —— 中止这一单(外层会干净收工)")
                return False
            if self._ctrl_paused:
                self.kb.release_all()
                if not getattr(self, "_ctrl_paused_told", False):
                    self._ctrl_paused_told = True
                    self.log("[控制] ⏸ 暂停 —— 停在**这一步之前**, 下 `resume` 继续")
                time.sleep(0.3)
                continue

            st = self.state()
            if not st or not st.get("inRound"):
                self.log("[引擎] 对局结束, 中止")
                return False
            km = self.map(st)
            if km is None:
                time.sleep(0.3)
                continue

            # **传递指令: 产物回来了没有** —— 回来了就把它记进 `_preposed`,
            #   下游那一步(`assemble Y`)下一轮自己变成可做, `do_op` 会走过去捡。
            #   必须排在下面探测候选**之前**(同一轮就生效)。
            try:
                self._handoff_received(km)
            except Exception as e:                                     # noqa: BLE001
                self.log(f"[传球] 查产物出错: {e!r}")
            # 杂活**每轮重新探测**(按钮会变可按、脏盘子会多出来)
            chores = []
            if CHORE_MODE not in ("0", "off", "no", "false", "none"):
                try:
                    chores = self._chore_candidates(km, st, flow)
                except Exception as e:
                    self.log(f"[杂活] 探测出错: {e!r}")
            # **传球**: 链条上"我这边做不了"的那一环 → 把料丢给人类队友(用户要求)。
            # ⚠ 可达集**在这里算一次传进去** —— 不传的话 `_stand_cell_of` 会为每个候选
            #   各跑一次 BFS(十几个候选 = 十几次泛洪, 而这个循环每 0.5 秒就转一圈)。
            try:
                _tm = self.terrain()
                _reach = None
                if _tm is not None and getattr(_tm, "ok", False):
                    _cx0, _cz0, _ = self.pos(st)
                    if _cx0 is not None:
                        _reach = _tm.distances_from(
                            _cx0, _cz0, at_y=self.chef_y(st),
                            extra_edges=self._travel_edges(km, _tm))
                chores += self._pass_candidates(km, st, flow, ops, pending,
                                                tm=_tm, reach=_reach)
            except Exception as e:
                self.log(f"[传球] 探测出错: {e!r}")
            # **救锅**: 和杂活/传球一样进同一张评分表(用户要求"报警了评分就该上涨")。
            # ⚠ 它在 `CHORE_MODE` 那个总开关**外**面 —— 一口快糊的锅不是"顺便做的杂活",
            #   调试期关杂活不该顺手把救火也关了(要关它用 `NEKO_RESCUE=0`)。
            try:
                chores += self._rescues(km, st)
            except Exception as e:
                self.log(f"[救锅] 探测出错: {e!r}")
            # **回溯**: 下游那一步要的料没了 ⇒ 把产出它的上游那一步重新提出(用户要求
            #   "回溯到上一级"; 进池让评分决定选不选, 见 `_redos`)。
            try:
                chores += self._redos(km, st, ops, pending, n_recipe)
            except Exception as e:
                self.log(f"[回溯] 探测出错: {e!r}")
            # **提前备料**(延迟收益): 订单栏要 N 份而现有 M 份 ⇒ 把缺的提出来。
            #   份数由 `lookahead.demand` **从订单算**(不是常数); 入池时机不开豁免,
            #   靠 `enroute` 闸门里"菜谱没得做就全放行"那条 —— 见 `_preps`。
            #   ⚠ 库存**这里只算一次**传进去, 别让每个候选各扫一遍台面。
            #   ☠ **`flow` 必须传**: "提不提"由**本单**口径判(`shortfall_of`), 只按订单栏
            #     口径筛会替别的单把料拿到手里 —— 见 `_preps` 的"两个口径"。
            try:
                chores += self._preps(km, st, flow, self._all_flows(st),
                                      self._inventory(km, st))
            except Exception as e:
                self.log(f"[备料] 探测出错: {e!r}")
            pool = ops + chores
            # **冷板凳上的菜谱步骤不进候选**(用户要求: 持续失败就去做其他事)。
            # 它们全在冷板凳上时也不硬试 —— 那样只会把时间烧在"重试 3 次 + 导航超时"上;
            # 此时候选里只剩杂活(杂活的"顺路"闸门这时是开的: 见 `_chore_admitted`),
            # 一个杂活都没有就走下面的 `IDLE_WAIT` **等**, 而不是退出。
            _fresh = [j for j in pending
                      if not self.step_benched(f"{pool[j].action} {pool[j].target}")]
            # ☠ **交出去的步骤不进候选池** —— 但**不是**从 `pending` 里删掉, 两个理由:
            #   ① 从 `pending` 删会让 `while pending` **提前排空 ⇒ 函数末尾 `return True`**
            #      ⇒ `run()` 当成"★ 完成 {订单}" —— **假成功**, 比卡住更坏
            #      (单子其实没交, 只是把活推出去了);
            #   ② 留在 `pending` 里、只是不进池子, 一样选不中它 ——
            #      于是 `_op_actionable` 里那条"预置在附近也算能做"的捷径
            #      (`_preposed_ok`)就够不着 `chop Y`, 不会跑去把产物**再切一次**。
            #   ⇒ 交出去的那几步就静静躺在 `pending` 里等队友, 台账到期/产物收回到手
            #     之后再正常参与(到期后 `handoff_live` 返回 False, 它自动回池)。
            _handed_n = [j for j in _fresh if j < n_recipe
                         and self.handoff_live(pool[j].action, pool[j].target)]
            _fresh = [j for j in _fresh if j not in _handed_n]
            if len(_fresh) + len(_handed_n) < len(pending):
                self.log(f"[引擎] {len(pending) - len(_fresh) - len(_handed_n)} 步在冷板凳上, "
                         f"这一轮只做别的")
            pool_pending = _fresh + list(range(n_recipe, len(pool)))

            # **外部指定**(`do <action> [target]`): 绕过评分, 直接做那一步(只做一次)。
            _f = self._forced_pick(pool, n_recipe)
            if _f is not None:
                self.log(f"[控制] ▶ 按指定来做: {pool[_f].action} {pool[_f].target}")
                self._forced = None
                picked = (_f, 0.0)
            else:
                if self._forced:            # 找不到就当它用掉了(否则会一直拦着)
                    self._forced = None
                # ☠ `steps=pending` —— **把"还没做完的全部步骤"传下去**(`pool_pending`
                #   是**候选集**, 冷板凳上/已交出去的那两类不在里面)。前置剪枝要按全集判,
                #   拿候选集判会让下游丢闸门 —— 见 `_rank_candidates` 的 `steps` 注释。
                #   ⚠ 传的是**外层那个 `pending`**(成功才销号), 不是 `_fresh`。
                picked = self._rank_candidates(km, st, pool, pool_pending,
                                               n_recipe=n_recipe, used=used, flow=flow,
                                               steps=pending)
            if picked is None:                 # 一个都到不了/没活可干
                # **订单已经不在订单栏上了 → 整条流程都作废了**(多半是别人交了,
                # 或者超时没了)。这时候**不能**当失败: 当失败会累加"连续失败"指纹,
                # 三次就把一局正常跑着的脚本停下来 —— 而那根本不是 bug。
                # 返回 True 让主循环干净地重新规划下一单。
                if not self._order_live(flow.name):
                    self.log(f"[引擎] 订单 {flow.name} 已经不在订单栏上了 "
                             f"(多半被别人交了/超时) —— 这一单不用做了")
                    return True
                # ☠☠ **链子断了 ⇒ 立刻判死, 别等 20 秒**(2026-09-15 实机打回来的)。
                #   实测那一局的账: `fetch`/`chop`/`assemble` 各自报过 `✓`(其中 `fetch`
                #   是**假 ✓** —— 报成功时手是空的), 于是 `pending` 里**只剩 `deliver`**;
                #   而 `deliver` 的手空闸门永远不放行(`_op_actionable` 那条**不能删** ——
                #   删了 `deliver` 的 100 分就会一直压过所有步骤, 见那边的注释),
                #   于是 `picked is None` 反复发生, 一等等 20 秒, 而且**每一秒都重打一张
                #   评分表**(日志里那串重复的 `deliver 不可达` 就是这么来的)。
                #   ⚠ 那 20 秒在这里**买不到任何东西**: 等的是"世界变了没有", 可缺的是一个
                #     **盘子** —— 而盘子要么在手、要么在台面上, **不在任何人手里**。
                #     提示语写的"该人类队友拿"是**说错了原因**(那条对应的是另一种情形)。
                #   判据: **手空 + 这一单剩下的步骤里没有 `fetch`** ⇒ 这条链**再也填不满手**
                #     (`fetch` 是菜谱里**唯一**能把东西拿到手上的动作; `chop`/`cook`/`mix`
                #      都要先有料, `assemble`/`deliver`/`pass` 都不产料)。
                #     这时等多久都不会变 ⇒ 立刻作废, 让外层拿去重新规划。
                #   ⚠ **范围刻意收窄**: 只看**这一单自己的 `pending`**。杂活(`serve_any` 等)
                #     不在 `pending` 里 —— 所以理论上"队友把一盘菜填好、`serve_any` 变成可用"
                #     能救回来。但拿 20 秒(一局 150 秒的 13%)去赌这个, 不划算; 而且实测
                #     那局等满也没等到。真发生了, 外层重新规划会再走进来。
                # ☠☠ **手占着 + 一个候选都没有 ⇒ 先把手腾出来, 别干等**
                #   用户 2026-09-15 原话:
                #     > "像这种都不可用的, **直接把盘子放地上去做其他事**就行了"
                #   实测那一局里这种局面出现了**几十次**:
                #     `5 rescue SushiRice ✗ 不可达(手上有 equipment_plate_01 (1), 杂活要先腾手)`
                #   —— 手占着 ⇒ 切/煮/杂活**全被 `_op_actionable` 挡掉** ⇒ 只剩干等。
                #   ⚠ 丢的是**脚下**: 走的是 `ReceiveTakeEvent(target=null)` ⇒
                #     游戏的 `carrier.TakeItem()` = 放地上。**东西还在场上、还能捡回来**
                #     (`_find_ground_item` 管地上的东西; `ScanItems` 现在也报地上的盘子了)。
                #   ⚠ 腾完手 **立刻 `continue` 重来一轮**(现在就重选, 不烧 1 秒), 而且
                #     把手是空的了 ⇒ **下一轮不会再走这条**, 不会反复丢。
                # ⚠ **必须在这里自己取一次** —— 下面那段(`_recipe_left` 之后)才赋
                #   `_held_now`, 而这段在它**前面**; 不自己取就会读到**上一轮的残留值**
                #   (`while pending` 循环里变量不隔离), 或者干脆 `NameError`。
                _, _, _held_now = self.pos(st)
                if _held_now:
                    self.log(f"[引擎] ↺ 手上还攥着 {_held_now!r}, 而一个候选都没有"
                             f" —— 按用户要求**先把它放到地上**, 腾出手去做别的")
                    # ⚠ 用 `_drop_held`(**记进 `_preposed`**)—— 不记就是"料躺在外面、
                    #   而引擎不知道它在哪", 下一轮只会去箱子再取一份(见那里的注释)。
                    self._drop_held(st, _held_now)
                    _idle_since = None          # 做过事了 —— 等待窗口重新计
                    continue
                #   ⚠ **"终结步"= 只能用手上已经有的东西，而且不产料**(`deliver`/`pass`/
                #     `assemble`)。这条链填不满手，它们就永远做不了。
                #   ☠☠ **`assemble` 必须算进来** —— 2026-09-15 实机打回来的:
                #     那一把的终局是 `决策点 2 个候选: assemble SushiFish + deliver`,
                #     两个都 `不可达(手空…)`, 而剩下的步骤里**没有 `fetch`** ⇒ 链子断了。
                #     可当时这里写的是 `("deliver", "pass")` ⇒ `_all_terminal` 为 False
                #     ⇒ **整条判据被绕过去**, 白等了 20 秒。
                #   我当时收窄的理由是"`chop`/`cook`/`assemble` 卡住时别人把料放到
                #   板上/台面上能救回来" —— **那一条只对 `chop`/`cook`/`mix` 成立**:
                #      · `chop X`/`cook X`: 别人把 X 放到板上/锅里, **我空手也能做**
                #        (`_op_actionable` 的"板上就是要切的它 ⇒ 放行") ⇒ 等有意义;
                #      · `assemble X`: 它要的是**我手里**有东西(料或盘子) ——
                #        **别人往哪放都填不满我的手** ⇒ 等下去不会变。
                _recipe_left = [pool[j] for j in pending if j < n_recipe]
                # ☠ **交出去的步骤要单独数出来** —— 它们"等"的是队友, 和"等也不会变"相反。
                #   判据整段抽成了纯函数(见 `_chain_dead`, 那里记着我把它写反过一次)。
                _handed_now = [pool[j] for j in pending if j < n_recipe
                               and self.handoff_live(pool[j].action, pool[j].target)]
                _n_handed = len(_handed_now)
                _, _, _held_now = self.pos(st)
                if self._chain_dead(_recipe_left, _held_now, _n_handed):
                    self.log(f"[引擎] ⛔ **链子断了**: 手上是空的, 而这一单剩下的 "
                             f"{len(_recipe_left)} 步里**没有 `fetch`**"
                             f"({', '.join(sorted({o.action for o in _recipe_left}))}) "
                             f"—— 这条链再也填不满手, **等也不会变**")
                    self.log(f"[引擎]    多半是前面有一步报了假 `✓`(做成了才销号, 销了号就回不去)。"
                             f"立刻作废这一单, 让外层重新规划, 不烧那 {IDLE_WAIT:.0f} 秒")
                    self._last_fail_step = f"score:链子断了(手空且剩下的步骤没有 fetch)"
                    return False
                # ★ **一个动作都做不了 —— 先别当失败, 等一会儿再看。**
                #   两种情形在这里长得一模一样, 但处置相反:
                #     · **该人类队友出马**(食材在他那半边 / 他在用唯一的锅) —— 等;
                #     · **卡在 bug 里**(判据自相矛盾) —— 立刻停。
                #   区别就是"等一会儿会不会变", 所以先等一个有界的窗口(见 `IDLE_WAIT`)。
                if _idle_since is None:
                    _idle_since = time.time()
                    _m = self._mate(st)          # 只在"等"这条路上取一次, 拿来看队友是不是在挂机
                    _mate_txt = "无队友"
                    if _m is not None:
                        _mate_txt = (f"队友@{_m[0]:.1f},{_m[1]:.1f} 持{_m[2]!r} "
                                     f"静止{_m[3]:.1f}s{'(人)' if _m[4] else '(bot)'}")
                    self.log(f"[引擎] 现在没有任何可做的动作(菜谱全够不着/没料, 也没有杂活) "
                             f"—— 先等 {IDLE_WAIT:.0f} 秒再看({_mate_txt})")
                    self.log(f"[引擎]    多半是食材在够不着的那半边, **该人类队友拿**; "
                             f"等满还是一场空才判失败。`NEKO_IDLE_WAIT=0` 可关掉这个等待")
                    if _n_handed:
                        self.log(f"[引擎]    ⤴ 其中 {_n_handed} 步是**已经交给队友**的"
                                 f"({', '.join(o.action + ' ' + o.target for o in _handed_now)})"
                                 f" —— 等的是他, 不是世界")
                # ☠ **交出去的那几步还活着 ⇒ 不判死, 继续等** ——
                #   `HANDOFF_TTL`(45s) > `IDLE_WAIT`(20s), 不特判的话人类还没切完
                #   单子就被判死作废了。等的对象是**队友**, 所以窗口按他的账期算;
                #   台账一过期(`handoff_live` 自己会清)这条路自然就恢复原样。
                if _n_handed and time.time() - _idle_since < HANDOFF_TTL + IDLE_WAIT:
                    self.kb.release_all()
                    time.sleep(1.0)
                    continue
                if time.time() - _idle_since < IDLE_WAIT:
                    self.kb.release_all()
                    time.sleep(1.0)
                    continue
                self._last_fail_step = "score:没有可做的候选"
                return False
            _idle_since = None

            i, fin = picked
            is_chore = i >= n_recipe
            op = pool[i]
            if is_chore:
                k = chore_key(op)
                used[k] = used.get(k, 0) + 1
            else:
                pending = [p for p in pending if p != i]

            # 订单没了就别再做它的杂活 —— 洗一次盘子能烧 20 秒, 而这一单已经不用做了。
            if is_chore and not self._order_live(flow.name):
                self.log(f"[引擎] 订单 {flow.name} 已经不在订单栏上了 —— "
                         f"这一单(含杂活)都不做了")
                return True

            # 摆盘位缺盘子就趁手空补上 —— 必须在**选完之后**调:
            # 它会真的把厨师挪过去("挪过去"会改变下一步的距离项, 所以不能选之前做)。
            # ⚠ 选了杂活时不补: 那是给"下一步要摆盘"用的, 先跑一趟拿盘子再去干杂活是白绕。
            if not is_chore:
                self._top_up_plate()
            _st0 = self.state()
            _c0 = self.pos(_st0) if _st0 else (None, None, "")
            _chef = (f"  厨师({_c0[0]:.1f},{_c0[1]:.1f}) 手持{_c0[2]!r}"
                     if _c0[0] is not None else "")
            # 杂活**不能**打 "第 i+1/total 步" —— 合成下标会越界(第15/14步), 看着像 bug。
            _tag = (f"[杂活] {op.action} {op.target}" if is_chore
                    else f"第{i+1}/{total}步 {op.action} {op.target}")
            self.log(f"[引擎] ▶ {_tag} (评分 {scoring.fmt(fin)}){_chef}")
            # ⚠ 这里**没有** `_maybe_mischief()` 掷骰 —— 走评分路径时人格由
            #   `ModeState.transform` 在评分向量上表达; 两个都开会叠加成双重捣蛋
            #   (见 `SCORE_DRIVES_MODE`)。要对照旧行为就 `set NEKO_SCORE=0`。

            done = False
            # ⚠ **杂活不重试**(`attempt` 只跑 0): 一次导航最多 25 秒, 重试三次就是 75 秒,
            #   而它只是个低价值的顺路活 —— 不值得。
            self._last_fail_kind = ""
            for attempt in range(1 if is_chore else retries + 1):
                st = self.state()
                if not st or not st.get("inRound"):
                    self.log("[引擎] 对局结束, 中止")
                    return False
                km = self.map(st)
                if km is None:
                    time.sleep(0.3)
                    continue
                try:
                    done = self.do_op(km, st, op, flow, attempt)
                except Exception as e:
                    self.log(f"[引擎] {op.action} 异常: {e}")
                    done = False
                finally:
                    self.kb.release_all()
                if done:
                    break
                # ☠ **"摔死"不重试**: 一趟导航摔死到上限(`MAX_RESPAWNS`)说明路本身走不通,
                #   再走两遍只是把 6 秒/次的重生等三遍(实机: 一路刷了 9 次重生)。
                #   直接交给下面的"放冷板凳 + 换目标"。
                if self._last_fail_kind == "death":
                    self.log(f"[引擎] ⚠ 第 {i+1} 步是**摔死**失败 —— 不再原路重试")
                    break
                # ☠ **这一支不行 ⇒ 换支, 不原地重试** —— 用户 2026-09-15:
                #   "**按一次没有得到对应的结果这条路就失败了, 可以返回到其他路**"。
                #   重试同一个动作只会撞同一面墙(板还是空的、那格还是过不去);
                #   台面已经在 op_* 里用 `mark_branch_dead` 划掉了 ⇒ 重选时会换一块。
                if self._last_fail_kind == KIND_BRANCH:
                    self.log(f"[引擎] ⚠ 第 {i+1} 步是**这一支不行**({op.action} {op.target})"
                             f" —— 不原地重试, 换支重选")
                    break
                self.log(f"[引擎] 第 {i+1} 步失败, 重试 {attempt+1}/{retries}")
            if not done:
                if is_chore:
                    # ☠ **杂活没做成 ≠ 整单失败**: 一个按钮没按对不值得停机(规则 5 的反面)。
                    #   记进黑板(本轮不再选它)后继续 —— 菜谱该干嘛干嘛。
                    # ⚠ **`rescue` 额外上冷板凳**: "本轮不再选它"只在本轮有效, 而 `pending`
                    #   每轮重建 ⇒ 走不到的那口锅下一轮又被选中。实测那口卡在 14/12 秒的锅
                    #   把半局烧在"走不到旁边(差 1.00 格)"上。
                    if op.action == "rescue":
                        self.bench_step(f"rescue {op.target}", "走不到那口锅旁边")
                    self.log(f"[引擎] ⚠ 杂活没做成: {op.action} {op.target} —— 本轮不再选它")
                    continue
                # ⚠ **指纹里不带下标**: 一旦顺序是动态的, 同一个 bug 两次可能选中不同的槽位,
                #   带下标就没法重复 ⇒ "连续 3 次一样就停机"永远不触发 ⇒ 整局白烧(规则 5)。
                #   去掉下标后, 只要**同一个动作**反复失败就会停 —— 比原来更稳。
                # ☠ **分支不行 ≠ 这一单完了**(用户: "可以返回到其他路")。
                #   选中那一刻已经**销了号**(上面 `pending = [p for p in pending if p != i]`)
                #   ⇒ 这里必须**把它放回去**, 否则重选时这一步根本不在池子里,
                #   只能一路走到"整单报废" —— 实测日志里 `placeCanHandle=false`
                #   连试 3 次就 `✗ 放弃: assemble` 废掉一整单, 而它只需要换个台面。
                #   放回去之后重选: 台面已被 `mark_branch_dead` 划掉 ⇒ 会挑**别的**台面。
                if self._last_fail_kind == KIND_BRANCH and _branch_n < BRANCH_RETRY_MAX:
                    _branch_n += 1
                    if i not in pending:
                        pending.append(i)
                        pending.sort()
                    self.log(f"[引擎] ↺ {op.action} {op.target} 放回候选 "
                             f"({_branch_n}/{BRANCH_RETRY_MAX}) —— 这一支不行, 换一支再选")
                    continue
                self._last_fail_step = f"{op.action} {op.target}"
                self.log(f"[引擎] ✗ 放弃: {op.action} {op.target} (评分 {scoring.fmt(fin)})")
                return False
            self.log(f"[引擎] ✓ {op.action} {op.target} (评分 {scoring.fmt(fin)})")
            # ☠ **加工做完了 ⇒ 同名的 `fetch` 立刻销号**(见 `_subsumes_fetch` 的注释:
            #   评分会把 `chop`(30) 排在 `fetch`(20) 前面, 而板上那块现成的生料让
            #   `chop` 成立 ⇒ 切完再 `fetch` 就多取了一块新的生的 ⇒ `assemble` 拿到它
            #   ⇒ 游戏 `placeCanHandle=false` ⇒ 整单报废)。
            # ⚠ 只销**同名**的 `fetch`; 另一份材料的 `fetch` 不动 —— 那正是要的并行。
            if not is_chore:
                _new = self._drop_subsumed_fetches(pool, pending, op)
                if _new != pending:
                    self.log(f"[引擎] {op.action} {op.target} 做完了 —— "
                             f"同名的 fetch 已经多余, 销号"
                             f"(去掉 {len(pending) - len(_new)} 步)")
                    pending = _new
            # ☠ **交掉了这一单 → 整条流程就此结束**(2026-09-15 实机打回来的一条)。
            #   `deliver`/`serve_any` 的成败判据都是"**这一单从订单栏上消失**"
            #   (`_deliver_plate`: `remaining < before` + 盘子离手) —— 也就是这一单**已经完成**。
            #   可 `pending` 里往往还留着菜谱那一步 `deliver`(交菜是**杂活** `serve_any` 干的,
            #   而它不算菜谱的销号), 此刻它**永远做不了**: 手是空的
            #   (`不可达(手空, 没东西可送)`) ⇒ 一路等到 `IDLE_WAIT` 20 秒。
            #   偏偏那个"订单没了就收工"的出口按**名字**判(`_order_live`), 而这关的菜谱池里
            #   就有同名菜 —— 交掉一单后游戏**立刻又会发一张同名的**, 出口于是永不成立。
            #   (实测 `s_sushi_1_3`: 交完 `Sushi_PlainPrawn` 之后连着十几轮只有那一个
            #    不可达的 `deliver`, 白白烧掉 20 秒 ≈ 一局 150 秒的 13%。)
            #   ⇒ 交完就收工, 让主循环干净地重新规划(新订单会走一条全新的 flow)。
            if self._flow_completed_by(op, flow):
                self.log(f"[引擎] ★ {flow.name} 已经交掉了 —— 这一单收工")
                return True
            # 杂活不 `remember`: 捣蛋鬼的"重复奖励"会让它反复做同一件杂活,
            # 而洗一次盘子能烧 20 秒 —— 哲学上正确, 计时上很贵(见 modes 的注释)。
            if self.mode_state is not None and not is_chore:
                self.mode_state.remember(f"{op.action} {op.target}")
        return True

    @staticmethod
    def _chain_dead(recipe_left, held: str, n_handed: int) -> bool:
        """**"这条链再也填不满手了吗"** —— 是就立刻作废整单, 别白烧 `IDLE_WAIT`。

        ☠ 四件事必须**同时**成立:
          ① **手是空的** —— 手上有东西就还有别的动作可做;
          ② 剩下的菜谱步里**没有 `fetch`** —— 它是菜谱里**唯一**能把东西拿到手上的动作
             (`chop`/`cook`/`mix`/`assemble` 用的都是"手上已有的"), 有它就填得满;
          ③ 剩下的**全是终结步**(`deliver`/`pass`/`assemble`) —— `chop X`/`cook X` 只要
             别人把料放到板上/锅里, 我**空手也能做** ⇒ 等有意义; 而 `assemble X` 要的是
             **我手里**有东西, **别人往哪放都填不满我的手** ⇒ 等下去不会变;
          ④ **没有交出去的步骤**(`n_handed == 0`) —— 那些"等"的是**队友**, 他做完了产物会
             回来, 和 ③ 说的"等也不会变"**正好相反**。

        ☠☠ **④ 是我自己写反过一次的地方**(2026-09-15 本轮): 第一版把它写成
          "交出去的也算终结步"加进了 `_all_terminal` —— 于是剩下 `chop Y` 一步、它又交出去了
          时, 这条判据**立刻判死整单**, 而那正是最该等的时候。
          (`HANDOFF_TTL` 45 秒 > `IDLE_WAIT` 20 秒 ⇒ 表现是"人类还没切完单子就没了"。)
          ⇒ 所以它现在是个**纯函数**, 下面那几条边界在 `runtime/_splitkitchen_probe.py` 里钉着。
        """
        if held:
            return False
        left = [getattr(o, "action", "") for o in (recipe_left or [])]
        if not left:
            return False
        if "fetch" in left:
            return False
        if not all(a in ("deliver", "pass", "assemble") for a in left):
            return False
        return not n_handed

    @staticmethod
    def _advance_bonus(op, held: str) -> float:
        """**"这一步用的正是手上拿着的那份"** → 给加成分。

        用户 2026-09-15 讲的机制: "比如 SushiRice, 想要放在盘子上需要**先煮熟**" ——
        生米**上不了盘**(游戏 `CanHandlePlacement=false`), 腾手又只能"进盘"或"丢地上",
        所以拿着生料时**唯一有价值的事就是把它的下一步做完**。
        实测那次评分选了 `fetch Cucumber`(-12) 而不是 `cook SushiRice`(-19) —— 纯按距离,
        于是生米晾在手上, 逼出 `op_fetch` 的腾手 → 游戏拒收 → 整局绕死。

        只认**会用到手上这份**的四个动作(`chop`/`cook`/`mix`/`assemble`)。
        `fetch` 不算 —— 它的语义正是"去拿**别的**东西", 而手上这份的归宿不该是等。
        纯函数(只看动作名和两个字符串), 方便离线核对。
        """
        if not held:
            return 0.0
        act = getattr(op, "action", "")
        if act not in ("chop", "cook", "mix", "assemble"):
            return 0.0
        return (scoring.HAND_ADVANCE_BONUS
                if Engine._held_is_norm(held, getattr(op, "target", "")) else 0.0)

    @staticmethod
    def _held_is_norm(held: str, want: str) -> bool:
        """`_held_is` 的纯函数版(同名/前缀式改名那套判据)。"""
        if not want:
            return False
        h, w = Engine._norm(held), Engine._norm(want)
        if not h:
            return False
        return h == w or h.startswith(w)

    #: 做完之后**会取代**同名 `fetch` 的那几个动作。
    #: 依据 `cookbook.derive()`: 给**一份**材料产出的是
    #: `fetch → [chop] → [cook/mix] → assemble` 这条链, 链上每一步作用的是
    #: **同一个物理个体** —— 所以后面那几步一旦做成了, "去把生的那份取回来"就没意义了。
    SUBSUMES_FETCH = ("chop", "cook", "mix")

    @staticmethod
    def _subsumes_fetch(op) -> str:
        """这一步做完之后, **同名的 `fetch` 是不是就多余了** —— 是的话返回它的 target。

        为什么要这条(实机 2026-09-15 `s_sushi_1_1` 打回来的整局):
          评分层是**按分重排**的, 而 `chop`(30) > `fetch`(20) —— 于是一块**已经在板上**
          的生料会让 `chop X` 抢在 `fetch X` 前面跑(`_op_actionable` 的 chop 分支:
          "板上就是要切的它 → 直接切")。切完之后 `fetch X` 又去取了**一块新的生料**,
          而 `assemble X` 拿到的正是那块新的(生的) ⇒ 游戏 `CanHandlePlacement=false`
          ⇒ 连试 3 次 ⇒ **整单报废**。
          `OP_PREREQ` 拦不住它: 那张表只管 `assemble`/`cook`/`mix`, **`chop` 没有前置**
          (那里的注释讲的是"取**另一份**材料要并行", 同名的不在此列)。

        ⇒ 规则: **前一步的产品已经被这一步取代了, 就把那一步销号**。
          这样保留了"顺手把板上的料切了"这个**有用**的机会主义, 只去掉那次白跑。

        纯函数(只看动作名), 方便离线核对。
        """
        return (getattr(op, "target", "")
                if getattr(op, "action", "") in Engine.SUBSUMES_FETCH else "")

    @classmethod
    def _drop_subsumed_fetches(cls, pool, pending: list, op) -> list:
        """`op` 做成了 ⇒ 把 `pending` 里**同名**的 `fetch` 去掉。返回新的 `pending`。

        ⚠ 只销同名的(`_norm` 比): 另一份材料的 `fetch` **必须留着** —— 那正是
          `OP_PREREQ` 注释里说的"取另一份材料就是我们要的并行"。
        纯函数(只依赖 pool/pending 的形状), 方便离线核对。
        """
        tgt = cls._subsumes_fetch(op)
        if not tgt:
            return pending
        tn = cls._norm(tgt)
        drop = [p for p in pending
                if p < len(pool) and getattr(pool[p], "action", "") == "fetch"
                and cls._norm(getattr(pool[p], "target", "")) == tn]
        if not drop:
            return pending
        return [p for p in pending if p not in drop]

    @staticmethod
    def _flow_completed_by(op, flow) -> bool:
        """这一步做完之后, **这一整单是不是就算交掉了**。

        只认两件事: 动作是 `deliver`/`serve_any`, 且它交的**就是本单**(按名字比)。
          · `deliver` —— 菜谱自己的最后一步, 交给送餐口。
          · `serve_any` —— **杂活**, 把台面上现成的一盘端去送(阶段二)。它不销菜谱的号,
            所以做完之后 `pending` 里那个 `deliver` 还在 —— 而手已经空了, 那一步**永远**做不了。
        两者共同的成败判据都是"这一单从订单栏上消失"(`_deliver_plate`), 所以命中即完成。
        纯函数, 方便离线核对。
        """
        return (getattr(op, "action", "") in ("deliver", "serve_any")
                and getattr(op, "target", "") == getattr(flow, "name", ""))

    def execute(self, flow: DishFlow, retries: int = 2) -> bool:
        # ⚠ 这里**不能**清空 assemble_spot —— 见 pick_assemble_spot() 里那段注释。
        #   清空的后果: 重新规划时挑到一个空台面 → 从那个台面的视角"什么都没有"
        #   → 同一份材料被反复取(实测一局取了 7 遍)。台面只在换关卡/对局结束时清(run())。
        self._prepare_plate(flow)
        ops = self._skip_already_on_spot(flow.ops)
        total = len(ops)
        # **评分接管"选哪个"**(交接包 §4)。`SCORE_DRIVES_MODE=0` 时下面的旧循环逐字不变 ——
        # 真机验证期留一条"一键退回旧行为"的路(`set NEKO_SCORE=0`), 没有离线测试就只有它兜底。
        if SCORE_DRIVES_MODE:
            return self._execute_scored(flow, ops, retries, total)
        for i, op in enumerate(ops):
            done = False
            # 摆盘位缺盘子就趁手空补上 —— 见 _top_up_plate() 的注释(一整轮 9 次失败都是它)
            self._top_up_plate()
            _st0 = self.state()
            _c0 = self.pos(_st0) if _st0 else (None, None, "")
            _loc = f" @({op.at_x:.1f},{op.at_z:.1f})" if (op.at_x or op.at_z) else ""
            _chef = f"  厨师({_c0[0]:.1f},{_c0[1]:.1f}) 手持{_c0[2]!r}" if _c0[0] is not None else ""
            self.log(f"[引擎] ▶ {i+1}/{total} {op.action} {op.target}{_loc}{_chef}")
            # 三模式：这一步开始前，先看要不要演一次失误/捣蛋（v1 §4/§5）
            if self.mode_state is not None:
                _km0 = self.map(_st0) if _st0 else None
                if _km0 is not None:
                    self._maybe_mischief(_km0, _st0)
            for attempt in range(retries + 1):
                st = self.state()
                if not st or not st.get("inRound"):
                    self.log("[引擎] 对局结束, 中止")
                    return False
                km = self.map(st)
                if km is None:
                    time.sleep(0.3)
                    continue
                try:
                    done = self.do_op(km, st, op, flow, attempt)
                except Exception as e:
                    self.log(f"[引擎] {op.action} 异常: {e}")
                    done = False
                finally:
                    self.kb.release_all()
                if done:
                    break
                self.log(f"[引擎] 第 {i+1} 步失败, 重试 {attempt+1}/{retries}")
            if not done:
                self.log(f"[引擎] ✗ 放弃: {op.action} {op.target}")
                # 记下失败在哪一步, 供主循环判断"是不是同一个 bug 在反复失败"
                self._last_fail_step = f"{i+1}.{op.action} {op.target}"
                return False
            self.log(f"[引擎] ✓ {op.action} {op.target}")
        return True

    # ---------------- 规划 ----------------
    def _needs_work(self, name: str) -> bool:
        """这个物体放在加工台上**还没加工完**吗 —— 有 `next` = 还能变成别的东西。

        (`Item.next` = "加工之后变成什么", 见 `cookbook.Item`。空 = 已经是成品。)

        ☠ **两边都要 `_norm`** —— 这是"探测不到 → 整类杂活静默失效"的坑:
          台面上那个名字是 Unity 子物体名(`SceneScanner` 取 `AttachStation` 子物体的
          `.name`), 实测带实例后缀:`"SushiPrawn (2)"`; 而知识表里是 `"SushiPrawn"`。
          精确比 ⇒ **永远不相等** ⇒ `work` 这一类杂活一个候选都出不来, 而且**看不出来**
          (老 `_chores` 第③段就是这么一直没生效的)。`_norm` 存在的唯一理由就是这件事。
        """
        if not name or self.know is None:
            return False
        n = self._norm(name)
        if not n:
            return False
        for it in getattr(self.know, "items", []) or []:
            if self._norm(getattr(it, "name", "")) == n:
                return bool(getattr(it, "next", ""))
        return False

    def _same_material(self, a: str, b: str) -> bool:
        """两个名字看着**是不是同一族的料** —— 加工阶段改的正是名字(`SushiPrawn` →
        `ChoppedSushiPrawn`), 所以"同族"= 互相包含。

        用途: 放宽档的兜底判据(见 `_op_actionable` 的 `assemble` 分支) ——
        放宽档要救的是"**名字认不准**", 不是"**手上随便有东西**"。

        ⚠ 和 `cookbook._same_family` 是**同一个意思**(DLC 前缀/下划线/大小写都不算区别),
          那份是模块级私有的(给 `resolve_leaf` 报可疑用); 这里引擎要判"能不能放行",
          语义不同(那边只**报**、不做判据), 所以各留一份**名字上写明**的实现。
          ⚠ 只用**互相包含**, 不用前缀/子串之外的花样 —— 越宽越容易把不相干的放进来,
            而放错一次的代价是**执行一个错的物理动作**(见 `_op_actionable` 的注释)。
        """
        x, y = self._norm(a), self._norm(b)
        if not x or not y:
            return False
        return x == y or x in y or y in x

    def _is_unprocessed(self, it) -> bool:
        """**这件东西还是生料吗** —— 看**实例自己**的 `workable`(C# `ScanItems` 的 `work`)。

        专门为"传递指令"的收回那一腿写的: 队友把切好的丢回来, 我要能认出
        "地上这个 `X` 是**成品**" —— 在"生料和成品同名"那一族关卡里(`SushiFish --切8次--> SushiFish`)
        名字一模一样, 只有实例身上的 `m_nextPrefab`(成品的实例没有)能分。

        ☠ **不要拿 `_needs_work` 顶替它**, 两处都不一样:
          · `_needs_work` 是拿**名字**去**知识表**里查 `bool(next)` —— 同名那一族在表里是
            **两条同名记录**, 它取第一条就返回 ⇒ 分不出手上/地上这个是哪一个;
          · 而且它匹配的是知识表的 `name`(prefab/GameObject 名), 和菜谱叶子用的
            **订单节点名**(`ing`/`next`)是**两个命名空间**(见 `map_model.Item.workable` 的注释)。
        ⚠ 拿不到那个字段(老 DLL / 台面格位上的东西)时**返回 False** = 当作"是成品"。
          这个默认方向的理由: 判据的用途是"**敢不敢把这一步当成做完了**" ——
          答"是成品"最坏是白跑一趟去捡, 而答"还是生料"会让闭环永远不动。
          而且白跑那一趟还会被游戏自己挡住(`_op_actionable` 的 assemble 阶段闸门:
          没加工完的料上不了盘, `CanHandlePlacement=false`)。
        """
        return bool(getattr(it, "workable", False))

    # ---------------- 杂活: 只读探测(阶段二) ----------------
    def _chore_candidates(self, km, st, flow=None) -> list:
        """**只读**探一遍"现在有哪些杂活可做" —— 返回 `[Op]`(目标已解析好), 绝不移动/按键。

        为什么必须另写一份而**不是**直接用 `op_press`/`op_wash`:
        那些是"边判边做"的(真的走过去按键)。而评分层要的是**纯探测** ——
        `_op_target_for_score` 的契约就是"永远不执行任何动作"(见那边的注释),
        没有这一层, 杂活就只能像以前那样"绕过评分、卡住了才硬做"。

        每个候选都把目标写进 `op.at_x/at_z/at_name`:
        这样**评分侧和执行侧看的是同一个台面**(同一份判据), 不会出现
        "按 A 台算的分、走到 B 台去执行"。`serve_any` 的 `target` 是**订单名**
        (交菜那段后半程要拿它和订单列表比), 台面只在坐标里。

        四类各自的判据**复用已有的那些 helper**(不抄第二份):
          · `press`  —— `dyn.buttons` 里 `pressable` 的(`op_press` 同一份数据源)
          · `wash`   —— `km.of("dirty_plates")` 有 `n>0` + `km.of("wash")` 存在
          · `work`   —— `s.on` 非空且 `_needs_work(on[0])`(`_chores` 第③段同款)
          · `serve_any` —— 台面上的盘子内容 ⊇ 当前 flow 的所有 `assemble` 目标(交菜)
        手上有东西时四类**全部不成立** —— 一次只能拿一个, 而拿着东西做杂活会到处乱放。
        """
        out = []
        cx, cz, held = self.pos(st)
        if cx is None:
            return out
        if held:
            return out          # 手上有东西 → 先让主流程/腾手逻辑处理

        # ⚠ **四类都"列全", 不按距离挑最近的那个** —— 两件事都靠它:
        #   ① 评分层要按四项自己排(挑最近是**评分**的活, 不是探测的活);
        #   ② "做过几次"的黑板 key 是 `(action, target)`, 而**探测阶段挑最近**会让
        #      key 随着厨师走动漂移 ⇒ 上限失效(同一件杂活被反复选中)、旁边那件能做的
        #      又看不见。列全之后 key 稳定, 距离交给 `scoring` 排。

        # ① 按机关(闸门/开关/传送带开关)
        try:
            btns = [b for b in (self._dyn().get("buttons") or []) if b.get("pressable")]
        except Exception:
            btns = []
        for b in btns:
            out.append(Op("press", (b.get("name") or b.get("type") or "机关"),
                          note="按机关",
                          at_name=(b.get("name") or ""),
                          at_x=float(b.get("x") or 0), at_z=float(b.get("z") or 0)))

        # ② 洗盘子(脏盘子堆 + 洗手池都在才成立)
        # ⚠ 目标 = **脏盘堆**(第一步是去端它)。洗手池够不够得着**探测阶段不查**
        #   (那要地形表, 而探测层刻意不碰地形) —— 够不着就是白跑一趟, 靠
        #   "杂活不重试 + 本轮记账上限"把代价压到一趟。日志里会把洗手池打出来。
        if km.of("wash"):
            for s0 in km.of("dirty_plates"):
                if int(getattr(s0, "n", 0) or 0) <= 0:
                    continue
                out.append(Op("wash", s0.id, note="洗盘子", at_name=s0.name,
                              at_x=s0.x, at_z=s0.z))

        # ③ 加工台面上"该加工还没加工"的料(切菜/搅拌/烘培是同一条路: 站旁边按交互)
        for sem in ("board", "mix", "hob", "oven", "fryer", "heat", "auto"):
            for s in km.of(sem):
                on = list(getattr(s, "on", []) or [])
                if not on or not self._needs_work(on[0]):
                    continue
                out.append(Op("work", s.id, note=f"加工 {on[0]}", at_name=s.name,
                              at_x=s.x, at_z=s.z))

        # ④ 交菜 —— 台面上已经拼好的一盘(不管谁拼的), 没人交就端去送餐口
        if flow is not None:
            for s in self._find_ready_dish(km, flow):
                out.append(Op("serve_any", flow.name, note="交菜(台面上已有拼好的一盘)",
                              at_name=s.name, at_x=s.x, at_z=s.z))
        return out

    def _dish_sets(self, flow) -> tuple:
        """本单的 `(必需集, 可选集)`(归一化材料名)。`_dish_matches` / `_find_ready_dish` 共用。"""
        req, opt = set(), set()
        for o in getattr(flow, "ops", []) or []:
            if o.action != "assemble":
                continue
            n = self._norm(o.target)
            if not n:
                continue
            (opt if getattr(o, "optional", False) else req).add(n)
        return req, opt

    @staticmethod
    def _dish_matches(have: set, req: set, opt: set) -> bool:
        """这一盘是不是本单拼好的那道菜(**判据只此一份**)。

        和游戏自己那条对齐(反编译 `CompositeAssembledNode.AssumeTypeMatch`):
        `Contains(required ∪ optional, 盘中) && Contains(盘中, required)`
        ⇔ **`required ⊆ 盘中 ⊆ required ∪ optional`**。
        """
        return bool(req) and bool(have) and req <= have <= (req | opt)

    @staticmethod
    def _station_named(km, name: str):
        """按**物体名**找台面 —— `op.at_name` 存的正是那个名字(如 `countertop_fake (6)`)。
        找不到返回 `None`, 调用方一律当"那盘菜没了"处理。"""
        if not name or km is None:
            return None
        for s in (getattr(km, "stations", None) or {}).values():
            if s.name == name:
                return s
        return None

    def _feasible(self, km, st, op, i, cx, cz, held, tm, reach, ops, pending, flow,
                  mate=None, strict: bool = True, steps=None) -> dict:
        """**这一步现在可行吗** —— 一次算齐"去哪儿 / 做不做得了 / 为什么不行 / 站哪个格"。

        这是"**可信验证**"的**唯一入口**。用户 2026-09-15 定的形状:
          > "可信验证不是说这个可信就去做, **需要剪枝的**, 用**菜谱需求校准**,
          >  保持更新的时候**都去计算一下接下来哪些东西是可行的**, 让…再寻路过去"

        三件事(分步做, 每步都能单独验证):
          ① **收成一个入口** —— 原来 `_rank_candidates.evaluate` 里那段内联代码搬到这里,
             **行为逐字不变**(本步只搬家, 好让"搬完一样"能被证明); *（已完成）*
          ② **数据新鲜度** —— 判据里读的东西都得是**当下**那一帧(`_spot_now` 那条线),
             喂旧帧的话"算得再勤也白算"; *（已完成）*
          ③ **剪枝 + 跨订单校准** —— 不可行的**连同下游**一起剪, 且 `⊕备料` 按**本单菜谱需求**
             筛。*（剪枝已完成 —— 判据落在 `_rank_candidates` 的 `steps` 参数上, 见那里的
             注释; 跨订单校准未做）*

        返回的字典**形状与搬家前逐字一致**(`op/label/why/cell/dist/follow`) ——
        `_rank_candidates` 后面的 `d`/`flow_ds`/`follow`/日志全都直接吃它, 一行不用改。

        ⚠ 只**读**世界, 不动任何状态(和 `_op_target_for_score` 同一个约束)。
        """
        target, label = self._op_target_for_score(km, st, op, cx, cz, tm=tm, reach=reach)
        ok, why = self._op_actionable(km, st, op, cx, cz, held, strict=strict,
                                      idx=i, ops=ops, pending=pending, flow=flow,
                                      steps=steps)
        if target is None and not why:
            # ☠ `_op_target_for_score` 的**"为什么解析不到目标"**就装在 `label` 里
            #   (如 "够不着: 计划货源@12.0,-6.0, 也没有别的够得着的货源")。
            #   不搬到 `why` 就会被日志丢掉, 表里只剩一句干巴巴的"够不着" ——
            #   而那正是"该去查哪个坐标/哪个箱子"的唯一线索(§5.1: 算了就要用)。
            why = label
        cell = None
        if ok and target is not None:
            if op.action == "pass" and mate is not None:
                # ☠ **传球有自己的判据** —— "站得到 + 丢得到队友", 不是"站得到队友旁边"。
                #   理由见 `_throw_spot`: 分厨房里"队友旁边"恰恰是走不到的地方,
                #   用 `_stand_cell_of` 会让 `pass` 恒 `-inf`(提议了却永远选不上)。
                #   仍然回**一个格**, 后面的 `d`/`flow_ds`/`follow` 一行都不用改。
                cell, _ = self._throw_spot(tm, reach, mate[0], mate[1])
            else:
                cell = self._stand_cell_of(tm, target[0], target[1], cx, cz,
                                           ortho_only=True, reach=reach)
        return {"op": op, "label": label, "why": why, "cell": cell,
                "dist": None, "follow": 0.0}

    def _dish_foreign(self, have, flow) -> set:
        """`have`(一盘里装的东西)里**本单不要**的那些 —— **非空就是"别的单的菜"**。

        ☠☠ 一条判据, 两个用处(2026-09-15 `s_sushi_1_3` 的"**两个错单**"):
          · **挑摆盘位时**(`_prepare_plate`): 那盘是别的单的 ⇒ 换一个台面,
            别把新料倒进旧菜里(实测把 `Sushi_PlainPrawn` 的虾倒进了**黄瓜那盘**);
          · **手上端着盘子时**(`_op_actionable` 的 `assemble` 分支): 那盘里有本单不要的
            ⇒ 不许把它并上本单的台面。
        ⚠ **半成品不算**: 全是本单材料的那些必须能继续用同一个台面 ——
          `pick_assemble_spot` 的"整局粘住"就是为它写的, 别误伤。
        ⚠ 判据来源和 `_deliver_plate`/`_spot_still_ready` **同一套**(`_dish_sets`)。
        """
        if not have or flow is None:
            return set()
        req, opt = self._dish_sets(flow)
        if not req:
            return set()
        return set(have) - (req | opt)

    def _spot_now(self, km):
        """**摆盘位的当前快照** —— 按 id 到新鲜 `km` 里重取, 并顺手更新 `self.assemble_spot`。

        ☠☠ 用户 2026-09-15 定的设计原则:
          > "**游戏本质上是实时, 我们不能用备份的数据了**, 应该积极更新数据, 包括脚本的订单、
          >  手上的盘、盘里的内容、盘的位置、厨师的位置等等"

        `self.assemble_spot` 是一个**整局粘住的 Station 对象**(`pick_assemble_spot` 的注释:
        "整局内粘住"), 它的 `.on`/`.onhas` **永远不会自己更新** —— 盘子被端走、被换掉、
        被并进别的菜, 读它还是老样子。
        ⇒ **凡是要读"那个盘里现在有什么"的地方, 一律走这里**, 别直接摸 `self.assemble_spot`。
          原来各处各读各的, 于是**同一条判据的结果取决于"有没有别的代码路径顺手刷新过这个对象"**
          (实测: `_spot_still_ready` 有时报对、有时漏报, 全看 `op_assemble`/`_top_up_plate`
           之前有没有走过) —— 这正是"行为像黑盒"的成因。

        ⚠ `km is None`(离线/没地图)时退回旧对象 —— 不制造新的崩溃点。
        ⚠ 顺手写回 `self.assemble_spot` 是**同一个 id 的新鲜对象**, 不改变"挑的是哪个台面",
          所以对"绝不改状态"的评分路径也是安全的。
        """
        if self.assemble_spot is None or km is None:
            return self.assemble_spot
        cur = getattr(km, "stations", {}).get(getattr(self.assemble_spot, "id", ""))
        if cur is not None:
            self.assemble_spot = cur
        return self.assemble_spot

    def _spot_still_ready(self, km, flow, spot) -> bool:
        """**执行时复核**: 这块台面上那盘**现在**还是不是本单拼好的菜。

        ☠☠ 为什么必须有(用户 2026-09-15 实机描述的现象):
          > "脚本拿着食材去盘子那, 放上盘子**被我拿着并且重新放一个空盘子**,
          >  脚本会拿着**空盘子**去提交。"

        "探测和执行看同一个台面"这条规矩,**台面名字没变不代表那盘菜没变**:
        人类队友把拼好的那盘端走、放一个**空盘**回去即可。而执行侧原来唯一的核对是
        `_approach(..., want=op.at_name)` —— 它问游戏"这个台面上是不是 `countertop_fake (6)`",
        **同一个台面名字, 内容可以完全换掉** ⇒ 照样通过 ⇒ 端起空盘送餐口
        ⇒ `✗ 送餐后订单 'Sushi_PlainFish' 仍在, 判为交付失败`(实机 2026-09-15 `s_sushi_1_1`)。

        ⚠ **判据不新写** —— 就是把 `_find_ready_dish`(候选生成用的那一条)**原样再跑一遍**:
          于是"能进候选"和"能端走"永远是同一个判据、同一份实现。
        ⚠ 读不到(没菜谱 / 抛异常)一律**放行** —— 同 `_order_live` 的取舍:
          宁可白跑一趟, 也不能因为读不到把整条流程卡死在交菜这一步。
        """
        if flow is None or spot is None:
            return True
        try:
            return any(s.id == spot.id for s in self._find_ready_dish(km, flow))
        except Exception:                                        # noqa: BLE001
            return True

    def _find_ready_dish(self, km, flow) -> list:
        """**所有**"已经拼好、可以交"的盘子所在台面(可能不止一个); 交菜用。

        判据跟**游戏自己那条**对齐(反编译 `CompositeAssembledNode.AssumeTypeMatch`):
        ```
            Contains(required ∪ optional, 盘中)  &&  Contains(盘中, required)
        ```
        即 **`required ⊆ 盘中 ⊆ required ∪ optional`**(双向包含, 重数也算)。
          · **只 `⊇` 是不够的**: 盘上多一样订单不认的料时, 游戏**拒收**(第二半不成立)
            ⇒ 我们会白端一趟。所以两头都要卡。
          · **`optional` 不能算进 `required`**: 可选料没加也算拼好了
            (`op_deliver` 那条闸门就是特意排除 optional 的, 两处规则必须一致)。
        台面范围: **有盘子且有内容**的台面, 排除三类
          · `serve`(已经在送餐口上了, 拿不回来) · `dirty_plates`(脏盘叠 `is_plate` 也为真)
          · `conveyor`(放上去会被传走)
        ⚠ 最终**送不送得进去由游戏说了算** —— `_deliver_plate` 里"订单剩余量变少 **且**
          盘子离手"才算成功, 所以这里判宽了也不会冒领功劳, 代价只是一趟路。
        ⚠ 保留**多个**候选(不再挑一个): 判据不完美时, 够不着的那盘不该把旁边那盘挡掉。
        """
        req, opt = self._dish_sets(flow)
        if not req:
            return []
        out = []
        for s in (km.of("counter") or km.of("board")):
            if s.id.startswith(("serve", "dirty_plates", "conveyor")):
                continue
            if s.kind in ("PlateStation", "DirtyPlateStack", "ConveyorStation"):
                continue
            if not self._has_plate(s):
                continue
            if self._dish_matches(self._plate_contents_on(s), req, opt):
                out.append(s)
        return out

    #: **"这一环的台面我走起来太远 ⇒ 不如把料丢给队友"** 的门槛(格/步)。
    #:
    #: 用户 2026-09-15: "**鼓励脚本在不可达或者路径过远的时候使用丢食材**"。
    #: 原来只有"够不着"(站位格解析不出来)才丢，于是"台面在厨房另一头、走 20 多格"
    #: 这种**明明该丢**的情况，脚本照样自己跑过去 —— 一局 150 秒，一趟 20 格就是 6 秒。
    #: `NEKO_PASS_FAR_CELLS` 可调; `0` = 退回旧行为(只有够不着才丢)。
    #: ⚠ 值别太小: 丢出去是**单向**的(丢完手就空了，还得回去捡/让对方处理)，
    #:   近处能自己做完的事不该甩给队友。20 格 ≈ 6 秒 ≈ 一局的 4%, 当起点。
    #: ⚠ 读它必须写 `self.PASS_TOO_FAR_CELLS` —— 它是**类属性**, 类体里当裸名字用会
    #:   `NameError`。2026-09-15 实机就栽在这: `_pass_candidates` 每帧抛一次, 异常被
    #:   上层吞掉只留一行 `[传球] 探测出错`, 于是**传球候选一个都生不出来** ——
    #:   新加的"路径过远"档整档静默失效, 而日志看起来只是"有点吵"。
    PASS_TOO_FAR_CELLS = float(os.environ.get("NEKO_PASS_FAR_CELLS") or 20.0)

    def _throw_spot(self, tm, reach, mx: float, mz: float):
        """**"站得到、又丢得到队友"的最省站位** —— 返回 `(格, 步数)`; 哪儿都不行就 `(None, None)`。

        ☠☠ **为什么传球不能走 `_stand_cell_of`**(2026-09-15 用户问的那个情景查出来的):
          `_stand_cell_of` 问的是"能不能站到**那个坐标**旁边" —— 对传球就是"站到**队友身边**",
          而在**分厨房**(两块地各成一个连通块)里, 那**恰恰是走不到的地方**。
          于是 `_pass_candidates` 每帧提议 `pass`、评分层却因为取不到站位格给 `-inf`
          ⇒ **传球一票也拿不到, 永远轮不到执行**。提议和评分是两套判据, 这是它们唯一对不上的地方。
          离线复现: `runtime/_splitkitchen_probe.py`(隔 2 格厚墙时 `score=-inf`);
          对照: 隔 **1 格薄墙**时 `_stand_cell_of` 恰好能取到"离队友 2 格"的那格
          (`max_di` 默认 2 ⇒ 那个函数实际接受 Δ=2 的格子, 而 Δ=2 是 2.4 格),
          所以只有**薄墙**那种分厨房碰巧能用 —— 判据本身是错的。

        传球真实的要求只有两条(和 `op_pass` 执行时那两句**一一对应**):
          ① 我走得到某处(`c in reach`);
          ② 从那儿**直线**距队友 ≤ `PASS_RANGE`。
        ⇒ 返回值里的**步数**就是评分要的 `d`(还是要按"走过去多远"扣分, 见 `scoring.W_DIST`),
          和"路径过远"那一档用的是同一个尺子。

        ☠ **这里**不加 `PASS_SLACK**: 那个余量是给**执行侧**的 —— 量的是厨师**实际站到哪儿**
          (`op_pass` 里量的是他走完之后的真实坐标, 而 `navigate_smart` 只保证到格心
          `tight≈0.5` 以内, 所以实际距离会比格心距离大一点)。评分这一侧量的是**格心**,
          必须**只紧不松**: 松了就会出现"评分说能做 ⇒ 选中 ⇒ 走到那儿执行时又说太远"
          的白跑一趟(`PASS_SLACK` 的注释里说的就是这件事, 只是方向反过来)。

        取**步数最小**的那个格: 既然哪儿都能丢到, 当然挑离自己最近的地方丢。
        """
        if tm is None or not getattr(tm, "ok", False) or not isinstance(reach, dict):
            return None, None
        lim = PASS_RANGE
        best, best_n = None, None
        for c, n in reach.items():
            wx, wz = tm.world_of(*c)
            if ((wx - mx) ** 2 + (wz - mz) ** 2) ** 0.5 > lim:
                continue
            if best_n is None or n < best_n:
                best, best_n = c, n
        return best, best_n

    def _mate_can(self, km, st, op, mate, tm=None, mreach=None):
        """**这一步他做得了吗** —— 返回 `(能?, 站位格, 步数, 为什么不能)`。

        ☠ **判据只有这一份**: 两个地方要问同一个问题, 各写一份迟早漂开 ——
          · `_rank_candidates` 的队友那一栏(`other_raw`, 供"让位"用);
          · `_pass_candidates` 的"**丢之前先看他做不做得了**"(别盲丢)。
          以前 `_pass_candidates` 只判"**我**做不了"就把料丢出去, 从不看他做不做得了。

        问的只有一件事: **他走得到那个台面吗**(`_stand_cell_of` + **他的**可达集)。
        ⚠ 用户 2026-09-15 要的就是它 —— "对菜单链的每一步做可行性验证, 才能接入
          正常的传递逻辑"。

        ☠⚠ **别拿它问 `assemble`**(2026-09-15 实机日志打回来的):
          `_op_target_for_score` 的 `assemble` 分支**不看位置**, 读的是 `self.assemble_spot`
          —— 那是**我**挑的摆盘位。所以用队友的坐标调它, 得到的其实是"**我**的摆盘位
          解析得出来吗", 报出来的理由会是 `还没挑摆盘位` 这种**关于我**的话。
          (`_pass_candidates` 因此把 `assemble` 整类排除了, 见那里的 docstring。)
          `chop`/`cook`/`mix` 没这个问题 —— 它们的台面是**关卡固有**的, 按位置解析。

        ⚠⚠ **"他手腾不腾得出来"这一条暂时不算**(用户 2026-09-15 明说"暂时不需要考虑
          丢过去他腾不出手的情况")。要加的话, 判据是"**料交过去之后**他做不做得了"
          (他手空、或手上本来就是这一件), **不是**"他现在手上拿着什么" —— 传球的
          前提就是他现在还没拿到(料在我手上), 拿 `_op_actionable` 套他必然答
          "他手空且板上没东西 ⇒ 做不了", 恰好把要判的那一类整个判死。
        """
        if mate is None or tm is None or not getattr(tm, "ok", False) or mreach is None:
            return False, None, None, "拿不到队友的可达集"
        # ⚠ 用**他**的位置和他那份可达集去解析目标 —— 和 `other_raw` 里原来那两行同源。
        target, label = self._op_target_for_score(km, st, op, mate[0], mate[1],
                                                  tm=tm, reach=mreach)
        if target is None:
            return False, None, None, label or "他那边也解析不到目标"
        oc = self._stand_cell_of(tm, target[0], target[1], mate[0], mate[1],
                                 ortho_only=True, reach=mreach)
        if oc is None:
            return False, None, None, "他也走不到那个台面"
        return True, oc, mreach.get(oc), ""

    def _pass_candidates(self, km, st, flow, ops, pending, tm=None, reach=None) -> list:
        """**链条上"我这边做不了"的那一环 → 把料丢给人类队友**(用户 2026-09-15 要求)。

        > "需要强化菜单链, 让脚本按菜单链走, 如果其中**有一环条件没达成就先行动在那一步**。
        >  如果脚本控制的地方**没有菜板切菜**, 但是有食物箱,
        >  那么脚本需要**拿到食物箱然后丢给我**。"

        判据(**只做有把握的那一种**, 免得把料乱丢):
          ① 这一环是**关卡固有台面**上的加工类 —— `chop`/`mix`/`cook`(⚠ **不含 `assemble`**,
             见下面那段 ☠);
          ② **它的台面我够不着 _或者_ 路径太远**(见 `PASS_TOO_FAR_CELLS`)
             —— 台面在的话多半就只在**人类那半边**(两个厨房是常态);
          ③ 那份料**我拿得到**(箱子/台面现货/地上 —— 走 `_fetch_source_live` 同一套判据);
          ④ ☠ **他也得走得到那个台面**(`_mate_can`) —— 用户 2026-09-15 的方向:
             "对菜单链的每一步做可行性验证, 才能接入正常的传递逻辑"。少了这一条就是
             **盲丢**: 丢过去他也够不着, 料躺在那边。
             跳过时会打一行 `[传球] ✗ 不丢 …(他也做不了)`, **只打一次**别刷屏。
             ⚠ "他手腾不腾得出来"**暂时不算**(用户明说), 见 `_mate_can` 的注释。
          手上正好拿着它 → 这条候选直接可做; 没拿 → 交给评分先选 `fetch`(下一步自然轮到它)。

        ☠☠ **`assemble` 为什么被排除**(2026-09-15 实机日志打回来的):
          `chop`/`mix`/`cook` 的台面是**关卡固有的**东西(砧板/搅拌台/灶),
          "够不着"就是真的够不着 —— 那条信息才说明"这一环得靠队友"。
          而 `assemble` 的"台面"是 `self.assemble_spot` —— **我自己挑的摆盘位**,
          `_op_target_for_score` 里那一段**压根不看位置**, 报的是
          `(None, "还没挑摆盘位")`。于是:
            · ② 会把"**我**的摆盘位被清空了/还没挑"当成"该传球"的理由 ——
              实测那局就差一点因此把**刚 fetch 到手的 Seaweed 丢给人类**;
            · ④ 问队友时走的还是**同一个**函数 ⇒ 印出来的理由是
              `他也做不了这一环(还没挑摆盘位)` —— **说的其实是我**, 谁看都会去查错人。
          ⇒ 两个方向都不成立, 所以整类排除。真要做"摆盘这一环交给队友", 得先能表达
            "**他**的组装台面在哪", 那是另一件事(`_op_target_for_score` 现在没这个概念)。

        ☠ **2026-09-15 用户要求补上"路径过远"这一档**:
          > "需要强调一下食材的传递, **鼓励脚本在不可达或者路径过远的时候使用丢食材,
          >  传递给另一个厨师的可达范围**。"
          原来只有**二值**判据(`_stand_cell_of` 返回 None = 够不着) —— 于是"台面在
          厨房另一头、走一趟 20 多格"这种**明明该丢**的情况，它照样自己跑过去。
          现在多一档: 站得到、但**步数超过 `PASS_TOO_FAR_CELLS`** 也提议传球。
        ⚠ **还没做的**: 落点现在还是**队友当前坐标**(`mate[0], mate[1]`)。
          用户说的是"传给**他的可达范围**" —— 而丢是有射程的(实测 5~8 格, 见 `_prepose`
          的"实测射程"日志)。队友站得远时会丢不到。**要按射程挑落点, 那是下一步**,
          得先有个"这一丢能落在哪"的模型(现在只在 `_prepose` 里有实测数据)。
        """
        out = []
        mate = self._mate(st)
        if mate is None:
            return out                      # 单人局没有"丢给谁"
        if tm is None or not getattr(tm, "ok", False):
            return out
        cx, cz, held = self.pos(st)
        if cx is None:
            return out
        seen = set()
        _mreach = None                      # 队友的可达集 —— **按需**算(见 ④)
        for i in pending:
            op = ops[i]
            # ☠ **`assemble` 不在列** —— 它的"台面"是**我自己挑的** `self.assemble_spot`,
            #   "解析不到"是**本机瞬时状态**, 不是"这一环得靠队友"(见 docstring 那段)。
            if op.action not in ("chop", "mix", "cook"):
                continue
            tn = self._norm(op.target)
            if not tn or tn in seen:
                continue
            seen.add(tn)
            # ① ☠ **已经交出去过的那一环, 别再提一次** —— 台账活着就跳过。
            #   不跳的话: 丢出去 → 手空 → 下一轮又提议 → 又去 crate 拿一份 → **再丢一次**,
            #   无限循环(`pass` 是杂活, **从不销号**, 见 `_execute_scored` 的 `is_chore`)。
            if self.handoff_live(op.action, op.target):
                continue
            # ② 我够不着那一环的台面吗 —— **或者路太远**(用户 2026-09-15 要求的那一档)
            _why = ""
            tgt, _lbl = self._op_target_for_score(km, st, op, cx, cz, tm=tm, reach=reach)
            if tgt is None:
                _why = "解析不到台面"
            else:
                _sc = self._stand_cell_of(tm, tgt[0], tgt[1], cx, cz,
                                          ortho_only=True, reach=reach)
                if _sc is not None:
                    _d = reach.get(_sc)
                    if _d is None or _d <= self.PASS_TOO_FAR_CELLS:
                        continue        # 够得着而且不远 → 自己做, 不用丢
                    _why = (f"走过去要 {_d:.0f} 格"
                            f"(超过 {self.PASS_TOO_FAR_CELLS:.0f})")
                else:
                    _why = "台面够不着"
            # ③ 这份料我拿得到吗
            src = self._fetch_source_live(km, st, Op("fetch", op.target), cx, cz,
                                          tm=tm, reach=reach)
            if src is None and not self._held_is(held, op.target):
                continue
            # ④ ☠ **他也得走得到那个台面** —— 原来只判"**我**做不了"就把料丢出去, 从不看他
            #   做不做得了, 那是**盲丢**: 丢过去他也够不着, 料就躺在那边(还不如留在手上)。
            #   判据和 `other_raw` 那一栏**同一份**(`_mate_can`), 别在两边各写一套。
            #   ⚠ "他手腾不腾得出来"暂时不算(用户 2026-09-15 明说), 见 `_mate_can`。
            #   ⚠ 队友的可达集**按需算**(这一轮真有东西要丢才算), 不给每轮白跑一次 BFS。
            if _mreach is None:
                _mreach = tm.distances_from(mate[0], mate[1],
                                            extra_edges=self._travel_edges(km, tm))
            _can, _mc, _mn, _mwhy = self._mate_can(km, st, op, mate, tm, _mreach)
            if not _can:
                _k = (op.action, op.target, _mwhy)
                if _k not in self._pass_told:       # 每个决策点都会走到这里, 别刷屏
                    self._pass_told.add(_k)
                    # ⚠ **把动作名一起打出来** —— 只说 `'Seaweed'` 看不出要丢的是哪一环
                    #   (同一份料在不同关卡可能是 `chop` 也可能是 `cook`),
                    #   而"为什么没丢"正是调参时要看的东西。
                    self.log(f"[传球] ✗ 不丢 {op.action} {op.target} 给队友 —— "
                             f"他也做不了这一环({_mwhy})")
                continue
            out.append(Op("pass", op.target,
                          note=f"我这边做不了 {op.action}({_why}) —— 丢给队友",
                          at_name="队友", at_x=mate[0], at_z=mate[1],
                          # ⚠ **必须带上被替代的那一步**(`chop`/`cook`/…)——
                          #   台账按 `(那一步的 action, target)` 记账, 而 `pass` 自己的
                          #   `action` 是 `"pass"`。别去解析 `note`, 见 `Op.handoff` 的注释。
                          handoff=op.action))
        return out

    def _redos(self, km, st, ops, pending, n_recipe: int) -> list:
        """**回溯**: 下游那一步要的料没了 → 把**产出它的上游那一步**重新提出来当候选。

        用户 2026-09-15 原话:
          > "菜谱不是链吗, 如果现状走不通就**回溯到上一级**看看能不能执行, 如果上一级
          >  不能那就再退到上上一级, **第一步是只有地图分块的时候做不了, 其他时候
          >  可以做的, 就不应该发呆**。"
          > "**我觉得回溯本身也是需要引入评分的, 不然太呆了**"

        ⇒ 所以这块**不写硬规则**("失败就自动退一级"), 而是**进同一张评分表**去和别的
          候选一起排 —— 判据只管"提不提", "选不选"交给评分:
            · 旁边一口锅快糊了 → `rescue`(15 + 紧迫度, 能涨到 100+) 赢, 先去救锅;
            · 真的没事干 → `redo fetch X`(步骤价 20) 赢过 `work boardN`(11), 去把料拿回来。
          这正是 `_chore_candidates`/`_pass_candidates`/`_rescues` 已经用了三次的形状。

        ☠☠ **为什么非要有它**(2026-09-15 `s_sushi_1_1` 实机): 一单**整单锁死**到对局结束。
          链路是 `fetch SushiFish` 成功(料随即被预置丢到 `(13.8,-7.9)`, 而那条预置随后
          因"走不到"作废) ⇒ `chop SushiFish` 判 `不可达(手空且板上没东西)` ⇒
          `assemble`/`deliver` 跟着 `-inf`。**三个候选全灭**, 而"把鱼拿到手上"那一步
          **已经从 `pending` 销号了**, 没有任何机制把它放回来。
          而且它**不会自己好**: `IDLE_WAIT` 的 20 秒窗口每次都被一件杂活
          (`work boardN`, 评分 9~11)复位 ⇒ `_idle_since` 永远攒不满 ⇒ 这一单
          **永不作废、永不重规划**。日志里二十来次"先等 20 秒再看", 一次都没等满。
          ⚠ 「链子断了」那条判据(`_execute_scored` 里)认得出这个局面, 但它的
            `_TERMINAL = ("deliver","pass","assemble")` **故意不含 `chop`** ——
            理由是"别人把料放到板上我空手也能切" —— 那条推理的前提是**料还在**。
            这里料已经没了, 于是判据被绕过。⇒ 与其放宽判据去"判死", 不如**把链补回来**。

        判据(**收窄过的**, 三条都要满足):
          · **下游是 `chop`/`cook`/`mix`** —— 这三个纯加工的前置百分之百是"**手上有 X**"。
            `assemble` **不在此列**: 它的前置是"手上有 X **或盘子**", 卡住的原因通常是
            盘子/站位(横向换台面已经覆盖), 回溯语义不同, 硬套会把料白跑一趟。
          · **手上的不是这个 X** —— 手上就是它 ⇒ 上游那一步还没走完, 不是"没了"。
          · **上游那一步在 `ops` 里、同名、且已经不在 `pending` 里** —— 还在 `pending`
            说明它本来就轮得到, 不用回溯。

        ⚠ **一次只退一级**(取下标最近的那个同名上游); 上一级要是也做不了, 下一轮它会
          自己再退一级(`fetch X` 是最上游, 到它为止)。
        ⚠ **杂活闸门不用为它开洞**: `enroute` 档的规则是"菜谱一个可做的都没有 ⇒ 都进",
          而它冒头的场合正是那个(`flow_ds` 空); 菜谱正常时它被"顺路"挡掉 = 不多跑一趟。
        """
        _, _, held = self.pos(st)
        out, seen = [], set()
        for i in pending:
            if i >= n_recipe:                 # 杂活(合成下标)不是菜谱步骤
                continue
            op = ops[i]
            if op.action not in ("chop", "cook", "mix"):
                continue
            t = self._norm(op.target)
            if not t or t in seen:
                continue
            seen.add(t)
            if self._held_is(held, op.target):
                continue                      # 手上就是它 → 上游没断
            # ☠☠ **交出去过的那一环不回溯** —— 这条是"死循环"的主犯, 别漏。
            #   它沿**同名链**往回找"已不在 pending 的同名上游", 而在"生料和成品同名"
            #   那一族关卡里(`SushiFish --切8次--> SushiFish`)正好能找到 `fetch SushiFish`
            #   ⇒ 生成 `redo fetch X` ⇒ **我再去拿一份, 再丢一次**。
            #   (名字会变的那一族 `fetch raw → chop Y` 不同名、找不到 —— 但判据该按
            #    "这一步已经交出去了", 不是按名字碰巧对不对。)
            if self.handoff_live(op.action, op.target):
                continue
            # 沿**同名链**往回找紧邻的上一级(下标更小、同名、且已经不在 pending 里)
            for k in range(i - 1, -1, -1):
                if k in pending:
                    continue
                if self._norm(ops[k].target) != t:
                    continue
                out.append(Op(ops[k].action, ops[k].target, redo=True,
                              note=f"回溯: {op.action} {op.target} 的料没了"
                                   f" → 补 {ops[k].action}"))
                break
        return out

    def _all_flows(self, st) -> list:
        """订单栏上**每一张**单的 `DishFlow` → `[(flow, t)]`, 按 `t` 升序。

        为什么能拿到"不是当前那单"的菜谱: `state.details` 报的是**整关菜谱池**
        (`StateCollector.cs:364-385`), 不是只有当前单 ⇒ 任意一张挂单都推得出来。
        (`Engine.plan()` 早就在做同一条链, 只是它只挑最紧急那一单。)

        ⚠ `derive` 不便宜, 而这是每 0.5 秒的循环 ⇒ **按"订单名集合"缓存**
          (`t` 每帧都在变、不进 key; 它只影响排序)。
        """
        try:
            orders = self.live_orders()
        except Exception:                                          # noqa: BLE001
            return []
        key = tuple((o.get("name") or "") for o in orders)
        if getattr(self, "_flows_key", None) == key:
            return getattr(self, "_flows_val", [])
        out = []
        for o in orders:
            name = o.get("name") or ""
            if not name:
                continue
            try:
                detail = self.find_detail(st, name)
                if detail:
                    out.append((self._derive_with_retry(detail, st), float(o.get("t", 1.0))))
            except Exception:                                      # noqa: BLE001
                continue
        out.sort(key=lambda ft: ft[1])
        self._flows_key, self._flows_val = key, out
        return out

    def _inventory(self, km, st) -> dict:
        """**场上现在每种材料有几份** `{归一化名: 份数}` —— 只读聚合, 一次算好。

        为什么需要它: 提前备料的判据是"订单要 N 份, **现在有几份** ⇒ 缺几份",
        而全仓现有的查询(`_find_item_station` / `_find_ground_item` /
        `_fetch_source_live` / `_plate_contents_on` / `km.of()`) **全是单点查询** ——
        问的是"最近那一件在哪", 没有"总共有几份"。
        来源: **手上**(含手上容器的内容) + **台面/板上**(含盘里) + **锅里** + **地上/未认领**。

        ⚠ 这是每 0.5 秒的循环里跑的 ⇒ **一次算好传下去**, 别让每个候选各扫一遍
          `km.stations`(那是"候选数 × 台面数"的线性增长)。
        ⚠ **按名字数, 不区分加工阶段** —— 同一关里"生料和切好的同名"时
          (`SushiFish --切8次--> SushiFish`)名字分不出阶段 ⇒ 这里只能按份数算。
          "提前切好"那半需要扫描报出加工阶段, 记在 `_preps` 的遗留里。
        """
        inv = {}

        def add(name):
            k = self._norm(name or "")
            if k:
                inv[k] = inv.get(k, 0) + 1

        try:
            _, _, held = self.pos(st)
            add(held)
            for c in self._held_contents(st):        # 手上那件容器里装了什么
                add(c)
        except Exception:                                          # noqa: BLE001
            pass
        for s in (getattr(km, "stations", None) or {}).values():
            for o in (getattr(s, "on", None) or []):
                add(o)
            try:
                for c in self._plate_contents_on(s):            # 盘里装的
                    add(c)
            except Exception:                                      # noqa: BLE001
                pass
        for ck in (getattr(km, "cooking", None) or []):          # 锅里
            add(getattr(ck, "inside", "") or getattr(ck, "ing", ""))
        try:
            for it in (km.unseen_items() or []):                 # 地上/台面外的
                add(getattr(it, "name", ""))
        except Exception:                                          # noqa: BLE001
            pass
        return inv

    @staticmethod
    def _first_chain_op(flow, mat: str):
        """在**这一张单**的菜谱里找 `mat` 那条链的**第一环**(`fetch`/`chop`/`cook`/`mix`)。

        链在 `DishFlow.ops` 里本来就是按序的(`fetch → [chop] → [cook|mix] → assemble`),
        所以"第一个同名且是加工类的 op"就是第一环。
        找不到返回 `None`(这张单不要它 ⇒ 不提议)。

        ⚠ **只看本单**, 不看整个订单栏 —— `fetch` 这个动作**本身不携带"给哪张单"的信息**,
          拿别的单的链首来提, 取回来的料对本单毫无用处(见 `_preps` 的"两个口径")。
        """
        for op in (getattr(flow, "ops", None) or []):
            if Engine._norm(getattr(op, "target", "")) != mat:
                continue
            if getattr(op, "action", "") in ("fetch", "chop", "cook", "mix"):
                return op
        return None

    def _preps(self, km, st, flow, flows, inv) -> list:
        """**提前备料** —— 订单栏要 N 份、现有 M 份 ⇒ 把缺的那份提出来当候选。

        用户 2026-09-15:
          > "…可以做一些**延迟收益**, **提前去切三条鱼**。"
          > "它得从'**当前场上有几张单、每张要几条**'**推**出来。"
          > "**'切三条'是举例, 不是常数。** … **别把 3 写进代码。**"

        `flow`  —— **正在做的这一张**(本单); `flows` —— 订单栏上**全部**的单(`_all_flows`)。

        ☠☠ **两个口径, 别混**(用户 2026-09-15: "**没有完全按菜谱来啊, 导致两个错单**"):
          · **提不提** ⇒ `lookahead.shortfall_of(flow, inv)` —— **本单**要的、而且**本单**
            现在还缺的那几种;
          · **提几份** ⇒ `lookahead.demand(flows, inv)` —— **订单栏口径**, 一个常数都没有
            (`lookahead.PREP_MAX` 只是"订单要得太多"时的刹车)。
        改之前**只有后者** ⇒ 它会**替别的单把料拿到手里**。而备料候选是在**本单的决策循环**
        里执行的(`_execute_scored(flow)`) ⇒ 手里攥着本单用不到的料 ⇒ 本单要用那只手
        ⇒ 按"空手是回退态"把料**丢在地上** ⇒ 白跑一趟, 而且那份料从此躺在一个没人找的
        地方(**两个单一起受伤**)。用户原话: "**导致两个错单**"。
        ⚠ 判据是"**本单还缺**", 不是"本单菜谱里有这个名字" —— 本单那份**已经拼进盘子**时,
          按订单栏口径它照样算"缺"(那其实是**别的单**的份数), 只按名字筛挡不住这一类。
        ⚠ 形状和 `_toss_budget`(取货那一趟连备几份)**是一致的**: 料来自本单、份数来自
          订单栏 —— 那边一直是这么写的, 这里是把它对齐。

        ⚠ **入池时机不开任何豁免**: 让它走现有的 `enroute` 闸门就够了 ——
          `_chore_admitted` 里"`flow_ds` 为空(菜谱一个可做的都没有) ⇒ 全部放行"
          那条**正好就是**"主链受阻时才做延迟收益"的语义; 而菜谱做得动时它会被
          "顺路"挡在外面。**所以 `scoring.py` 一行都不用改, 阶段一排序不动。**

        ☠ **遗留(明确不做, 别当它已经支持)**:
          现在只能做到"**把份数凑够**"(提前去把料拿到手), **做不到"提前切好/煮好"** ——
          因为同一关里生料和切好的料**同名**(`SushiFish --切8次--> SushiFish`),
          `_inventory` 按名字数、分不出阶段 ⇒ "还缺 1 份"在"手里有一条生鱼"时就已经算满足了。
          要真正做到"提前切三条鱼", 得先让扫描**报出加工阶段**(那是 C# 侧的事)。
        """
        if flow is None or not flows:
            return []
        # ① **本单还缺什么** —— 决定"提不提"。缺一样都算白提, 所以空集合直接收工。
        mine = lookahead.shortfall_of(flow, inv)
        if not mine:
            return []
        try:
            gap = lookahead.demand(flows, inv)
        except Exception as e:                                     # noqa: BLE001
            self.log(f"[备料] 算缺口出错: {e!r}")
            return []
        out = []
        for mat in sorted(mine):
            # 份数报**订单栏口径**(那才是"该备几份"的来源); 这单刚下架、不在订单栏里时,
            # 退回本单口径 —— 别让一次读不到算出 0 份(那等于静默把备料关掉)。
            n = int(gap.get(mat, mine[mat]))
            src = self._first_chain_op(flow, mat)
            if src is None:
                continue
            out.append(Op(src.action, src.target, wait=src.wait,
                          chop_stages=src.chop_stages, in_pot=src.in_pot,
                          prep=True, note=f"提前备料(本单还缺 {mine[mat]} 份, "
                                          f"订单栏共缺 {n} 份)"))
        return out

    def _rescues(self, km, st) -> list:
        """**锅快糊了** → 去把它从灶上端下来(候选; 分随紧迫度上涨)。

        数据来源: `layout.cooking[]`, 每条 = 一个烹饪句柄(`SceneScanner.ScanCooking`):
          `prog`(已煮秒) / `need`(需煮秒) / `state`(Raw|Cooked|Burnt) / `burning` / `in`(锅里是什么)

        ⚠ **`state == "Cooked"` 就是游戏的"报警"**: 那边 `CookingUIController` 的
          `OverDoing` 状态(警告图标脉冲 + `GameOneShotAudioTag.CookingWarning` 音效)正对应
          `need < prog <= 2*need`。我们**不用听声音也不用看图标** —— `prog/need` 是连续量,
          能直接算出"还差几秒糊"(`2*need - prog`)。
        ⚠ 用户 2026-09-15 定的规矩: **"如果报警, 对应的评分应该上涨"** —— 所以这里
          **不设硬阈值闸门**, 只把 `ratio` 交给 `scoring.burn_urgency` 换算成加成分:
          刚报警时 0 分(低于 `YIELD_MIN_SCORE` ⇒ 让位给站在旁边的人),
          越接近糊涨得越猛(130 上限 ⇒ 压过 `deliver` 的 100 ⇒ 必须我去)。
        ⚠ **不排除"自己的锅"**: 我正在煮的那口由 `op_cook` 盯着(那里根本不进评分),
          而"上一轮失败/被顶开后留在灶上的我的锅"和队友的锅一样需要救。
        ⚠ 已经糊了(`ratio >= 2`)的不生成 —— 那道菜已经废了, 端下来也没用;
          真烧起来了(`burning`)是**另一条路**(主循环的灭火), 这里也不管。
        """
        if (os.environ.get("NEKO_RESCUE") or "1").strip().lower() in ("0", "off", "no", "false"):
            return []
        out = []
        for c in km.cooking or []:
            need = float(getattr(c, "need", 0) or 0)
            prog = float(getattr(c, "prog", 0) or 0)
            if need <= 0 or prog <= need:
                continue                      # 还没到点(或数据不全)
            ratio = prog / need
            if ratio >= 2.0:
                continue                      # 已经糊了
            if str(getattr(c, "burning", "")).lower() in ("true", "1"):
                continue                      # 烧起来了 → 走灭火那条路
            x, z = getattr(c, "x", None), getattr(c, "z", None)
            if x is None or z is None:
                continue
            what = getattr(c, "inside", "") or getattr(c, "name", "") or "锅"
            # ☠ **走不到的那口锅要上冷板凳** —— 实测(`s_sushi_1_3`)一口**卡在 14/12 秒
            #   不再变化**的锅让 `rescue` 每轮都被选中(40 分)、每轮都"走不到旁边(差 1.00 格)",
            #   把半局烧在同一个不可能的动作上。杂活那条"本轮不再选它"只在本轮有效,
            #   而 `pending` 每轮重建 ⇒ 下一轮它又回来了。这里用冷板凳跨轮压住。
            if self.step_benched(f"rescue {what}"):
                continue
            # ☠☠ **拾取一件"架在台面上"的东西时, 游戏报的是【台面名】, 不是锅名。**
            #
            # 依据(反编译, 规则 1): `ServerAttachStation.cs:107-119`
            #     public bool CanHandlePickup(ICarrier _carrier) {
            #         if (m_item != null) {
            #             IHandlePickup h = m_item.AccessGameObject().RequireInterface<IHandlePickup>();
            #             return h.CanHandlePickup(_carrier);      // ← 转发给 m_item(锅)
            #         } ... }
            #   `HandlePickup` 同样转发, 靠 `AttachChangedCallback` 判断"东西真被拿走了"。
            #   ⇒ **台面自己不是拾取目标** —— "对着台面按拾取 = 把锅端走"。
            #
            # 而这里原来传的是**锅名** ⇒ `_approach` 拿"游戏说的抓取目标"去比, 永远
            # "✗ 不是它" ⇒ **端不下来** ⇒ 米烧糊。实机账(`s_sushi_1_3`):
            #     [救锅] SushiRice 已经过火(15/12 秒, 剩 9 秒糊) —— 端下来
            #     [接近] (13.4,7.8) 距 1.17 格, 游戏说: 抓取='countertop_01_standard_wood (5)' ✗ 不是它
            #     [接近] (13.4,8.3) 距 1.01 格, 游戏说: 抓取='countertop_01_standard_wood (5)' ✗ 不是它
            #   ⇒ 用户原话: "**还有锅报警的或需要把锅移开**"。
            #
            # 判据用**挂载点**(`Item.on`, 上一轮刚报上来的): 挂着 → 要**台面名**;
            #   没挂(在地上/端在手上)→ 要**物件自己的名字**(那时没人转发)。
            #   ⚠ 查不到(老 dll 没 `ScanItems` 的 `on`, 或那件东西不在 `km.items` 里)
            #     ⇒ **退回锅名**, 与改之前一致。
            _pot_name = getattr(c, "name", "") or what
            _mount = ""
            for _it in (getattr(km, "items", None) or []):
                if _it.name == _pot_name and getattr(_it, "on", ""):
                    _mount = _it.on
                    break
            out.append(Op(
                "rescue", what,
                "%s 已经过火(%.0f/%.0f 秒, 剩 %.0f 秒糊) —— 端下来" % (
                    what, prog, need, max(0.0, 2 * need - prog)),
                at_name=_mount or _pot_name, at_x=x, at_z=z,
                urgency=scoring.burn_urgency(ratio)))
        return out

    def op_rescue(self, km, x, z, op: Op, st: dict) -> bool:
        """把一口**已经在报警窗口里**的锅从灶上端下来, 放到旁边的空台面。

        为什么是"端锅"而不是"取菜": 报警窗口里锅里的东西**已经熟了**(`state == Cooked`),
        再放下去就是糊(`> 2*need` → Ruined)。端下来 = 停火, 料还在锅里, 之后照常处理。
        这也是唯一一个"不需要先想清楚这锅是谁的"的动作 —— 糊了对谁都没好处。
        """
        self.log(f"[救锅] {op.note or op.target}")
        if not self._approach(km, op.at_x, op.at_z, tight=0.8, want=op.at_name):
            self.log(f"[救锅] 走不到 {op.at_name or op.target} 旁边")
            return False
        if not self.interact("pickup", verify_hold_change=True):
            self.log("[救锅] ✗ 没端起来(锅还在灶上继续烧)")
            return False
        # 端起来就已经**停火**了(游戏里锅离开灶台就不再加温), 所以下面这步是"收拾干净"。
        # 放不下也不当失败 —— 端着锅比让它继续烧强。
        self._put_down_plate(km, x, z)
        return True

    def _chore_admitted(self, chore, d, flow_ds, used: dict) -> tuple:
        """这件杂活**现在能不能进候选池** —— 返回 `(能不能, 原因)`。**纯函数, 可离线核对。**

        "混合: 能插就插"(用户选的档)的全部规则都在这一个函数里:

          · `serve_any`       —— **永远进**(交菜是临门一脚, 见 `scoring.STEP_VALUE` 的注释)
          · `used >= 上限`    —— 不进(防"按钮按了还是 pressable"这类反复被选中)
          · 模式 `0`/`off`    —— 只有 `serve_any` 进(一键拔保险丝)
          · 模式 `always`     —— 都进
          · 模式 `stuck`      —— 只有"菜谱一个可做的都没有"时才进
          · 模式 `enroute`(默认) —— 菜谱没有可做的 → 都进("拿不到食材就做别的");
                                   有的话只放**明显顺路**的:
                                   `d ≤ NEARBY` 且 `|d − 最近的菜谱目标| ≤ ENROUTE`

        参数(距离全部由调用方从**已经算好的**那张表里取, 这里不跑 BFS):
          `d`       —— 这件杂活"站到旁边"的**步数**; `None` = 走不到/站不到
          `flow_ds` —— 菜谱候选里**可做**那些的步数列表; 空列表 = 菜谱一个可做的都没有
        """
        a = getattr(chore, "action", "")
        if a == "pass":
            # **传球不受杂活闸门管** —— 它不是"顺路顺手做的杂活", 而是
            #   "链条上有一环我这边做不了"时的**唯一出路**(用户要求"先行动在那一步")。
            #   它由 `_pass_candidates` 在生成时就卡过条件了(够不着 + 料拿得到)。
            return True, ""
        if a == "rescue":
            # **救锅同样不吃顺路闸门**(同 `pass` 的理由) —— 但和 `pass` 不同:
            #   它**也不吃 `NEKO_CHORES=0` 那个总开关**。一口快糊的锅不是"顺便做的杂活",
            #   是"再不动这道菜就废了 + 那个台面着火后不能拿放/不能切"。调试期想关它,
            #   用 `NEKO_RESCUE=0`(见 `_rescues`), 别让"关杂活"顺手把救火也关了。
            #   什么时候该让位、什么时候必须我上, 全由**紧迫度打分**表达(见 `scoring.burn_urgency`),
            #   所以这里不设任何阈值。
            # ⚠ **`--mode sabotage` 下救援会垫底**(`scoring.transform` 取负 ⇒ 分越高越低):
            #   那是**故意的**, 不是 bug —— 捣蛋鬼的 `Mischief.BURN` 本来就是"放任灶台烧糊"。
            return True, ""
        if getattr(chore, "redo", False):
            # **回溯同样不吃顺路闸门、也不吃 `NEKO_CHORES=0`**(同 `pass`/`rescue` 的理由)。
            #   用户 2026-09-15: "菜谱不是链吗, 走不通就回溯到上一级…**就不应该发呆**"。
            #   它是"链断了"时的自修动作, 不是"顺手做的杂活" —— 要是被总开关一起关掉,
            #   那么"不发呆"这个性质就**悄悄挂在一个调试开关上**(调试期开 `NEKO_CHORES=0`
            #   二分定位时, 链一断就又开始发呆, 看起来像修好的东西又坏了)。
            #   ⚠ 认它用显式标记 `Op.redo`, **不是** `action == "fetch"` —— 后者等于
            #     赌"以后不会有 fetch 类杂活"。
            return True, ""
        # ⚠ **总开关排在最前面** —— 包括 `serve_any`。调试期要靠 `NEKO_CHORES=0`
        #   把整条杂活链路一次性拔掉做二分定位; 留一个"关不掉的动作"会让它没用。
        if CHORE_MODE in ("0", "off", "no", "false", "none"):
            return False, "杂活开关关了(NEKO_CHORES=0)"
        key = chore_key(chore)
        if used.get(key, 0) >= scoring.CHORE_REPEAT_MAX:
            return False, f"本轮已经做过 {used.get(key, 0)} 次"
        if a == "serve_any":
            return True, ""            # 交菜不受"顺路"限制(见 scoring.STEP_VALUE 的注释)
        if CHORE_MODE == "always":
            return True, ""
        if not flow_ds:
            return True, ""                       # 菜谱一个可做的都没有 → 拿不到食材就做别的
        if CHORE_MODE == "stuck":
            return False, "菜谱还有能做的动作(模式=stuck)"
        if d is None:
            return False, "走不到那件杂活的站位"
        if d > scoring.CHORE_NEARBY_CELLS:
            return False, f"不顺路(要 {d} 格)"
        if min(abs(d - f) for f in flow_ds) > scoring.CHORE_ENROUTE_CELLS:
            return False, "不顺路(不在去菜谱目标的路上)"
        return True, ""

    def _chores(self, km, st) -> str:
        """主流程卡住 → **改做一件杂活**。返回做了什么(空串 = 没得做)。

        用户的要求(原话):
          "如果拿不到食材, 就检查能否做其他事 —— 切菜, 洗盘子, 交菜, 灭火,
           控制机关, 搅拌, 烘培。我们需要脚本有完成菜谱的完整能力, 但是
           我们不希望脚本自己做自己的 —— 这个游戏始终是个合作游戏,
           可以让人类处理一部分评分不高的行为。"

        ⚠ **定位: 杂活只在主流程卡住时才做。** 没卡住时脚本专心做菜,
          那些低分值的活就摆在那儿 —— 人类想干就干。这样天然就"不抢着全干",
          不需要去猜"人类是不是在挂机"。

        ⚠ 灭火**不在这里** —— 它在主循环里优先级更高(火会把台面一片片烧失效)。
        """
        cx, cz, held = self.pos(st)
        if cx is None:
            return ""
        if held:
            return ""          # 手上有东西时先别做杂活(可能正要交给主流程用)

        # ① 按机关按钮(闸门/开关/传送带开关) —— 最独立, 先试它
        try:
            if self.op_press(km, st):
                return "按机关"
        except Exception as e:
            self.log(f"[杂活] 按机关没做成: {e}")

        # ② 洗盘子 —— 脏盘子堆有货 + 这关有洗手池
        try:
            if self.op_wash(km, st):
                return "洗盘子"
        except Exception as e:
            self.log(f"[杂活] 洗盘子没做成: {e}")

        # ③ 台面上放着**该加工还没加工**的料 → 加工掉。
        #   切菜/搅拌/烘培**是同一条路**: 都是"站到台子旁边按交互键",
        #   所以不用按台子类型分开写(分开写迟早会漂)。
        for sem in ("board", "mix", "hob", "oven", "fryer", "heat", "auto"):
            for s in km.of(sem):
                on = list(getattr(s, "on", []) or [])
                if not on or not self._needs_work(on[0]):
                    continue
                self.log("[杂活] %s 上放着没加工完的 %s, 去加工" % (s.id, on[0]))
                try:
                    if self._approach(km, s.x, s.z, want=s.name):
                        self.interact("chop", verify_hold_change=False)
                        return f"加工 {on[0]}"
                except Exception as e:
                    self.log(f"[杂活] 加工 {on[0]} 没做成: {e}")
        return ""

    def plan(self, st: dict) -> tuple | None:
        """根据状态规划"现在该做哪道菜", 返回 (订单名, 剩余比例, DishFlow) 或 None。

        取当前挂在订单栏上、剩余时间最少的那张订单 —— 订单是顺序出现的,
        不需要预测, 读它就行。
        """
        if self.know is None and not self.ensure_knowledge(st):
            return None
        orders = self.live_orders()
        for o in orders:
            name = o["name"]
            # 双人: 一张订单只由一个厨师认领, 否则两人做同一道菜会互相打架
            if self.board is not None and not self.board.claim_order(name, self.cid):
                continue
            detail = self.find_detail(st, name)
            if not detail:
                if self.board is not None:
                    self.board.release_order(name, self.cid)
                continue
            return name, float(o.get("t", 1.0)), self._derive_with_retry(detail, st)
        return None

    # ---------------- 主循环 ----------------
    def _persona_name(self) -> str:
        """当前人格名(coop/clumsy/sabotage), 没有就空串 —— 给猫娘认"现在谁在玩"。"""
        try:
            return self.mode_state.mode.value if self.mode_state is not None else ""
        except Exception:                                        # noqa: BLE001
            return ""

    def _publish_status(self, st: dict = None, paused: bool = False) -> None:
        """把"这局什么情况"写到 `runtime/status.json`(契约见 `neko/status.py`)。

        这是给**猫娘/宿主**的反方向通道: `neko/control.py` 是"外面下命令给引擎"
        (方案文档 D10 说的"猫娘 = 调 set_mode 的开关"), 而这条是"引擎报状态给外面" ——
        猫娘光能下命令、不知道结果, 就永远说不出"赢啦"。

        节流 1 秒(文件很小, 但没必要每轮 0.5 秒都刷盘)。
        ⚠ 写失败**只记一行日志**, 绝不影响主循环 —— 这是给别人看的旁路,
          它坏了不该让脚本停摆。
        """
        now = time.time()
        if now - self._status_at < 1.0:
            return
        self._status_at = now
        try:
            import status as _status
            self._status_prev = _status.publish(
                st or {}, prev=self._status_prev,
                persona=self._persona_name(), paused=bool(paused))
        except Exception as e:                                   # noqa: BLE001
            self.log(f"[状态] 写 status.json 失败: {e}")

    def _status_beat_loop(self) -> None:
        """心跳线程: 每秒把"进程还活着"刷一下(数据不动)。

        ☠ 为什么需要它: 主循环**一次迭代可能跑十几秒**(导航到厨房另一头), 这期间
          一次状态都不写 —— 如果"没写"就等于"引擎挂了", 猫娘会在局中被反复告知
          "引擎没在跑", 而它明明正在跑。所以"进程活着"和"数据多旧"分开报:
          这个线程只推 `heartbeat_ts`, `ts`(数据时刻)只有 `_publish_status` 会动。
        """
        while True:
            time.sleep(1.0)
            try:
                import status as _status
                _status.beat()
            except Exception:                                    # noqa: BLE001
                pass

    def run(self, dry: bool = False):
        self.log("[引擎] 启动, 等对局...")
        self.log(f"[引擎] 杂活模式: {CHORE_MODE}"
                 f"(NEKO_CHORES=enroute|stuck|always|0; 交菜 serve_any 也归它管)")
        self.log("[引擎] 焦点策略: 不抢你的焦点 —— 切出去干活时脚本会自动暂停并松开所有键;")
        self.log("[引擎]           按 " + (os.environ.get("NEKO_PANIC_KEY") or "F12") +
                 " 可以急停(只读按键状态, 不影响你在游戏里的操作)")
        # 状态出口的心跳线程(见 `_status_beat_loop` 与 `neko/status.py`)。
        #   daemon: 主线程退它就跟着没, 不需要 join。
        try:
            import threading as _th
            _th.Thread(target=self._status_beat_loop, daemon=True,
                       name="neko-status-beat").start()
        except Exception as e:                                   # noqa: BLE001
            self.log(f"[状态] 心跳线程起不来(不影响主循环): {e}")
        _warned_unfocused = False
        _warned_tr = False
        _fail_sig, _fail_n = None, 0
        while True:
            # ---- 焦点/急停闸门 ----
            # SendInput 是系统级注入, 键会发给**当前前台窗口**。所以游戏不在前台时
            # 绝不能发键 —— 一是会打进别人家窗口, 二是用户根本没法用电脑。
            if panic_pressed():
                self.kb.release_all()
                self.log("[引擎] 急停键被按住 —— 松手即继续 (停止请按 Ctrl+C)")
                self._publish_status(paused=True)
                time.sleep(0.3)
                continue
            if not game_focused():
                self.kb.release_all()
                if not _warned_unfocused:
                    self.log("[引擎] 游戏不在前台 —— 已暂停并松开所有键, 切回游戏自动继续")
                    _warned_unfocused = True
                # **暂停时也要报状态**(心跳不能断): 否则外面会把"你切走了"读成
                # "引擎挂了" —— 那两件事该让猫娘说得不一样。
                self._publish_status(paused=True)
                time.sleep(0.4)
                continue
            if _warned_unfocused:
                self.log("[引擎] 游戏回到前台, 继续")
                _warned_unfocused = False

            # ---- 外部命令闸门(`neko/control.py`: mode / do / pause / resume / stop) ----
            self.apply_commands()
            if self._ctrl_stop:
                self.kb.release_all()
                self.log("[控制] 收工(Ctrl+C 之外的正常退出)")
                return
            if self._ctrl_paused:
                self.kb.release_all()
                time.sleep(0.4)
                continue

            st = self.state()
            if not st:
                time.sleep(1)
                continue
            # **状态出口**: 拿一次状态就报一次(幂等, 内部按 1 秒节流)。
            #   放在 `inRound` 判断**之前** —— 局中和"不在局里"两种都要报,
            #   否则对局结束后文件会永远停在"进行中"的假象上。
            self._publish_status(st)
            if not st.get("inRound"):
                if self.scene:
                    self.log("[引擎] 对局结束, 清空缓存")
                self.know, self.scene, self.assemble_spot = None, "", None
                self._assemble_sid = ""     # 换关卡/下一局 → 组装台面重新挑
                self._belt_dirs_cache, self._belt_speeds_cache = None, {}
                self._terrain, self._terrain_scene = None, ""
                # 队友位置记忆也要清 —— 否则下一局第一次决策会拿**上一局最后的位置**
                # 去算"他多久没动", 开局就先误判一次挂机。
                self._mate_track.clear()
                if self.mode_state is not None:
                    self.mode_state.recent.clear()   # 捣蛋鬼的"最近做过"同理, 别跨局
                time.sleep(1)
                continue

            # **关卡正在变形 → 停手等**(理由见 `world_transitioning`)。
            #   必须放在"灭火/规划/执行"**之前** —— 它们全都假设地图是稳的:
            #   变形期间地形正在改, 按着旧图走过去就是掉水里/踩进新生成的空洞。
            _tr = self.world_transitioning()
            if _tr:
                self.kb.release_all()
                if not _warned_tr:
                    self.log("[引擎] ⚠ 关卡正在变形(%s) —— 停手等它变完"
                             % ", ".join(sorted(set(t.get("type") or "?" for t in _tr))))
                    _warned_tr = True
                time.sleep(0.3)
                continue
            if _warned_tr:
                self.log("[引擎] 关卡变形结束, 继续")
                _warned_tr = False

            km = self.map(st)
            if km is None:
                time.sleep(0.5)
                continue
            if not self.ensure_knowledge(st):
                time.sleep(2)
                continue
            self._log_triggers(km)

            # **有火先灭火** —— 优先级高于做菜。理由(反编译 ServerFlammable.cs:214-224):
            #   着火时台面上的 `Interactable` / `PickupItemSpawner` / `Workstation`
            #   三个组件被 `enabled = false` ⇒ **那个台子既不能拿放也不能切**;
            #   而且火会按 m_fireSpreadRadius=1.5 扩散到相邻可燃物。
            #   等它烧开, 整关的台面会一片片失效 —— 那比晚做一道菜严重得多。
            # 节流 2 秒: 每次都要问桥要 dyn, 不必每帧。
            now = time.time()
            if now - self._last_fire_check >= 2.0:
                self._last_fire_check = now
                n = self.extinguish(km, st)
                if n:
                    continue          # 灭了火 → 这一轮重新规划(世界变了)

            planned = self.plan(st)
            if planned is None:
                time.sleep(0.5)
                continue
            name, left, flow = planned
            self.log(f"\n[引擎] 当前订单 {name} (剩 {left*100:.0f}%)")
            self.log(str(flow))

            if dry:
                self.log("[引擎] --dry: 只打印计划, 不执行")
                time.sleep(3)
                continue

            # 进入对局后确定键位: 优先按"厨师归属的玩家"(权威), 老 dll 才退到实测探测
            if not self._probed:
                self._probed = True
                if not self.bind_keys_by_player(km):
                    self.log("[键位] dll 未提供 player 字段, 改用实测探测")
                    self.probe_bindings()

            if self.execute(flow):
                self.log(f"[引擎] ★ 完成 {name}")
                _fail_sig, _fail_n = None, 0
            else:
                # **★ 主流程卡住 → 改做一件杂活**, 别把这一轮白烧掉(用户要求:
                #   "如果拿不到食材, 就检查能否做其他事")。
                #   做成了就**不算原地打转** —— 重规划继续(世界多半也变了)。
                #
                # ⚠ **阶段二之后这条路只在杂活不进池时才走**(`NEKO_CHORES=0`):
                #   杂活已经并入评分候选池(`_execute_scored`), 那里有闸门、有让位、
                #   有按距离排序。这里再跑一遍就是"绕开闸门、绕开让位、还要再做一次"
                #   的第二条路 —— 同一套判据维护两份, 迟早漂。
                _did = ""
                if CHORE_MODE in ("0", "off", "no", "false", "none"):
                    try:
                        _did = self._chores(km, st)
                    except Exception as _e:
                        self.log(f"[杂活] 出错: {_e!r}")
                if _did:
                    self.log(f"[引擎] 主流程卡住 → 改做杂活: {_did}")
                    _fail_sig, _fail_n = None, 0
                    time.sleep(0.3)
                    continue
                self.log(f"[引擎] 订单 {name} 未完成")
                # ---- 同一步反复同样失败 → **不再停机**, 把它放冷板凳去做别的 ----
                # 旧行为是"连续 3 次一样就 return"(`开发约定 规则5: 别烧整局`),
                # 但那会把整局停掉 —— **用户明确要求: "不允许退出，如果持续失败，就去做其他事"**
                # (2026-09-14)。所以改成:
                #   ① 记住指纹, 连续 3 次一样 ⇒ 把那一步 `bench_step` 掉(`STEP_COOLDOWN` 秒);
                #   ② 冷板凳上的步骤下一轮**不进候选**(`_execute_scored` 里过滤) ——
                #      引擎自然会去挑**别的步骤 / 别的菜 / 杂活**(杂活这时也会进池:
                #      菜谱候选一少, `_chore_admitted` 那扇"顺路"门就开了);
                #   ③ 到期自动回池(失败往往是暂时的: 东西被队友拿走了、路被 NPC 堵了)。
                # ⚠ 仍然打印 ⛔ 那一行 —— 它现在是**诊断信息**(出 bug 时要看的), 不再是退出口。
                sig = (name, getattr(self, "_last_fail_step", ""))
                if sig == _fail_sig:
                    _fail_n += 1
                else:
                    _fail_sig, _fail_n = sig, 1
                if _fail_n >= 3:
                    self.log("")
                    self.log("=" * 62)
                    self.log(f"[引擎] ⛔ 同一步连续失败 {_fail_n} 次: {name} / {sig[1]}")
                    self.log("[引擎]    **不停机**(用户要求): 这一步放冷板凳, 改去做别的。")
                    self.log("[引擎]    如果这行反复出现, 才是真有 bug —— 把第一次失败的日志发给开发者。")
                    self.log("=" * 62)
                    self.bench_step(str(sig[1]), f"{name} 连续 {_fail_n} 次")
                    _fail_sig, _fail_n = None, 0
                    time.sleep(0.5)
                    continue
            if self.board is not None:
                self.board.release_order(name, self.cid)
            time.sleep(0.5)
