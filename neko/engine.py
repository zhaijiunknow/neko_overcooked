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

防御设计: 对局结束即中止 / 异常路径必释放按键 / 卡住侧移脱困 / 每步闭环验证。
"""

from __future__ import annotations

import os
import time

from bridge.keyboard_input import KeyboardPlayer, PLAYER1, PLAYER2, ensure_focus, game_focused, panic_pressed
from map_model import (KitchenMap, Station, is_plate, is_pot, is_extinguisher,
                       teleport_edges, conveyor_edges)
#: 地面传送带的字符。**从 terrain 导进来而不是抄一份** ——
#: "判定和显示只能有一条规则", 抄一份迟早会漂(这一轮已经栽过好几次)。
from terrain import CH_TRAVELATOR

#: 选站位时给**传送带格**加的距离惩罚 —— 只是排到所有非传送带格之后,
#: 不是排除(旁边只有带子时还是得站上去)。取个远大于地图尺寸的数:
#: 格子距离是平方, 一张图最多几百格, 1e6 足够"跨类别"而不影响同类内部的远近排序。
BELT_STAND_PENALTY = 1e6
from pathing import dir_for_step
from cookbook import Knowledge, derive, Op, DishFlow

# 灶台语义(按食材要求的 CookingStationType 映射)
COOK_SEMS = ("hob", "oven", "fryer", "firepit", "barbeque", "floorburner", "flamethrower")


class Engine:
    def __init__(self, bridge, cid=0, bindings=None, log=print, board=None,
                 mode_state=None, world=None):
        self.bridge = bridge
        self.cid = cid
        self.kb = KeyboardPlayer(bindings or PLAYER1)
        self.log = log
        self.board = board       # 双人时的订单黑板(单人传 None)
        self.mode_state = mode_state   # 三模式的个体状态(neko/modes/); None=纯合作不捣蛋
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
        self._last_fire_check = 0.0                  # 上次查火的时间(节流见 run())
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
        于是把它当成"卡住", 一边按侧移一边把超时耗光。
        """
        return bool(self.chef(st).get("respawning"))

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

    def ensure_knowledge(self, st: dict) -> bool:
        """每场景拉一次食材知识表(切/煮/货源)。"""
        scene = st.get("scene", "")
        if self.know is not None and self.scene == scene:
            return True
        try:
            self.know = Knowledge.from_json(self.bridge.get_knowledge())
            self.scene = scene
            self.log(f"[引擎] 食材知识表已加载: {len(self.know.items)} 项 (场景 {scene})")
            return True
        except Exception as e:
            self.log(f"[引擎] 食材知识表读取失败: {e}")
            return False

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
        reach_t = None
        last_pos = None
        stuck = 0
        prev_dir = None       # 上一轮真正按下的主导轴方向, 用于识别"目标附近来回抖"
        flips = 0             # 目标附近方向翻转次数(受扰动/目标在动的证据)
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

                # 死亡重生中: 游戏接管角色, 按键无效。必须松手等, 不能当"卡住"处理 ——
                # 这是之前"按键探针明明能用、导航却一直卡住"的真凶之一。
                if self.is_respawning(st):
                    self.log("[导航] ⚠ 厨师正在死亡重生, 松手等游戏救回来")
                    if not self.wait_respawn():
                        return False
                    t0 = time.time()
                    last_pos = None
                    stuck = 0
                    continue

                # 掉水里 / 踩空: 这时按键完全无效(游戏接管了角色), 硬按只会白等超时。
                # 正确做法是松手等游戏把他捞回来(重生点), 然后重新开始计时。
                if tm is not None and tm.ok and tm.is_danger_world(x, z):
                    self.log("[导航] ⚠ 厨师在危险格上(掉水/坠落), 等游戏救回重生点")
                    if not self.wait_respawn(4.0):
                        # 位置还停在危险格但没进入重生态: 先自己往外挪一格再说
                        self.log("[导航] 没进重生态, 先脱离危险格")
                    t0 = time.time()
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
                    self.log("[导航] 卡住, 侧移脱困")
                    if abs(dx) >= abs(dz):
                        key = self._key("W" if dz > 0 else "S")
                    else:
                        key = self._key("D" if dx > 0 else "A")
                    key_down(key)
                    time.sleep(0.35)
                    key_up(key)
                    time.sleep(0.1)
                    stuck = 0
                    continue

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
                    driver.move(dx * inv, -dz * inv)
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
                hold = step_dist / self.speed
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

        d = dir_for_step(dx, dz, deadzone=0.02)
        if not d:
            return True
        key = self._key({"left": "A", "right": "D", "up": "W", "down": "S"}[d])
        key_down(key)
        time.sleep(hold)
        key_up(key)
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
        """游戏说的放置目标, 是我们想放的那个吗? 返回 (是否OK, 游戏说的目标名)。"""
        tgt = self.placement_target(self.state(force=True))
        if not tgt:
            return True, "(空)"        # 游戏没给目标 —— 判断不了, 交给交互本身去失败
        t = self._norm(tgt)
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
            return ("\n         直调那一趟: ok=%s method=%s target=%r pick=%r place=%r"
                    % (r.get("ok"), r.get("method"), r.get("target"),
                       r.get("pick"), r.get("place")))
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
    def pick_assemble_spot(self, km: KitchenMap, x: float, z: float) -> Station | None:
        """挑摆盘位。**优先挑已经有盘子的台面** —— 那样材料放上去就直接进盘,
        不必先跑去拿盘子。双人时通过黑板保证两人不用同一个。

        ⚠ **整局内粘住**(用户实测的 bug): 半成品就放在某个台面上, 换个台面等于从零重来。
          旧行为是"优先挑空台面"(`not s.on`) —— 于是重新规划时, 上一轮放了材料的台面
          因为 `on` 非空而**被排除**, 挑到一个空台面; 从空台面看"什么都没有", 就把
          同一份材料再取一遍。实测一局里同一份海带取了 **7 遍**, 材料还散落在多个台面上
          (用户原话: "菜谱三个食材 1、2 加过了缺少 3, 但是脚本还会拿 1 去补")。
        """
        allc = [s for s in (km.of("counter") or km.of("board"))
                if not s.spawn and s.kind != "CookingStation"]
        if not allc:
            return None
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
                if self._has_plate(prev) or not with_plate:
                    return prev
            else:
                self._assemble_sid = ""    # 台面没了(换关/被拆) → 重新挑

        if with_plate:
            cands = with_plate
        if self.board is not None:
            spot = self.board.pick_spot(cands, self.cid, (x, z))
        else:
            serve = km.nearest("serve", x, z)
            ax, az = (serve.x, serve.z) if serve else (x, z)
            spot = min(cands, key=lambda s: (s.x - ax) ** 2 + (s.z - az) ** 2)
        if spot is not None:
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

        返回 (世界x, 世界z) 或 None。
        """
        if tm is None or not tm.ok:
            return None
        # ⚠ **`st` / `km` 必须自己取**: 这里原来直接引用了 `st` 和 `km`, 而它们
        #   **既不是参数也不是局部变量** —— 那是必然的 `NameError`, 调用点没有一个
        #   try 兜着, 于是整个"走到台面旁边"的路径全瘫(实测: `_approach` 一进去就炸)。
        #   症状像 §5.2 记的那种"改调用处时误伤": `at_y=` / `extra_edges=` 是后加的,
        #   参数没跟着穿进来。自己取一帧最省事(`state()` 吃 TTL 缓存, 几乎不要钱)。
        st = self.state()
        km = self.map(st) if st else None
        i, j = tm.cell_of(tx, tz)
        reach = tm.reachable_from(cx, cz, at_y=self.chef_y(st),
                                  extra_edges=self._travel_edges(km, tm))
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
                if best_d is None or d < best_d:
                    best_d, best = d, (wx, wz)
        return best

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
        want = getattr(spot, "name", "") or ""
        if not want:
            return True                      # 不知道期望名字时只能放行(老 dll)
        for k in range(tries):
            st = self.state(force=True)
            if not st or not st.get("inRound"):
                return False
            ph = (self.chef(st) or {}).get("placeh") or ""
            if self._name_is(ph, want):
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

    def _approach(self, km: KitchenMap, tx: float, tz: float, attempt: int = 0,
                  tight: float = 0.8, want: str = "") -> bool:
        """接近一个台子并**转身面向它**。

        先站到"能站的相邻格", 再转身 —— 而不是朝台面本身推(那是障碍格)。
        attempt>0(上一次拿错了)时换个方位站: 相邻台子只隔 1.2 格, 游戏靠朝向决定
        交互哪一个, 换个方向最后一步的朝向就不同。
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
                    st4 = self.state(force=True)
                    pick, use = self.interaction_targets(st4)
                    self.log("[接近] ✗ 微调后游戏仍说作用不到目标(抓取=%r 工位=%r)" % (pick, use))

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
            for o in s.on:
                on = self._norm(o)
                if not on:
                    continue
                if on == tn:
                    exact.append(s)
                    break
                # 子串也要认: 订单说的容器是 "Plate", 场景里的实例却叫 "equipment_plate_01"
                if tn in on or on in tn:
                    loose.append(s)
                    break
        best = exact or loose
        if not best:
            return None
        if x is None:
            return best[0]
        return min(best, key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2)

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
            self.log(f"[步骤] 手上还有 {held}, 先放到组装台面")
            if not self.op_assemble(km, x, z, Op("assemble", held), st):
                return False
            _, _, held = self.pos(self.state() or {})
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
            _ok_src = None
            if _tm0 is not None and _tm0.ok and cx is not None:
                _r0 = _tm0.reachable_from(
                    cx, cz, at_y=self.chef_y(st),
                    extra_edges=self._travel_edges(km, _tm0))

                def _ok_src(s, _tm=_tm0, _r=_r0):
                    _c = _tm.cell_of(s.x, s.z)
                    for _di, _dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        _n = (_c[0] + _di, _c[1] + _dj)
                        if _tm.inside(*_n) and _tm.walkable(*_n) and _n in _r:
                            return True
                    return False

            live_src = km.find_source(op.target, cx or x, cz or z, ok=_ok_src)
            if live_src is None and _ok_src is not None:
                # 区分"这关没有这个箱子"和"有, 但都够不着" —— 后者是**地图/订单**的问题,
                # 不是导航的问题, 日志必须说清楚(否则下一个人会去查寻路)。
                _all = km.find_source(op.target, cx or x, cz or z)
                if _all is not None:
                    self.log(f"[步骤] ⚠ 有出 {op.target} 的箱子({_all.id} "
                             f"@{_all.x:.1f},{_all.z:.1f}), 但**这只厨师走不到它旁边** —— "
                             f"多半是两个厨房(要传接球), 不是寻路问题")
            if live_src is not None:
                tx, tz = live_src.x, live_src.z
                self.log(f"[步骤] 取 {op.target} @实时箱子 {live_src.id}({tx:.1f},{tz:.1f})")
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
        if not self._approach(km, tx, tz, attempt, want=want_name):
            return False
        if not self.interact("pickup", verify_hold_change=True):
            return False
        st2 = self.state()
        _, _, got = self.pos(st2)
        if not self._held_is(got, op.target):
            self.log(f"[步骤] 拿到的是 {got!r}, 不是 {op.target!r} → 放回去")
            self.interact("pickup", verify_hold_change=False)
            return False
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
        cell_len = abs(tm.cellx) or 1.2

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
            t_item = (k * cell_len) / belt_speed
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
                self.face(live.x, live.z)      # 朝它**当前**位置转身(只认前方 180°)
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
                    grabs += 1
                    if grabs >= 5:
                        return False
                    time.sleep(0.2)
                    continue
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
        spot = self.assemble_spot or self.pick_assemble_spot(km, x, z)
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
          `navigate()` 发的移动键全落到平台上; 卡住检测判"厨师卡住"→ 侧移脱困 → 更乱。
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
            侧移脱困反复几次, 把 `step_timeout` 白耗光
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

    def _dynamic_blocks(self, km, tm) -> set:
        """**会动的东西当前占住的格子** —— 路人 / 车辆 / 移动危险物。

        为什么要单独算(用户指出: "对于路人和车辆完全没有建模"):
          地形是**整局一次的静态快照**, 而车会开、路人会走。快照把它们冻在
          第一次扫到的位置 —— 于是"地图说安全的地方"可能是车当前的位置。
          **这比不知道更危险: 它让人放心地走进去。**
          所以每次规划都拿 movers 的**当前位置**重新禁一遍。
        """
        if km is None or tm is None or not getattr(tm, "ok", False):
            return set()
        try:
            return km.blocked_by_movers(tm)
        except Exception as e:
            self.log(f"[导航] 动态禁行格算失败: {e}")
            return set()

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
                if not self.navigate(px, pz, arrive=0.9, step_timeout=6.0):
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
            return ok
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

    def op_chop(self, km, x, z, op: Op, st: dict) -> bool:
        """在切菜板上把东西切到完成。

        刀数依据 ClientWorkableItem: HasFinished() = (m_progress == m_stages-1),
        每 chopsPerSlice 刀推进一片; 合作模式 2 人时 chopsPerSlice=1, 单人时=5。
          ⇒ 刀数 = (m_stages - 1) * chopsPerSlice
        完成判定优先看"板上的东西名字变了"(完成时 GameObject 会被 m_nextPrefab 替换),
        比字符串猜名字可靠 —— 生料名往往就包含成品名(CucumberWhole ⊃ Cucumber)。
        """
        board = km.nearest("board", x, z)
        if board is None:
            self.log("[步骤] 没有切菜板(Workstation)")
            return False
        _, _, held = self.pos(st)
        self.log(f"[步骤] 去 {board.id} 切 → {op.target}")
        if not self.navigate_smart(km, board.x, board.z, tight=0.8):
            return False
        self.face(board.x, board.z)
        if not self._align_for_place(board):     # 见 _align_for_place: 挨得近会判到旁边台子
            return False
        if held:
            self.interact("pickup", verify_hold_change=False)   # 先放上板
            time.sleep(0.25)

        n_players = len((st.get("layout") or {}).get("chefs") or [])
        per_slice = 1 if n_players >= 2 else 5     # GameConfig.SingleplayerChopTimeMultiplier
        stages = op.chop_stages or 0
        max_chops = max(1, stages - 1) * per_slice if stages else 10
        base = self._board_item(board.id)
        self.log(f"[步骤] 需切 {max_chops} 刀" + (f" (板上: {base})" if base else ""))

        done = False
        for i in range(max_chops + 3):
            if not self.round_active():
                return False
            self.kb.chop()
            time.sleep(0.35)
            cur = self._board_item(board.id)
            if base and cur and cur != base:
                self.log(f"[步骤] 切好了({i+1} 刀): {base} → {cur}")
                done = True
                break
            if not base and i + 1 >= max_chops:
                done = True   # 读不到板上的名字, 按刀数收工
                break
        if not done:
            # ClientWorkableItem 完成时会替换板上的物件。若名字仍未变化，说明
            # use 没有送到游戏；把生料拿回手里不能算切菜完成。
            self.log("[步骤] ✗ 切完但板上物品没有变化，拒绝把生料当成成品")
            return False
        return self.interact("pickup", verify_hold_change=True)

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
            return 0

        # ---- 1) 先弄到灭火器(拿着它自己也不会着火) ----
        _, _, held = self.pos(st)
        if not is_extinguisher(held or ""):
            owner, pos = self._find_extinguisher(km)
            if pos is None:
                self.log(f"[灭火] ⚠ 场上有 {len(fires)} 处着火, 但**找不到灭火器**")
                return 0
            if owner is not None and owner != self.cid:
                self.log(f"[灭火] ⚠ 灭火器在队友(P{owner + 1})手上, 拿不到")
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
        t0 = _t.time()
        while _t.time() - t0 < budget:
            fires = self.fires()
            if not fires:
                break
            cx, cz, _ = self.pos(self.state() or {})
            if cx is None:
                break
            # 从**最近**那处开始灭(走得少, 也最快止住扩散)
            fx, fz = min(fires, key=lambda f: (f[0] - cx) ** 2 + (f[1] - cz) ** 2)
            # 站在火的**正前方 1~2 格**(射程 4, 但锥角窄 —— 近了容错更大)
            if not self.navigate_smart(km, fx, fz, tight=1.4):
                self.log(f"[灭火] ✗ 走不到火 ({fx:.1f},{fz:.1f}) 旁边")
                break
            self.face(fx, fz)                      # 那 15° 锥要求必须正对
            self.log(f"[灭火] 对 ({fx:.1f},{fz:.1f}) 喷 0.9s (需 0.5s 灭一个满强度的火)")
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
                self.log(f"[灭火] ✓ 灭了一处(还剩 {left})")
            else:
                self.log("[灭火] ⚠ 喷了但火没灭 —— 多半是没正对(15° 锥很窄)")
                break

        # ---- 3) 手上有灭火器的话保持拿着(它能防火), 不主动放下 ----
        return done

    # ---------------- 锅 ----------------
    def _pick_stove(self, km: KitchenMap, x: float, z: float, want_pot: bool):
        """挑一个灶台。want_pot=True 只挑"灶上已经有锅"的; False 只挑灶上没有锅的。

        为什么要分开(用户实测要求: "不要把锅拿走"):
          · 米饭这类"容器煮"的菜只能进**锅**; 手拿米饭对空灶台按交互是放不上去的
            (CookableContainer.AllowItemPlacement 要求物体带 CookableProperties 且
             允许该加热方式, 米饭和灶台的档案对不上) —— 表现就是"按了没反应"。
          · 反过来, 自带 CookingHandler 的食材直接放灶台, 塞进锅反而被拒。
        占用判据必须用 Cooking.busy: 锅架在灶上时**空锅也一直有 CookingHandler**(进度 0),
        旧代码用 `cooking_on(s) is not None` 判占用, 会把所有带锅的灶台都当成"正在煮",
        一个都用不了。
        """
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
                if self.board is not None and not self.board.claim_stove(s.id, self.cid):
                    continue
                return s, pot, ck
        return None, "", None

    def _find_pot_with(self, km: KitchenMap, x: float, z: float, target: str):
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
                if self.board is not None and not self.board.claim_stove(s.id, self.cid):
                    continue
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
            if s.empty_plate_names():
                cand.append((1, (s.x - x) ** 2 + (s.z - z) ** 2, s))
        if not cand:
            return None
        cand.sort(key=lambda r: (r[0], r[1]))
        return cand[0][2]

    def _get_plate_for_pot(self, km: KitchenMap, x: float, z: float, want_type: str) -> bool:
        """准备一个"去锅里取菜"用的盘子。

        顺序: 手上已有盘子 → 组装台面那个已经装菜的盘子(首选) → 台面上的空盘。

        为什么首选组装台面那盘: 海带这类前面已经摆进盘里的食材, 必须和煮好的米饭
        进**同一个**盘子。拿一个空盘去锅里取, 要么另成一盘拼不起来, 要么交互直接
        不生效(实测: 手拿空盘对锅按交互, 饭没出来)。
        """
        _, _, held = self.pos(self.state() or {})
        if held:
            if self._is_plate(held):
                self.log(f"[步骤] 手上已经端着盘子 {held!r}, 直接用它去锅里取")
                return True
            self.log(f"[步骤] ⚠ 手上有 {held!r} 不是盘子, 没法去锅里取菜")
            return False
        spot = self.assemble_spot or self.pick_assemble_spot(km, x, z)
        src = None
        used_assemble = False
        if spot is not None and self._has_plate(spot):
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
        _, _, held = self.pos(st)
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
            stove, pot, _ = self._pick_stove(km, x, z, want_pot)
        if stove is None and want_pot:
            self.log("[步骤] ⚠ 没有『灶上放着锅』的灶台, 退回直接放灶台(可能放不上去)")
            want_pot = False
            stove, pot, _ = self._pick_stove(km, x, z, False)
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
            if not self._get_plate_for_pot(km, x, z, plate_type):
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
            self.log(f"[步骤] 手拿盘子对 {stove.id} 按交互取菜(第 {k+1} 次, "
                     f"游戏放置目标={placeh!r}) —— 锅不动")
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

    def _has_plate(self, s: Station) -> bool:
        """台面上有没有盘子。优先用游戏自己的 Unity Tag(Plate), 名字只做兜底。"""
        for i, o in enumerate(s.on or []):
            if is_plate(o, s.tag_of(i)):
                return True
        return any("plate" in self._norm(o) for o in (s.on or []))

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
        """摆盘位上一个盘子都没有时, 才真去拿一个放上来(正常情况台面上本来就有)。"""
        src = self._find_item_station(km, "Plate", x, z)
        if src is None:
            spots = km.of("plates")
            if spots:
                src = min(spots, key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2)
        if src is None:
            self.log("[步骤] 全场找不到盘子")
            return False
        self.log(f"[步骤] 摆盘位 {spot.id} 没盘子, 去 {src.id} 拿一个")
        if not self.navigate_smart(km, src.x, src.z, tight=0.8):
            return False
        if not self.interact("pickup", verify_hold_change=True):
            return False
        if not self.navigate_smart(km, spot.x, spot.z, tight=0.6):
            return False
        return self.interact("pickup", verify_hold_change=True)

    def op_press(self, km, st) -> bool:
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

    def op_wash(self, km, st, budget: float = 20.0) -> bool:
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

    def op_assemble(self, km, x, z, op: Op, st: dict) -> bool:
        """把手上的材料放到摆盘位 —— 台面上有盘子时, 这一步本身就是"摆盘"。

        特殊情况(用锅煮的菜): 上一步"从锅里取菜"结束时手上**已经端着那个盘子**了:
          · 不能再"给摆盘位补一个盘子"(_ensure_plate 会把手上这盘菜放到别处去);
          · 直接放到摆盘位 —— 那儿本来就有盘子时, 手上这盘会并进它。
        """
        _, _, held = self.pos(st)
        if not held:
            self.log("[步骤] 组装: 手上空, 跳过")
            return True
        holding_plate = self._is_plate(held)
        spot = self.assemble_spot
        if spot is None:
            spot = self.pick_assemble_spot(km, x, z)
            if spot is None:
                self.log("[步骤] 找不到摆盘位")
                return False
            self.assemble_spot = spot
        if holding_plate:
            self.log(f"[步骤] 手上端着盘子 {held!r} —— 直接放到 {spot.id}(不再另取盘子)")
        elif not self._has_plate(spot):
            # ⚠ 这里**不能**去补盘子: 手上正拿着材料, 对着盘子堆按交互只会把材料
            #   放到盘子堆上(实测踩过)。补盘子在 execute() 开头做 —— 那时手是空的。
            self.log(f"[步骤] ⚠ 摆盘位 {spot.id} 上没有盘子, 材料只能先干放在台面上")
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
            # (PlacementContainer + IngredientToContainerBehaviour)。只看"手空了"
            # 会漏掉"海带其实没进盘"这种情况 —— 后面煮饭去拿这盘会变成空盘。
            st0 = self.state(force=True)
            placeh = (self.chef(st0) or {}).get("placeh") or ""
            self.log(f"[步骤] 放置目标游戏报={placeh!r} (期望 {spot.name!r} 或它的盘子)")
            before = self._plate_contents_on(spot)
            if not self.interact("pickup", verify_hold_change=False):
                return False
            time.sleep(0.4)
            st2 = self.state(force=True)
            km2 = self.map(st2) if st2 else None
            if km2 is not None:
                spot2 = km2.stations.get(spot.id) or spot
                after = self._plate_contents_on(spot2)
                if after != before:
                    self.log(f"[步骤] ✓ {held} 进了 {spot.id} 那盘"
                             f"({sorted(before)} → {sorted(after)})")
                    return True
                self.log(f"[步骤] ✗ {held} 没进盘({sorted(before)} 没变), "
                         f"盘子里是 {sorted(after)}")
                return False
        return self.interact("pickup", verify_hold_change=True)

    def op_deliver(self, km, x, z, op: Op, st: dict) -> bool:
        """端起容器送到送餐口。"""
        _, _, held = self.pos(st)
        # 手上还端着盘子(多半是"并进台面那盘"之后剩下的空盘): 先放下腾出手,
        # 否则会端着空盘去送餐口 —— 空盘送不出东西(ServerPlateStation 判空)。
        if held and self._is_plate(held) and self.assemble_spot is not None:
            if self._put_down_plate(km, x, z):
                st = self.state()
                _, _, held = self.pos(st)
        if not held and self.assemble_spot is not None:
            self.log(f"[步骤] 去组装台面 {self.assemble_spot.id} 端容器")
            if not self.navigate_smart(km, self.assemble_spot.x, self.assemble_spot.z, tight=0.6):
                return False
            if not self.interact("pickup", verify_hold_change=True):
                return False
            st = self.state()
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
        if not self.interact("pickup", verify_hold_change=False):
            return False
        deadline = time.time() + 2.5
        while time.time() < deadline:
            time.sleep(0.15)
            remaining = sum(1 for order in self.live_orders() if order.get("name") == op.target)
            if remaining < before:
                self.log(f"[步骤] ✓ 已交付 {op.target}")
                return True
        self.log(f"[步骤] ✗ 送餐后订单 {op.target!r} 仍在，判为交付失败")
        return False

    # ---------------- 执行一个订单 ----------------
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
        spot = km.stations.get(self.assemble_spot.id)   # 用新鲜的, 别用陈旧快照
        if spot is None or self._has_plate(spot):
            return
        self.log(f"[步骤] 摆盘位 {spot.id} 还缺盘子 —— 趁手空补一个")
        self._ensure_plate(km, *self.pos(st)[:2], spot)

    def _prepare_plate(self, flow: DishFlow) -> bool:
        """开局(手还空着)先把摆盘位那个盘子备好。

        为什么必须放在这里: 摆盘位的"盘子"是**材料自动进盘**的前提
        (PlacementContainer + IngredientToContainerBehaviour.TransferToContainer),
        台面上没盘子的时候, 材料只会干放在台面上, 后面怎么拼都拼不出菜。
        而"去拿盘子"必须**手是空的**才做得了 —— 手上拿着材料去盘子堆按交互,
        只会把材料放到盘子堆上(实测踩过)。
        拿不到不算致命: 大多数关卡台面上本来就摆着盘子, 这里只是兜底。
        """
        if not flow.plate:
            return True
        st = self.state()
        km = self.map(st) if st else None
        if km is None:
            return False
        _, _, held = self.pos(st)
        if held:
            return True                      # 手上有东西, 这一步做不了, 交给后面的流程
        spot = self.pick_assemble_spot(km, *self.pos(st)[:2])
        if spot is None:
            return False
        self.assemble_spot = spot
        if self._has_plate(spot):
            return True
        self.log(f"[步骤] 开局: 摆盘位 {spot.id} 上没有盘子, 先补一个")
        return self._ensure_plate(km, *self.pos(st)[:2], spot)

    def do_op(self, km: KitchenMap, st: dict, op: Op, flow: DishFlow,
              attempt: int = 0) -> bool:
        if op.optional:
            self.log(f"[步骤] {op.action} {op.target} 是可选材料, 跳过")
            return True
        x, z, _ = self.pos(st)
        if x is None:
            return False
        if op.action == "fetch":
            return self.op_fetch(km, x, z, op, st, attempt)
        if op.action == "plate":
            # 容器这步"尽力而为": 拿不到不该把整条流程卡死 ——
            # 后面的取料/切/煮照样要跑, 不然连问题出在哪都看不出来。
            if not self.op_take_plate(km, x, z, op, flow):
                self.log("[步骤] ⚠ 取容器没成, 先跳过(继续取料/切/煮)")
                self.assemble_spot = self.assemble_spot or self.pick_assemble_spot(km, x, z)
            return True
        if op.action == "chop":
            return self.op_chop(km, x, z, op, st)
        if op.action == "cook":
            return self.op_cook(km, x, z, op, st, flow)
        if op.action == "assemble":
            return self.op_assemble(km, x, z, op, st)
        if op.action == "deliver":
            return self.op_deliver(km, x, z, op, st)
        # tool / mix 暂不处理
        return True

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
        for op in ops:
            group.append(op)
            if op.action != "assemble":
                continue
            mat = self._norm(op.target)
            if mat and mat in have:
                self.log(f"[步骤] 台面 {spot.id} 上已有 {op.target} —— "
                         f"跳过这 {len(group)} 步({group[0].action} {group[0].target} 起)")
            else:
                out.extend(group)
            group = []
        out.extend(group)          # 尾部不以 assemble 收尾的(例如 deliver)
        return out

    def execute(self, flow: DishFlow, retries: int = 2) -> bool:
        # ⚠ 这里**不能**清空 assemble_spot —— 见 pick_assemble_spot() 里那段注释。
        #   清空的后果: 重新规划时挑到一个空台面 → 从那个台面的视角"什么都没有"
        #   → 同一份材料被反复取(实测一局取了 7 遍)。台面只在换关卡/对局结束时清(run())。
        self._prepare_plate(flow)
        ops = self._skip_already_on_spot(flow.ops)
        total = len(ops)
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
        """
        if not name or self.know is None:
            return False
        for it in getattr(self.know, "items", []) or []:
            if getattr(it, "name", "") == name:
                return bool(getattr(it, "next", ""))
        return False

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
            return name, float(o.get("t", 1.0)), derive(detail, self.know)
        return None

    # ---------------- 主循环 ----------------
    def run(self, dry: bool = False):
        self.log("[引擎] 启动, 等对局...")
        self.log("[引擎] 焦点策略: 不抢你的焦点 —— 切出去干活时脚本会自动暂停并松开所有键;")
        self.log("[引擎]           按 " + (os.environ.get("NEKO_PANIC_KEY") or "F12") +
                 " 可以急停(只读按键状态, 不影响你在游戏里的操作)")
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
                time.sleep(0.3)
                continue
            if not game_focused():
                self.kb.release_all()
                if not _warned_unfocused:
                    self.log("[引擎] 游戏不在前台 —— 已暂停并松开所有键, 切回游戏自动继续")
                    _warned_unfocused = True
                time.sleep(0.4)
                continue
            if _warned_unfocused:
                self.log("[引擎] 游戏回到前台, 继续")
                _warned_unfocused = False

            st = self.state()
            if not st:
                time.sleep(1)
                continue
            if not st.get("inRound"):
                if self.scene:
                    self.log("[引擎] 对局结束, 清空缓存")
                self.know, self.scene, self.assemble_spot = None, "", None
                self._assemble_sid = ""     # 换关卡/下一局 → 组装台面重新挑
                self._belt_dirs_cache, self._belt_speeds_cache = None, {}
                self._terrain, self._terrain_scene = None, ""
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
                _did = ""
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
                # ---- 同一步反复同样失败 → 立刻停下报错, 别把整局烧光 ----
                # 实测: 一个 bug(Station.sem)让引擎把 150 秒整局都耗在
                # "重试 3 次 → 放弃 → 重新规划同一单 → 再来一遍" 上, 白白烧完一局。
                # 现在记住"订单+失败在哪一步"的指纹, 连续 3 次一样就停。
                sig = (name, getattr(self, "_last_fail_step", ""))
                if sig == _fail_sig:
                    _fail_n += 1
                else:
                    _fail_sig, _fail_n = sig, 1
                if _fail_n >= 3:
                    self.log("")
                    self.log("=" * 62)
                    self.log(f"[引擎] ⛔ 同一步连续失败 {_fail_n} 次: {name} / {sig[1]}")
                    self.log("[引擎]    这多半是代码 bug 而不是运气问题, 已停止以免烧完整局。")
                    self.log("[引擎]    把上面第一次失败的日志发给开发者。")
                    self.log("=" * 62)
                    return
            if self.board is not None:
                self.board.release_order(name, self.cid)
            time.sleep(0.5)
