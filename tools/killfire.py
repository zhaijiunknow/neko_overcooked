# -*- coding: utf-8 -*-
"""**一条命令灭火** —— 别再让人等诊断了。

用户实测反馈: "磨磨唧唧的, 关卡结束了"。所以这个工具**不做任何前置检查**,
直接: 找火 → 拿灭火器 → 走到火正前方 → 面向 → 喷到灭。

用法:
  python -u tools/killfire.py            # 用 P1(cid=0)
  python -u tools/killfire.py --cid 1
  python -u tools/killfire.py --budget 40

⚠ 两个当前环境的坑(修好后可以删):
  1. (已解决) 诊断命令早就改名 `sprayinfo`, 子串撞车不存在了 ——
     **发小写 `spray` 就行**。⚠ 千万别再发全大写 `SPRAY`: 它虽然能进分支,
     但里面那个比较是大小写敏感的, 实际执行的是**停喷**(见 InteractDirect)。
  2. 单人模式下两个厨师都报 `Player.One`, 所以 `player=1` 找不到人 —— **只能用 cid=0**。
"""
import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from bridge.client import BridgeClient      # noqa: E402
from engine import Engine                   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cid", type=int, default=0)
    ap.add_argument("--budget", type=float, default=30.0, help="最多花几秒")
    args = ap.parse_args()

    b = BridgeClient()
    b.connect(retries=6, interval=1.0)

    eng = Engine(b, cid=args.cid, log=lambda *a: print(*a, flush=True))
    try:
        st = eng.state()
        if not st or not st.get("inRound"):
            print("[灭火] 不在对局里")
            return 1
        km = eng.map(st)
        if km is None:
            print("[灭火] 读不到地图")
            return 1

        fires = eng.fires()
        print("[灭火] 场上 %d 处着火" % len(fires), flush=True)
        if not fires:
            print("[灭火] 没火, 收工")
            return 0
        for x, z in fires:
            print("       (%.1f, %.1f)" % (x, z), flush=True)

        n = eng.extinguish(km, st, budget=args.budget)
        left = len(eng.fires())
        print("[灭火] 灭了 %d 处, 还剩 %d 处" % (n, left), flush=True)
        return 0 if left == 0 else 2
    finally:
        try:
            eng.kb.release_all()
        except Exception:
            pass
        b.close()


if __name__ == "__main__":
    sys.exit(main())
