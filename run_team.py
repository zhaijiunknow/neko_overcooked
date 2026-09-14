"""双人自动做菜: 两个独立的个体各跑一个引擎循环, 共享一块订单黑板。

P1 用 WASD + 左Shift/左Ctrl/左Alt,  P2 用 方向键 + 右Shift/右Ctrl/右Alt
—— 走的是游戏自带的分屏双键盘。

输入层(环境变量 NEKO_INPUT 或 --input):
  keys     (默认) 系统级键盘注入 —— 要求游戏在最前台
  virtual  游戏内虚拟手柄 —— **每个厨师一个独立的虚拟手柄**, 游戏放后台也能做菜。
           两个厨师跑在两个线程里, 驱动是**按线程隔离**的(keyboard_input._local),
           否则后启动的线程会把前一个的驱动覆盖掉, 表现是"一个乱动另一个不动"。

用法(先在大厅让两个玩家都加入, 再进对局):
  python run_team.py             # 双人自动做菜
  python run_team.py --dry       # 只规划不驱动
  python run_team.py --only 1    # 只跑 P1(单人调试) —— **建议先用这个把单厨师流程跑通**
  python run_team.py --input virtual
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "neko"))

from bridge.client import BridgeClient              # noqa: E402
from bridge.keyboard_input import PLAYER1, PLAYER2  # noqa: E402
from engine import Engine                           # noqa: E402
from team import OrderBoard                         # noqa: E402
from world import World                             # noqa: E402
from modes import Roster, parse_mode_spec           # noqa: E402


def worker(cid: int, bindings: dict, board: OrderBoard, dry: bool, roster,
           use_virtual: bool, world):
    tag = f"P{cid + 1}"

    def log(*a):
        print(f"[{tag}]", *a, flush=True)

    bridge = BridgeClient(log=log)
    if not bridge.connect(retries=None, interval=2.0):
        log("连不上桥")
        return
    st = roster.get(cid)
    log(f"模式={st.mode.value}")

    pad = None
    if use_virtual:
        # 注意: 这一步装的是**本线程**的驱动(keyboard_input 用 threading.local),
        # 所以两个厨师互不干扰。attach_virtual_input 会自己等进对局, 并按
        # "厨师归属的玩家"(PlayerIDProvider.GetID) 去装 —— 不依赖对象枚举顺序。
        from bridge.virtual_pad import attach_virtual_input
        pad = attach_virtual_input(bridge, chef=cid, log=log)
        if pad is None:
            log("⚠ 虚拟手柄装不上, 退回键盘注入(需要游戏在前台)")

    # teammate_is_human=False: 两只都是脚本。
    # ⚠ **不能让位** —— 两个引擎各自 `board.claim_order` 领的是**不同的订单**,
    #   评价分是拿两张不同的 DishFlow 在比, 步骤价那一项根本没有共同基准;
    #   而且队友正在煮他那道菜时, 从我这看就是"他站在我的灶台边",
    #   于是我会一直让位给他 —— 让到天荒地老。见 scoring.choose 的注释。
    eng = Engine(bridge, cid=cid, bindings=bindings, board=board, log=log,
                 mode_state=st, world=world, teammate_is_human=False)
    try:
        eng.run(dry=dry)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(f"异常退出: {e!r}")
    finally:
        try:
            eng.kb.release_all()
        except Exception:
            pass
        if pad is not None:
            try:
                from bridge import keyboard_input as _ki
                _ki.set_driver(None)
                pad.uninstall()
            except Exception:
                pass
            # 双人时两名厨师各有一行, 用来判断"是没人做交互"还是"做了但游戏不认"
            log(f"直调统计: calls={pad.direct_calls} hits={pad.direct_hits} "
                f"miss={pad.direct_miss} fails={pad.direct_fails} "
                f"last={pad.last_direct.get('method')}->{pad.last_direct.get('target')}")
        bridge.close()
        board.release_all(cid)
        log(f"退出统计: {st.summary()}")
        log("已退出")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只规划不驱动")
    ap.add_argument("--only", type=int, default=0, choices=[0, 1, 2],
                    help="只跑某一个玩家(1 或 2), 默认两个都跑")
    ap.add_argument("--input", default=os.environ.get("NEKO_INPUT", "keys"),
                    help="输入层: keys(默认) | virtual(每个厨师一个独立虚拟手柄)")
    ap.add_argument("--mode", default="coop",
                    help="个体模式: coop | clumsy | sabotage(可写 1:sabotage,2:coop)")
    args = ap.parse_args()

    use_virtual = (args.input or "keys").strip().lower() in ("virtual", "ver", "hook")

    # 模式册: **每个厨师一份独立状态**(原代码这里漏了, roster 未定义 → 双人入口直接 NameError)
    roster = Roster(chefs=[0, 1])
    spec = parse_mode_spec(args.mode)
    for k, v in spec.items():
        if k == "*":
            roster.set_all(v)
        else:
            roster.set_mode(k, v)

    board = OrderBoard()

    # ---- 共享世界: 一张地图 + 两个厨师的实时位置, 两个引擎共用 ----
    # 它用自己的一条**只读**连接(state/map), 不跟厨师各自的驱动连接抢 socket。
    world_bridge = BridgeClient()
    if not world_bridge.connect(retries=None, interval=2.0):
        print("共享世界: 连不上桥", flush=True)
        return 1
    world = World(world_bridge, log=lambda *a: print("[世界]", *a, flush=True))
    print("[世界] 已建立共享地图与位置视图(两个厨师共用一份)", flush=True)

    players = []
    if args.only in (0, 1):
        players.append((0, PLAYER1))
    if args.only in (0, 2):
        players.append((1, PLAYER2))

    print(f"[输入] {'虚拟手柄(每个厨师一个, 后台也能跑)' if use_virtual else '键盘注入(需要游戏在最前台)'}",
          flush=True)
    threads = []
    for cid, bindings in players:
        t = threading.Thread(target=worker,
                             args=(cid, bindings, board, args.dry, roster, use_virtual, world),
                             daemon=True, name=f"chef{cid}")
        t.start()
        threads.append(t)
        time.sleep(1.0)   # 错开启动, 避免两条连接同时抢窗口焦点

    print(f"已启动 {len(threads)} 个厨师, Ctrl+C 停止", flush=True)
    try:
        while any(t.is_alive() for t in threads):
            # 每 10 秒打一行共享世界摘要(两个厨师的位置 + 缓存命中情况), 便于判断"是不是各看各的"
            if int(time.time()) % 10 == 0:
                print("[世界] " + world.note(), flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n停止中...", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
