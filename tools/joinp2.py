# -*- coding: utf-8 -*-
"""在主界面**让第二个玩家加入** —— 双人启动里唯一不能自动化的那一步。

为什么非要有这么个工具(这是个**鸡生蛋**):
  · 驱动 P2 靠的是"虚拟手柄", 而它的安装接口要求**场景里已经有 `PlayerControls`**
    ⇒ 得先在对局里 (`VirtualInput.cs:226`: "场景里没有 PlayerControls(还没进对局?)")
  · 而对局里要有第二只厨师, P2 得先在主界面加入
  · 真正消费输入的那套虚拟手柄**故意绕开加入流程**(它替换 `ControlSchemeData`,
    不碰设备枚举 —— `VirtualInput.cs:99-103`)
  ⇒ **只有 `VirtualGamepads`(真的 InControl 设备)能触发"有手柄接上了"那个事件。**
    这个工具就是去点它的 A。

⚠ **它只能确认"命令被游戏接受", 不能确认"人真的加进来了"** ——
  大厅里有几个人在 `StartScreen` **读不到**(要进对局才有 `PlayerControls`)。
  所以跑完**看一眼屏幕**, 或者直接开局、看对局里有几只厨师。

用法(游戏停在主界面):
  python -u tools/joinp2.py              # 虚拟设备 1, 按 3 次 A
  python -u tools/joinp2.py --pad 0      # 换一个设备号
  python -u tools/joinp2.py --tries 5 --hold 0.8
"""
import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from bridge.client import BridgeClient            # noqa: E402
from bridge.virtual_pad import join_player        # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pad", type=int, default=1, choices=[0, 1],
                    help="用第几个虚拟 InControl 设备(默认 1; 不行试 0)")
    ap.add_argument("--tries", type=int, default=3, help="按几次 A")
    ap.add_argument("--hold", type=float, default=0.6, help="每次按住 A 多久(秒)")
    args = ap.parse_args()

    b = BridgeClient()
    b.connect(retries=5, interval=2.0)
    try:
        st = b.get_state() or {}
        print("场景 = %s   在局 = %s" % (st.get("scene"), st.get("inRound")), flush=True)
        n = len(((st.get("layout") or {}).get("chefs") or []))
        if st.get("inRound"):
            print("⚠ 已经在对局里了(%d 只厨师)—— 加入是在**主界面**做的。" % n, flush=True)
            return 0
        ok = join_player(b, pad=args.pad, hold=args.hold, tries=args.tries)
        print()
        if ok:
            print("✓ 命令被接受了 —— **去看一眼屏幕**, 大厅里出第二个玩家了吗?", flush=True)
            print("  出来了就开局, 然后: python -u run_engine.py --cid 1 --input virtual",
                  flush=True)
        else:
            print("✗ 命令没被接受 —— 看上面的错误(多半是桥没通 / dll 是旧的)", flush=True)
        return 0 if ok else 1
    finally:
        b.close()


if __name__ == "__main__":
    sys.exit(main())
