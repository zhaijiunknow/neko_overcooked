# -*- coding: utf-8 -*-
"""**对外唯一入口** —— 切状态 + 读情况, 一个脚本两个方向。

为什么要合成一个: 原来是两个单功能脚本(`cmd.py` 下命令、`status.py` 读状态),
外部(宿主/猫娘/编排层)要调两次、还得认两种输出形状。这里收成一个:
**同一个进程级入口, 同一套 `--json` 外壳**, 谁都能直接调。

```
python -u tools\\ctl.py                         # 不看子命令 = status
python -u tools\\ctl.py status [--json] [--watch [N]] [--require-engine]
python -u tools\\ctl.py modes [--json]          # 有哪几种人格可选(不用记)
python -u tools\\ctl.py mode <coop|clumsy|sabotage> [--wait N]
python -u tools\\ctl.py do <动作> [目标] [--wait N]
python -u tools\\ctl.py pause | resume | stop [--wait N]
```

**输出契约**(所有子命令都支持 `--json`, 都是这**一个**形状):
```json
{
  "ok": true,                 // 这次调用本身成没成
  "action": "status",         // 你调的那个子命令
  "engine_running": true,     // 引擎进程活着吗(按心跳判, 见 neko/status.py)
  "stale_seconds": 0.4,       // 心跳多久以前
  "data_age": 0.4,            // 这份**数据**多久以前采样(和上面不是一回事)
  "persona": "coop",          // 当前人格
  "in_round": true,
  "say": "《…》进行中: …",     // 一行中文, 可直接塞进 prompt
  "status": { … }             // 原始 status.json(只在 status 里有)
}
```
控制类子命令多两个字段: `"command"`(下发了什么) 和 `"waited"`(等了几秒 / null)。

⚠ **退出码**: `0` 成功; `1` 参数不对; `2` 引擎没在跑(`--require-engine`)/`--wait` 超时。
  不传 `--require-engine` 时, "引擎没在跑"不算失败 —— 那本身也是一条**有效状态**。

⚠ 拿它当**库**用就别调进程: 直接 `import control` / `import status`(在 `neko/` 里),
  这两个模块才是真正的实现, 本脚本只是它们的命令行外壳。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "neko"))

import control                                   # noqa: E402
import status as st_mod                          # noqa: E402

#: 人格的**一句话说明**。只放"这是什么", 数字一律从 `modes.PROFILES` 现取 ——
#: 免得这里写死一份、那边改了这里不动(那种错最难看出来)。
_MODE_BLURB = {
    "coop": "好好干, 只留低概率的自然失误",
    "clumsy": "想做对但笨手笨脚: 慢半拍、容易失手",
    "sabotage": "主动捣蛋: 发呆/绕路/倒队友的料/放任烧糊",
}

EXIT_OK, EXIT_USAGE, EXIT_NO_ENGINE = 0, 1, 2


# ---------------------------------------------------------------- 读
def _running(payload: dict, now: float = None):
    """引擎活着吗 —— 心跳没过期就是活着(见 `neko/status.py` 为什么用心跳而不是数据时刻)。"""
    age = st_mod.stale_seconds(payload, now=now) if payload else None
    return (age is not None and age <= st_mod.STALE_AFTER), age


def envelope(payload: dict, action: str, **extra) -> dict:
    """**统一外壳** —— 外部只认这一个形状, 不必知道内部结构。"""
    run, age = _running(payload)
    eng = (payload or {}).get("engine") or {}
    live = (payload or {}).get("live")
    out = {
        "ok": True,
        "action": action,
        "engine_running": run,
        "stale_seconds": None if age is None else round(age, 1),
        "data_age": None if not payload else (
            None if st_mod.data_age(payload) is None else round(st_mod.data_age(payload), 1)),
        "persona": eng.get("persona") or "",
        "paused": bool(eng.get("paused")),
        "in_round": bool(live),
        "say": (payload or {}).get("say") or "",
        "say_result": (payload or {}).get("say_result") or "",
    }
    out.update(extra)
    return out


def _emit(env: dict, as_json: bool, human: str) -> None:
    print(json.dumps(env, ensure_ascii=False, indent=1) if as_json else human, flush=True)


# ---------------------------------------------------------------- 写
def _wait_for(pred, timeout: float, payload0: dict):
    """轮询 `status.json` 直到 `pred(payload)` 为真。返回 (成没成, 等了几秒, 最后那份)。"""
    t0 = time.time()
    p = payload0
    while time.time() - t0 < timeout:
        time.sleep(0.25)
        p = st_mod.read() or p
        try:
            if pred(p):
                return True, time.time() - t0, p
        except Exception:                                        # noqa: BLE001
            pass
    return False, time.time() - t0, p


def _send(cmd: str, action: str, args: list, as_json: bool,
          wait: float = 0.0) -> int:
    line = " ".join([cmd] + [a for a in args if a])
    p = control.send(line)
    before = st_mod.read()
    run, _ = _running(before)

    env = envelope(before, action, command=line, sent=True, waited=None)
    human = "[ctl] 已下发: %s" % line
    if not run:
        # ⚠ **命令是队列, 不是 RPC** —— 引擎没在跑时它只是排在那儿, 不会报错。
        #   必须说清楚, 否则调用方会以为"切好了"。
        human += "  (⚠ 引擎没在跑 —— 命令已排队, 引擎一起来就执行)"
        env["ok"] = False
        env["note"] = "engine_not_running_command_queued"

    if wait > 0 and run:
        ok, secs, after = _wait_for(_pred_for(action, args), wait, before)
        env2 = envelope(after, action, command=line, sent=True, waited=round(secs, 1))
        env2["ok"] = ok
        if not ok:
            env2["note"] = "wait_timeout"
        human += "\n[ctl] %s (等了 %.1fs)%s" % (
            "✓ 已生效" if ok else "✗ 没等到生效", secs,
            "" if ok else " —— 引擎可能没在跑, 或这局不认这个命令")
        if not ok:
            env2["say"] = env.get("say") or env2.get("say")
            _emit(env2, as_json, human)
            return EXIT_NO_ENGINE
        _emit(env2, as_json, human)
        return EXIT_OK

    human += "\n      (文件: %s —— 引擎在下一轮读它; 看**引擎那个终端**的『[控制] …』回执)" % p
    _emit(env, as_json, human)
    return EXIT_OK if run else EXIT_NO_ENGINE


def _pred_for(action: str, args: list):
    """`--wait` 等的条件 —— **只有能从 status.json 直接看出来的**才等得了。

    `do`(指定下一步) 等不了: 那是一次性动作, 做完就没了, 状态文件里没有它的痕迹。
    """
    if action == "mode":
        want = (args[0] if args else "").strip().lower()
        return lambda p: ((p.get("engine") or {}).get("persona") or "") == want
    if action == "pause":
        return lambda p: bool((p.get("engine") or {}).get("paused"))
    if action == "resume":
        return lambda p: not bool((p.get("engine") or {}).get("paused"))
    if action == "stop":
        # 收工之后心跳就断了(引擎从 run() 返回, 进程跟着退)
        return lambda p: not _running(p)[0]
    return lambda p: False


# ---------------------------------------------------------------- 子命令
def cmd_status(a) -> int:
    payload = st_mod.read()
    run, age = _running(payload)
    env = envelope(payload, "status", status=payload)
    human = human_status(payload, run, age)
    _emit(env, a.json, human)
    if (not run) and a.require_engine:
        return EXIT_NO_ENGINE
    return EXIT_OK


def human_status(payload: dict, run: bool, age) -> str:
    if payload is None:
        return "[ctl] 没有 %s —— 引擎没在跑过(或从没写过)。" % st_mod.path()
    eng = payload.get("engine") or {}
    tag = []
    if eng.get("persona"):
        tag.append(eng["persona"])
    if eng.get("paused"):
        tag.append("已暂停")
    head = "[ctl]"
    if not run:
        head += " ⚠ 引擎没在跑(这份是 %.0f 秒前的)" % (age or 0)
    elif tag:
        head += " (" + " ".join(tag) + ")"
    d = st_mod.data_age(payload)
    if run and d is not None and d > st_mod.STALE_AFTER:
        head += " (引擎在跑, 读数 %.0f 秒前的)" % d
    return "%s %s" % (head, payload.get("say") or "(没法说)")


def _mode_names() -> list:
    """可选人格名 —— **打印出来的必须是能直接敲的那几个字**。

    ☠ 这里踩过: `Mode` 是 `class Mode(str, Enum)`, 而 Python 3.11 下
      `"%s" % Mode.CLUMSY` 得到的是 **`"Mode.CLUMSY"`**(不是 `"clumsy"`) ——
      照着自己打印的清单敲 `ctl.py mode Mode.CLUMSY` 会被拒。必须取 `.value`。
    """
    try:
        from modes import Mode
        return sorted(m.value for m in Mode)
    except Exception:                                            # noqa: BLE001
        return ["clumsy", "coop", "sabotage"]


def _enum_key(mapping: dict, name: str):
    """`PROFILES` 的键是 `Mode` 枚举, 用字符串名去取会取不到 —— 兜一层。"""
    for k in mapping:
        if getattr(k, "value", None) == name or str(k).lower().endswith(name):
            return k
    return name


def cmd_modes(a) -> int:
    try:
        from modes import PROFILES
    except Exception:                                            # noqa: BLE001
        PROFILES = {}
    rows = []
    for m in _mode_names():
        pf = PROFILES.get(m) or PROFILES.get(_enum_key(PROFILES, m))
        num = ""
        if pf is not None:
            num = " [失误 %.0f%% / 使坏 %.0f%%]" % (pf.fumble_rate * 100,
                                                    pf.mischief_rate * 100)
        rows.append({"mode": m, "blurb": _MODE_BLURB.get(m, ""), "profile": num.strip()})
    payload = st_mod.read()
    env = envelope(payload, "modes", modes=rows)
    human = "[ctl] 可选人格(用 `ctl.py mode <名字>` 切, 下一决策点生效):"
    for r in rows:
        human += "\n        %-9s %s%s" % (r["mode"], r["blurb"], r["profile"])
    human += "\n      当前: %s" % (env["persona"] or "(读不到)")
    _emit(env, a.json, human)
    return EXIT_OK


def cmd_mode(a) -> int:
    try:
        from modes import Mode
        valid = [m.value for m in Mode]
    except Exception:                                            # noqa: BLE001
        valid = ["coop", "clumsy", "sabotage"]
    want = (a.name or "").strip().lower()
    if want not in valid:
        env = {"ok": False, "action": "mode", "error": "unknown_mode",
               "got": a.name, "valid": valid}
        _emit(env, a.json, "[ctl] ✗ 没有这个人格: %r —— 可选: %s"
              % (a.name, ", ".join(valid)))
        return EXIT_USAGE
    return _send("mode", "mode", [want], a.json, a.wait)


def cmd_do(a) -> int:
    if not a.action:
        _emit({"ok": False, "action": "do", "error": "missing_action"}, a.json,
              "[ctl] ✗ 要指定做什么, 例如: ctl.py do wash / do fetch Flour")
        return EXIT_USAGE
    if a.wait:
        print("[ctl] ⚠ `do` 是**一次性动作**, 等不了它生效(状态文件里没有它的痕迹) —— "
              "已忽略 --wait", flush=True)
    return _send("do", "do", [a.action] + list(a.args or []), a.json, 0.0)


def main() -> int:
    # `--json` 挂到**每个**子解析器上, 这样 `ctl.py status --json` 和
    # `ctl.py --json status` 两种写法都能用 —— 外部调用方不该被迫记住位置。
    #
    # ☠ 两个 argparse 的坑, 都踩过:
    #   ① 子解析器会拿**自己的默认值覆盖**父解析器已经设好的值 —— 于是
    #      `--json status` 写完就被子解析器改回 False, 输出变成人话, 调用方解析炸。
    #      解法: 子解析器那份 `--json` 用 `default=SUPPRESS`(没显式传就不碰这个属性)。
    #   ② 一个子命令都不给时(argparse 不进任何子解析器), 子解析器上的参数**根本不存在**
    #      ⇒ `a.watch` 直接 AttributeError。解法: 在顶层 `set_defaults` 补齐。
    top = argparse.ArgumentParser(add_help=False)
    top.add_argument("--json", action="store_true", help="机器可读(统一外壳)")
    sub_p = argparse.ArgumentParser(add_help=False)
    sub_p.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                       help="机器可读(统一外壳)")

    ap = argparse.ArgumentParser(
        description="切状态 + 读情况 —— 一个入口两个方向(见文件头的契约)",
        parents=[top])
    ap.set_defaults(watch=None, require_engine=False)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("status", parents=[sub_p], help="这局什么情况(默认)")
    p.add_argument("--watch", type=float, nargs="?", const=3.0, default=None,
                   help="每 N 秒重打(默认 3)")
    p.add_argument("--require-engine", action="store_true",
                   help="引擎没在跑就退出码 2(编排层轮询用)")

    sub.add_parser("modes", parents=[sub_p], help="列出可选人格")

    p = sub.add_parser("mode", parents=[sub_p], help="热切换人格")
    p.add_argument("name")
    p.add_argument("--wait", type=float, nargs="?", const=3.0, default=0.0,
                   help="等它生效, 最多 N 秒(默认不等)")

    p = sub.add_parser("do", parents=[sub_p], help="指定下一步(一次性)")
    # `nargs="?"` 而不是必填: 让**我们**给出"要指定做什么"的提示并退 1,
    # 而不是让 argparse 甩一段 usage 退 2 —— 外部调用方认的是我们的契约。
    p.add_argument("action", nargs="?", default=None)
    p.add_argument("args", nargs="*")
    p.add_argument("--wait", type=float, nargs="?", const=3.0, default=0.0)

    for name in ("pause", "resume", "stop"):
        p = sub.add_parser(name, parents=[sub_p],
                           help={"pause": "暂停(松手)", "resume": "继续",
                                 "stop": "收工(引擎正常退出)"}[name])
        p.add_argument("--wait", type=float, nargs="?", const=5.0, default=0.0,
                       help="等它生效, 最多 N 秒(默认不等)")

    a = ap.parse_args()
    name = a.cmd or "status"
    try:
        if name == "status":
            if a.watch:
                while True:
                    cmd_status(a)
                    time.sleep(a.watch)
            return cmd_status(a)
        if name == "modes":
            return cmd_modes(a)
        if name == "mode":
            return cmd_mode(a)
        if name == "do":
            return cmd_do(a)
        return _send(name, name, [], a.json, a.wait)
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
