"""共享世界: **一张地图 + 两个厨师的实时位置**, 双人时两个引擎共用同一份。

为什么必须共享(用户定的架构, 也是双人方案的正确分层):
  · 移动层是**每人一份** —— 每个厨师一个虚拟手柄, 各控各的。
  · 世界层是**全局一份** —— 地图、危险区、台面、两个厨师的位置, 只有一份真相。
  分开之前每个 Engine 各自缓存一份地形(各拉一次 `map`), 于是:
    ① 同一关拉两遍, 白费一次 8 秒超时的重活;
    ② 两份缓存刷新时刻不同 → 两个厨师看到的"同一张地图"可能不一样
       (动态关卡尤其致命);
    ③ 谁都不知道另一个人在哪、要去哪 → 无法避让, 两个人会互相堵门/互推。

360° 自由寻路的前提就在这里: **先有"我"和"他"的实时坐标 + 一张共同的可走图**,
才谈得上"规划两条不打架的连续轨迹"。

线程纪律: 双人时两个引擎跑在两个线程里, 本类所有公开方法都加锁。
它用**自己的一条专用连接**(只读 state/map), 不和厨师各自的驱动连接混在一起 ——
那样每个厨师按一次键都要和地图读取抢同一把 socket 锁, 延迟会互相拖累。
"""

from __future__ import annotations

import os
import threading
import time


class World:
    """共享的只读世界视图 + 一张"占位预约表"。"""

    def __init__(self, bridge, log=print, state_ttl: float = None):
        self.br = bridge
        self.log = log
        #: 状态快照的保质期(秒)。**默认 0 —— 每次读都要最新的一帧。**
        #:
        #: ☠ **2026-09-15 从 0.10 改成 0**(用户定的规矩, 原话):
        #:   "我们的地图更新是**和雷达一样**的机制, 使用要求脚本**每次都使用最新的地图**,
        #:    本身地图就小, 占用无关紧要。"
        #:   原来那个 0.1 秒服务的是"整帧一致性 + 省桥流量"(见类文档), 那是给**规划**用的;
        #:   可它同时喂给了**判据** —— 台面上还有没有那个盘子、我手上是不是已经空了,
        #:   于是判据读到的最多可以是 0.1 秒前的世界。0.1 秒在这个游戏里是**半个动作**
        #:   (一次交互认定 0.35 秒、按一下切菜 0.35 秒) ⇒ 陈旧读数直接变成
        #:   "对着空台子按放置""东西明明在手上却报了空"。
        #:   ⚠ 桥读本身不贵(插件主线程**每帧刷新的字符串**, 读一次就一个本地 TCP 往返),
        #:     真正贵的 map/raw 另有缓存。`NEKO_STATE_TTL` 可以调回去(双人省流量时)。
        self.state_ttl = (float(os.environ.get("NEKO_STATE_TTL") or 0.0)
                          if state_ttl is None else float(state_ttl))
        self._lock = threading.RLock()
        self._st: dict | None = None
        self._st_t = 0.0
        self._km = None
        self._km_t = 0.0
        self._tm = None
        self._tm_scene = ""
        self._tm_ver = ""       # 上面那份地形的版本号(C# 算的), 见 terrain()
        self._tm_at = 0.0       # 上面那份是什么时候取的
        #: 地形保质期(秒)。**这是"跳海"的保险丝** —— 理由见 terrain() 的注释:
        #: 限时平台升降只改高度不改字符, 整局不刷新就会拿着旧图走进海里。
        #: ⚠ **2026-09-14 从 1.5 压到 0.5**(用户要求"提高地图更新的频率"):
        #:   实测强制扫一次 **32ms**(41x24 关, 逐格 2 条射线) ⇒ 0.5 秒的占空比 6%。
        #:   而 1.5 秒 = 厨师走出 **6 格**, 拿 6 格前的图规划就是往海里走。
        #:   ⚠ 和 `Engine.terrain_ttl` **必须一致** —— 双人时两个引擎走的是
        #:     这一条(`Engine.terrain()` 有 world 就直接 return `world.terrain()`)。
        #:   `NEKO_TERRAIN_TTL` 可覆盖(大关卡耗时 ∝ 格子数)。
        self.terrain_ttl = float(os.environ.get("NEKO_TERRAIN_TTL") or 0.5)
        self._resv: dict[tuple, tuple] = {}     # cell -> (cid, 过期时刻)
        self.state_fetches = 0
        self.state_cache_hits = 0
        self.map_fetches = 0
        self.tm_rebuilds = 0

    # ---------------- 状态快照 ----------------
    def state(self, force: bool = False) -> dict | None:
        """整局状态(含**所有**厨师的位置/手持物)。TTL 内直接复用。

        双人时两个引擎都调它, 于是同一次读取被两个人共用 ——
        既省一半桥流量, 又保证两个人看到的是**同一帧**世界。
        """
        now = time.time()
        with self._lock:
            if (not force and self._st is not None
                    and now - self._st_t < self.state_ttl):
                self.state_cache_hits += 1
                return self._st
        try:
            st = self.br.get_state()
        except Exception as e:
            self.log(f"[世界] 读状态失败: {e}")
            with self._lock:
                return self._st
        with self._lock:
            if st:
                self._st = st
                self._st_t = time.time()
                self.state_fetches += 1
                self._km = None          # 台面/厨师变了, 地图模型作废
            return self._st

    def kitchen(self):
        """共享的 KitchenMap(由当前状态快照构造)。"""
        from map_model import KitchenMap
        st = self.state()
        if not st:
            return None
        lay = st.get("layout") or {}
        if not lay.get("chefs"):
            return None
        with self._lock:
            if self._km is None:
                self._km = KitchenMap.from_layout(lay)
            return self._km

    # ---------------- 地形(整关可走图) ----------------
    def terrain(self, force: bool = False):
        """共享的 TerrainMap: 同一关卡只拉一次、只解一次 —— **但有保质期**。

        ⚠ 保质期和版本号的理由同 `Engine.terrain()` 那段长注释: 限时平台升降、
          荷叶沉浮这类变化**只改高度不改字符**, 整局不刷新就会拿着"平台还升着"
          的旧图去寻路 → 走进海里。双人模式走的就是这一条路径, 所以这里也必须改。
        """
        from terrain import TerrainMap
        st = self.state()
        scene = (st or {}).get("scene") or ""
        now = time.time()
        with self._lock:
            if (not force and self._tm is not None
                    and self._tm_scene == scene and self._tm.ok
                    and (now - self._tm_at) < self.terrain_ttl):
                return self._tm
        try:
            data = self.br.get_map(force=force, max_age=self.terrain_ttl)
        except Exception as e:
            self.log(f"[地形] 取图失败: {e}")
            with self._lock:
                return self._tm
        with self._lock:
            self.map_fetches += 1
        tm = TerrainMap(data)
        if tm.error:
            self.log(f"[地形] 报错: {tm.error}")
            with self._lock:
                return self._tm
        if not tm.ok:
            self.log("[地形] 网格数据不完整, 退回旧寻路")
            with self._lock:
                return self._tm
        with self._lock:
            old = self._tm
            if old is not None and old.ok and self._tm_scene == scene:
                if tm.ver:
                    same = (tm.ver == self._tm_ver)       # 权威判据
                else:
                    same = (tm.counts == old.counts)      # 老 dll: 退化成比计数
                self._tm_at = now
                if not force and same:
                    return old                                # 没变 → 沿用旧对象
            if old is None or not old.ok or tm.counts != old.counts:
                self.log(f"[地形] 更新 ver={tm.ver or 'n/a'}  {tm.w}x{tm.h} 格 "
                         f"步长({tm.cellx:.2f},{tm.cellz:.2f}) " + tm.describe_dangers())
            self._tm = tm
            self._tm_scene = scene
            self._tm_ver = tm.ver
            self._tm_at = now
            self.tm_rebuilds += 1
        return tm

    # ---------------- 两个厨师的实时位置 ----------------
    # ⚠ 位置相关的一律 **force 读**, 不吃 TTL 缓存。
    #   理由(用户明确要求"实时得到俩厨师的位置"): 位置是闭环导航的输入,
    #   0.15 秒前的位置换算成方向就是错的, 表现就是目标附近来回抖 —— 那正是我们
    #   花大力气在治的病。而状态快照的 TTL 缓存是为"整帧一致性 + 省桥流量"服务的,
    #   它服务于**规划**(这一步世界是什么样), 不该服务于**控制**。
    #   桥读本身很便宜(插件主线程每帧刷新的字符串, 读一次就是一个本地 TCP 往返),
    #   真正贵的 map/raw 另有缓存, 所以这里不必省。
    def chefs(self) -> list:
        st = self.state(force=True)
        return list(((st or {}).get("layout") or {}).get("chefs") or [])

    def chef(self, cid: int) -> dict:
        for c in self.chefs():
            if int(c.get("id", -1)) == cid:
                return c
        return {}

    def pos(self, cid: int):
        c = self.chef(cid)
        if not c:
            return (None, None, "")
        return (float(c.get("x") or 0), float(c.get("z") or 0), c.get("held", ""))

    def others(self, cid: int) -> list:
        """**另一个厨师**(双人时的队友)的位置与手持物 —— 避让/协作的依据。"""
        return [c for c in self.chefs() if int(c.get("id", -1)) != cid]

    def age(self) -> float:
        """状态快照有多旧(秒)。位置越旧, 闭环导航越容易抖。"""
        with self._lock:
            return time.time() - self._st_t if self._st_t else 999.0

    # ---------------- 占位预约(两个人别抢同一格) ----------------
    def reserve(self, cid: int, xy, ttl: float = 1.2):
        """预约一个世界坐标附近的目标格。用于"我要站这里, 你别来"。"""
        if xy is None or xy[0] is None:
            return
        key = (round(float(xy[0]) / 0.6), round(float(xy[1]) / 0.6))   # 0.6 ≈ 半格
        with self._lock:
            self._resv[key] = (cid, time.time() + ttl)

    def reserved_by_others(self, cid: int):
        """别人正在用的格(过期自动清)。返回世界坐标列表。"""
        now = time.time()
        out = []
        with self._lock:
            for k, (owner, exp) in list(self._resv.items()):
                if exp < now:
                    self._resv.pop(k, None)
                    continue
                if owner != cid:
                    out.append((k[0] * 0.6, k[1] * 0.6))
        return out

    def occupied_by_others(self, cid: int, tm) -> set:
        """队友当前站的格子 + 他预约的格子(供寻路避开)。"""
        cells = set()
        for c in self.others(cid):
            if tm is not None and tm.ok:
                cells.add(tm.cell_of(float(c.get("x") or 0), float(c.get("z") or 0)))
        for xy in self.reserved_by_others(cid):
            if tm is not None and tm.ok:
                cells.add(tm.cell_of(xy[0], xy[1]))
        return cells

    def note(self) -> str:
        with self._lock:
            who = " ".join(
                f"#{int(c.get('id', -1))}({float(c.get('x') or 0):.1f},{float(c.get('z') or 0):.1f})"
                f"{'持' + c.get('held') if c.get('held') else ''}"
                for c in ((self._st or {}).get("layout") or {}).get("chefs") or [])
            return (f"世界: {who or '(无厨师)'} | 状态读 {self.state_fetches} 次 "
                    f"(命中缓存 {self.state_cache_hits}) 地图 {self.map_fetches} 次 "
                    f"旧 {self.age():.2f}s")
