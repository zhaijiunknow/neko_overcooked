"""双人协同: 订单黑板。

两个厨师是**两个独立的个体**, 各自跑自己的引擎循环(P1 用 WASD, P2 用方向键),
但共享一块黑板来避免互相踩踏:

  · 一张订单只由一个厨师认领 —— 否则两人做同一道菜, 材料翻倍、盘子打架
  · 每个厨师占一个自己的组装台面 —— 否则材料会混进同一个容器
  · 每个厨师占一个灶台 —— 否则两人去抢同一个锅

黑板只在内存里, 谁先 claim 谁得。认领会随订单完成/失败释放。
"""

from __future__ import annotations

import threading


class OrderBoard:
    def __init__(self):
        self._lock = threading.Lock()
        self._orders = {}      # 订单名 -> cid (认领者)
        self._spots = {}       # cid -> 组装台面 id
        self._stoves = {}      # 灶台 id -> cid
        self.log = lambda *a: None

    # ---- 订单 ----
    def claim_order(self, name: str, cid: int) -> bool:
        with self._lock:
            owner = self._orders.get(name)
            if owner is None or owner == cid:
                self._orders[name] = cid
                return True
            return False

    def release_order(self, name: str, cid: int) -> None:
        with self._lock:
            if self._orders.get(name) == cid:
                del self._orders[name]

    def release_all(self, cid: int) -> None:
        with self._lock:
            for k in [k for k, v in self._orders.items() if v == cid]:
                del self._orders[k]
            self._spots.pop(cid, None)
            for k in [k for k, v in self._stoves.items() if v == cid]:
                del self._stoves[k]

    def orders_of(self, cid: int) -> list:
        with self._lock:
            return [k for k, v in self._orders.items() if v == cid]

    # ---- 台子 ----
    def claim_stove(self, stove_id: str, cid: int) -> bool:
        with self._lock:
            owner = self._stoves.get(stove_id)
            if owner is None or owner == cid:
                self._stoves[stove_id] = cid
                return True
            return False

    def stove_owner(self, stove_id: str):
        """**只读**: 这个灶台归谁(没人占 → None)。

        给评分层用 —— 它要替队友也算一遍"这个灶台能不能用", 那种调用**不能顺手占位**
        (否则拿我的 cid 把一个队友本来能用的灶台抢走)。
        """
        with self._lock:
            return self._stoves.get(stove_id)

    def release_stove(self, stove_id: str, cid: int) -> None:
        with self._lock:
            if self._stoves.get(stove_id) == cid:
                del self._stoves[stove_id]

    def pick_spot(self, candidates: list, cid: int, pos: tuple):
        """给厨师挑一个组装台面(尽量不与他人重复), 并记下归属。"""
        if not candidates:
            return None
        with self._lock:
            mine = self._spots.get(cid)
            if mine:
                for s in candidates:
                    if s.id == mine:
                        return s
                self._spots.pop(cid, None)
            used = {v for k, v in self._spots.items() if k != cid}
            free = [s for s in candidates if s.id not in used] or candidates
            best = min(free, key=lambda s: (s.x - pos[0]) ** 2 + (s.z - pos[1]) ** 2)
            self._spots[cid] = best.id
            return best
