"""单人自动做菜入口。

用法(先进对局):
  python run_engine.py                     # **驱动 Player.Two 那只**(默认, 走虚拟手柄)
                                           #   键盘留给玩家; 没有 Player.Two 会停下报错
  python run_engine.py --cid 0             # 按**序号**显式指定厨师(单人调试用)
  python run_engine.py --dry               # 只规划不驱动(打印"当前订单要怎么做")
  python run_engine.py --mode clumsy       # 用"失误"模式
  python run_engine.py --mode sabotage     # 用"捣蛋"模式
  python run_engine.py --input keys        # 非要键盘注入时才加(见下)

⚠ **`--cid` 是厨师序号, 不是玩家身份** —— 两者在单人局里**不一样**:
  单人(Overcooked 单人也是两只厨师, 都归 Player.One, 你自己切换着玩)时
  `--cid 1` 拿到的是**你的**厨师。所以默认改成**按身份找 Player.Two**。

  --mode 可写:  coop | clumsy | sabotage
               也可对多人分别指定(如 --cid 0 时用 "1:sabotage")

输入层(环境变量 NEKO_INPUT):
  virtual  (**默认**) 游戏内虚拟手柄 —— 直接换掉厨师的逻辑输入, **游戏放后台也照样做菜**,
           而且**不占你的键盘/鼠标**(双人时键盘留给玩家)
  keys     系统级键盘注入(SendInput) —— **要求游戏在最前台**, 失焦就暂停;
           而且会真的按键, 玩家在用键盘时两边会抢
           ⚠ **装不上虚拟手柄时不会自动退回这个** —— 见下

⚠ **默认改成 virtual 的原因**(2026-09-14 实测): 双人时"键盘给玩家、手柄给脚本"是
  正常用法, 而 `keys` 会把脚本的按键和玩家的键盘混在一起 —— 实测出现过
  "脚本在玩玩家那只厨师"。虚拟手柄走的是**按玩家身份**接管(`installplayer`),
  和谁在用键盘无关。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "neko"))

from bridge.client import BridgeClient   # noqa: E402
from engine import Engine                # noqa: E402
from world import World                  # noqa: E402
from modes import Roster, parse_mode_spec  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只规划不驱动")
    ap.add_argument("--cid", type=int, default=None,
                    help="驱动哪个厨师(按**序号**: 0=P1, 1=P2)。"
                         "**不给就按玩家身份自动挑 Player.Two 那只** —— "
                         "单人局没有 Player.Two, 会报错让你先让 P2 加入")
    ap.add_argument("--mode", default="coop",
                    help="个体模式: coop | clumsy | sabotage(可写 1:sabotage,2:coop)")
    ap.add_argument("--input", default=os.environ.get("NEKO_INPUT", "virtual"),
                    help="输入层: virtual(默认, 游戏内虚拟手柄, 后台也能跑) | keys(键盘注入)")
    args = ap.parse_args()

    bridge = BridgeClient()
    print("连桥...", flush=True)
    bridge.connect(retries=None)

    # ---- 挑要驱动的厨师 ----
    # 不给 `--cid` 就**按玩家身份**挑 Player.Two, **不用序号猜** ——
    #   厨师序号 ≠ 玩家身份: 单人局(两只厨师但都归 Player.One, 你自己切换着玩)里,
    #   `--cid 1` 拿到的是**你的**厨师, 脚本会当场抢人(实测日志:
    #   `已接管厨师#1(指定 Player=0 → 实际 0)` + `厨师#1 属于 One → 用 P1 键位`)。
    if args.cid is None:
        from bridge.virtual_pad import chef_of_player
        args.cid = chef_of_player(bridge, "Two")
        if args.cid is None:
            print("[玩家] ✗ 这局没有 Player.Two —— **不知道要驱动谁, 停下**", flush=True)
            print("[玩家]   双人: 先在主界面让 P2 加入:  python -u tools\\joinp2.py", flush=True)
            print("[玩家]   单人: 只有你自己, 那本就不该跑脚本; 非要跑就显式 --cid 0", flush=True)
            bridge.close()
            return 1
        print(f"[玩家] 自动选中 Player.Two 的厨师 = 厨师#{args.cid}", flush=True)

    # 模式册: 每个个体一份状态, 可热切换(v1 D6/D10)
    # ⚠ --mode none: **纯执行, 一个失误都不演** —— 调寻路/调流程时必须用这个。
    #   coop 也带"低概率自然失误"(发呆/绕路/多切/忘盘), 测出来的卡顿分不清是 bug 还是演的。
    clean = (args.mode or "").strip().lower() in ("none", "off", "clean", "no")
    roster = Roster(chefs=[args.cid])
    if not clean:
        spec = parse_mode_spec(args.mode)
        for k, v in spec.items():
            if k == "*":
                roster.set_all(v)
            else:
                roster.set_mode(k, v)
        print(f"[模式] P{args.cid + 1} → {roster.get(args.cid).mode.value}", flush=True)
    else:
        print(f"[模式] P{args.cid + 1} → 纯执行(不演任何失误, --mode none)", flush=True)

    # ---- 输入层选择 ----
    mode = (args.input or "virtual").strip().lower()
    pad = None
    if mode in ("virtual", "ver", "hook"):
        from bridge.virtual_pad import attach_virtual_input
        # 会自己等进对局, 并按"厨师归属的玩家"安装(不依赖对象枚举顺序)
        pad = attach_virtual_input(bridge, chef=args.cid, log=print)
        if pad is None:
            # ☠ **绝不自动退回键盘**(2026-09-14 改)。
            #   键盘注入发的是**真的系统按键** —— 双人时玩家也在用键盘,
            #   两边会**互相抢**。实测就是这么出现"脚本在玩玩家那只厨师"的:
            #   手柄没装上 → 静默退回键盘 → 脚本的键和玩家的键混在一起打给同一个人。
            #   所以装不上就**停下来说清楚**, 由人决定(想用键盘就显式 --input keys)。
            print("[输入] ✗ 虚拟手柄装不上 —— **不会自动退回键盘**", flush=True)
            print("       常见原因: ① 起脚本时还没进对局(`wait_for_round` 等 300 秒);", flush=True)
            print("                 ② 这局没有厨师#%d(单人只有一只 → 用 --cid 0)" % args.cid,
                  flush=True)
            print("       要键盘注入请**显式**加: --input keys", flush=True)
            bridge.close()
            return 1
        else:
            # 这一行就是"体检": 跑全程的同时把决定性的数字打出来, 不用再单独开终端查。
            #   paused 存在      -> 加载的是新 dll(BepInEx 只在游戏启动时读 dll, 改完必须重启游戏)
            #   paused.Network   -> 若为 true, 交互那整段更新会被跳过(能走不能按)
            #   rebinds          -> >0 说明游戏在抢我们的按键(我们每帧抢回)
            #   pickupIsDownCalls-> 游戏到底有没有来读我们的拾取键
            #   schemePickupIsOurs-> 那一刻方案里的拾取键还是不是我们的实例
            st = pad.status()
            app = st.get("app") or {}
            if "paused" not in app:
                print("[输入] ⚠ 加载的是**旧 dll**(没有 paused 遥测) —— "
                      "完全退出游戏再重开才会加载新版", flush=True)
            print(f"[输入] 后台遥测: {app}", flush=True)
            for p in (st.get("pads") or []):
                print(f"[输入] 手柄 Player={p.get('player')} netButtons={p.get('netButtons')} "
                      f"rebinds={p.get('rebinds')} pickupIsDownCalls={p.get('pickupIsDownCalls')} "
                      f"schemePickupIsOurs={p.get('schemePickupIsOurs')}", flush=True)
                print(f"[输入]   判决书: pickupSetTrue={p.get('pickupSetTrue')} "
                      f"clientOurs={p.get('clientOurs')} serverOurs={p.get('serverOurs')} "
                      f"direct={p.get('direct')} forced={p.get('forced')} "
                      f"missing={p.get('missing')!r}", flush=True)
            lay = (bridge.get_state().get("layout") or {}).get("chefs") or []
            for c in lay:
                print(f"[输入] 厨师#{c.get('id')} local={c.get('local')} canpress={c.get('canpress')} "
                      f"pick={c.get('pick')!r}", flush=True)
    else:
        print("[输入] ⚠ 键盘注入(需要游戏在最前台; 会真的按键 —— "
              "玩家也在用键盘时会互相抢。默认是 --input virtual)", flush=True)

    # teammate_is_human=True: 这个入口只驱动**一只**厨师, 另一只归玩家 ——
    # 让位判定(规格: "对方是人类时, 帮助他评距离和可达性")只在这种局面下开。
    eng = Engine(bridge, cid=args.cid,
                 mode_state=None if clean else roster.get(args.cid),
                 world=World(bridge, log=print),
                 teammate_is_human=True)
    try:
        eng.run(dry=args.dry)
    except KeyboardInterrupt:
        print("\n停止", flush=True)
    finally:
        eng.kb.release_all()
        try:
            from bridge import keyboard_input as _ki
            _ki.set_driver(None)          # 松开虚拟手柄(值归零)
            if pad is not None:
                pad.uninstall()            # 把厨师的输入还给游戏
        except Exception:
            pass
        bridge.close()
        print(f"[模式] 本局统计: {roster.describe()}", flush=True)
        if pad is not None:
            # 收尾这一行是"直调到底有没有在用"的唯一凭据:
            #   calls=0        -> 引擎一次交互都没做过(流程根本没走到)
            #   hits=0 miss>0  -> 调通了但游戏总说身边没东西 ⇒ 站位问题(寻路/落脚点)
            #   hits>0         -> 直调真的接上了, 取件不再依赖游戏自己的消息链
            print(f"[输入] 直调统计: calls={pad.direct_calls} hits={pad.direct_hits} "
                  f"miss={pad.direct_miss} fails={pad.direct_fails} "
                  f"last={pad.last_direct.get('method')}->{pad.last_direct.get('target')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
