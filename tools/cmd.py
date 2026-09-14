# -*- coding: utf-8 -*-
"""**给跑着的引擎下命令** —— 不用重开一局。

用法(引擎正在跑的时候, 另开一个终端):
  python -u tools\\cmd.py mode sabotage        # 热切换人格(下一决策点生效)
  python -u tools\\cmd.py do wash              # 指定下一步: 去洗盘子
  python -u tools\\cmd.py do fetch Flour       # 指定下一步: 去取 Flour
  python -u tools\\cmd.py pause                # 暂停(松手)
  python -u tools\\cmd.py resume
  python -u tools\\cmd.py stop                 # 收工(引擎正常退出)

原理: 往 `runtime/cmd.txt` **追加**一行; 引擎每轮读一次、读完清空
(见 `neko/control.py` —— 那里写了为什么用文件而不是 socket)。

⚠ 命令是**一次性**的: `do` 只做那一步, 做完就回到评分自己选。
   `mode` 是**持续**的, 一直到你切回来。
"""
import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "neko"))

import control                     # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="给跑着的引擎下命令")
    ap.add_argument("cmd", choices=["mode", "do", "pause", "resume", "stop"])
    ap.add_argument("args", nargs="*", help="mode: 人格名 | do: <动作> [目标]")
    a = ap.parse_args()
    line = " ".join([a.cmd] + a.args)
    p = control.send(line)
    print("[cmd] 已下发: %s   (文件: %s)" % (line, p))
    print("      引擎会在下一轮读它 —— 看**引擎那个终端**的回执日志"
          "(『[控制] …』那几行)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
