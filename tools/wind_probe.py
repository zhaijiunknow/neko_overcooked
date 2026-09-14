"""风: 把**反编译定不了的两条前提**量出来(站进对局里跑, 一次一分钟)。

为什么要单独一个工具(开发约定 规则 1/规则 3):
  迎风补偿的整条公式建立在两条**从 Assembly-CSharp 里读不出来**的前提上 ——
  它们要么是 Unity 引擎内部行为(`MovePosition` 对非运动学刚体到底是什么语义),
  要么是"代码这么写但没人验证过"的约定。**猜错方向就是把厨师往更糟的地方推**,
  所以宁可开一局量一次, 也不要照着公式上线:

    A. **风是「和输入相加」还是「覆盖」输入?**
       `ApplyWindForce` 调 `MovePosition(pos + v·dt)`(ClientPlayerControlsImpl_Default.cs:411),
       而输入那一路后面才 `SetVelocity`(:433-435), 两者打的是**同一个刚体、同一个 1/60 步**。
       相加 ⇒ 站在风里推 +x 一秒, 位移 ≈ `W + (R,0)`; 覆盖 ⇒ 位移 ≈ `W`。
       ⚠ 这条是**承重的**: 若是覆盖, 人在风区里**根本走不动**, 整个补偿模型要重写。

    B. **摇杆「大小」影响速度吗?**
       `GetControlAxis` 结尾是 `.normalized`(PlayerControlsHelper.cs:70) ⇒ 只认方向;
       但仓库里没有一次实验钉死它(文档里那句"只认方向"是推断)。
       量法: 推 `1.0` 与推 `0.2` 各一秒 —— 位移一样 ⇒ 归一化; 差 5 倍 ⇒ 线性。
       ⚠ 若真是线性的, 摇杆大小就成了**可用的控制量**(补偿可以做到"精确 t"), 方案要改。

    C. (顺带) 新 DLL 的字段到底有没有值 —— `dyn["winds"]` 与每厨师的
       `wind`/`run`/`alignx`/`aligny`。这几个是几何投影和补偿的**唯一数据源**,
       插件里反射写错了就会静默报 0(**最坏的那种错**: 看着有字段、其实永远没风)。

用法(先开一局, 把厨师走到你选好的位置):

    python -u tools\\wind_probe.py --check      # 只看字段(C, 不动厨师)
    python -u tools\\wind_probe.py              # 测 A+B(会真的把厨师推走 ~4 格)
    python -u tools\\wind_probe.py --cid 0      # 指定厨师(默认按身份挑 Player.Two)

测 A 要**站在风区里**(地图上 `~`, 或先跑 `python -u tools\\mapview.py --watch 3` 看);
测 B 要**站在没有风、没有传送带、没有平台的平地上** —— 否则量到的是"风+带子+人"的合力。
两个测试都会**先把厨师推走**, 所以别在正在做的活旁边跑。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "neko"))

from bridge.client import BridgeClient                      # noqa: E402
from bridge.virtual_pad import attach_virtual_input, chef_of_player   # noqa: E402


def chef_info(bridge, cid: int):
    """返回 `(x, z, 整个厨师字典)`; 拿不到返回 `(None, None, None)`。"""
    st = bridge.get_state() or {}
    for c in ((st.get("layout") or {}).get("chefs") or []):
        try:
            if int(c.get("id", -1)) == cid:
                return float(c.get("x") or 0.0), float(c.get("z") or 0.0), c
        except (TypeError, ValueError):
            continue
    return None, None, None


def wind_of(c) -> tuple:
    """从厨师字典里取**游戏报的**风 `(vx, vz)`; 读不到返回 `None`。"""
    w = (c or {}).get("wind")
    if not isinstance(w, dict) or not w.get("ok"):
        return None
    try:
        return float(w.get("vx") or 0.0), float(w.get("vz") or 0.0)
    except (TypeError, ValueError):
        return None


def print_fields(bridge, cid: int) -> None:
    """C: 把数据源原样打出来 —— 这一节过了才谈得上补偿。"""
    st = bridge.get_state() or {}
    chefs = (st.get("layout") or {}).get("chefs") or []
    print("\n---- C. 数据源体检 ----", flush=True)
    for c in chefs:
        w = c.get("wind")
        print("  厨师#%s player=%s  位置 (%.2f,%.2f)" % (
            c.get("id"), c.get("player"), float(c.get("x") or 0), float(c.get("z") or 0)))
        if w is None:
            print("    `wind` 字段**不存在** ⇒ 加载的是旧 DLL(或反射失败) —— "
                  "补偿会退回几何投影(偏保守), 见 engine._wind_of")
        else:
            print("    wind=%s  run=%s  scale=%s  alignx=%s aligny=%s" % (
                w, c.get("run"), c.get("scale"), c.get("alignx"), c.get("aligny")))
            if not w.get("ok"):
                print("    ⚠ `wind.ok=false` ⇒ 反射没读到 WindReceiver.GetVelocity()"
                      "(厨师身上没有 WindAccumulator? 字段改名?)")
            elif abs(float(w.get("vx") or 0)) < 1e-3 and abs(float(w.get("vz") or 0)) < 1e-3:
                print("    (此刻没风 —— 站在风区里再跑一次才验得到 vx/vz)")
    try:
        dyn = bridge.get_dyn() or {}
    except Exception as e:                                   # noqa: BLE001
        print("  取 dyn 失败: %s" % e)
        dyn = {}
    winds = dyn.get("winds")
    print("  dyn.counts=%s" % (dyn.get("counts"),))
    if winds is None:
        print("  ⚠ `dyn` 里**没有 winds 这一类** ⇒ 旧 DLL")
    elif not winds:
        print("  `winds` = [](这一关没有风区 —— 想看效果换一关有风的)")
    for w in winds:
        print("  风区 %s @ (%.2f,%.2f) on=%s vx/vz=(%s,%s) 半长=(%s,%s) rot=%s 吹的厨师=%s" % (
            w.get("name"), float(w.get("cx") if w.get("cx") is not None else (w.get("x") or 0)),
            float(w.get("cz") if w.get("cz") is not None else (w.get("z") or 0)),
            w.get("on"), w.get("vx"), w.get("vz"), w.get("ex"), w.get("ez"), w.get("rot"),
            w.get("chefs")))


def shot(bridge, pad, cid: int, ax: float, ay: float, t: float, settle: float = 0.4):
    """**松手 → 量干净起点 → 推杆 t 秒 → 松手 → 量干净终点。**

    为什么要"先松手再量"(而不是直接拿推杆前那一帧): 位置是外力改的 ——
    风/传送带每帧 `MovePosition`、松开摇杆那一下还会被服务器坐标吸附
    (见 `Engine.checkpoint` 的注释)。停手那一帧才是干净的真值。

    返回 `(dx, dz, 推杆期间采到的风, 起点, 终点)`。
    """
    pad.release_all()
    time.sleep(settle)
    x0, z0, _ = chef_info(bridge, cid)
    if x0 is None:
        return None
    pad.move(ax, ay)
    t_end = time.time() + t
    samples = []
    while time.time() < t_end:
        time.sleep(0.1)
        _, _, c = chef_info(bridge, cid)
        w = wind_of(c)
        if w is not None:
            samples.append(w)
    pad.release_all()
    time.sleep(settle)
    x1, z1, _ = chef_info(bridge, cid)
    if x1 is None:
        return None
    return (x1 - x0, z1 - z0, samples, (x0, z0), (x1, z1))


def mean_wind(samples) -> tuple:
    if not samples:
        return (0.0, 0.0)
    return (sum(s[0] for s in samples) / len(samples),
            sum(s[1] for s in samples) / len(samples))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cid", type=int, default=None,
                    help="驱动哪个厨师(默认按玩家身份挑 Player.Two, 同 run_engine)")
    ap.add_argument("--t", type=float, default=1.0, help="每次推杆的秒数(默认 1.0)")
    ap.add_argument("--check", action="store_true",
                    help="**只体检字段, 不动厨师**(先跑这个)")
    args = ap.parse_args()

    bridge = BridgeClient()
    print("连桥...", flush=True)
    bridge.connect(retries=None)

    cid = args.cid
    if cid is None:
        cid = chef_of_player(bridge, "Two")
        if cid is None:
            print("[玩家] ✗ 这局没有 Player.Two —— 双人先 `python -u tools\\joinp2.py`, "
                  "单人显式 --cid 0", flush=True)
            bridge.close()
            return 1
        print("[玩家] 自动选中 Player.Two = 厨师#%d" % cid, flush=True)

    try:
        print_fields(bridge, cid)
    except Exception as e:                                   # noqa: BLE001
        print("体检失败: %s" % e)

    if args.check:
        print("\n--check: 只体检, 没动厨师。", flush=True)
        bridge.close()
        return 0

    pad = attach_virtual_input(bridge, chef=cid, log=print)
    if pad is None:
        print("[输入] ✗ 虚拟手柄装不上 —— 这个工具**必须**用模拟量(测的是摇杆), "
              "装不上就没法测", flush=True)
        bridge.close()
        return 1

    print("\n---- A. 风是相加还是覆盖? (推世界 +x %.1fs) ----" % args.t, flush=True)
    a = shot(bridge, pad, cid, 1.0, 0.0, args.t)
    if a is None:
        print("  量不到位置(对局结束了?)", flush=True)
        bridge.close()
        return 1
    dx, dz, samples, p0, p1 = a
    wx, wz = mean_wind(samples)
    _, _, c = chef_info(bridge, cid) or (None, None, None)
    c = c or {}
    try:
        R = float(c.get("run") or 4.0) * float(c.get("scale") if c.get("scale") is not None else 1.0)
    except (TypeError, ValueError):
        R = 4.0
    print("  起点 (%.2f,%.2f) → 终点 (%.2f,%.2f)   位移 (%.2f,%.2f)  |Δ|=%.2f" % (
        p0[0], p0[1], p1[0], p1[1], dx, dz, (dx * dx + dz * dz) ** 0.5), flush=True)
    print("  推杆期间厨师身上的风: (%+.2f, %+.2f)  采样 %d 次" % (wx, wz, len(samples)), flush=True)
    if len(samples) == 0:
        print("  ⚠ 一次都没采到 `wind` 字段 —— 加载的是旧 DLL, A 测不出来", flush=True)
    elif abs(wx) < 0.05 and abs(wz) < 0.05:
        print("  ⚠ 这段时间**没有风** —— 测 A 要站进风区里(图上 `~`); "
              "本节只能说明'没风时人是走得动的'", flush=True)
    else:
        exp_add = ((R + wx) * args.t, wz * args.t)
        exp_ovr = (wx * args.t, wz * args.t)
        e_add = ((dx - exp_add[0]) ** 2 + (dz - exp_add[1]) ** 2) ** 0.5
        e_ovr = ((dx - exp_ovr[0]) ** 2 + (dz - exp_ovr[1]) ** 2) ** 0.5
        print("  预测·相加(摇杆R + 风): (%.2f,%.2f)  误差 %.2f 格" % (exp_add + (e_add,)), flush=True)
        print("  预测·覆盖(只有风):     (%.2f,%.2f)  误差 %.2f 格" % (exp_ovr + (e_ovr,)), flush=True)
        print("  ⇒ %s" % ("**相加**(补偿模型成立, 迎风可以靠摇杆顶回去)"
                          if e_add < e_ovr else
                          "**覆盖**(人在风区里根本走不动 —— 补偿模型要重写!)"), flush=True)

    print("\n---- B. 摇杆大小影响速度吗? (1.0 与 0.2 各推 %.1fs) ----" % args.t, flush=True)
    b1 = shot(bridge, pad, cid, 1.0, 0.0, args.t)
    b2 = shot(bridge, pad, cid, 0.2, 0.0, args.t)
    if b1 is None or b2 is None:
        print("  量不到位置", flush=True)
        bridge.close()
        return 1
    n1 = (b1[0] ** 2 + b1[1] ** 2) ** 0.5
    n2 = (b2[0] ** 2 + b2[1] ** 2) ** 0.5
    w1, w2 = mean_wind(b1[2]), mean_wind(b2[2])
    print("  move(1.0,0): 位移 %.2f 格  (期间风 (%+.2f,%+.2f))" % (n1, w1[0], w1[1]), flush=True)
    print("  move(0.2,0): 位移 %.2f 格  (期间风 (%+.2f,%+.2f))" % (n2, w2[0], w2[1]), flush=True)
    if max(abs(w1[0]), abs(w1[1]), abs(w2[0]), abs(w2[1])) > 0.05:
        print("  ⚠ 这两次里**有风** —— 结果不可用。B 要站在没有风/传送带/平台的平地上重测",
              flush=True)
    elif n1 <= 0.05:
        print("  ⚠ 推了杆人却没动 —— 要么是输入没生效, 要么是被挡住/被压制, 先查这两条",
              flush=True)
    else:
        ratio = n2 / n1
        print("  比值 0.2 档 / 1.0 档 = %.2f" % ratio, flush=True)
        print("  ⇒ %s" % ("**归一化**(只认方向, 大小被丢弃 —— 补偿只能控制方向, 与现有写法一致)"
                          if ratio > 0.6 else
                          "**线性**(大小影响速度 —— 摇杆多了一个可用的控制量, 方案要改!)"), flush=True)

    try:
        from bridge import keyboard_input as _ki
        _ki.set_driver(None)
        pad.uninstall()
    except Exception:                                        # noqa: BLE001
        pass
    bridge.close()
    print("\n完。把上面两行 ⇒ 的结论贴回交接包, 再决定补偿要不要开。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
