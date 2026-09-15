# -*- coding: utf-8 -*-
"""**把 mod 扒下来的东西全倒出来** —— 一次看清这一关到底有什么。

用户 2026-09-15:
  > "**让 mod 多扒一点信息下来, 别这么吝啬, 有的信息全拔下来**"

`SceneScanner.ScanRaw` 原来只报**组件名**; 现在每个游戏组件把**全部字段**也摊出来了
(`compsFull`)。这个工具就是那个口子的入口 —— 没有它, 那份数据出不来。

用法:
  python -u tools\\rawdump.py                  # 摘要: 每个物体一行(名字/tag/组件)
  python -u tools\\rawdump.py --fields         # 连**每个组件的每个字段**一起打(很啰嗦, 但全)
  python -u tools\\rawdump.py --fields --grep cook   # 只看名字或组件名里带 cook 的
  python -u tools\\rawdump.py --fields -o dump.json  # 存文件(推荐: 一次抓全, 慢慢看)
  python -u tools\\rawdump.py --know           # 顺带把食材知识表也摊开(含 cookSteps)

⚠ **只读**: 不按键、不移动 —— 引擎正在跑的时候也能开。
⚠ `--fields` 很大(每个物体 × 每个组件 × 每个字段) ⇒ 建议 `-o` 存盘再看。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from bridge.client import BridgeClient          # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fields", action="store_true",
                    help="连每个组件的**每个字段**一起打(源: `compsFull`)")
    ap.add_argument("--grep", default="", help="只看名字/组件名里含这个子串的物体")
    ap.add_argument("--know", action="store_true", help="顺带摊开食材知识表")
    ap.add_argument("-o", "--out", default="", help="把原始 JSON 写进这个文件")
    args = ap.parse_args()

    b = BridgeClient()
    try:
        b.connect(retries=3, interval=1.0)
    except Exception as e:                                        # noqa: BLE001
        print("连不上桥(游戏没开?): %s" % e)
        return 1

    raw = None
    try:
        raw = b.get_raw() or {}
    except Exception as e:                                        # noqa: BLE001
        print("取 raw 失败: %r" % e)

    closed = []
    if args.know:
        try:
            closed.append(("知识表", b.get_knowledge() or {}))
        except Exception as e:                                    # noqa: BLE001
            print("取知识表失败: %r" % e)
    try:
        b.close()
    except Exception:                                             # noqa: BLE001
        pass

    items = (raw or {}).get("raw") or []
    if args.out and raw is not None:
        payload = {"raw": raw}
        if closed:
            payload["knowledge"] = closed[0][1]
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print("已写入 %s(%d 个物体)" % (args.out, len(items)))

    pat = (args.grep or "").lower()

    def hit(it) -> bool:
        if not pat:
            return True
        if pat in (it.get("name") or "").lower():
            return True
        if pat in (it.get("tag") or "").lower():
            return True
        return any(pat in (c or "").lower() for c in (it.get("comps") or []))

    shown = 0
    for it in items:
        if not hit(it):
            continue
        shown += 1
        print("%-34s tag=%-18s (%6.1f,%6.1f)  %s"
              % (it.get("name") or "?", it.get("tag") or "-",
                 float(it.get("x") or 0), float(it.get("z") or 0),
                 ",".join(it.get("comps") or []) or "-"))
        if not args.fields:
            continue
        for comp in it.get("compsFull") or []:
            fields = comp.get("fields") or {}
            if not fields:
                continue
            # ⚠ 只打**有值**的字段: 绝大多数字段是 null/0/""(预制件没填),
            #   全打会把真正有信息的那几个淹掉 —— 而这一节是给人看的。
            interesting = {k: v for k, v in fields.items()
                           if v not in (None, 0, "", "false", "[]", "0.0")}
            if not interesting:
                continue
            print("    [%s] %s" % (comp.get("type") or "?",
                                   json.dumps(interesting, ensure_ascii=False)))

    if args.know and closed:
        k = closed[0][1]
        its = k.get("items") or []
        print("\n---- 食材知识表(%d 项) ----" % len(its))
        for it in its:
            steps = it.get("cookSteps") or []
            if not steps and not it.get("station"):
                continue
            print("  %-22s ing=%-18s station=%-14s cookSteps=%s"
                  % (it.get("name") or "?", it.get("ing") or "-",
                     it.get("station") or "-",
                     ",".join("%s#%s" % (s.get("name") or "?", s.get("id"))
                              for s in steps) or "-"))

    print("\n物体 %d 个(显示 %d)" % (len(items), shown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
