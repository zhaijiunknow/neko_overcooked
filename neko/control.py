"""**外部控制通道** —— 一个命令文件, 跑着的引擎每轮读一次、读完清空。

命令(一行一条, `#` 之后忽略):

```
mode coop|clumsy|sabotage      热切换人格(下一个决策点生效, 不用重开一局)
do <action> [target]           **指定下一步做什么**(绕过评分, 只做一次)
pause / resume                 暂停(松手; 世界继续跑)
stop                           收工(引擎干净退出)
```

为什么要这条通道: 一局只有 150 秒, 而**测试一次行为要开一局** ——
想验"捣蛋鬼会怎样""它到底会不会洗盘子", 只能靠改代码重启。
现在可以在**同一局里**下命令, 把三种人格、各种动作当场跑一遍。

⚠ **为什么用文件而不是 socket**: 引擎是**单进程的同步循环**(每轮 sleep 0.5s),
下命令的是另一个终端里的一次性脚本 —— 文件是两边都不用改架构就能接上的最小通道
(游戏里的 `run_engine.py` 已经在跑, 不能为了收命令把它改成 server)。
**只有引擎读、外部写**, 不存在两边同时写的竞态。

⚠ 路径可用 `NEKO_CMD_FILE` 覆盖(默认 `<仓库>/runtime/cmd.txt`)。

> **外部要用, 走 `tools/ctl.py`** —— 那是"下命令 + 读状态"的**统一入口**
> (同一个进程级脚本、同一套 `--json` 外壳)。本模块是它的底层实现,
> 直接 import 用也可以(宿主是 Python 时更省一次进程启动)。
"""
from __future__ import annotations

import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def path() -> str:
    return os.environ.get("NEKO_CMD_FILE") or os.path.join(_ROOT, "runtime", "cmd.txt")


def send(text: str) -> str:
    """**外部脚本用**: 追加一条命令。返回写到了哪个文件(给日志用)。"""
    p = path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
    except Exception:
        pass
    with open(p, "a", encoding="utf-8") as f:
        f.write(text.strip() + "\n")
    return p


def take() -> list:
    """**引擎每轮用**: 取走当前所有命令并清空文件。

    ⚠ **清空而不是删除**: 删掉的话, 外部那一下 `open(..., "a")` 正好落在
      "删了还没建"的缝里, 命令就丢了(报错倒不会, 是静默丢)。
    """
    p = path()
    try:
        with open(p, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return []
    except Exception:
        return []
    if not raw.strip():
        return []
    try:
        with open(p, "w", encoding="utf-8"):
            pass                                   # 清空
    except Exception:
        return []                                  # 清不掉就别执行(免得重复执行)
    out = []
    for ln in raw.splitlines():
        ln = ln.split("#", 1)[0].strip()
        if ln:
            out.append(ln)
    return out


def parse(line: str) -> tuple:
    """`"do fetch Flour"` → `("do", "fetch", "Flour")`; `"stop"` → `("stop", "", "")`。"""
    parts = (line or "").split()
    if not parts:
        return ("", "", "")
    return (parts[0].lower(),
            parts[1] if len(parts) > 1 else "",
            " ".join(parts[2:]) if len(parts) > 2 else "")
