"""虚拟手柄驱动: 把"按键语义"翻译成游戏内部的模拟量 + 按键, 经桥下发。

为什么这么做(而不是去造一个假手柄设备):
  游戏真正消费输入的那一层是 `PlayerControls.ControlSchemeData`
  (`PlayerControls.cs:65-95`), 里面
      m_moveX / m_moveY   : ILogicalValue —— **float 模拟量**
      m_pickupButton / m_worksurfaceUseButton / m_dashButton / m_curseButton : ILogicalButton
  消费点在 `ClientPlayerControlsImpl_Default.Update_Movement`
  (`:398-403` → `PlayerControlsHelper.BuildControlAxisData`, 每帧现读 `ControlScheme`,
   `PlayerControlsHelper.cs:45-50`) ⇒ **运行时替换当帧生效**。
  插件侧 `VirtualInput` 把 6 个字段换成自己的实现, 于是:
    · 不走 `GateLogicalValue`(失焦时它返回 0f, `GateLogicalValue.cs:13-20`)
    · `VirtualButton.CanProcessInput()` 覆写为 true(基类失焦会 Claim 掉事件, `LogicalButtonBase.cs:74-96`)
    · 和窗口在不在前台**完全无关** ⇒ 游戏放后台也能继续做菜

这一层为什么要"按键语义 → 模拟量"的翻译:
  引擎和工具里有几十处 key_down("W") / tap("LSHIFT")。装箱成同一个接口后,
  换输入层不需要改任何调用点; 而且**同时按住两个方向键就得到真正的斜向**(模拟量),
  不再是"先横着走一段再竖着走一段"的折线。
"""

from __future__ import annotations

import threading
import time

#: 方向键名 → 要发给游戏的轴值。
#:
#: ⚠ **Y 是取负的, 这不是笔误。** 依据 `PlayerControlsHelper.GetControlAxis`
#:   (PlayerControlsHelper.cs:64-71):
#:       float x = ±MoveX;
#:       float z = ±(0f - MoveY);        ← Y 被取负
#:   即"轴Y = +1"对应世界"z = -1"。我们要的是**世界方向**(引擎的 nx/nz 就是世界坐标),
#:   所以"往世界 +z 走"必须发 y = -1。
#:   这条踩过: 一开始发 y=+1, 结果"目标是 z=6.0 而厨师从 4.8 跑到 2.2" ——
#:   方向整体反了, 表现为"卡在边界只会左右移动", 日志里只看得出'卡住/超时'。
#:   (XAxisAllignment/YAxisAllignment 还是每关可覆盖的 [SerializeField], 所以引擎侧
#:    另有一次运行时标定 calibrate_axes(), 兜住关卡级的反转。)
DIR_ROLES = {"up": (0.0, -1.0), "down": (0.0, 1.0), "left": (-1.0, 0.0), "right": (1.0, 0.0)}
#: 动作键名 → 桥上的按键位名
BTN_ROLES = {"pickup": "pickup", "chop": "use", "dash": "dash"}


class VirtualPadError(Exception):
    pass


class VirtualPad:
    """一个厨师的虚拟手柄。

    用法:
        pad = VirtualPad(bridge, chef=0, log=print)
        pad.install()                 # 换掉那个厨师的输入(需要在主线程执行, 插件自己会处理)
        keyboard_input.set_driver(pad)
        ...引擎照常跑 key_down/tap...
        pad.uninstall()               # 还原成游戏原生输入
    """

    def __init__(self, bridge, chef: int = 0, bindings: dict | None = None, log=print):
        from bridge import keyboard_input as ki

        self.br = bridge
        self.chef = chef
        self.log = log
        # 物理键名 → 角色。默认把两套键位都收进来: 引擎会按"厨师归属的玩家"改绑
        # (P1=WASD/LSHIFT..., P2=方向键/RSHIFT...), 只认一套的话另一套会静默失效。
        self.role_of: dict[str, str] = {}
        sets = [bindings] if bindings else [ki.PLAYER1, ki.PLAYER2]
        for b in sets:
            for role, key in (b or {}).items():
                if key:
                    self.role_of[str(key).upper()] = role

        self.player = -1          # 安装后由游戏告诉我们(PlayerIDProvider.GetID)
        self.installed = False
        self.sent = 0
        self.dropped = 0          # 未知键(没映射到角色)的次数
        self.direct_calls = 0     # 直调游戏交互入口的次数(见 tap)
        self.direct_hits = 0      # 其中接上目标的次数
        self.direct_miss = 0      # 调通了但游戏没给交互对象(人没站到位)
        self.direct_fails = 0
        self._direct_unsupported: set[str] = set()
        self.last_direct: dict = {}
        self._dirs: set[str] = set()
        self._btns = {"pickup": False, "use": False, "dash": False, "curse": False}
        self._axis_override: tuple[float, float] | None = None
        self._lock = threading.Lock()

    # ---------------- 安装 / 卸载 ----------------
    def install(self, player: int | None = None) -> bool:
        """安装。

        player 给了就按**玩家身份**装(0=Player.One, 1=Player.Two) —— 双人首选:
        它不依赖 FindObjectsOfType 的枚举顺序, 所以"哪个厨师是 P1"不会有歧义。
        没给就按 chef 序号装(单人调试够用)。
        """
        if player is None:
            r = self.br.pad("install", chef=self.chef)
        else:
            r = self.br.pad("installplayer", player=player)
        if not r.get("ok"):
            self.log(f"[虚拟手柄] ✗ 安装失败: {r.get('error')}")
            return False
        self.player = int(r.get("player", -1))
        self.installed = True
        extra = ""
        if r.get("changedRunInBackground"):
            extra = " (顺手把 Application.runInBackground 打开了 —— 后台运行的前提)"
        who = f"Player={self.player}" if player is None else f"指定 Player={player} → 实际 {self.player}"
        self.log(f"[虚拟手柄] ✓ 已接管厨师#{self.chef}({who})的输入{extra}")
        if r.get("runInBackground") is False:
            self.log("[虚拟手柄] ⚠ runInBackground=false: 窗口一失焦 Unity 主循环会停, 后台做菜会失效")
        return True

    def uninstall(self) -> bool:
        r = self.br.pad("uninstall", chef=self.chef)
        self.installed = False
        self.log(f"[虚拟手柄] 已还原为游戏原生输入: {r.get('note') or r.get('error')}")
        return bool(r.get("ok"))

    def status(self) -> dict:
        return self.br.pad("status")

    # ---------------- 驱动接口(与 keyboard_input 的按键语义一致) ----------------
    def key_down(self, name: str):
        role = self.role_of.get(str(name or "").upper())
        if role is None:
            self.dropped += 1
            return
        with self._lock:
            self._axis_override = None
            if role in DIR_ROLES:
                self._dirs.add(role)
            elif role in BTN_ROLES:
                self._btns[BTN_ROLES[role]] = True
            else:
                return
            self._flush()
    def key_up(self, name: str):
        role = self.role_of.get(str(name or "").upper())
        if role is None:
            return
        with self._lock:
            if role in DIR_ROLES:
                self._dirs.discard(role)
            elif role in BTN_ROLES:
                self._btns[BTN_ROLES[role]] = False
            else:
                return
            self._flush()

    def tap(self, name: str, duration: float = 0.08):
        """短按。语义与 keyboard_input.tap 一致(按下→保持→松开)。

        顺序刻意是 **先直调、后按键**(先生要的"hook 函数方案, 键盘注入兜底"):

        1. 直调游戏自己的交互入口。移动早就没问题, 卡的一直是**拾取**;
           六轮实测把输入侧的怀疑全排除了 —— `pickupIsDownCalls=16111` 说明游戏每帧
           在读我们的键、`clientOurs/serverOurs=True`、`rebinds=0`、`paused` 全 false,
           而前台真手柄是好的 ⇒ 卡在**客户端消息链**(Update_Carry → ChefEventMessage
           → 服务端路由)。服务端真正执行取件的那一句是 public 且无门
           (`ServerPlayerControlsImpl_Default.cs:152-163`), 直接调它即可绕开整条链,
           目标由**游戏自己**算好(`CurrentInteractionObjects.m_TheOriginalHandlePickup`)。
        2. 直调没成功(没目标/抛错)才按虚拟键兜底。

        为什么不"按键 + 直调"都做: 万一原生链某天生效了, 同一次 tap 就会做两次动作
        (取了 A 又把手里的放下去), 表现为"按一下人不动/东西来回跳"。先直调就杜绝了它。
        """
        role = self.role_of.get(str(name or "").upper())
        # "使用"键(切菜/互动)在游戏里是**两条**消息 —— Interact(开始互动) + TriggerInteract
        # (真正干一下), 插件侧的 `use` 动作一次把两条都发出去。少发一条就是"按了没反应"。
        # 冲刺不走直调: 它是 SendServerEvent(Dash), 没有对应的 Receive 入口。
        act = {"pickup": "pickup", "chop": "use"}.get(role or "")
        if act and self.call_direct(act):
            return
        self.key_down(name)
        time.sleep(duration)
        self.key_up(name)

    def call_direct(self, act: str) -> bool:
        """直调游戏自己的交互入口。成功(插件 ok 且真有目标)返回 True。

        返回 False 表示"这次没接上"(身边没东西/没安装/报错) —— 调用方按兜底处理,
        不当作错误刷屏: 大多数 tap 本来就发生在空手对着空处的时候。
        """
        if not self.installed:
            return False
        if act in self._direct_unsupported:
            return False
        try:
            r = self.br.direct(act, player=self.player)
        except Exception as e:
            self.direct_fails += 1
            self.log(f"[虚拟手柄] ⚠ 直调 {act} 异常: {e}")
            return False
        self.direct_calls += 1
        self.last_direct = r
        if not r.get("ok"):
            self.direct_fails += 1
            error = str(r.get("error") or "")
            if "未知动作" in error or "unknown action" in error.lower():
                self._direct_unsupported.add(act)
                self.log(
                    f"[虚拟手柄] ✗ 游戏里的 DLL 不支持 direct/{act}；"
                    "请把当前 build/Overcooked2AI.dll 覆盖到 BepInEx/plugins 后完全重启游戏"
                )
            else:
                self.log(f"[虚拟手柄] ⚠ 直调 {act} 失败: {error}")
            return False
        # target 为 "(null)" = 游戏此刻没给出交互对象(人没站到位), 不算成功, 走兜底
        tgt = str(r.get("target") or "").strip()
        if not tgt or tgt in ("(null)", "None"):
            self.direct_miss += 1
            return False
        self.direct_hits += 1
        if self.direct_hits == 1 or self.direct_hits % 25 == 0:
            self.log(f"[虚拟手柄] ✓ 直调 {act} 命中 {tgt} ({r.get('method')}, 第 {self.direct_hits} 次)")
        return True

    def move(self, x: float, y: float):
        """直接设模拟量(给"连续导航"用 —— 真 360°, 不走按键量化)。"""
        with self._lock:
            self._axis_override = (float(x), float(y))
            self._flush()

    def release_all(self):
        with self._lock:
            self._dirs.clear()
            self._axis_override = None
            for k in self._btns:
                self._btns[k] = False
            self._flush()

    # ---------------- 内部 ----------------
    def _axis(self) -> tuple[float, float]:
        if self._axis_override is not None:
            return self._axis_override
        ax = ay = 0.0
        for role in self._dirs:
            dx, dy = DIR_ROLES[role]
            ax += dx
            ay += dy
        # 两轴各自夹到 -1..1; 斜向时游戏自己会归一化(GetControlAxis), 不需要我们算长度
        return (max(-1.0, min(1.0, ax)), max(-1.0, min(1.0, ay)))

    def _flush(self):
        if not self.installed or self.player < 0:
            return
        ax, ay = self._axis()
        try:
            self.br.pad("drive", player=self.player, x=ax, y=ay,
                        pickup=self._btns["pickup"], use=self._btns["use"],
                        dash=self._btns["dash"], curse=self._btns["curse"])
            self.sent += 1
        except Exception as e:
            self.dropped += 1
            self.log(f"[虚拟手柄] 下发失败: {e}")


#: 游戏里 Player 枚举的字符串写法 → 索引
PLAYER_INDEX = {"one": 0, "two": 1, "three": 2, "four": 3}


def resolve_chef_player(bridge, chef: int, log=print) -> int | None:
    """问游戏: 这个厨师是几号玩家?

    为什么要问: 插件按 Player 身份安装虚拟手柄, 而 bridge 的 chef id 是
    `FindObjectsOfType(PlayerControls)` 的枚举序号 —— 两者不保证一致。
    游戏状态里每个厨师都带 `player`(来自 `PlayerIDProvider.GetID()`),
    用它把 chef id 翻译成玩家号, 就不存在"装错人"的可能。
    """
    try:
        st = bridge.get_state()
    except Exception as e:
        log(f"[虚拟手柄] 读状态失败: {e}")
        return None
    for c in ((st or {}).get("layout") or {}).get("chefs") or []:
        if int(c.get("id", -1)) != chef:
            continue
        key = str(c.get("player") or "").strip().lower().replace("player.", "")
        if key in PLAYER_INDEX:
            return PLAYER_INDEX[key]
        log(f"[虚拟手柄] ⚠ 厨师#{chef} 的 player 字段读不出来({c.get('player')!r})")
        return None
    log(f"[虚拟手柄] ⚠ 状态里没有厨师#{chef}(还没进对局?)")
    return None


def wait_for_round(bridge, log=print, timeout: float = 300.0) -> bool:
    """等进对局。虚拟手柄必须在对局里装 —— 那时场景里才有 PlayerControls。"""
    import time as _t

    t0 = _t.time()
    told = False
    while _t.time() - t0 < timeout:
        try:
            st = bridge.get_state()
        except Exception:
            _t.sleep(1.0)
            continue
        if st and st.get("inRound"):
            return True
        if not told:
            log("[虚拟手柄] 等进对局(需要场景里有厨师才能接管)...")
            told = True
        _t.sleep(1.0)
    return False


def attach_virtual_input(bridge, chef: int = 0, player: int | None = None,
                         bindings: dict | None = None, log=print,
                         wait_round: bool = True):
    """一把装好: 安装虚拟手柄 + 把输入驱动切成它 + 修好引擎里"按值导入"的那两个名字。

    最后那一步是必须的: `engine.py` 顶部写的是
        from bridge.keyboard_input import ..., ensure_focus, game_focused, ...
    —— 这是**值拷贝**, 之后我们改 keyboard_input 模块里的定义, 引擎那份引用不会跟着变。
    (tests/test_flow_sim.py 出于同样原因也得手动补这两行。)

    player 没给时, 自动按 chef id 去问游戏(见 resolve_chef_player)。
    """
    from bridge import keyboard_input as ki

    if wait_round and not wait_for_round(bridge, log=log):
        log("[虚拟手柄] ⚠ 等不到对局, 放弃安装")
        return None
    if player is None:
        player = resolve_chef_player(bridge, chef, log=log)
    pad = VirtualPad(bridge, chef=chef, bindings=bindings, log=log)
    if not pad.install(player=player):
        return None
    ki.set_driver(pad)
    try:
        import engine as eng_mod
        eng_mod.ensure_focus = ki.ensure_focus
        eng_mod.game_focused = ki.game_focused
    except Exception:
        pass
    return pad


def install_all_pads(bridge, log=print) -> dict:
    """一次给**场上所有厨师**装上虚拟手柄(双人一次到位)。

    注意: 它只负责"把输入接管掉", 不负责"谁喂哪一路" ——
    喂值由每个线程自己的 VirtualPad 做(驱动是按线程隔离的)。
    返回 {"ok":bool, "pads":[{"chef":i,"player":p,"ok":bool}, ...]}
    """
    r = bridge.pad("installall")
    if r.get("ok"):
        for p in r.get("pads") or []:
            log(f"[虚拟手柄] 已接管 厨师#{p.get('chef')} → Player={p.get('player')}")
    else:
        log(f"[虚拟手柄] ✗ 一次安装失败: {r.get('error')}")
    return r


def chef_of_player(bridge, player: str = "Two", log=print):
    """**按玩家身份找厨师** —— 返回 chef id, 找不到返回 None。

    为什么要这个而不是"序号"(`--cid 1`): **厨师序号 ≠ 玩家身份**。
      · 双人局: 厨师#0=One / 厨师#1=Two —— 序号恰好对上
      · **单人局**: Overcooked 单人**也是两只厨师**, 但**都归 Player.One**
        (你自己切换着玩) ⇒ `--cid 1` 拿到的是**玩家的厨师**, 脚本会当场抢人。
        实测日志就是这么暴露的:
          `[虚拟手柄] ✓ 已接管厨师#1(指定 Player=0 → 实际 0)` +
          `[键位] 厨师#1 属于 One → 用 P1 键位`

    所以"驱动 P2"要按**身份**找, 找不到就该停 —— 而不是默默地开走玩家的厨师。
    """
    try:
        st = bridge.get_state() or {}
    except Exception as e:
        log(f"[玩家] 读状态失败: {e}")
        return None
    chefs = ((st.get("layout") or {}).get("chefs") or [])
    if not chefs:
        log("[玩家] 状态里没有厨师(还没进对局?)")
        return None
    want = (player or "").strip().lower()
    hit = [int(c.get("id", -1)) for c in chefs
           if str(c.get("player") or "").strip().lower() == want]
    if not hit:
        who = ", ".join("厨师#%s=%s" % (c.get("id"), c.get("player")) for c in chefs)
        log(f"[玩家] 这局**没有** Player.{player} —— 现在是: {who}")
        log(f"[玩家]   ({len(chefs)} 只厨师但都属于同一个玩家 = 单人局, 你自己在玩)")
        return None
    return hit[0]


def join_player(bridge, pad: int = 1, hold: float = 0.6, tries: int = 3,
                log=print) -> bool:
    """在**主界面**让第二个玩家加入(按虚拟手柄的 A)。

    为什么非要有这一步 —— 这是个**鸡生蛋**, 只能走这条"旧路":
      · `pad("installplayer")` 那套(真正消费输入的那个)要求场景里**已经有
        `PlayerControls`** ⇒ 得先在对局里才能装(`VirtualInput.cs:226` 那句
        "场景里没有 PlayerControls(还没进对局?)")
      · 而要"对局里有第二只厨师", P2 得先在主界面加入
      · 而 `VirtualInput` **故意绕开加入流程**(`VirtualInput.cs:99-103`: "不碰设备枚举、
        不碰加入流程") —— 它替换的是 `ControlSchemeData`, 压根不参与设备接入
      ⇒ **只有 `VirtualGamepads`(真的 InControl 设备)能触发
        `PCPadInputProvider.OnDeviceAttached`, 也就是"有手柄接上了"那个事件。**

    ⚠ **能否加入，本函数只能确认"命令被接受"** —— 大厅里有几个人在
      `StartScreen` 是**读不到**的(要进对局才有 `PlayerControls`)。
      真正加入没有要靠眼睛看, 或者进对局后数 `chefs`。

    `pad`: 用第几个虚拟设备(0/1)。哪个对应"第二玩家"取决于游戏怎么分配槽位,
           默认 1; 不行就试 0。
    `hold`: 按住 A 的时长 —— 设备接上到被枚举可能要几帧, 太短会漏。
    """
    ok_any = False
    for k in range(tries):
        # ⚠ 整份覆盖: 这个接口是**状态写**不是增量, 少写一个字段就等于把它清零。
        #   所以 `connected=1` 每次都要带上, 否则设备会被"拔掉"。
        r1 = bridge.vpad(pad, connected=1, A=1)
        time.sleep(hold)
        r2 = bridge.vpad(pad, connected=1, A=0)
        ok = bool(r1.get("ok") and r2.get("ok"))
        ok_any = ok_any or ok
        log(f"[加入] 第 {k + 1}/{tries} 次按 A (pad={pad}, 按住 {hold}s) -> "
            f"{'命令已接受' if ok else '失败: ' + str(r1.get('error') or r2.get('error'))}")
        if ok:
            time.sleep(0.4)
    if ok_any:
        log(f"[加入] pad={pad} 的 A 已按过 {tries} 次 —— "
            f"**去看一眼大厅里出没出第二个玩家**(这边读不到)")
    return ok_any
