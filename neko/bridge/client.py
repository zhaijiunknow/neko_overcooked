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

    def get_map(self, force: bool = False) -> dict:
        """整张关卡网格 + 危险区 + 空洞 + 平台。见 neko/terrain.py 的 TerrainMap。"""
        return self._send({"cmd": "map", "arg": "force" if force else ""})

    def get_dyn(self) -> dict:
        """关卡里的机关/陷阱: 按钮 / 传送带方向 / 触发机器 / 平台 / 正在烧的东西 / 关卡变形。"""
        return self._send({"cmd": "dyn"})

    def get_spray(self) -> dict:
        """灭火器诊断: 喷雾的**触发字符串**(写在 prefab 里, 反编译源码和 bundle 都读不到)
        + 它的组件清单(找 Interactable)。见 InteractiveScan.SprayDiag()。"""
        return self._send({"cmd": "spray"})

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

    def send_action(self, chef: int, kind: str, target: str = "", duration: float = 0.0) -> dict:
        return self._send({"cmd": "action", "chef": chef, "kind": kind,
                           "target": target, "duration": duration})
