"""**状态出口** —— 引擎把"这局现在什么情况"写成一个文件, 外面谁想看谁读。

和 `neko/control.py` **严格对称**:
  · `control.py` —— 外部**写**、引擎读(下命令: mode / do / pause / stop)
  · `status.py`  —— 引擎**写**、外部读(报状态: 分数 / 星级 / 输赢)
两边都用文件通道, 理由同一条: 引擎是个**单进程同步循环**, 不能为了收发消息把它改成
server; 而"只有一边写"就不存在竞态。

**为什么要有这个出口**: 方案文档 D10 写的是"猫娘未来 = 调 `set_mode` 的开关" ——
那条线是"猫娘 → 引擎"。但猫娘只知道**下命令**, 不知道**结果**: 一局打完是赢是输、
几颗星, 她无从得知。这个模块补的就是反方向。

对外契约(宿主/编排层照着读就行, 别去猜内部结构):
    {
      "ts": 1757912345.6, "heartbeat_ts": 1757912345.6,
      "engine": {"scene": "s_sushi_1_3", "persona": "coop", "game_mode": "Campaign"},
      "live":   {...} | null,     # 局中的实时读数, 不在局里就是 null
      "result": {...} | null,     # **最近一次结算**(出局那一下的快照), 没有就是 null
      "say":        "《…》进行中: …",   # 一行中文, 给猫娘直接用
      "say_result": "《…》结束: 2 星过关, …"
    }

⚠ **`passed` 是游戏给的, 不是我们定的**: `stars > 0`
  (依据 `BossLevelOutroFlowroutine.cs:27  m_succeeded = StarsAwarded > 0`)。
  读不到星级就给 `null`, **绝不编一个**。

> **外部要用, 走 `tools/ctl.py`** —— "下命令 + 读状态"的**统一入口**。
> 本模块只负责"组装 / 落盘 / 说人话", 是它的底层实现。
"""
from __future__ import annotations

import json
import os
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 心跳多久算"引擎可能没在跑"(秒)。读者用它决定要不要报旧数据。
STALE_AFTER = 5.0


def path() -> str:
    return os.environ.get("NEKO_STATUS_FILE") or os.path.join(_ROOT, "runtime", "status.json")


def read() -> dict | None:
    """**外部用**: 读当前状态。文件不在/读坏了都返回 None(不抛)。"""
    try:
        with open(path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write(payload: dict) -> None:
    """**引擎用**: 原子写 —— 先写 `.tmp` 再 `os.replace`。

    为什么必须原子: 读者是**另一个进程**(宿主/工具), 它可能正好在我们写到一半时
    打开文件 —— 非原子写会让它读到**半截 JSON**, 那比读不到更糟(读不到还能重试,
    半截 JSON 会让对面以为"格式就是坏的")。`os.replace` 在同一卷上是原子的。
    """
    p = path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
    except Exception:
        pass
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def stale_seconds(payload: dict, now: float = None) -> float | None:
    """**进程还活着吗** —— 心跳是多久以前的(秒)。"""
    if not payload:
        return None
    ts = payload.get("heartbeat_ts") or payload.get("ts")
    if not ts:
        return None
    try:
        return max(0.0, (now if now is not None else time.time()) - float(ts))
    except (TypeError, ValueError):
        return None


def data_age(payload: dict, now: float = None) -> float | None:
    """**这份数据多旧** —— `ts` 是采集时刻, 只有 `publish()` 会动它。

    ☠ 为什么必须和心跳分开: 引擎主循环**一次迭代可能跑十几秒**(导航到厨房另一头、
    等一锅饭熟), 中间一次状态都不写。要是"没写"就等于"引擎挂了", 猫娘会在局中
    被反复告知"引擎没在跑" —— 而它明明正在跑, 只是**手上这份读数有点旧**。
    两件事分开报, 读者才能自己决定: 活着的进程 + 12 秒前的读数, 该信到什么程度。
    """
    if not payload:
        return None
    ts = payload.get("ts")
    if not ts:
        return None
    try:
        return max(0.0, (now if now is not None else time.time()) - float(ts))
    except (TypeError, ValueError):
        return None


#: 本进程最后一次写出去的 payload —— 给心跳线程复用(见 `beat`)。
_last: dict = None


def beat() -> None:
    """**心跳线程用**: 只把"进程还活着"刷一下, **数据一个字不动**。

    不读桥、不碰游戏状态 —— 所以它可以安全地跑在后台线程里(主循环可能正忙)。
    `ts`(数据时刻)保持不变, 只有 `heartbeat_ts` 往前走。
    """
    global _last
    p = _last
    if not p:
        return
    p = dict(p)
    p["heartbeat_ts"] = time.time()
    _last = p
    try:
        write(p)
    except Exception:
        pass


# ---------------------------------------------------------------- 组装
def build(st: dict, prev: dict = None, persona: str = "", paused: bool = False) -> dict:
    """把桥的 `state` 压成对外契约。

    `prev` = 上一次写出去的 payload —— 两个用途:
      · **跨引擎重启保留 `result`**: 快照活在 C# 那一侧, 引擎一重启它那份就空了,
        而"上一局赢没赢"不该因此消失;
      · **暂停时沿用最后一份读数**(见下)。

    `paused` = 引擎因为"游戏不在前台"停手了。这时 `st` 通常是 `None`(焦点闸门在
      取状态**之前**就 `continue` 了), 但不能因此对外谎称"没在对局里" ——
      所以沿用 `prev` 的 `live`, 同时把 `paused` 标出来, 让读者自己决定信不信。
    """
    st = st or {}
    in_round = bool(st.get("inRound"))
    scene = st.get("scene") or ""
    # C# 那两个块: `round` 只在局中有值, `lastResult` 只在出局后有值(见 RoundScore.cs)
    live_raw = st.get("round") if in_round else None
    res_raw = st.get("lastResult") or None

    # ⚠ **在不在对局, 和"读不读得到分数"是两件事**。
    #   老 DLL 没有 `round` 块, 但 `inRound` 照样是 true —— 那种时候也必须给出
    #   `live`(字段全 null), 否则 `say()` 会说"没在对局里", 把"读不到数"说成了
    #   "没在玩"。这是测试里真抓到过的。
    live = _slim(live_raw or {}) if in_round else None
    if live is not None:
        live["in_round"] = True
        live["scene"] = live.get("scene") or scene
    result = _slim(res_raw) if res_raw else None
    if result is None and prev:
        result = prev.get("result") or None      # 跨重启保留
    if paused:
        live = live or (prev or {}).get("live") or None

    # **读不出来时, 原因是什么** —— 这两件事必须分开, 否则外面只会得到一句
    # "没上报分数", 而"没加载新 DLL"和"加载了但反射链断了"要采取的行动完全不同。
    round_why = st.get("roundWhy") or ""
    if not round_why and "round" not in st:
        round_why = "old-dll"          # 连 `round` 这个键都没有 ⇒ 装的还是老 DLL
    has_round = bool(live_raw or res_raw)

    payload = {
        "ts": time.time(),
        "engine": {
            "scene": scene or ((prev or {}).get("engine") or {}).get("scene") or "",
            "persona": persona or "",
            "game_mode": st.get("mode") or "",
            "paused": bool(paused),
            "has_round_data": has_round,
            "round_why": "" if has_round else round_why,
        },
        "live": live,
        "result": result,
    }
    payload["heartbeat_ts"] = payload["ts"]
    payload["say"] = say(payload)
    payload["say_result"] = say_result(payload)
    return payload


def publish(st: dict, prev: dict = None, persona: str = "", paused: bool = False) -> dict:
    """组装 + 落盘, 返回写出去的那份(调用方留着当下一次的 `prev`)。"""
    global _last
    payload = build(st, prev=prev, persona=persona, paused=paused)
    _last = payload          # 心跳线程从这里取(见 beat)
    write(payload)
    return payload


#: C# 报的字段 → 对外字段。**只搬运, 不换算** —— 数字全是游戏自己算的。
_FIELDS = (
    ("score", "score"), ("base", "base"), ("tips", "tips"),
    ("expiredDeduction", "expired_deduction"), ("delivered", "delivered"),
    ("failed", "failed"), ("combo", "combo"), ("comboMaintained", "combo_maintained"),
    ("stars", "stars"), ("nextStarPoints", "next_star_points"),
    ("oneStarScore", "one_star_score"), ("timeLeft", "time_left"),
    ("passed", "passed"), ("seq", "seq"), ("scene", "scene"),
)


def _slim(raw: dict) -> dict:
    out = {}
    for src, dst in _FIELDS:
        out[dst] = raw.get(src)
    return out


# ---------------------------------------------------------------- 人话
def say(payload: dict) -> str:
    """当前这一句 —— 给猫娘/人看的一行中文。"""
    eng = payload.get("engine") or {}
    paused = bool(eng.get("paused"))
    live = payload.get("live")
    if live:
        line = _say_live(live, why=eng.get("round_why") or "")
        # ⚠ 暂停这一句必须说清楚: 数据是"最后一次读数", 不是此刻 —— 否则外面会
        #   拿着几秒前的分数当现状(而且**局可能已经在我们停止读取时打完了**)。
        return ("脚本已暂停(游戏不在前台); 最后一次读数: " + line) if paused else line
    res = payload.get("result")
    if res:
        return say_result(payload)
    scene = eng.get("scene") or ""
    if scene:
        return "没在对局里(脚本待命; 上一个场景 %s)" % scene
    return "没在对局里(脚本待命)"


def say_result(payload: dict) -> str:
    """结算那一句。没有结算就返回空串(调用方可据此判断"没得说")。"""
    res = payload.get("result")
    return _say_result(res) if res else ""


def _say_live(live: dict, why: str = "") -> str:
    scene = live.get("scene") or "?"
    parts = []
    n = _num(live.get("delivered"))
    if n is not None:
        s = "已交 %d 单" % n
        f = _num(live.get("failed"))
        if f:
            s += "、废 %d 单" % f
        parts.append(s)
    score = _num(live.get("score"))
    stars = _num(live.get("stars"))
    if score is not None:
        s = "%d 分" % score
        if stars is not None:
            s += "(%d 星)" % stars
        parts.append(s)
    t = live.get("time_left")
    if isinstance(t, (int, float)):
        parts.append("还剩 %.0f 秒" % t)
    if not parts:
        # 读不到分数 —— **必须说清是哪一种**, 因为要采取的行动完全不同:
        #   · old-dll        ⇒ 装的是老 DLL(或改完没重启游戏)
        #   · no-monitor 之类 ⇒ 新 DLL 加载了, 但反射链某一环断了(标签就是那一环)
        if why == "old-dll" or not why:
            return "《%s》进行中(这一版 DLL 没上报分数 —— 需要重编重部署并重启游戏)" % scene
        return "《%s》进行中(新 DLL 已加载, 但**读分数失败**: %s —— 把这一句发给开发者)" % (
            scene, why)
    line = "《%s》进行中: %s" % (scene, ", ".join(parts))
    # 距离下一颗星还差多少 —— 猫娘中途插话最好用的一句
    if score is not None and stars is not None:
        nxt = _num(live.get("next_star_points"))
        if nxt and nxt > score:
            line += "; 再 %d 分到 %d 星" % (nxt - score, stars + 1)
        elif stars >= 4:
            line += "; 已经满星"
    return line


def _say_result(res: dict) -> str:
    scene = res.get("scene") or "?"
    stars = _num(res.get("stars"))
    passed = res.get("passed")
    if stars is None:
        head = "结束(星级没读到)"
    elif passed:
        head = "%d 星过关" % stars
    else:
        head = "0 星没过关"
    bits = []
    score = _num(res.get("score"))
    if score is not None:
        bits.append("%d 分" % score)
    d = _num(res.get("delivered"))
    if d is not None:
        bits.append("交 %d 单" % d)
    f = _num(res.get("failed"))
    if f:
        bits.append("废 %d 单" % f)
    return "《%s》%s" % (scene, head + ("(" + "、".join(bits) + ")" if bits else ""))


def _num(v):
    """只有真的是数字才返回, 别的(None / 空串 / 老 DLL 缺字段)一律 None —— **不编 0**。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    return None
