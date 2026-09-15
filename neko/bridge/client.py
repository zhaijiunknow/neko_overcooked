"""桥 client: 连游戏内 C# TCP server, 拉状态/发动作。行协议: 每行一个 JSON。"""

from __future__ import annotations

import json
import socket
import time


class BridgeError(Exception):
    pass


class BridgeClient:
    """与 C# 薄桥的 TCP 连接。断线自动重连。"""

    def __init__(self, host="127.0.0.1", port=48778, timeout=3.0, log=print):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.log = log
        self._sock: socket.socket | None = None
        self._file = None

    # ---- 连接 ----
    def connect(self, retries=999, interval=2.0) -> bool:
        """阻塞重试直到连上(游戏没开也能等)。返回 True。"""
        n = 0
        while True:
            try:
                self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                self._sock.settimeout(self.timeout)
                self._file = self._sock.makefile("rw", encoding="utf-8", newline="\n")
                self.log("[桥] 已连接 C# 薄桥")
                return True
            except OSError:
                n += 1
                if retries is not None and n >= retries:
                    raise BridgeError("连不上桥")
                time.sleep(interval)

    def close(self):
        try:
            if self._file:
                self._file.close()
            if self._sock:
                self._sock.close()
        finally:
            self._file = None
            self._sock = None

    def reconnect(self):
        self.close()
        self.connect(retries=None)

    # ---- 协议 ----
    def _send(self, payload: dict) -> dict:
        if self._sock is None:
            raise BridgeError("未连接")
        line = json.dumps(payload, ensure_ascii=False)
        try:
            self._sock.sendall((line + "\n").encode("utf-8"))
            resp = self._file.readline()
            if not resp:
                raise BridgeError("桥返回空(可能掉线)")
            return json.loads(resp)
        except (OSError, ValueError) as exc:
            self.log(f"[桥] 通信失败: {exc}")
            raise BridgeError(str(exc)) from exc

    def ping(self) -> bool:
        try:
            return bool(self._send({"cmd": "ping"}).get("pong"))
        except BridgeError:
            return False

    def get_state(self) -> dict:
        return self._send({"cmd": "state"})

    def get_raw(self) -> dict:
        """全量物体组件清单(带 Collider 的物体 + 其游戏自定义组件 + 台面物品)。"""
        return self._send({"cmd": "raw"})

    def get_live_orders(self) -> dict:
        """当前挂在订单栏上的订单(含剩余时间比例 t)。"""
        return self._send({"cmd": "live"})

    def get_knowledge(self) -> dict:
        """食材知识表: 每个食材/箱子/厨具的加工方式(切/煮/出什么)。"""
        return self._send({"cmd": "know"})

    def get_path(self, tx: float, tz: float, chef: int = 0) -> dict:
        """问**游戏自己**的寻路网格(GridNavSpace): 边界/橱柜/墙壁全都算障碍。

        注意: 它**不认得水面和空洞**(水是 RespawnCollider 触发器, 不占格子),
        所以它给出的路径可能横穿水面, 用之前必须拿 terrain 过滤一遍。
        """
        return self._send({"cmd": "path", "chef": chef, "tx": tx, "tz": tz})

    def get_map(self, force: bool = False, max_age: float = None,
                foot: bool = False) -> dict:
        """整张关卡网格 + 危险区 + 空洞 + 平台。见 neko/terrain.py 的 TerrainMap。

        `max_age` = **最多接受多旧的数据**(秒)。C# 侧默认缓存 5 秒 —— 对人看地图
        够用, 但**寻路前**太旧: 限时平台升降这类变化, 5 秒足够厨师走出 20 格,
        拿着旧图规划就是往海里走。传小的值(如 1.0)会强制它更新鲜。

        `foot=True` —— 额外算 **交互足迹**: 每个台面"站在哪些格子上、面朝它,
        游戏说能作用到"。返回里多一个 `foot` 数组
        (`[{"iid":N,"n":"名字","cells":[n,…]}]`, `n = j*w + i`, 和 `grid` 同索引)。

        ⚠ **为什么这个能替代 fork 游戏状态**: 判据 `InteractWithItemHelper.IsColliderInArc`
          只依赖 (厨师位置, 朝向, 碰撞体几何) —— 是**纯几何**的, 所以能对**还没站上去**
          的格子问。完整理由见 `Overcooked2AI/Game/LevelInfo.cs:Footprint` 的注释。
        ⚠ 它比建图本身还贵(每台面 × 每能站格 × 每碰撞体一次判定), 所以 C# 侧
          **只在 arg 里有 `foot` 时才跑**, 而且**不走缓存也不写缓存**。
        """
        arg = "force" if force else ""
        if max_age is not None and not force:
            arg = ("%s maxage=%s" % (arg, float(max_age))).strip()
        if foot:
            arg = ("%s foot" % arg).strip()
        return self._send({"cmd": "map", "arg": arg})

    def get_dyn(self) -> dict:
        """关卡里的机关/陷阱: 按钮 / 传送带方向 / 触发机器 / 平台 / 正在烧的东西 / 关卡变形。"""
        return self._send({"cmd": "dyn"})

    def get_movers(self) -> dict:
        """**会动的东西**: 路人 / 车辆 / 移动危险物。

        地形图是整局一次的静态快照, 看不见它们 —— 而车会开、路人会走,
        所以"地图标的安全格"可能是过期的。每条带 `moved` 字段(和上一帧比位置变没变),
        那才是"它是不是在动"的通用判据。见 MoverScan 的注释。
        """
        return self._send({"cmd": "movers"})

    def get_spray(self) -> dict:
        """灭火器诊断: 喷雾的**触发字符串**(写在 prefab 里, 反编译源码和 bundle 都读不到)
        + 它的组件清单(找 Interactable)。见 InteractiveScan.SprayDiag()。"""
        return self._send({"cmd": "sprayinfo"})

    def get_grid(self) -> dict:
        """**游戏自己的网格**: 格子↔世界坐标换算参数(m_origin/m_size/transform) + 占位表。
        依据 GridManager.cs / QuadGridManager.cs —— 权威, 不是我们采样推断的。"""
        return self._send({"cmd": "grid"})

    def direct(self, action: str, player: int = 0) -> dict:
        """**直接调游戏自己的交互入口**(绕开输入层/消息链)。
        action: pickup/place/take/interact/trigger/throw"""
        return self._send({"cmd": "direct", "player": player, "action": action})

    def get_cells(self) -> dict:
        """对齐游戏格心的完整格子图(kind/conv/occ 三张 RLE 位图 + 交叉验证)。"""
        return self._send({"cmd": "cells"})

    def pad(self, action: str, **kw) -> dict:
        """虚拟手柄。

        action:
          install / uninstall —— 换掉/还原某个厨师的输入(插件侧走主线程)
          drive              —— 喂值: player/x/y/pickup/use/dash/curse(**不走主线程, 延迟最低**)
          release            —— 松手(轴归零+全键弹起)
          status             —— 当前虚拟手柄状态 + 应用遥测(focused/runInBackground/timeScale/menu)
        """
        payload = {"cmd": "pad", "action": action}
        payload.update(kw)
        return self._send(payload)

    def vpad(self, pad: int, **state) -> dict:
        """**旧机制**: 喂一个"虚拟 InControl 手柄"的状态(整份覆盖, 不是增量)。

        和 `pad("drive", ...)` 那套(替换 `ControlSchemeData`)是**两回事**:
          · `pad("drive")` 那套**故意绕开加入流程** —— 它只对**已经在对局里**的厨师生效
          · `vpad` 走的是 `VirtualGamepads`(真的 InControl 设备), 会触发
            `PCPadInputProvider.OnDeviceAttached` —— **这是"按 A 加入"唯一能走的路**
            (见 `VirtualInput.cs:99-103` 的注释: 那条被称作"死路", 但加入只能靠它)

        ⚠ **`pad` 这个键必须排在 `cmd` 前面** —— 桥的 `GetStr`(`BridgeServer.cs:313`)
          用的是 `IndexOf("\\"pad\\"")` 找**第一处**, 而 `{"cmd":"pad", ...}` 里
          第一处 `"pad"` 是 **cmd 的值**不是键, 于是它读到 `"cmd":"pad"` 后面那个 `:`
          再往后解析 → 失败 → 返回 -1 → 报 `pad index`。
          **实测: 键顺序换一下就从报错变 `{'ok': True}`。**
          正经修法是在 C# 的 `GetStr` 里匹配 `"key":` 而不是 `"key"`, 那要重编 DLL;
          这里先用顺序绕开。
        """
        # dict 保序: pad 在 cmd 之前 => IndexOf 先撞到真正的键
        payload = {"pad": int(pad), "cmd": "pad"}
        payload.update(state)
        return self._send(payload)

    def send_action(self, chef: int, kind: str, target: str = "", duration: float = 0.0) -> dict:
        return self._send({"cmd": "action", "chef": chef, "kind": kind,
                           "target": target, "duration": duration})
