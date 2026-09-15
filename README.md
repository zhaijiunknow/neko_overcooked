# neko_overcooked

> ## ⚠️ 改代码之前，先读 [`docs/开发约定.md`](docs/开发约定.md)
> **第一条硬规则：改游戏交互 / 数值 / 机制之前，必须先在 `overcooked_decomp/` 里
> 把相关代码读出来、带行号写进注释，再写代码 —— 不要猜。**
> 这个项目里每一次"猜"都错了，而且都伪装成别的问题（该文档里有全部真实案例对照表）。

《胡闹厨房 2》(Overcooked! 2) 自动化脚本 —— **BepInEx 插件 + Python 外部驱动**。
目标是让两个厨师在全自动脚本下通关：读订单、读配方、自己切菜煮菜、摆盘送餐，
并且**不撞墙、不淹死、不踩空**。

> ⚠️ **免责声明**
> 本项目是非官方个人研究项目，与 Team17 / Ghost Town Games 无关。
> 使用需要你**自行拥有正版游戏**。
> 仓库内**不包含**任何游戏本体资源、模型、音频，也**不包含**反编译得到的游戏源码
> （原因见 [重新生成反编译源码](#重新生成反编译源码)）。
> 请勿将本项目用于联机对战或任何影响他人游戏体验的场景。

---

## 这是什么 / 不是什么

**是** —— 一套"把游戏内部状态读出来、再用模拟键盘把操作打回去"的闭环工具。
插件在游戏进程内读网格、台面、订单、配方、危险区；Python 侧做规划，
然后用 `SendInput` 模拟键盘（P1 用 WASD 区、P2 用方向键区，游戏原生支持分屏双键盘）。

**不是** —— 不是内存修改器，不改游戏逻辑，不做注入式作弊。
所有操作都等价于一个手速稳定、不会累的玩家在按键。

---

## 环境要求

| 项 | 说明 |
|---|---|
| 游戏 | Overcooked! 2（Steam appid 728880） |
| 游戏运行时 | **Unity 2017.4 + Mono（x86）**，不是 IL2CPP。CLR 2.0.50727 |
| Mod 加载器 | BepInEx **5.4.23.5 win_x86**（`tools/BepInEx_win_x86.zip`） |
| 编译 | .NET SDK（用 `csc.dll` 直接编译，无需 VS/MSBuild） |
| 脚本 | Python 3.8+（开发用 3.13），纯标准库，**零第三方依赖** |
| 系统 | Windows（脚本侧用 `ctypes` 调 `SendInput`） |

---

## 构建与部署

1. 把 BepInEx 解压到游戏根目录，先运行一次游戏让它生成 `BepInEx/plugins/`。
2. 改 `build.bat` 顶部的两个路径：
   ```bat
   set GAME=E:\SteamLibrary\steamapps\common\Overcooked! 2\Overcooked2_Data\Managed
   set BEP=%~dp0tools\BepInEx_x86\BepInEx\core
   ```
3. 编译：
   ```bat
   build.bat
   ```
   > 关键点：引用的是 **`C:\Windows\Microsoft.NET\Framework\v2.0.50727`**（.NET 2.0），
   > **不是** `v4.0.30319`。游戏的 CLR 是 2.0，用 .NET 4 的引用会编译通过但加载时报
   > `Method not found: 'System.Threading.Monitor.Enter'`。
4. 把 `build/Overcooked2AI.dll` 复制到
   `<游戏目录>\BepInEx\plugins\Overcooked2AI.dll`。
5. 启动游戏。插件会在 **127.0.0.1:48778** 起一个 TCP 桥（行 JSON 协议）。

---

## 使用

> **跑脚本时电脑照样能用。** 默认策略下脚本**绝不抢焦点**：你切出去干别的，
> 它会自动暂停并松开所有按键；切回游戏自动继续。想让脚本自己抢焦点（旧行为）
> 用环境变量 `NEKO_FOCUS=always`。急停键默认 **F12**（只读按键状态，不影响你在
> 游戏里的操作），可用 `NEKO_PANIC_KEY` 换。
>
> 原因：`SendInput` 是系统级注入，键会发给**当前前台窗口**，没法指定目标窗口。
> 所以只能在"游戏就是前台"时发键 —— 否则键会打进你正在用的别的程序里。

```bat
:: 先看这一关长什么样（网格 + 危险区 + 机关），强烈建议每次都先跑这个
:: ⚠ **一律带 --watch**（用户要求），而且它要长连接，别包一层"断了自己重连"的外壳
python -u tools\mapview.py --watch

:: 盯着地图/台面怎么变（实时刷新，排查"东西怎么不见了"）
python -u tools\gridwatch.py

:: 问"这一格为什么过不去 / 这个台面为什么够不着"
python -u tools\whyreach.py
python -u tools\pathtest.py

:: 单人自动做菜
python -u run_engine.py
python -u run_engine.py --dry              :: 只规划不驱动，打印"这单要怎么做"
python -u run_engine.py --mode none        :: **纯执行, 一个失误都不演** —— 调寻路/调流程必须用这个
python -u run_engine.py --mode sabotage     :: 三模式: coop | clumsy | sabotage

:: 双人（两个引擎线程，共用一个订单黑板避免抢活）
python -u run_team.py
```

**看护（开着不用管）** —— 轮询游戏状态, 大厅缺 P2 就补, 进对局自动拉起 `run_engine.py`,
对局结束把它收掉(**一局一连**):

```bat
python -u run_watch.py
```

> - ⚠ **它会把游戏切到前台**(启动时、以及每次要按 A 之前)。这不是顺手 —— 大厅里
>   还没装虚拟手柄, `runInBackground` 没打开, **游戏一失焦 Unity 主循环就停**,
>   那时候按 A 不生效、`get_state()` 也冻在旧快照上。`NEKO_WATCH_FOCUS=0` 可关。
>   (代价: 起完脚本后终端会失去焦点; 要 Ctrl+C 得先点回终端。)
> - **收尾走 `CTRL_BREAK_EVENT`(等价 Ctrl+C), 不 kill** —— 引擎的 `finally` 里要跑
>   `pad.uninstall()`; 不跑的话那只厨师的输入**持续被虚拟手柄接管**, 直到重开游戏。
> - `tools\joinp2.py` 现在会**先检查是不是已经双人, 是就跳过** ——
>   **A 是"加入下一个玩家"不是 toggle**, 按多了会引进第三个人。
>   判据来自游戏自己的 `ClientUserSystem.m_Users`(反编译依据见 `SceneScanner.ScanUsers`)。
> - 环境变量: `NEKO_WATCH_INTERVAL` / `NEKO_WATCH_MISSES` / `NEKO_WATCH_HEARTBEAT` /
>   `NEKO_WATCH_FOCUS` / `NEKO_WATCH_PY`。


> ⚠ **`--mode none` 是调 bug 时的默认档**。`coop` 也带"低概率自然失误"（发呆/绕路/多切/忘盘），
> 拿它测出来的卡顿**分不清是 bug 还是演的**。

外部控制（**不用重启脚本**，`neko/control.py` 那个命令文件）：

```bat
python -u tools\ctl.py mode clumsy      :: 热切人格
python -u tools\ctl.py do wash          :: 指定下一步做什么
python -u tools\ctl.py pause / resume / stop
```

### 桥协议（`neko/bridge/client.py`）

每行一个 JSON，请求 `{"cmd": "...", ...}`：

| cmd | 作用 |
|---|---|
| `state` | 状态快照（场景/是否在局/厨师/台面/烹饪进度/配方池） |
| `orders` / `live` | 订单（含剩余时间比例） |
| `know` | 食材知识表：每个食材/箱子/厨具的加工方式 |
| `raw` | 全量物体清单（带 Collider 的物体 + 自定义组件） |
| `map` | **整张关卡网格 + 危险区 + 空洞 + 平台**（见下） |
| `dyn` | **机关/陷阱**：按钮、传送带方向、触发机器、平台、着火、关卡变形 |
| `path` | 问游戏原生 `GridNavSpace` 寻路（**仅兜底，理由见下**） |
| `action` | 入队一个动作，由主线程执行 |

---

## 逆向文档

`docs/关卡逆向/` 是为写这套脚本而做的系统性逆向，**569 KB / 9 篇，全部结论带
`文件名.cs:行号` 引用**，未验证的一律标注「未验证」：

| 文档 | 内容 |
|---|---|
| 01 网格系统与边界重生 | 网格/占用物、KillPlane 与越界、重生时序（≈6 秒） |
| 02 危险物与动态关卡变换 | 火灾的四条点火源与灭火路径、潮水/木筏等动态变换 |
| 03 移动平台传送带与玩家运动学 | 玩家速度链、平台"谁在驾驶"、传送带速度叠加 |
| 04 关卡配置与对局流程 | 状态机、计分与连击规则、"结束→下一局"链路 |
| 05 触发器机关系统内核 | 字符串触发总线、8 类触发条件、发现机关被触发的手段 |
| 06 按钮开关与传送带方向 | 按钮怎么按、传送带方向怎么读、开关↔传送带的连线在哪一层 |
| 07 消失平台与物件销毁 | 荷叶等"会消失的地面"的物理真相与运行时探测方案 |
| 08 移动危险物与打滑推挤 | 打滑、击退量级、**该直接拒玩的关卡清单** |
| 09 分屏双键盘键位权威表 | 两套键盘绑定表的区别、三跳推导出 P1/P2 的确切按键 |

根目录另有两份总纲：`胡闹厨房2-玩法核心逻辑逆向.md`（订单/配方/烹饪/摆盘/计分）、
`胡闹厨房2-全脚本通关方案v1.md`（整体设计）。

---

## 硬核踩坑速查

这一节是本项目最贵的部分 —— 每一条都是"看起来没问题、实际必然失败"的坑。

### 寻路

- **`GridNavSpace` 不是给厨师用的**。它的 `m_nodeMap` 全工程**只被老鼠 NPC 的
  `GridNavigator` 消费**（`GridNavigator.cs:18`），而且**只在 `Start()` 建一次、永不刷新**
  （`GridNavSpace.cs:44-56`）。移动平台的出生格会被这份快照**永久记成墙**，
  平台后来驶入的格却仍是"可走" —— 两个方向都错。
- **可走判定只有一条**：`m_nodeMap[x,z] = (GetGridOccupant(index) == null)`。
  而**水面不是占用物**（它是 `RespawnCollider` 触发器），所以原生寻路会把水面当可走格，
  直接横穿过去把厨师淹死。
- 正确做法：自己读 `GridManager` 的公开 API 建图 + 向下射线判地面 + 按 tag 分类，
  然后把 `MovingPlatform` / `Travelator` 视作可站格、水面/岩浆视作禁行。

### 交互

- **交互半径是 1.0，量的是到碰撞体「表面」的距离**
  （`PlayerControls.cs:745` → `GetCollidersInArc(1f, PI, ...)`）。
  停在 1.8 格处按键是够不着台子的。
- **交互判定是朝向敏感的**：`IsColliderInArc` 用
  `Dot(transform.forward, 指向目标) >= cos(PI/2) == 0`，**只认前方 180° 半圆**
  （`InteractWithItemHelper.cs:153-163`）。而厨师的面朝方向 = **它最后一次移动的方向**，
  绕路过来很可能是背对的 —— 这时按交互键完全没反应，日志只会显示"持有物未变"。
- 交互是 **JustPressed 边沿触发**，必须"按下 → 松开"，长按不会连续触发。

### 运动与按键

- `PlayerControls.Movement.RunSpeed = 4f`（`PlayerControls.cs:28`），且平地水平速度是
  **每帧直接赋值**的 ⇒ 没有加速度、没有惯性、没有刹车。
  **位移 = 4 × 按住秒数，1 格(1.2u) 正好 0.30 秒。**
- 位移方向取自**输入向量**，与厨师朝向无关 —— 所以轻点一下就能"转头"。
- 键位有**两套绑定表**，别搞混：`GetDefaultCombinedKeyboardBindings()`（一个键盘当一只手柄）
  和 `GetDefaultSplitKeyboardBindings()`（一个键盘拆成两个虚拟手柄，**本项目用这套**）。
  详见 09 号文档。另外游戏实际用的是 `m_UserKeyboardBindings`（玩家自定义键位），
  所以 `probe_bindings()` 实测兜底必须保留。

### 死亡 / 重生

- 重生全程 ≈ **6 秒**（`m_respawnTime` 5s + 粒子 1s），期间 `PlayerControls.m_bRespawning == true`，
  **按键完全无效**。把它当成"卡住"去按侧移，只会把超时耗光。
- 只判 `m_bRespawning` 也不够：**过场压制**（`IsSuppressed()`）和**喷灭火器**
  （`MovementScale` 被设成 0）一样按不动。
- **任何一次死亡都会强制切换该手柄的活跃厨师**（`ClientPlayerRespawnBehaviour.cs:177-190`
  → `PlayerSwitchingManager.cs:118-125`），之后按键驱动的是**另一只厨师而且不报错**。

### 会变的地面

- "荷叶踩过就消失"**不是被销毁的**。工程里没有 `LilyPad`/`Lotus` 类，唯一证据是成对音效
  `DLC_13_LilyPad_*_Plunge / _Pop`（`GameOneShotAudioTag.cs:317-321`）——
  说明它踩下去会沉、之后还会浮回来，物理上就是**碰撞体跟着下沉动画走低**。
  于是**向下射线全程都有命中**，只判"有没有命中"的探测会一路认为这格能走，
  直到最后一刻才发现，而游戏给的反应窗口只有 **0.2 秒**
  （`m_timeBeforeFalling`，`PlayerControls.cs:270`）。
  → 必须校验**落点高度**（本项目的做法：低于 `StepHeightMax = 0.65` 就判不可走）。

### 假成功（这一族**最贵**，因为它伪装成"卡住"）

判据只数次数、不看回执，就会报 `✓` 而实际上什么都没发生。**代价不是少做一步，是整单再也无法重规划**——
`pending` 是按"做成了才销号"记账的，**销了号就回不去**，后面每一步的 `不可达(手空…)` 都是它的余波。

- **`op_chop`**：`base`（板上读到的名字）为空时，"按满刀数就收工"这条兜底**没有任何前提**。
  板是空的 ⇒ 游戏的 `m_interactable` 是 null ⇒ `use` 没有目标 ⇒ 十几下全是空按，却报 `✓`。
  收尾那句 `interact("pickup", verify_hold_change=True)` 又把**放下**当成了**拿起**
  （判据只是"持有物变了没有"，放下也会让它变）。
- **`op_fetch` 的报成功处**必须打出手上到底是什么 —— 它有三个 `return True`，不打就分不清是哪条路。
- **`work` 杂活**（`op_work`）按一下交互键就返回，判据挂在 `verify_hold_change=False` 上，**天然不校验**。

### 交互的两条硬几何

- **生料上不了盘**。`PlacementContainer.CanCombine`（`PlacementContainer.cs:7`）要求
  `CanAddIngredient`；台面空的时 `AttachStation.CouldAttachToSelfIfEmpty`（`:89`）要求 carried item
  有 `IAttachment`。生料两处都过不去 ⇒ 游戏回 `placeCanHandle=false`，**放置根本不会发生**。
  用户原话："想要放在盘子上需要**先煮熟**"。判据用"知识表里还有没有 `next`"，**不是比名字**。
- **斜角够不着**。`IsColliderInArc`（`InteractWithItemHelper.cs:153`）量的是**到碰撞体表面**的距离
  （半径 1.0）+ 朝向前 180°。斜角格心距 1.70 格 > 1.0 —— 但那是**格心距离**，对**长柜台**的另一头
  并不成立；而真正的外层 `GetCollidersInArc` 还带 `_gridSelection`（按朝向给候选格打分，
  **会把锁吸到旁边那块台面上**）。所以"站位判据"照 `Engine._stand_cell_of(ortho_only=True)` 走，别自己算。

### 两个人不能同时用一个台面

`_pick_board` / `pick_assemble_spot` **以前完全不看队友** ⇒ 挑中人类正在用的那块板，走过去挤、
`placeh` 为空、失败，而脚本不知道原因（表现为"排队"而不换一块）。
判据：**交互半径 1.0 ⇒ 站在台面旁边的人才用得了它** ⇒ 队友坐标在 1.6 格内就当"他在用"。
⚠ 队友坐标**本来就在地图上**（`state.layout.chefs`）—— 不需要任何新通信。
⚠ **必须留兜底**：全被占着时退回不排除的那份，否则"唯一一块板有人在用"会变成卡死。

### 失败要分「哪一类」

`_last_fail_kind` 这个钩子一直在，但长期**只被赋过一个值** `"death"`，其余原因只进日志、不进控制。
现在分三类，处置**互相冲突**，混在一起就只能"重试 3 次 → 废掉整单"：

- **`KIND_BRANCH`** —— 这一支不行（板是空的 / 走不到 / 对不齐 / `placeCanHandle=false`）
  ⇒ **换支**，不原地重试，**也不算整单失败**
- **`"death"`** —— 摔死到上限，路本身走不通 ⇒ 不原路重试
- **`""`（空）** —— 执行抖动（差 1 格、被推开、被抢）⇒ 值得原地重试

⚠ **冷板凳的 key 必须带台面**：原来 `f"{action} {target}"` 不含台面 ⇒ 一块板走不到就封掉**所有**板。
⚠ **分支失败要把 op 放回 `pending`** —— 选中那一刻已经销了号，不放回去重选时它根本不在池子里。

### 地图字符表（`map` 命令）

```
.  可走          #  被墙/橱柜/台面占住
F  火焰危险物    P  移动平台(能站, 会动)
T  传送带        H  危险区(水面/岩浆/边界墙)
V  空洞(脚下没地面)
v  地板太低(单向落差 / 正在下沉的平台, 例如荷叶)
```

---

## 已知限制

子代理逆向给出的 **A 级"原理上不应自动游玩"** 关卡（按键是开环控制，这些机制无法可靠应对）：

- **陨石关** —— 落点时刻与格子**双随机**（`MeteorManager.cs:32,40-42`）
- **弹射物关** —— 落点顺序随机，还可能顺手在随机格点火
- **车辆关**（`RespawnType.Car`）—— 接触即死，车辆时序在 C# 中完全不可读
- **荷叶关（DLC13）** —— 同一格可站立性反复翻转，没有可靠落脚点

**B 级"可玩但必须补偿"**：打滑区（输入完全失效 ≈0.8~1.2s）、冰面（按键只占 1.7% 权重）、
传送带（按 `m_speed` 加减时长）、风区。

另外：`tools/BepInEx_win_x86.zip` 是 BepInEx 的再分发，遵循其自身许可证（LGPL-2.1）。

---

## 重新生成反编译源码

本项目所有逆向结论都来自反编译源码，但**源码本身不随仓库分发** ——
它是 Team17 的专有代码，公开传播会构成侵权。

你可以自己从**你拥有的正版游戏**重新生成（本项目用 ilspycmd 9.1）：

```bat
:: 1. 安装反编译器
dotnet tool install -g ilspycmd

:: 2. 反编译游戏主程序集（注意游戏是 Mono/x86，目标是 Assembly-CSharp.dll）
ilspycmd -p -o overcooked_decomp ^
  "E:\SteamLibrary\steamapps\common\Overcooked! 2\Overcooked2_Data\Managed\Assembly-CSharp.dll"
```

`-p` 会按命名空间建子目录，产出约 2366 个 `.cs` 文件 / 5.2 MB。
生成后放在仓库根目录的 `overcooked_decomp/` 即可（该目录已在 `.gitignore` 中）。

---

## 离线读关卡资产（不用进游戏）

这一层是从**文件**里拿数据，不必让游戏跑起来 —— 排查问题和做批量分析时快得多。

> ⚠ **下面点名的那几个脚本（`dump_level_objects.py` / `parse_unity_tables.py` / `bundle_index.py`）
> 已随 `5123360` 一起清掉了**，命令跑不了。但这一节里的**结论仍然是权威的**
> （tag 怎么编码、关卡在哪儿、台面的角色写在 tag 上而不是组件上），照着写脚本就行。

### 关卡在哪

整机只有一个场景 `Assets/Scenes/Boot.unity`，**所有关卡都是运行时从 AssetBundle 加载的**，
就在：

```
Overcooked2_Data\StreamingAssets\Windows\
  s_sushi_4_1   5.73 MB      ← 文件名 == 桥报的 scene 名
  s_sushi_4_5  10.18 MB
  movingplatform2 ~ 5 / s_beach_* / s_chinatown_* / worldmap ...
```

### tag 表和 layer 表

`tools/parse_unity_tables.py` 直接从 `globalgamemanagers` 里按"长度前缀字符串"顺序解析出
工程完整的 tag 表和 32 个 layer 槽位（不能靠正则捞，否则拿不到 layer 下标，而下标就是位掩码的位数）。

### 关卡内容清单

```bat
pip install UnityPy
python -u tools\dump_level_objects.py --list              :: 列出所有关卡包
python -u tools\dump_level_objects.py s_sushi_4_1 --detail :: 详细清单
python -u tools\dump_level_objects.py s_sushi_4_1 --summary :: 一行结论(便于批量扫)
python -u tools\bundle_index.py                            :: 各包大小与内嵌资源名
```

输出示例（`s_sushi_4_1`，与运行时扫描结果**逐项吻合**）：

```
[Plate]           ×3     Plate 5 (1)(2)(3)
[PlateReturn]     ×1     workstation_plate_return
[Crate]           ×1     dispenser_crate_01
[CookingUtensil]  ×2     utensil_pot_01
[ChoppingStation] ×2     countertop_01_chopping_board_wood_...
[PlateStation]    ×1     workstation_plate_station
[CookingStation]  ×2     workstation_cooker_01
组件层另有: ConveyorStation×8  RespawnCollider×7  Flammable×15  RubbishBin×2  WashingStation×1
```

### tag 的编码方式（踩过的坑）

AssetBundle 里 `GameObject.m_Tag` 是**下标**，名字不在包里：

- 内置 tag 的实际下标**不连续**：`0=Untagged 1=Respawn 2=Finish 3=EditorOnly 5=MainCamera 6=Player 7=GameController`
  （实测 `Player 1..4` 是 6、`Camera` 是 5、`CampaignGameEnvironment` 是 7，中间空了一位）
- **自定义 tag 用 `20000 + 下标`**：`Plate=20000`、`PlateReturn=20002`、`Crate=20006`、
  `CookingUtensil=20007`、`ChoppingStation=20008`、`PlateStation=20009`、`CookingStation=20012`

### 台面的角色写在 tag 上，不是组件上

这点很关键（也是绕了一圈才确认的）：**光看组件类型分不出"这个台面是灶台还是回收台"**。
游戏的 `ServerUtensilRespawnBehaviour.cs:123` 就是这么判的 ——
`CompareTag("CookingStation")` / `("PlateReturn")` / `("PlateStation")`
加 `RequestComponent<RubbishBin/ConveyorStation/WashingStation>()`。

所以分类是**tag + 组件混合**：

| 角色 | 靠什么 |
|---|---|
| 灶台 / 送餐口 / 盘子回收 / 菜板 / 食材箱 / 锅 / 盘子 | **tag** |
| 垃圾桶 / 洗手池 / 台面传送带 / 按钮 | **组件** |

`GameUtils.cs:504-707` 是游戏自己的"读地图" API，也全是「按 tag 取一批 + 按组件筛」：
`GetIngredientCrates("Crate")`、`FindEmptyContainers("Plate")`、`GetPlayerHeldItems("Player")`、
`GetAllIngredients("Pre-Ingredient"|"Ingredient")`。

---

## 目录结构

```
Overcooked2AI/Game/        BepInEx 插件 (C#)
  Plugin.cs                入口, 主线程状态刷新
  BridgeServer.cs          TCP 桥 (行 JSON)
  StateCollector.cs        主线程"请求-执行"任务泵
  SceneScanner.cs          台面/厨师/烹饪扫描
  LevelInfo.cs             整张关卡网格 + 危险区 + 空洞   ← 寻路的地基
  InteractiveScan.cs       机关/陷阱扫描 (按钮/传送带/触发机器/火)
  NavPath.cs               原生寻路封装 (仅兜底)
  OrderCapture.cs          订单读取
  RecipeReader.cs          配方树读取
  ItemKnowledge.cs         食材知识表
  ActionExecutor.cs        动作队列
neko/                      Python 侧
  bridge/client.py         桥客户端
  bridge/keyboard_input.py SendInput 键盘模拟 + 窗口激活 (兜底输入层)
  bridge/virtual_pad.py    **默认输入层**: 游戏内虚拟手柄, 按玩家身份接管, 后台也能跑
  engine.py                单个厨师的完整引擎 (导航/交互/各 op)
  terrain.py               关卡网格模型 + 避开危险格的 A*    ← 寻路的地基
  map_model.py             台面/厨师语义模型
  cookbook.py              配方推导 (食材 → 加工链 → 成菜)
  scoring.py               四项评分 (可达/距离/顺路/推进) + 让位判定  ← 决定"选哪个 op"
  world.py                 **共享世界**: 一张地图 + 两只厨师的实时位置, 双人共用一份
  control.py               外部命令通道 (mode/do/pause/resume/stop), run() 每轮读一次
  status.py                给外部报输赢的 **status.json** 通道 (快照在 C# 侧算)
  pathing.py               网格换算 + 运动学常量
  team.py                  双人订单黑板
  modes/                   三模式 (合作/失误/捣蛋) 个体状态机
tools/                     诊断与观测工具 (mapview / gridwatch / whyreach / pathtest / wind_probe …)
docs/关卡逆向/              9 篇逆向文档
runtime/                   **gitignore** —— 运行期产物 + 自己写的临时验证脚本
```

> ⚠ **输入层的默认值是"虚拟手柄", 不是键盘**（2026-09-14 改）。键盘注入发的是**真的系统按键**,
> 双人时玩家也在用键盘, 两边会**互相抢** —— 实测出现过"脚本在玩玩家那只厨师"。
> 所以装不上虚拟手柄时**不会自动退回键盘**, 而是停下来说清楚。

---

## 测试

```bat
:: 编译（build.bat 用 %~dp0，所以 cwd 无所谓）
build.bat

:: 部署 —— **必须游戏完全退出**：BepInEx 只在启动时读 dll，而且文件会被占用
copy /Y build\Overcooked2AI.dll "D:\Steam\...\BepInEx\plugins\Overcooked2AI.dll"
```

### 离线验证

**仓库里目前没有离线测试套件**（`tests/` 已随 `5123360` 删除，`开发约定` 规则 3 点名的那些工具
也不在了）。要验就**自己写临时脚本** —— 本项目的做法是放 `runtime/`（整个目录在 `.gitignore` 里），
跑完不用清理也不会进仓库。

改动 `engine.py` 里**判据类**的逻辑时，建议照这个模式写：构造一个 `Engine` 子类、
**不调 `Engine.__init__`**（那会去连桥），把 I/O 面（`state`/`pos`/`chef`/`kb`/`navigate_smart`/
`interact`）换成脚本化的桩，然后跑**真的**那个函数。被测代码一行不改。

> ☠ **桩必须给全**，否则判据会静默失效 —— 本仓库踩过：`_mate()` 第一行是
> `if self.world is None: return None`，桩里不给 `world` ⇒ 队友永远是"没有" ⇒
> 所有"队友占用"的判据一次都不触发，而测试照样全绿。

---

## 平台说明

插件是 **x86** 的（游戏是 32 位 Mono），Python 侧用 `SendInput` 走的是
**游戏原生的分屏双键盘**方案，不依赖任何内核驱动或虚拟手柄
（早期试过 ViGEm，需要装内核驱动、且换台电脑就得重装，已放弃）。
这样做的好处是**可移植**：换一台装了正版游戏的 Windows 机器，
解压 BepInEx + 放一个 dll + 装 Python 就能跑。
