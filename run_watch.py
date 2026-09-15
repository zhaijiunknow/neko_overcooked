# -*- coding: utf-8 -*-
"""**看护**: 轮询游戏状态 → 缺 P2 就补 → 进对局拉起 `run_engine.py` → 对局结束收掉。

用法(开着不用管):
    py run_watch.py

它替你做的三件事, 以及为什么必须按这个顺序:
  · **加入只能在大厅做** —— 对局里才有 `PlayerControls`, 而装虚拟手柄要求场景里已经有它
    (`VirtualInput.cs:226`)。所以"补 P2"这个动作的地点被锁死在大厅。
  · **`run_engine.py` 没有 `Player.Two` 会 `return 1` 直接退出**(`run_engine.py:68-76`)
    ⇒ 必须等确知有 P2 再启动它。
  · **收尾必须让引擎自己跑完** —— 它 `finally` 里有 `pad.uninstall()`;
    不执行的话那只厨师的输入**持续被虚拟手柄接管**, 而且进程退出后**要等游戏完全重启才干净**
    (`VirtualInput.cs:656-696`)。⇒ 收尾走 `CTRL_BREAK_EVENT`(等价 Ctrl+C), **绝不 `kill()`**。

生命周期是**一局一连**: 进对局才起进程, 对局结束就收掉, 下一局再起一个新的。

⚠ 桥是**长连接**: 建一条用到最后, **不写重连包装器** ——
  `tools/pathtest.py:20-22` 与 `README.md:78` 都明写: 桥的 `AcceptLoop`
  (`BridgeServer.cs:50-69`)一出异常就 `break`, 之后整个会话不再接新连接。

环境变量:
  `NEKO_WATCH_INTERVAL`  轮询间隔秒(默认 2.5)
  `NEKO_WATCH_MISSES`    "对局结束"要连续几次读不到 `inRound` 才算(默认 3, 防抖)
  `NEKO_WATCH_HEARTBEAT` 无变化时的心跳间隔秒(默认 30; `0` = 不打)
  `NEKO_WATCH_PY`        子进程用的解释器(默认 `sys.executable`)
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from bridge.client import BridgeClient, BridgeError           # noqa: E402
from bridge.virtual_pad import join_player, lobby_users       # noqa: E402

INTERVAL = float(os.environ.get("NEKO_WATCH_INTERVAL") or 2.5)
MISSES = int(os.environ.get("NEKO_WATCH_MISSES") or 3)
HEARTBEAT = float(os.environ.get("NEKO_WATCH_HEARTBEAT") or 30.0)
#: 收掉子进程时等它自己退多久(秒)。**超时不 kill** —— 见模块 docstring。
STOP_TIMEOUT = float(os.environ.get("NEKO_WATCH_STOP_TIMEOUT") or 30.0)

#: `subprocess.CREATE_NEW_PROCESS_GROUP` —— 让子进程**不**接收控制台的 Ctrl+C。
#: 这样 Ctrl+C 只到看护, 由看护**转发** `CTRL_BREAK_EVENT` 给子进程,
#: 保证收尾走的永远是子进程**自己**那段清理(而不是被硬杀)。
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _player_of(chef) -> str:
    return str((chef or {}).get("player") or "").strip().lower()


def has_player_two(st: dict) -> bool:
    """这一局里有没有 `Player.Two` 那只厨师 —— 权威判据是 `layout.chefs[].player`
    (来自游戏的 `PlayerIDProvider.GetID()`, 见 `chef_of_player` 的注释)。"""
    for c in ((st.get("layout") or {}).get("chefs") or []):
        if _player_of(c) == "two":
            return True
    return False


class Watcher:
    """状态机。**I/O 全部从外面注入** —— 于是 `tick()` 是纯逻辑, 能离线喂序列跑。

    `get_state` / `start_engine` / `stop_engine` / `join_lobby` 都是可调用对象;
    探针换成桩即可。`log` 同理。
    """

    def __init__(self, get_state, start_engine, stop_engine, join_lobby,
                 log=print, misses: int = MISSES, heartbeat: float = HEARTBEAT):
        self.get_state = get_state
        self.start_engine = start_engine
        self.stop_engine = stop_engine
        self.join_lobby = join_lobby
        self.log = log
        self.misses = max(1, int(misses))
        self.heartbeat = heartbeat

        self.in_round = False       # 防抖之后的"在不在局里"
        self._miss = 0              # 连续读到"不在局里"的次数
        self._pressed = False       # **这一趟大厅**里补过 P2 没有
        self._no_p2_logged = False  # "这局没有 P2"只打一次
        self._started = False       # 这一局启动过引擎没有
        self._last_beat = time.time()

    # ---------------- 单步 ----------------
    def tick(self, st: dict) -> None:
        """喂一份状态, 推进一格。**纯逻辑, 不碰 I/O**(除了注入的那几个回调)。"""
        raw_round = bool((st or {}).get("inRound"))
        if raw_round:
            self._miss = 0
            if not self.in_round:
                self.in_round = True
                self._pressed = False          # 进局 ⇒ 下一趟大厅可以再补
                self._no_p2_logged = False
                self._started = False
                self.log("[看护] ▶ 进入对局")
        else:
            self._miss += 1
            if self.in_round and self._miss >= self.misses:
                self.in_round = False
                self.log(f"[看护] ◀ 离开对局(连续 {self._miss} 次读不到 inRound)")
                self.stop_engine("对局结束")
                # ⚠ **这一轮到此为止** —— 否则会接着落到下面的大厅分支, 又调一次
                #   `stop_engine("在大厅")`: 同一个 tick 里收两遍。收第二遍是空操作
                #   (子进程已经没了), 但日志会出现两条不同理由的"收掉引擎", 看着像 bug。
                #   大厅那一摊(补 P2)下一轮再做, 差一个轮询间隔, 无所谓。
                return
            if self.in_round:
                return          # 防抖窗口内 —— 还当在局里, 什么都别做

        if self.in_round:
            if has_player_two(st):
                if not self._started:
                    self._started = True
                    self.log("[看护] ✓ 这局有 Player.Two")
                    self.start_engine()
            else:
                if not self._no_p2_logged:
                    self._no_p2_logged = True
                    self.log("[看护] ⚠ 这局没有 Player.Two —— **加入只能在大厅做**, "
                             "等这局结束回大厅再补")
                # 这局没 P2 ⇒ 引擎起来也会立刻 `return 1`; 先收掉, 别让它白起一遍
                self.stop_engine("这局没有 Player.Two")
            return

        # ---- 真的在大厅/主界面 ----
        self.stop_engine("在大厅")               # 幂等: 没在跑就什么都不做
        if not self._pressed:
            self._pressed = True
            self.join_lobby(st, lobby_users_of(st))

    # ---------------- 真循环 ----------------
    def run(self) -> None:
        while True:
            try:
                st = self.get_state() or {}
            except BridgeError as e:
                # ⚠ **不重连** —— 见模块 docstring。读不到就是读不到。
                self.log(f"[看护] ✗ 读状态失败: {e}")
                self.log("[看护]   桥可能断了。**不做重连**(桥的 accept 循环一出异常就永久退出), "
                         "请重启游戏/插件后再跑。")
                return
            self.tick(st)
            self._beat(st)
            time.sleep(INTERVAL)

    def _beat(self, st) -> None:
        """长时间没变化时打一行心跳 —— 否则"没输出"会被读成"挂了"。
        (照 `tools/mapview.py` 的 30 秒心跳惯例。)"""
        if self.heartbeat <= 0:
            return
        now = time.time()
        if now - self._last_beat < self.heartbeat:
            return
        self._last_beat = now
        where = "对局中" if self.in_round else "大厅/主界面"
        self.log(f"[看护] · 心跳: {where}, 场景={st.get('scene')!r}, "
                 f"玩家={_users_txt(lobby_users_of(st))}")


def lobby_users_of(st: dict):
    """从**已经拿到的那份 state** 里取玩家名单(不再发一次请求)。
    `None` = 读不到(旧 dll / 插件报 null) —— 和"0 人"是两回事。"""
    if "users" not in (st or {}):
        return None
    u = st.get("users")
    if u is None or not isinstance(u, list):
        return None
    return u


def _users_txt(users) -> str:
    if users is None:
        return "读不到"
    if not users:
        return "0 人"
    return "%d 人:%s" % (len(users), ",".join(u.get("slot") or "?" for u in users))


# ---------------------------------------------------------------- 真 I/O
class ChildEngine:
    """`run_engine.py` 子进程 —— **一局一个**, 收尾走 `CTRL_BREAK_EVENT`。"""

    def __init__(self, log=print):
        self.log = log
        self.p = None
        self._stop_at = None

    def running(self) -> bool:
        return self.p is not None and self.p.poll() is None

    def start(self) -> None:
        if self.p is not None:
            # 上一只还赖着没退(收尾超时) —— **不并起第二个**, 否则两只抢同一个厨师
            self.log(f"[看护] ⚠ 上一只引擎还没退(pid={self.p.pid}) —— 先不起新的")
            return
        py = os.environ.get("NEKO_WATCH_PY") or sys.executable
        argv = [py, "-u", os.path.join(_ROOT, "run_engine.py")]
        try:
            self.p = subprocess.Popen(
                argv, cwd=_ROOT, creationflags=CREATE_NEW_PROCESS_GROUP)
        except Exception as e:                                     # noqa: BLE001
            self.log(f"[看护] ✗ 起引擎失败: {e!r}")
            self.p = None
            return
        self.log(f"[看护] ▶ 起引擎 pid={self.p.pid} ({os.path.basename(py)}) —— "
                 f"下面开始是它的日志")

    def stop(self, why: str) -> None:
        if self.p is None:
            return
        if self.p.poll() is not None:
            self.log(f"[看护] 引擎已退出(码={self.p.returncode}), {why}")
            self.p = None
            self._stop_at = None
            return
        if self._stop_at is None:
            self._stop_at = time.time()
            self.log(f"[看护] ⏹ 收掉引擎({why}) —— 发 CTRL_BREAK(等价 Ctrl+C), "
                     f"等它自己 uninstall 完")
            try:
                os.kill(self.p.pid, signal.CTRL_BREAK_EVENT)
            except Exception as e:                                 # noqa: BLE001
                self.log(f"[看护] ⚠ 发 CTRL_BREAK 失败: {e!r}(会继续等它自己退)")
        if time.time() - self._stop_at > STOP_TIMEOUT:
            self.log(f"[看护] ⚠ 等了 {STOP_TIMEOUT:.0f}s 引擎还没退(pid={self.p.pid}) —— "
                     f"**不 kill**(kill 会把虚拟手柄留在游戏里, 那只厨师就废了直到重启游戏)。"
                     f"要么它自己缓过来, 要么你手动关掉它。")
            self._stop_at = time.time()      # 重新计时, 别刷屏


def main() -> int:
    log = lambda m: print(m, flush=True)                           # noqa: E731
    log("[看护] 启动 —— 轮询游戏状态, 缺 P2 就补, 进对局拉起 run_engine.py")
    log(f"[看护] 间隔 {INTERVAL}s / 对局结束要连续 {MISSES} 次读不到 / "
        f"心跳 {HEARTBEAT:.0f}s;   Ctrl+C 收工")
    b = BridgeClient()
    log(f"[看护] 连桥... (retries=999, **不做重连**)")
    try:
        b.connect(retries=999, interval=2.0)
    except BridgeError as e:
        log(f"[看护] ✗ 连不上桥: {e}")
        return 1
    log("[看护] ✓ 桥已连上(游戏没开时这里会一直等)")

    eng = ChildEngine(log=log)

    def get_state():
        return b.get_state() or {}

    def start_engine():
        eng.start()

    def stop_engine(why):
        eng.stop(why)

    def join_lobby(st, users):
        if users is None:
            log("[看护] ⚠ 大厅里读不到玩家名单(插件是旧的?) —— **不按 A**。"
                "按 A 是「加入下一个玩家」, 猜错会引进第三个人。")
            log("[看护]   要修: 重编重装 build\\Overcooked2AI.dll, 并**完全退出游戏再开**")
            return
        log(f"[看护] 大厅玩家: {_users_txt(users)} —— 检查是否需要补 P2")
        ok = join_player(b, log=log)
        log("[看护] " + ("补 P2 这一步完成(跳过或已按, 见上面那行日志)"
                         if ok else "⚠ 补 P2 没做成 —— 见上面原因; 这一趟大厅不再重试"))

    w = Watcher(get_state=get_state, start_engine=start_engine,
                stop_engine=stop_engine, join_lobby=join_lobby, log=log)
    try:
        w.run()
    except KeyboardInterrupt:
        log("\n[看护] Ctrl+C —— 先收子进程(等它 uninstall), 再退出")
    finally:
        eng.stop("看护退出")
        # 最后再等一小会儿, 让子进程把日志打完
        t0 = time.time()
        while eng.p is not None and eng.p.poll() is None and time.time() - t0 < STOP_TIMEOUT:
            time.sleep(0.2)
        try:
            b.close()
        except Exception:                                          # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
