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

⚠ **A 是"加入下一个玩家", 不是 toggle** ⇒ 按多了会**引进第三个人**。
  所以 `join_player` 现在**先检查是不是已经双人, 是就跳过**(判据见 `ScanUsers` 的反编译依据,
  数据来自游戏自己的 `ClientUserSystem.m_Users`)。

⚠ 它只能确认"命令被游戏接受", **不能确认"人真的加进来了"** —— 这一条要
  **看一眼屏幕**, 或者直接开局、看对局里有几只厨师。

用法(游戏停在主界面):
  python -u tools/joinp2.py              # 已经是双人就跳过; 否则虚拟设备 1 按 A
  python -u tools/joinp2.py --force      # 越过"已经双人就跳过"的检查, 强行按
  python -u tools/joinp2.py --pad 0      # 换一个设备号
  python -u tools/joinp2.py --tries 5 --hold 0.8
"""
import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from bridge.client import BridgeClient                        # noqa: E402
from bridge.virtual_pad import join_player, lobby_users       # noqa: E402


def _users_line(users):
    """把名单打成一行 —— `None` 要打成"读不到", **不能打成"0 人"**(见 `lobby_users`)。"""
    if users is None:
        return "读不到(插件是旧的? 状态里没有 `users` 字段)"
    if not users:
        return "0 人"
    return "%d 人: %s" % (len(users), ", ".join(
        "%s%s" % (u.get("slot") or "?", "(本地)" if u.get("local") else "")
        for u in users))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pad", type=int, default=1, choices=[0, 1],
                    help="用第几个虚拟 InControl 设备(默认 1; 不行试 0)")
    ap.add_argument("--tries", type=int, default=3, help="按几次 A")
    ap.add_argument("--hold", type=float, default=0.6, help="每次按住 A 多久(秒)")
    ap.add_argument("--force", action="store_true",
                    help="越过「已经双人就跳过」的检查, 强行按 A"
                         "(⚠ 已经在双人局里按下去会引进第三个人)")
    args = ap.parse_args()

    b = BridgeClient()
    b.connect(retries=5, interval=2.0)
    try:
        st = b.get_state() or {}
        print("场景 = %s   在局 = %s" % (st.get("scene"), st.get("inRound")), flush=True)
        print("大厅玩家: %s" % _users_line(lobby_users(b, st)), flush=True)
        n = len(((st.get("layout") or {}).get("chefs") or []))
        if st.get("inRound"):
            print("⚠ 已经在对局里了(%d 只厨师)—— 加入是在**主界面**做的。" % n, flush=True)
            return 0
        ok = join_player(b, pad=args.pad, hold=args.hold, tries=args.tries,
                         force=args.force)
        print()
        if ok:
            # "跳过"和"按过了"都会走到这里 —— 靠上面 join_player 的日志区分。
            print("✓ 完成(**看一眼屏幕**确认大厅里的玩家对不对)", flush=True)
        else:
            print("✗ 没做成 —— 看上面的原因(桥没通 / dll 旧 / 读不到名单)", flush=True)
        return 0 if ok else 1
    finally:
        b.close()


if __name__ == "__main__":
    sys.exit(main())
