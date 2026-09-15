using System;
using System.Collections.Generic;
using System.Reflection;
using System.Text;
using UnityEngine;

namespace Overcooked2AI.Game
{
    /// <summary>L2 采集: 枚举场景台子/厨师。简化版只读布局(类型+名字+位置), 供 Python 规划。</summary>
    public static class SceneScanner
    {
        // 要枚举的台子类型名(全局类, 无命名空间)。
        // 顺序 = 优先级: 一个物体常同时挂多个组件(CookingStation 也 RequireComponent(AttachStation)),
        // 扫描时按 instanceID 去重, 只归入**第一个**命中的类型, 否则同一台子会出现好几条。
        //   PlateStation   = 送餐口(放上"装了菜的盘子"才算送餐)
        //   CleanPlateStack= 干净盘子堆 —— 盘子唯一的来源(PlateStation.m_createPlateTime 是废弃字段, 不放盘子)
        //   Workstation    = 切菜板(负责 chop); AttachStation = 普通台面(只能放/拿)
        //
        // **顺序有语义**: 这个循环是"先匹配到的类型赢"(见 Scan() 里的 seen 去重), 而
        // FindObjectsOfType(基类) 会把子类实例也返回, 所以**派生类必须排在基类前面**。
        // 实测踩到: HeatedCookingStation : CookingStation, 原来 "CookingStation" 排在前面,
        // 于是加热灶台全被记成普通灶台, 子类型信息直接丢了。
        //
        // 清单来源: MultiplayerController.m_EntitySerialiser.AddSynchronisedType(...)
        //   (MultiplayerController.cs:273-556) —— 游戏自己登记的全部可同步玩法对象。
        private static readonly string[] StationTypes =
        {
            // 盘子体系
            "PlateStation",          // 送餐口
            "CleanPlateStack",       // 干净盘子堆(取盘)
            "DirtyPlateStack",       // 脏盘子堆
            "PlateReturnStation",    // 盘子回收
            // 灶台: 派生类在前
            "HeatedCookingStation",  // 加热型灶台(烤箱/炸锅一类)
            "HeatedStation",         // 加热容器台(单独一类, 见 HeatedStation.cs)
            "CookingStation",        // 普通灶台(锅)
            "MixingStation",         // 搅拌
            "AutoWorkstation",       // 自动工位
            // 功能台
            "WashingStation",        // 洗手池(洗盘子)
            "RubbishBin",            // 垃圾桶
            "ConveyorStation",       // 台面传送带(物品会被传走)
            "SwitchStation",         // 按钮(交互键可按)
            // 关卡机关(MultiplayerController 注册表里确认存在)
            "Teleportal",            // 传送门
            "Terminal",              // 驾驶台(移动平台的操控)
            "Cannon",                // 大炮
            "PushableObject",        // 可推物体
            "CookingRegion",         // 烹饪区域
            // 生成器: 食材箱/分发器。箱子可能只挂这些, 不挂 AttachStation
            "PickupItemSpawner",
            "AttachItemSpawner",
            "PlacementItemSpawner",
            // 基础台
            "Workstation",           // 切菜板
            "AttachStation",         // 普通台面(兜底, 必须靠后)
            // 危险物
            "FireHazard",
            "SplatHazard",
        };

        private static readonly string[] ChefMarkers =
        {
            "PlayerControls", "ChefAvatarSynchroniser",
        };

        // ---------------------------------------------------------------- 静态缓存
        //
        // 为什么要拆(用户提的"实时地图"):
        //   原实现每次 Scan() 都对 23 个类型各做一次 FindObjectsOfType
        //   (= 23 次**全场景**遍历), 再加每台一次 DescribeStation(内含 3 次
        //   GetComponentInChildren + 若干反射)。这个开销只能在 1Hz 下勉强跑,
        //   而"台面上现在放着什么"恰恰是最需要新鲜的数据 ——
        //   引擎靠它判断"这个材料已经放上去了没有"(见 engine._skip_already_on_spot)。
        //
        // 拆成两层:
        //   · **静态**(身份/几何): 找对象那 23 次遍历 + tag/sub/spawn/plate ——
        //     一关只做一次, 之后只做 5 秒一次的兜底重扫(应对会中途变形的关卡,
        //     见 docs/关卡逆向/02)。**引用缓存下来, 不再重复找对象**。
        //   · **动态**(内容): 每帧走一遍缓存的 Transform 读 childCount/子物体名 ——
        //     微秒级, 不碰 FindObjectsOfType。
        //
        // ⚠ `onhas` 是动态层里最贵的一项: 它走反射(ItemKnowledge.ContentsNames
        //   → GetContents() 的 MethodInfo.Invoke)。DynamicInterval 默认 0.1 秒
        //   就是为它留的余量; 想更实时可以调到 0, 但 60Hz 下请先实测帧率。
        private sealed class StationRef
        {
            public GameObject go;
            public Transform attach;      // 内容物挂点(Stack 优先, 否则 AttachStation.m_attachPoint)
            public string name = "";
            public int iid;
            /// <summary>**发现时**归入的那个类型 —— 只当**兜底提示**, 不再是"身份"。
            ///
            /// ☠ 别拿它当权威: 运行期真的会变(`ServerFlamethrowerSpray.cs:70-71` 给已有台面
            ///   `AddComponent<CookingStation>()` 并改 `m_stationType`)。
            ///   每帧要用 `Reclassify()` 拿当下的类型 —— 理由见那边的长注释。</summary>
            public string hint = "";
            /// <summary>身份 JSON 片段(tag/sub/spawn/plate/session…)。**每帧重算** ——
            /// 见 `DescribeIdentity` 的注释(它原来叫 `staticJson`/`DescribeStatic`,
            /// 是**5 秒缓存**的, 2026-09-15 用户点名改成实时)。</summary>
            public string identityJson = "";
            /// <summary>上一次算出来的 onhas —— **按子物体下标各存各的**。
            ///
            /// ☠ 原来这里是**一个** `string`。台面上放了两件以上东西时, 节流帧里
            ///   每个子物体读到的都是"上一帧**最后一个**子物体"的内容:
            ///   循环里 `r.lastHas = has` 被反复覆盖, 而 `heavy` 是**循环外**算的
            ///   ⇒ 循环内一次都不写, 只反复读同一个值。
            ///   形状全对(三个数组仍然等长、下标仍然一一对应)、**内容全错** ——
            ///   所以从来没有任何一处断言能发现它。
            ///   下游后果(`engine._plate_contents_on` 就是按下标配对的):
            ///     · `op_assemble` 手空时判"盘里已有 X ⇒ 跳过" ⇒ 跳过没做过的事
            ///     · 并盘的 before/after 比较 ⇒ 把别人的差值当成自己并成了
            ///     · `_find_ready_dish` 判"这盘拼好了没有" ⇒ 真拼好的那盘看着不对
            /// </summary>
            public List<string> lastHas = new List<string>();
            public float lastHasAt = -99f;
        }

        /// <summary>易变类型: 会中途出现/消失的。**每帧重扫** —— 只有这 3 个
        /// (≈ 3 次全场景遍历), 剩下 20 个固定类型才走 5 秒兜底(23 次)。
        /// 火会烧起来也会灭; 可推物体按设计就会动。</summary>
        private static readonly string[] VolatileTypes =
        {
            "FireHazard", "SplatHazard", "PushableObject",
        };

        private static List<StationRef> _cache;
        private static string _cacheScene = "";
        private static float _cacheBuiltAt;

        /// <summary>`on`/`ontags`/`onhas`/`ing` 每个台面最多报几件。
        /// 原值是 3 —— 台面堆到第 4 件就看不见了(而传送带关卡很容易堆)。放宽到 6。
        /// 上限存在的意义只是别让一个堆满的台面把整条状态撑爆。</summary>
        public const int MaxOnShown = 6;

        /// <summary>固定台面的重扫间隔(秒) —— 只为"发现新建的固定台面"和变形关卡兜底。</summary>
        public static float StaticRescanInterval = 5f;
        /// <summary>`onhas`(容器里装了什么, 走反射)的刷新间隔(秒)。**0 = 每帧**。
        /// 其余动态字段(n/on/ontags)一律每帧 —— 它们只是走一遍 Transform。
        ///
        /// ☠ **2026-09-15 从 0.1 改成 0**(用户定的规矩: "我们的地图更新是和雷达一样的
        ///   机制, 使用要求脚本**每次都使用最新的地图**, 本身地图就小, 占用无关紧要")。
        ///   原来那 0.1 秒是"省一次反射"的考虑 —— 但它买来的陈旧读数会变成
        ///   "对着空台子按放置"、"盘里明明有却说没有"这类**动作级**的错。
        ///   ⚠ 代价必须有人付: `ItemKnowledge.ContentsMethod` 已经把
        ///     `GetContents` 的 `MethodInfo` 按类型缓存掉了, 否则这里每帧
        ///     几千次 `Type.GetMethod` 是拿帧率换新鲜度。想调回去用这个字段。</summary>
        public static float OnhasInterval = 0f;

        /// <summary>每次都由 StateCollector **每帧**调 —— 静态身份走缓存, 动态内容/位置每帧读。
        ///
        /// ⚠ 用户指出的要点: **本游戏没有绝对静态的台面** —— 食材会放到台面上, 锅会被端走,
        ///   可推物体本来就会动, 火会烧起来也会灭。所以:
        ///     · **位置和存活每帧从缓存引用重读**(一次 transform.position 是微秒级的),
        ///       缓存里焊死的只有"身份"(tag/sub/spawn/plate/名字)和引用本身。
        ///     · **易变类型每帧重扫**(只有 3 个: 火/油渍/可推物体) —— 这样"新烧起来的火"
        ///       当帧就能看见, 而不是等 5 秒兜底。
        ///     · 被销毁的(`go == null`)每帧剔除。
        /// </summary>
        /// <summary>兼容接口: 台面 + 烹饪一起出。台面每帧、烹饪 0.1 秒由调用方各自控制。</summary>
        public static string Scan(bool force = false)
        {
            return string.Format("{{\"stations\":{0},\"cooking\":{1}}}",
                                 ScanStations(force), ScanCooking());
        }

        /// <summary>**只出台面数组** —— 由 StateCollector **每帧**调用。</summary>
        public static string ScanStations(bool force = false)
        {
            float now = Time.realtimeSinceStartup;
            long t0 = System.Diagnostics.Stopwatch.GetTimestamp();
            EnsureStationCache(force);
            // 发现(20 次全场景遍历)到此为止 —— 下面这一段才是**每帧**的部分。
            // 分开计时是为了让日志里的两个数能回答两个不同的问题, 见 MeasuredScan()。
            long t1 = System.Diagnostics.Stopwatch.GetTimestamp();

            var stations = new StringBuilder();
            stations.Append("[");
            int n = 0;
            var seen = new Dictionary<int, int>();

            // ① 缓存的固定台面: 身份用缓存, 位置/内容/存活现读
            var cache = _cache;
            if (cache != null)
            {
                for (int i = 0; i < cache.Count; i++)
                {
                    var r = cache[i];
                    if (r.go == null)          // 被销毁(换关/被拆) → 剔除
                        continue;
                    seen[r.iid] = 1;
                    AppendRef(stations, ref n, r, now);
                }
            }

            // ② 易变类型: 每帧重扫(3 个类型 ≈ 3 次全场景遍历, 不是 23 次)
            foreach (var tn in VolatileTypes)
            {
                var type = FindType(tn);
                if (type == null)
                    continue;
                try
                {
                    var objs = UnityEngine.Object.FindObjectsOfType(type);
                    foreach (var o in objs)
                    {
                        var go = GetGameObject(o);
                        if (go == null)
                            continue;
                        int iid = go.GetInstanceID();
                        if (seen.ContainsKey(iid))
                            continue;
                        seen[iid] = 1;
                        AppendRef(stations, ref n, MakeRef(go, tn), now);
                    }
                }
                catch (Exception) { }
            }

            // 厨师位置单独走高频刷新(ScanChefs), 这里不再包含
            stations.Append("]");
            MeasuredScan(t0, t1, now);
            return stations.ToString();
        }

        //: **台面扫描有多贵**(毫秒, 窗口内**最坏**那一帧)。
        //: `ScanAllMs` = 含每 5 秒一次的发现重建; `ScanFrameMs` = 纯每帧部分(身份+位置+内容)。
        //: 为什么报**最坏**而不是平均: 卡顿是"某一帧突然贵", 平均值会把它抹平。
        //: ☠☠ 这两个数是"**身份改成每帧重算**"这件事的成本凭据 —— 用户 2026-09-15
        //:   定的形状是"**先量再说**"(地形那张图就是这么定下 32ms 的)。
        //:   若 `ScanFrameMs` 太大, 正确的下一步是去优化 `DescribeIdentity` 里那几次反射
        //:   (按 `RoundScore.cs` 的 `_monitorM`/`_scoreP` 范例**按类型缓存句柄**),
        //:   **不是**把身份改回缓存 —— 那等于把刚修掉的错放回来。
        public static float ScanAllMs;
        public static float ScanFrameMs;

        private static float _scanAllMax, _scanFrameMax, _scanWinAt;

        private static double _ms(long a, long b)
        {
            return (b - a) * 1000.0 / System.Diagnostics.Stopwatch.Frequency;
        }

        private static void MeasuredScan(long t0, long t1, float now)
        {
            try
            {
                long t2 = System.Diagnostics.Stopwatch.GetTimestamp();
                double all = _ms(t0, t2), frame = _ms(t1, t2);
                if (all > _scanAllMax) _scanAllMax = (float)all;
                if (frame > _scanFrameMax) _scanFrameMax = (float)frame;
                ScanAllMs = _scanAllMax;
                ScanFrameMs = _scanFrameMax;
                if (now - _scanWinAt < 10f)
                    return;
                _scanWinAt = now;
                // ⚠ 措辞别把意图写成结果: `ScanAllMs` 那个**含**每 5 秒一次的发现重建,
                //   所以它天然比 `ScanFrameMs` 大 —— 别读成"每帧都这么贵"。
                Plugin.Log?.LogInfo(string.Format(
                    System.Globalization.CultureInfo.InvariantCulture,
                    "[Overcooked2AI] 台面扫描 10s 内最坏: 每帧部分 {0:F2} ms / 含发现重建 {1:F2} ms",
                    _scanFrameMax, _scanAllMax));
                _scanAllMax = 0f;
                _scanFrameMax = 0f;
            }
            catch (Exception) { }
        }

        /// <summary>把一条台面记录拼成 JSON(**位置、身份、内容, 全部每帧现读**)。</summary>
        private static void AppendRef(StringBuilder stations, ref int n, StationRef r, float now)
        {
            if (r.go == null)
                return;
            float x, y, z;
            try
            {
                var pos = r.go.transform.position;   // ← 每帧重读, 不缓存坐标
                x = pos.x; y = pos.y; z = pos.z;
            }
            catch (Exception) { return; }

            // ☠☠ **身份每帧重算, 不看缓存**(2026-09-15 用户: "台面身份千万别缓冲,
            //   这一块需要实时的")。理由与确凿依据见 `Reclassify` 的长注释。
            //   ⚠ **类型名只算一次** —— 它同时当 `id` 前缀和 `kind`, 两处必须是同一个值,
            //     否则 Python 侧按 `id` 建的索引会和 `kind` 对不上。
            string typeName = Reclassify(r.go, r.hint);
            r.identityJson = DescribeIdentity(r.go, typeName);

            if (n > 0)
                stations.Append(",");
            // `active` = 这个物体**现在在不在场**(GameObject.activeInHierarchy)。
            // 为什么要有(用户实测指出): 限时构件(例: s_wizard_school_3_4 的传送门,
            // 和限时楼梯轮换)会**整块启用/停用**, 而台面清单原来不管这个 ——
            // 于是一扇当前根本不存在的门, 在地图上照样被画成一个常驻的 `O`。
            // **那是显示层在替限时构件打包票**, 和人读到"地图说安全"就去走同一个毛病。
            bool active = true;
            try { active = r.go.activeInHierarchy; } catch (Exception) { }
            // ⚠ **这条 `string.Format` 的形状一个字都不许动** —— `{8}`/`{9}` 是
            //   "自带前导逗号"的片段, 逗号归谁是最容易错的地方(少一个前导逗号
            //   实测废掉过一整局)。本轮只换了 {8} 的**来源**(缓存字段 → 每帧重算),
            //   参数个数、类型、位置全不变。
            stations.Append(string.Format(
                System.Globalization.CultureInfo.InvariantCulture,
                "{{\"id\":\"{0}_{1}\",\"iid\":{2},\"kind\":\"{3}\",\"name\":\"{4}\",\"x\":{5:F2},\"y\":{6:F2},\"z\":{7:F2},\"active\":{10}{8}{9}}}",
                typeName, n, r.iid, typeName, SafeName(r.name), x, y, z,
                r.identityJson, DescribeDynamic(r, now), active ? "true" : "false"));
            n++;
        }

        /// <summary>只扫厨师(含手持物)。很轻, 给高频刷新用 ——
        /// 导航是"读位置→按键"的闭环, 位置读得慢就等于用旧坐标开车, 必然来回震。</summary>
        public static string ScanChefs()
        {
            var chefs = new StringBuilder();
            int chefCount = 0;
            var pcType = FindType("PlayerControls");
            if (pcType != null)
            {
                try
                {
                    var objs = UnityEngine.Object.FindObjectsOfType(pcType);
                    foreach (var o in objs)
                    {
                        var go = GetGameObject(o);
                        if (go == null)
                            continue;
                        var pos = go.transform.position;
                        // 手上拿着什么(+ 那件容器里装了什么) —— 优先服务端权威值,
                        // 客户端那份会晚一帧(见 ReadHeldItems)
                        string held, heldC, heldHas;
                        ReadHeldItems(go, out held, out heldC, out heldHas);
                        // **厨师归属哪个玩家** —— 这是决定用哪套键盘的唯一权威依据。
                        // 依据 ClientInputTransmitter.Setup(): iD = GetComponent<PlayerIDProvider>().GetID()
                        // Player.One → 键盘左半(SplitPadHost) → WASD
                        // Player.Two → 键盘右半(SplitPadGuest) → 方向键
                        // 注意: 这里的 chefCount 只是"枚举序号", 与 Player 编号**没有必然关系**。
                        string player = ReadPlayerId(go);
                        // **游戏认不认这个厨师是"本地控制"** —— 决定性诊断。
                        // ClientPlayerControlsImpl_Default.cs:204 起:
                        //     bool flag = m_playerIDProvider.IsLocallyControlled();
                        //     if (flag) { UpdateNearbyObjects(); Update_Carry(); ... }   // ← 拾取只在这里
                        //     Update_Movement(...)                                        // ← 移动不看 flag
                        // 所以"能走不能按"必须先看这个值: 若为 false, 拾取更新整段根本不执行。
                        string local = ReadLocallyControlled(go);
                        // **现在能不能指挥得动这个厨师** —— 见 ReadControl。
                        // 只判 respawning 不够: 过场压制、喷灭火器(scale=0) 也一样按不动。
                        string control = ReadControl(go);
                        // **游戏自己认为"现在按交互键会作用到哪个物体"** —— 见 ReadInteraction。
                        string inter = ReadInteraction(go);
                        if (chefCount > 0)
                            chefs.Append(",");
                        chefs.Append(string.Format(
                            "{{\"id\":{0},\"seq\":{0},\"player\":\"{1}\",\"name\":\"{2}\",\"x\":{3:F2},\"y\":{4:F2},\"z\":{5:F2},\"held\":\"{6}\",\"heldc\":\"{7}\",\"heldhas\":\"{8}\"{9}{10}{11}}}",
                            chefCount, player, SafeName(go.name), pos.x, pos.y, pos.z,
                            held, heldC, heldHas, control, inter, local));
                        chefCount++;
                    }
                }
                catch (Exception) { }
            }
            return "[" + chefs + "]";
        }

        /// <summary>**大厅/主界面里已加入的玩家名单** —— 数组, 每项 `{slot, local, name}`。
        ///
        /// ☠☠ **为什么必须有这个**(2026-09-15, 用户: "**需要去检查一下游戏本体的数据**"):
        ///   `tools/joinp2.py` 的注释原来写着"大厅里有几个人在 `StartScreen` 读不到,
        ///   要进对局才有 `PlayerControls`" —— **那句是错的**, 它让"要不要按 A 加入 P2"
        ///   变成了一个只能靠猜的决定。而按 A 是**"加入下一个玩家"**(不是 toggle):
        ///   猜错就会把 **P3 引进来**(用户实测)。所以这一条必须读准。
        ///
        /// 真正的名单在 `Team17.Online.ClientUserSystem.m_Users`
        ///   (`overcooked_decomp/Team17.Online/ClientUserSystem.cs:36`):
        ///     `public static FastList<User> m_Users = new FastList<User>(4);`
        ///   (主机/权威视图是 `ServerUserSystem.m_Users`, 同签名, 作后备)
        /// **铁证**: `GamepadEngagementManager.cs:92-105` —— 那正是"按 A 加入下一个玩家"的
        ///   轮询器**自己**, 它先判 `ClientUserSystem.m_Users.Count < 4` 才允许 engage。
        ///   也就是说"够不够人"这条判据, 我们和游戏用的是**同一个数据源**。
        ///   `FrontendPlayerLobby.cs` 也到处读 `Count` 来渲染玩家槽位。
        ///
        /// 沿用 `ScanChefs` 的做法: 能读到就叫"读到了", 读不到就 `[]` ——
        ///   ⚠ **Python 侧要靠"键在不在"区分"读不到"和"真的只有 0 人"**,
        ///     所以 `[]` 在这里表示"没读到"(见 `engine`/`virtual_pad` 那边的守卫)。
        ///
        /// ⚠ 这是**每帧**都跑的路径(由 `StateCollector.Refresh` 调) ⇒ 反射查出来的
        ///   `FieldInfo`/`PropertyInfo` **必须缓存**(同 `ItemKnowledge.ContentsMethod`
        ///   那条教训: 不缓存就是拿帧率换新鲜度)。类型在一次会话里不变, 缓存安全。
        /// </summary>
        public static string ScanUsers()
        {
            var sb = new StringBuilder();
            sb.Append("[");
            int n = 0;
            bool any = false;       // 有没有**成功读到**过名单(见下面 `return "null"`)
            // 客户端视图优先(大厅 UI 读的就是它); 读不到再退到 ServerUserSystem。
            foreach (var typeName in new[] { "Team17.Online.ClientUserSystem",
                                             "Team17.Online.ServerUserSystem" })
            {
                if (!ResolveUserReflection(typeName))
                    continue;
                any = true;
                try
                {
                    object list = _usersListField.GetValue(null);
                    if (list == null)
                        continue;
                    // ⚠ **必须给两个参数** —— 目标是 .NET 2.0(`build.bat` 里 `FW=v2.0.50727`),
                    //   那时 `PropertyInfo.GetValue(object)` 这个单参数重载**还不存在**
                    //   (CS1501)。`PropStr`/`PropBool` 里也是这个写法。
                    int cnt = (int)_usersCountProp.GetValue(list, null);
                    var arr = _usersItemsField.GetValue(list) as Array;
                    if (arr == null || cnt <= 0)
                        continue;
                    for (int i = 0; i < cnt && i < arr.Length && n < MaxUsersShown; i++)
                    {
                        object u = arr.GetValue(i);
                        if (u == null)
                            continue;
                        if (n > 0)
                            sb.Append(",");
                        // ⚠ 片段自带前导逗号是给"插进别人 JSON"用的; 这里是一个**独立数组**,
                        //   逗号必须由循环自己控制(上面那行), 别照抄字符串片段那套。
                        sb.Append(string.Format(
                            "{{\"slot\":\"{0}\",\"local\":{1},\"name\":\"{2}\"}}",
                            SafeName(PropStr(_usersEngProp, u)),
                            PropBool(_usersLocalProp, u) ? "true" : "false",
                            SafeName(PropStr(_usersNameProp, u))));
                        n++;
                    }
                    break;          // 读到了就不再翻后备
                }
                catch (Exception) { }
            }
            // ☠☠ **读不到必须与"0 人"区分开** —— 否则 Python 侧会把"读不到"当成
            //   "大厅里没人", 于是去按 A ⇒ 而按 A 是"加入**下一个**玩家" ⇒ **引进 P3**。
            //   所以两个类型**一个都没解析出来**时返回 `null`(不是 `[]`)。
            if (!any)
                return "null";
            sb.Append("]");
            return sb.ToString();
        }

        /// <summary>`on`/`ontags`/`onhas` 之外, **`users` 最多报几项**。
        /// 和 `MaxOnShown` 同理: 上限只是别让一条状态撑爆, 这个列表本来就 ≤4。</summary>
        public const int MaxUsersShown = 4;

        /// <summary>`ScanUsers` 的反射句柄 —— **解析一次、全程复用**(见那边的注释)。
        /// 只在**成功**时置 `_usersReady`, 否则下次还会重试(程序集可能还没加载完)。</summary>
        private static bool _usersReady;
        private static FieldInfo _usersListField;
        private static PropertyInfo _usersCountProp;
        private static FieldInfo _usersItemsField;
        private static PropertyInfo _usersEngProp, _usersLocalProp, _usersNameProp;
        private static string _usersTypeName = "";

        private static bool ResolveUserReflection(string typeName)
        {
            if (_usersReady && _usersTypeName == typeName)
                return true;
            _usersReady = false;
            try
            {
                var t = FindType(typeName);
                if (t == null)
                    return false;
                var lf = t.GetField("m_Users",
                                    BindingFlags.Public | BindingFlags.Static);
                if (lf == null)
                    return false;
                var lt = lf.FieldType;                       // FastList<User>
                var cnt = lt.GetProperty("Count");
                var items = lt.GetField("_items");
                if (cnt == null || items == null)
                    return false;
                var et = items.FieldType.GetElementType();   // User
                if (et == null)
                    return false;
                var eng = et.GetProperty("Engagement");
                var loc = et.GetProperty("IsLocal");
                var nam = et.GetProperty("DisplayName");
                if (eng == null || loc == null)
                    return false;
                _usersListField = lf;
                _usersCountProp = cnt;
                _usersItemsField = items;
                _usersEngProp = eng;
                _usersLocalProp = loc;
                _usersNameProp = nam;                        // DisplayName 可能没有, 允许 null
                _usersTypeName = typeName;
                _usersReady = true;
                return true;
            }
            catch (Exception) { }
            return false;
        }

        private static string PropStr(PropertyInfo p, object o)
        {
            if (p == null)
                return "";
            try
            {
                object v = p.GetValue(o, null);
                return v == null ? "" : v.ToString();
            }
            catch (Exception) { return ""; }
        }

        private static bool PropBool(PropertyInfo p, object o)
        {
            if (p == null)
                return false;
            try
            {
                object v = p.GetValue(o, null);
                return v is bool && (bool)v;
            }
            catch (Exception) { return false; }
        }

        /// <summary>读厨师"现在能不能被指挥"(PlayerControls 的几个 public 成员)。
        ///
        /// respawning / suppressed / scale 三者任何一个不满足, 发方向键都是白费:
        ///   · m_bRespawning  public 字段        (PlayerControls.cs:303-304)
        ///   · IsSuppressed() public 方法        (:494-497, 过场/表情/重生期间被压制)
        ///   · MovementScale  public 属性        (:390, 喷灭火器时被设成 0 完全不能动)
        /// 只判 respawning 是不够的 —— 这是之前"按键看似有效、导航却原地不动"的隐藏原因之一。</summary>
        private static string ReadControl(GameObject chefGo)
        {
            try
            {
                var pcType = FindType("PlayerControls");
                if (pcType == null)
                    return "";
                var comp = chefGo.GetComponent(pcType);
                if (comp == null)
                    return "";
                bool respawning = false;
                var fr = pcType.GetField("m_bRespawning");
                if (fr != null)
                {
                    var v = fr.GetValue(comp);
                    respawning = v is bool && (bool)v;
                }
                bool suppressed = false;
                try
                {
                    var m = pcType.GetMethod("IsSuppressed");
                    if (m != null)
                    {
                        var v = m.Invoke(comp, null);
                        suppressed = v is bool && (bool)v;
                    }
                }
                catch (Exception) { }
                float scale = 1f;
                try
                {
                    var p = pcType.GetProperty("MovementScale");
                    if (p != null)
                    {
                        var v = p.GetValue(comp, null);
                        if (v is float)
                            scale = (float)v;
                    }
                }
                catch (Exception) { }
                var beh = comp as Behaviour;
                bool enabled = beh == null || beh.enabled;
                bool can = enabled && !respawning && !suppressed && scale > 0.01f;

                // 游戏自己说"现在按键有没有用"。PlayerControls.CanButtonBePressed()
                // (PlayerControls.cs:453-468) 同时检查: 窗口在前台 + 角色直接受控 +
                // 没有打开的对话框/根菜单。**"停了暂停菜单"时按键全部无效但位置也不变,
                // 表现和"卡住"一模一样** —— 有这个字段就能一眼区分, 不用再猜。
                bool canpress = false;
                try
                {
                    var m = pcType.GetMethod("CanButtonBePressed");
                    if (m != null)
                    {
                        var v = m.Invoke(comp, null);
                        canpress = v is bool && (bool)v;
                    }
                }
                catch (Exception) { }

                // **正被击退/冲量推着吗** —— `ClientPlayerControlsImpl_Default.m_impactTimer > 0`
                // 就是"此刻有外部冲量在覆盖速度"(ClientPlayerControlsImpl_Default.cs:426-431):
                // 冲量期间它按 S 曲线把 `m_impactVelocity` 插值进运动, **按键只起一部分作用**,
                // 所以脚本这时最该做的是**松手等它衰减**(0.2 秒), 而不是硬顶。
                // 三类触发(§2.4.2): 撞火 / 被投掷物砸中 / 两名厨师冲刺对撞 —— 都不可预判。
                // ⚠ `ClientPlayerControlsImpl_Default` **不是** `PlayerControls` 的子类
                //   (`: ClientSynchroniserBase`), 是同一个 GameObject 上的**另一个组件**,
                //   所以得单独取一次; `m_impactTimer` 是私有字段, 走反射读。
                bool impacted = false;
                try
                {
                    var icType = FindType("ClientPlayerControlsImpl_Default");
                    if (icType != null)
                    {
                        var ic = chefGo.GetComponent(icType);
                        if (ic != null)
                        {
                            var fi = icType.GetField("m_impactTimer",
                                BindingFlags.Instance | BindingFlags.NonPublic);
                            if (fi != null)
                            {
                                var v = fi.GetValue(ic);
                                impacted = v is float && (float)v > 0f;
                            }
                        }
                    }
                }
                catch (Exception) { }

                // ---- 风: **直接问游戏**"此刻这个厨师身上的合力是多少" ----
                //
                // 依据(反编译, 一条链):
                //   PlayerControls.WindReceiver                    (PlayerControls.cs:408, 576)
                //     → WindAccumulator.GetVelocity() = m_totalForce
                //     = Σ 各 IWindSource.GetVelocity()             (WindAccumulator.cs:44-51, 71-74)
                //   而 ClientPlayerControlsImpl_Default.cs:902-906 的 ApplyWindForce()
                //   **用的就是这个数**(RigidbodyMotion.Movement(v, dt) = MovePosition(pos + v*dt))。
                //
                // ⇒ **权威来源**。相比"把风区体积投影到格子再猜人在不在风里", 它一次解决四件事:
                //   · 多股风**已经求和**(重叠的风区/风箱喷雾都不用手算)
                //   · `m_windFilter` 层掩码**已经过掉**(体积可以配成"不吹厨师")
                //   · 碰撞体的真实形状/旋转/层级不用管(那是 Unity 物理的事)
                //   · `enabled` / `m_windSpeed` 的变化天然即时(GetVelocity 现算)
                //   几何投影只需留给"规划"(哪几格会吹), 见 `InteractiveScan.WindExtra` 的 chefs[]。
                bool windOk = false;
                float wvx = 0f, wvz = 0f;
                try
                {
                    var p = pcType.GetProperty("WindReceiver");
                    var acc = (p == null) ? null : p.GetValue(comp, null);
                    if (acc != null)
                    {
                        var m = acc.GetType().GetMethod("GetVelocity", Type.EmptyTypes);
                        if (m != null)
                        {
                            var v = (Vector3)m.Invoke(acc, null);
                            wvx = v.x;
                            wvz = v.z;
                            windOk = true;
                        }
                    }
                }
                catch (Exception) { }

                // ---- 真实 RunSpeed / 轴向反转 ----
                // `MovementData`(PlayerControls.cs:22-54)里 `RunSpeed` / `XAxisAllignment` /
                //   `YAxisAllignment` 都是 **public 字段**, 从 `PlayerControls.Movement`(:388) 取一次实例就行。
                // 为什么要它: 补偿公式要除以**摇杆增益** `RunSpeed * MovementScale` ——
                //   Python 侧一直硬编码 `4.0 * 0.9`(`engine.py:115`, 那个 0.9 是按键欠冲用的),
                //   拿它当除数会把风高估 11%。而 `RunSpeed` 是 prefab 上的 `[SerializeField]`,
                //   只有游戏自己知道。`align*` 顺手把"轴是否被关卡反转"这个符号问题也堵上。
                float run = 0f;
                string alignx = "", aligny = "";
                try
                {
                    var p = pcType.GetProperty("Movement");
                    var mov = (p == null) ? null : p.GetValue(comp, null);
                    if (mov != null)
                    {
                        var mt = mov.GetType();
                        var fRun = mt.GetField("RunSpeed");
                        if (fRun != null)
                        {
                            var v = fRun.GetValue(mov);
                            if (v is float)
                                run = (float)v;
                        }
                        var fx = mt.GetField("XAxisAllignment");
                        if (fx != null)
                        {
                            var v = fx.GetValue(mov);
                            alignx = (v == null) ? "" : v.ToString();
                        }
                        var fy = mt.GetField("YAxisAllignment");
                        if (fy != null)
                        {
                            var v = fy.GetValue(mov);
                            aligny = (v == null) ? "" : v.ToString();
                        }
                    }
                }
                catch (Exception) { }

                // ⚠ **前导逗号必须在这里**(2026-09-15 补): 它是"插在别人 JSON 里的一段",
                //   调用方(`ScanChefs` 的 `{9}`)因此**不能**再给它加字面逗号。
                //   原来这里是 `"\"respawning\"…`, 而父格式串写的是 `,\"heldhas\":\"{8}\",{9}{10}{11}`
                //   —— 两者**只有一边**提供逗号, 于是这个函数一旦走 `return ""`
                //   (找不到 `PlayerControls` / 组件不在), 拼出来就是 `…,"heldhas":"X",,}`
                //   ⇒ **整条厨师 JSON 报废**。这正是那条老教训的形状
                //   (片段必须自带前导逗号; 漏了就编译干净、运行必炸), 只是换了个位置。
                //   `inter`(`ReadInteraction`) / `local`(`ReadLocallyControlled`)
                //   本来就是自带的, 只有这一个不是。
                return string.Format(
                    System.Globalization.CultureInfo.InvariantCulture,
                    ",\"respawning\":{0},\"suppressed\":{1},\"scale\":{2:F2},\"canmove\":{3},"
                    + "\"canpress\":{4},\"impacted\":{5},\"wind\":{{\"ok\":{6},\"vx\":{7:F2},\"vz\":{8:F2}}},"
                    + "\"run\":{9:F2},\"alignx\":\"{10}\",\"aligny\":\"{11}\"",
                    respawning ? "true" : "false",
                    suppressed ? "true" : "false",
                    scale,
                    can ? "true" : "false",
                    canpress ? "true" : "false",
                    impacted ? "true" : "false",
                    windOk ? "true" : "false",
                    wvx,
                    wvz,
                    run,
                    SafeName(alignx),
                    SafeName(aligny));
            }
            catch (Exception) { }
            return "";
        }

        /// <summary>游戏自己认为这个厨师**现在按交互键会作用到哪个物体**。
        ///
        /// 依据(公开属性, 不是 private 字段):
        ///   PlayerControls.CurrentInteractionObjects              (PlayerControls.cs:394)
        ///     → InteractionObjects.m_TheOriginalHandlePickup      // 抓取键作用物
        ///     → InteractionObjects.m_interactable                 // 工位交互键作用物
        ///   (InteractionObjects 字段定义见 PlayerControls.cs:150-171)
        ///
        /// **为什么必须问游戏、不能自己算几何**(用户指出的问题):
        ///   交互判定是 InteractWithItemHelper.IsColliderInArc (InteractWithItemHelper.cs:153-163):
        ///       closest = GetClosestPointOnSurface(collider, chefPos)   // :119-151
        ///       要求 |closest - chefPos| 的 XZ 距离 < 1.0
        ///       且 Dot(chefForward, 该方向) >= cos(PI/2) = 0            // 只认前 180°
        ///   量的是**到碰撞体表面的距离** —— 而台面是有体积的, 厨师永远走不到台面正中间。
        ///   脚本拿"格子中心"去算距离毫无意义。直接读游戏算好的结果就不用猜站位了。
        /// </summary>
        private static string ReadInteraction(GameObject chefGo)
        {
            try
            {
                var pcType = FindType("PlayerControls");
                if (pcType == null)
                    return "";
                var comp = chefGo.GetComponent(pcType);
                if (comp == null)
                    return "";
                var prop = pcType.GetProperty("CurrentInteractionObjects");
                if (prop == null)
                    return "";
                object io = prop.GetValue(comp, null);
                if (io == null)
                    return "";
                var ioType = io.GetType();
                string pick = GoFieldName(ioType, io, "m_TheOriginalHandlePickup");
                string use = CompFieldName(ioType, io, "m_interactable");
                // ⚠ 关键: Update_Carry 用的是 **m_iHandlePickup**(接口实例), 不是 m_TheOriginalHandlePickup(物体)。
                //   两者是分开赋值的: 前者 = GetControllingPickupHandler_Client(后者)。
                //   如果后者有名字而前者是 null, 那么"游戏说能抓取"是真的, 但**客户端根本不会发取件消息**
                //   —— 正好是"能走不能按"的形状。这个字段是这条链上唯一还没量过的一环。
                string pickh = CompFieldName(ioType, io, "m_iHandlePickup");
                string useh = CompFieldName(ioType, io, "m_iHandlePlacement");
                return string.Format(",\"pick\":\"{0}\",\"use\":\"{1}\",\"pickh\":\"{2}\",\"placeh\":\"{3}\"",
                                     SafeName(pick), SafeName(use), SafeName(pickh), SafeName(useh));
            }
            catch (Exception) { }
            return "";
        }

        private static string GoFieldName(Type t, object inst, string field)
        {
            try
            {
                var f = t.GetField(field);
                var v = (f == null) ? null : f.GetValue(inst) as GameObject;
                return v == null ? "" : v.name;
            }
            catch (Exception) { return ""; }
        }

        private static string CompFieldName(Type t, object inst, string field)
        {
            try
            {
                var f = t.GetField(field);
                var v = (f == null) ? null : f.GetValue(inst) as Component;
                return v == null ? "" : v.gameObject.name;
            }
            catch (Exception) { return ""; }
        }

        /// <summary>读厨师归属的玩家(PlayerIDProvider.GetID() → Player.One/Two/…)。
        /// 这才是"该给它发哪套键"的依据; 枚举序号 id 不可靠。</summary>
        /// <summary>游戏认不认这个厨师"本地控制"(PlayerIDProvider.IsLocallyControlled)。
        /// 拾取/工位交互那段更新只在它为真时执行, 而移动不看它 —— 见上面 ReadPlayerId 的注释。</summary>
        private static string ReadLocallyControlled(GameObject chefGo)
        {
            try
            {
                var pt = FindType("PlayerIDProvider");
                if (pt == null)
                    return "";
                var prov = chefGo.GetComponent(pt);
                if (prov == null)
                    return "";
                var m = pt.GetMethod("IsLocallyControlled", Type.EmptyTypes);
                if (m == null)
                    return "";
                var v = m.Invoke(prov, null);
                return ",\"local\":" + (v is bool && (bool)v ? "true" : "false");
            }
            catch (Exception) { }
            return "";
        }

        private static string ReadPlayerId(GameObject chefGo)
        {
            try
            {
                var pt = FindType("PlayerIDProvider");
                if (pt == null)
                    return "";
                var provider = chefGo.GetComponent(pt);
                if (provider == null)
                    return "";
                var m = pt.GetMethod("GetID");
                if (m == null)
                    return "";
                var v = m.Invoke(provider, null);
                return v == null ? "" : v.ToString();
            }
            catch (Exception) { }
            return "";
        }

        /// <summary>正在烹饪的物体: 进度/状态/是否烧焦/需要的灶台。
        /// 依据 CookingHandler.GetCookedOrderState: progress<=cookTime 为 Raw, >2*cookTime 为 Burnt,
        /// 中间的 Cooked 才是订单要的 —— 所以"什么时候从灶上取下"必须依据这里的实时进度。</summary>
        /// <summary>**全场景按 tag 找食材** —— 照抄游戏自己的 `GameUtils.GetAllIngredients()`
        /// (`GameUtils.cs:504-509`: `FindGameObjectsWithTag("Pre-Ingredient")
        ///                      .Union(FindGameObjectsWithTag("Ingredient"))`)。
        ///
        /// 为什么必须有这一条(用户提的"地图建模是否还有遗漏"):
        ///   我们原来的食材只从三个地方来 —— `station.on` / `station.onhas` / `chef.held`。
        ///   **不在台面上的食材完全看不见**: 掉在地上的、在移动平台/荷叶上的、
        ///   任何不在我们扫的那 25 个台面类型下的。
        ///   而"场上有哪些食材"的权威定义就是游戏那两个 tag。
        ///
        /// 成本: 3 次 `FindGameObjectsWithTag`(比 `FindObjectsOfType` 便宜, 走 tag 索引),
        ///       所以挂在 0.1 秒档, 不跟台面一起每帧跑。
        ///
        /// ☠☠ **2026-09-15 补上 `"Plate"`** —— 原来只照抄了游戏的
        ///   `GetAllIngredients()`, 于是这份清单里**只有食材、没有盘子**。
        ///   而 Python 侧 `km.items` 有**两个**消费者:
        ///     · `_fetch_source_live` 的第 ② 个货源"掉在地上/台面外的料" —— 那一半是好的;
        ///     · `_ensure_plate` 的**第三个盘子来源**"地上的盘子"
        ///       (`engine._find_ground_item(km, "Plate", ...)`) —— **永远是空的**,
        ///       因为地上根本没有盘子被报上来。⇒ 那段"补上了漏掉的『地上的盘子』"
        ///       的修补是**死代码**, 而地上的盘子恰恰是**我们自己造的**
        ///       (腾手时丢在脚下、放置失败掉在地上)。
        ///   代价(用户已明说无所谓): "本身地图就小, 占用无关紧要"。
        ///   ⚠ `Plate` 是自定义 tag(不是内置的), 由游戏的 TagManager 定义 ——
        ///     `FindGameObjectsWithTag` 对它有效; tag 不存在时下面那圈 try/catch 会跳过。
        /// </summary>
        public static string ScanItems()
        {
            var sb = new StringBuilder();
            sb.Append("[");
            int n = 0;
            var seen = new Dictionary<int, int>();
            foreach (var tag in new string[] { "Pre-Ingredient", "Ingredient", "Plate" })
            {
                GameObject[] objs = null;
                try { objs = GameObject.FindGameObjectsWithTag(tag); }
                catch (Exception) { continue; }
                if (objs == null)
                    continue;
                foreach (var go in objs)
                {
                    if (go == null)
                        continue;
                    int iid = go.GetInstanceID();
                    if (seen.ContainsKey(iid))
                        continue;
                    seen[iid] = 1;
                    float x, z;
                    try
                    {
                        var pos = go.transform.position;
                        x = pos.x; z = pos.z;
                    }
                    catch (Exception) { continue; }
                    // **这件东西还能不能被加工**(= 生料还是成品)。用途: "交出去的那份料
                    // 回来了没有"要按**实例**判阶段 —— 完成时 GameObject 会被 m_nextPrefab
                    // 替换, 所以**成品实例身上没有 next**。
                    // ☠ **不能用 Unity Tag 判** —— 实测同一关里生虾是 Ingredient、生鱼却是
                    //   Pre-Ingredient(见 neko/cookbook.py 里 raw_for 的注释), 靠 tag 会漏。
                    // ☠ 也不能靠**名字查知识表** —— "生料和成品同名"那一族
                    //   (`SushiFish --切8次--> SushiFish`) 表里是两条同名记录, 分不出是哪个。
                    // 反射整段照抄 ItemKnowledge.One 里那段(同一套 FindType/GetComponent + try/catch)。
                    bool work = false;
                    try
                    {
                        var wt = FindType("WorkableItem");
                        if (wt != null)
                        {
                            var wi = go.GetComponent(wt);
                            if (wi != null)
                            {
                                var gm = wt.GetMethod("GetNextPrefab");
                                if (gm != null)
                                    work = gm.Invoke(wi, null) != null;
                            }
                        }
                    }
                    catch (Exception) { }
                    if (n > 0)
                        sb.Append(",");
                    sb.Append(string.Format(
                        System.Globalization.CultureInfo.InvariantCulture,
                        "{{\"name\":\"{0}\",\"tag\":\"{1}\",\"x\":{2:F2},\"z\":{3:F2},\"work\":{4}}}",
                        SafeName(go.name), SafeName(tag), x, z, work ? "true" : "false"));
                    n++;
                }
            }
            sb.Append("]");
            return sb.ToString();
        }

        /// <summary>烹饪进度(1× FindObjectsOfType + 每口锅几次反射)。由调用方定节奏 ——
        /// 现在挂在 StateCollector 的 0.1 秒档, 不跟台面一起每帧跑。</summary>
        public static string ScanCooking()
        {
            var sb = new StringBuilder();
            int n = 0;
            var ct = FindType("ClientCookingHandler");
            if (ct == null)
                ct = FindType("CookingHandler");
            if (ct == null)
                return "";
            try
            {
                var objs = UnityEngine.Object.FindObjectsOfType(ct);
                foreach (var o in objs)
                {
                    if (o == null)
                        continue;
                    var go = GetGameObject(o);
                    if (go == null)
                        continue;

                    float prog = 0f, need = 0f;
                    string state = "", station = "";
                    bool burning = false;
                    try
                    {
                        var m = ct.GetMethod("GetCookingProgress");
                        if (m != null)
                            prog = (float)m.Invoke(o, null);
                    }
                    catch (Exception) { }
                    try
                    {
                        var m = ct.GetMethod("GetCookedOrderState");
                        if (m != null)
                        {
                            var v = m.Invoke(o, null);
                            state = v == null ? "" : v.ToString();
                        }
                    }
                    catch (Exception) { }
                    try
                    {
                        var m = ct.GetMethod("IsBurning");
                        if (m != null)
                            burning = (bool)m.Invoke(o, null);
                    }
                    catch (Exception) { }
                    try
                    {
                        var m = ct.GetMethod("GetRequiredStationType");
                        if (m != null)
                        {
                            var v = m.Invoke(o, null);
                            station = v == null ? "" : v.ToString();
                        }
                    }
                    catch (Exception) { }
                    try
                    {
                        var p = ct.GetProperty("AccessCookingTime");
                        if (p != null)
                            need = (float)p.GetValue(o, null);
                    }
                    catch (Exception) { }

                    var pos = go.transform.position;
                    string tag = "";
                    try { tag = go.tag; }
                    catch (Exception) { }
                    // 锅里装的是什么。**关键**: 锅身上的 CookingHandler 只知道"熟没熟",
                    // 不知道"煮的是什么"(ItemKnowledge.IngredientName(锅) 永远是空串),
                    // 所以必须另外读容器的 m_contents, 否则无法判断"这口锅是不是我的/是空的"。
                    string inside = "";
                    try { inside = ItemKnowledge.ContentsNames(go); }
                    catch (Exception) { }
                    // ☠☠ **这个容器"是哪种加热方式"** —— 它就是游戏拒收那道菜的**权威判据**。
                    //
                    // 依据(反编译, 规则 1):
                    //   `CookableContainer.cs:44-60` `AllowItemPlacement(_object, _ctx, _handler)`:
                    //       var cp = _object.RequestComponent<CookableProperties>();
                    //       if (cp == null || !cp.AllowsCookingStep(_handler.AccessCookingType))
                    //           return false;
                    //   而 `CookableProperties.cs:11-13` 的判据是**比 `m_uID`**:
                    //       Array.FindIndex(AllowedCookingSteps, x => x.m_uID == _stepData.m_uID) != -1
                    //   `AccessCookingType`(=`CookingHandler.m_cookingType`, `CookingHandler.cs:9`)
                    //   是个 `CookingStepData`(ScriptableObject), 身份就是 `CookingStepData.m_uID`。
                    // ⇒ **食材能进哪个容器 ⇔ 食材的 `AllowedCookingSteps` 里有没有这个 `m_uID`。**
                    //   两边都是**运行时读的同一个整数**, 不需要跨会话稳定, 直接比就行。
                    //
                    // 为什么必须报出来(用户 2026-09-15): `s_mine_2_5` 里米被拿去用了
                    //   **煮切过的肉**那口平底锅 —— 引擎只知道"要装进容器", 不知道该装哪个,
                    //   于是挑"最近那口有锅的灶台"。游戏当场拒收(`placeCanHandle=false`)。
                    // ⚠ 空锅也有 `CookingHandler`(进度 0), 所以**这一条对空锅同样有效** ——
                    //   `_pick_stove` 正是在"空锅"上做选择, 这个字段就是为那一步准备的。
                    // ⚠ 名字只为**日志/离线**可读; **判据只用 `m_uID`**(名字可能重复或为空)。
                    int cookId = 0;
                    string cookName = "";
                    try
                    {
                        var p = ct.GetProperty("AccessCookingType");
                        var step = p != null ? p.GetValue(o, null) : null;
                        if (step != null)
                        {
                            var st2 = step as UnityEngine.Object;
                            if (st2 != null)
                                cookName = SafeName(st2.name);
                            var f = step.GetType().GetField("m_uID");
                            if (f != null)
                                cookId = Convert.ToInt32(f.GetValue(step));
                        }
                    }
                    catch (Exception) { }
                    if (n > 0)
                        sb.Append(",");
                    sb.Append(string.Format(
                        System.Globalization.CultureInfo.InvariantCulture,
                        "{{\"name\":\"{0}\",\"ing\":\"{1}\",\"in\":\"{2}\",\"tag\":\"{3}\",\"prog\":{4:F1},\"need\":{5:F1},\"state\":\"{6}\",\"burning\":{7},\"station\":\"{8}\",\"x\":{9:F2},\"z\":{10:F2},\"cookId\":{11},\"cookName\":\"{12}\"}}",
                        SafeName(go.name), SafeName(ItemKnowledge.IngredientName(go)),
                        SafeName(inside), SafeName(tag),
                        prog, need, SafeName(state), burning ? "true" : "false",
                        SafeName(station), pos.x, pos.z, cookId, cookName));
                    n++;
                }
            }
            catch (Exception) { }
            return sb.ToString();
        }

        /// <summary>全量清单: 枚举场景里所有带 Collider 的物体及其"游戏自定义组件"。
        /// 用途: 不靠预设类型名猜台子种类, 一次看清某关到底有哪些组件(如 CleanPlateStack/Stack/PlateStation)。
        /// 过滤掉 UnityEngine.* 命名空间的组件, 只留 Assembly-CSharp / Team17.* 等游戏类型。</summary>
        /// <summary>把一个组件的**全部字段**摊出来(名字 + 值)。
        ///
        /// 用户 2026-09-15: "**让 mod 多扒一点信息下来, 别这么吝啬, 有的信息全拔下来**"。
        ///
        /// 为什么值得(这一条比"多读几个字段"重要得多): 我们一直在**手挑字段** ——
        ///   `m_stationType` / `m_stages` / `m_itemPrefab` / `AllowedCookingSteps` /
        ///   `m_cookingType` / `m_exitPortal`… 每遇到一个新机制, 都要先**猜**"该读哪个",
        ///   猜错了就是白烧一局实机(而一局要人工开、150 秒)。就在今天, "米该进哪口锅"
        ///   这件事就花了两个来回(`CookableProperties.AllowsCookingStep` 那条判据)。
        ///   **全摊出来**之后, 新关卡/新机制只要 dump 一次就看得见, 不用再猜。
        ///
        /// ⚠ 这是**诊断口**(`raw` 命令), **不在每帧路径上** ⇒ 宽一点没关系。
        /// ⚠ 但**必须有上限**: 桥是"一行一个 JSON", 无界 dump 会把整条连接撑爆。
        ///   所以: 数组只取前 `MaxFieldItems` 项、字符串截断、每个组件最多
        ///   `MaxFieldsPerComp` 个字段 —— **宁可少, 不可断**。
        /// ⚠ 值只做**一层**展开(数组/List 的元素会再取一层), 不做深递归:
        ///   游戏对象互相引用成环, 深递归会死循环。
        ///   `UnityEngine.Object` 一律只报**名字** —— 那正是我们要看的东西
        ///   (`m_itemPrefab` 报出 "SushiRice" 这种, 比一串实例 id 有用得多)。
        /// </summary>
        private static void AppendFields(StringBuilder sb, Component c)
        {
            sb.Append("{\"type\":\"").Append(SafeName(c.GetType().Name)).Append("\",\"fields\":{");
            int k = 0;
            try
            {
                var flds = c.GetType().GetFields(
                    System.Reflection.BindingFlags.Instance
                    | System.Reflection.BindingFlags.Public
                    | System.Reflection.BindingFlags.NonPublic);
                for (int i = 0; i < flds.Length && k < MaxFieldsPerComp; i++)
                {
                    var f = flds[i];
                    if (f.IsStatic)
                        continue;
                    string val;
                    try { val = FieldValue(f.GetValue(c), 0); }
                    catch (Exception) { val = "\"<读不到>\""; }
                    if (k > 0)
                        sb.Append(",");
                    sb.Append("\"").Append(SafeName(f.Name)).Append("\":").Append(val);
                    k++;
                }
            }
            catch (Exception) { }
            sb.Append("}}");
        }

        private const int MaxFieldsPerComp = 40;
        private const int MaxFieldItems = 10;
        private const int MaxStrLen = 60;

        private static string Trunc(string s)
        {
            s = s ?? "";
            return s.Length <= MaxStrLen ? s : s.Substring(0, MaxStrLen) + "…";
        }

        /// <summary>把一个字符串安全地塞进 JSON(转义 + 拍平控制字符)。
        ///
        /// ☠☠ **`SafeName` 不够** —— 它只把 `"` 换成 `'`, 而 **`\`、换行、制表符**
        ///   在 JSON 字符串里都是**非法**的: 裸 `\` 后面跟个 `U`(如 `C:\Users`)
        ///   直接让整行解析失败, 一个换行符更会把"一行一个 JSON"的协议**撕开**。
        ///   物体名/字段名里从来不会出现这些, 所以一直没暴露; 但 `AppendFields` 喂的是
        ///   **任意 `ToString()` 的输出** —— 那就什么都可能有了。
        ///   (2026-09-15 已经因为**少一个逗号**废掉过一整局, 这条比那个更容易踩。)
        /// </summary>
        private static string JsonStr(string s)
        {
            if (string.IsNullOrEmpty(s))
                return "";
            var sb = new StringBuilder(s.Length + 8);
            foreach (char ch in s)
            {
                switch (ch)
                {
                    case '"': sb.Append('\''); break;      // 与 `SafeName` 同风格: 换成单引号
                    case '\\': sb.Append('/'); break;      // 反斜杠换斜杠 —— 路径照样看得懂
                    case '\n': case '\r': case '\t': sb.Append(' '); break;
                    default: sb.Append(ch < ' ' ? ' ' : ch); break;
                }
            }
            return sb.ToString();
        }

        private static string Quote(string s)
        {
            return "\"" + JsonStr(Trunc(s)) + "\"";
        }

        /// <summary>把一个字段值渲染成 JSON 里能放的东西(见 `AppendFields` 的注释)。</summary>
        private static string FieldValue(object v, int depth)
        {
            if (v == null)
                return "null";
            if (v is string)
                return Quote((string)v);
            if (v is bool)
                return ((bool)v) ? "true" : "false";
            if (v is Enum)
                return Quote(v.ToString());          // 枚举报**名字**(Oven/Hob/Fried…)
            var uo = v as UnityEngine.Object;
            if (uo != null)
                return Quote(SafeName(uo.name));     // 资产/物体引用 → **名字**
            var t = v.GetType();
            if (t.IsPrimitive)
                return Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture);
            var en = v as System.Collections.IEnumerable;
            if (en != null && depth < 2)
            {
                var inner = new StringBuilder();
                int k = 0;
                bool more = false;
                try
                {
                    foreach (var e in en)
                    {
                        if (e == null)
                            continue;
                        if (k >= MaxFieldItems) { more = true; break; }
                        if (k > 0)
                            inner.Append(",");
                        inner.Append(FieldValue(e, depth + 1));
                        k++;
                    }
                }
                catch (Exception) { }
                return "[" + inner + (more ? ",\"…\"" : "") + "]";
            }
            // 其它一律 `ToString()` 截断 —— **不深递归**(会顺着引用成环)
            return Quote(Trunc(v.ToString()));
        }

        public static string ScanRaw()
        {
            var sb = new StringBuilder();
            var seen = new Dictionary<int, int>();
            int n = 0;
            try
            {
                var objs = UnityEngine.Object.FindObjectsOfType(typeof(Collider));
                foreach (var o in objs)
                {
                    var col = o as Collider;
                    if (col == null)
                        continue;
                    var go = col.gameObject;
                    if (go == null)
                        continue;
                    int iid = go.GetInstanceID();
                    if (seen.ContainsKey(iid))
                        continue;
                    seen[iid] = 1;

                    var comps = go.GetComponents(typeof(Component));
                    var names = new StringBuilder();
                    var full = new StringBuilder();      // ★ 全字段(见 AppendFields)
                    int cn = 0, fn = 0;
                    foreach (var c in comps)
                    {
                        if (c == null)
                            continue;
                        var t = c.GetType();
                        string ns = t.Namespace;
                        if (!string.IsNullOrEmpty(ns) && ns.StartsWith("UnityEngine"))
                            continue; // 内置组件无诊断价值
                        if (cn > 0)
                            names.Append(",");
                        names.Append("\"").Append(SafeName(t.Name)).Append("\"");
                        // ★ **别吝啬**: 每个游戏组件把**全部字段**摊出来。理由见 `AppendFields`。
                        if (fn > 0)
                            full.Append(",");
                        AppendFields(full, c);
                        cn++; fn++;
                    }
                    if (cn == 0)
                        continue;

                    var pos = go.transform.position;
                    string tag = "";
                    try { tag = go.tag; }
                    catch (Exception) { }

                    if (n > 0)
                        sb.Append(",");
                    // ⚠ `comps` 保持**原样**(数组 of 名字) —— 别的工具在吃它, 别改形状。
                    //   全字段挂在**新键** `compsFull` 上(纯新增, 谁都不受影响)。
                    sb.Append(string.Format(
                        "{{\"i\":{0},\"name\":\"{1}\",\"tag\":\"{2}\",\"x\":{3:F2},\"y\":{4:F2},\"z\":{5:F2},\"comps\":[{6}],\"compsFull\":[{9}],{7},{8}}}",
                        n, SafeName(go.name), SafeName(tag), pos.x, pos.y, pos.z, names,
                        ReadContent(go), ReadSpawn(go), full));
                    n++;
                }
            }
            catch (Exception) { }
            return string.Format("{{\"raw\":[{0}],\"count\":{1}}}", sb, n);
        }

        /// <summary>读台面上有什么: AttachStation.m_attachPoint 的子物体 / Stack 的子物体(盘子堆)。
        /// 物品是作为子 Transform 挂上去的, 不是字段, 所以这里看子物体名。</summary>
        private static string ReadContent(GameObject go)
        {
            var items = new StringBuilder();
            var tags = new StringBuilder();
            var hases = new StringBuilder();
            int cnt = 0;

            var at = FindType("AttachStation");
            if (at != null)
            {
                try
                {
                    var comp = go.GetComponent(at);
                    if (comp != null)
                    {
                        var f = at.GetField("m_attachPoint");
                        var tr = f != null ? f.GetValue(comp) as Transform : null;
                        cnt += CollectChildren(items, tags, hases, tr, cnt);
                    }
                }
                catch (Exception) { }
            }

            var st = FindType("Stack");
            if (st != null)
            {
                try
                {
                    var comp = go.GetComponent(st);
                    if (comp != null)
                    {
                        Transform tr = null;
                        var gm = st.GetMethod("GetAttachPoint");
                        if (gm != null)
                        {
                            try { tr = gm.Invoke(comp, new object[] { null }) as Transform; }
                            catch (Exception) { }
                        }
                        if (tr == null)
                        {
                            var f = st.GetField("m_AttachPoint",
                                System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance);
                            if (f != null)
                                tr = f.GetValue(comp) as Transform;
                        }
                        cnt += CollectChildren(items, tags, hases, tr, cnt);
                    }
                }
                catch (Exception) { }
            }

            // ontags: 用**游戏自己的 Unity Tag** 标出台面上每样东西的角色 ——
            // 锅是 CookingUtensil、盘子是 Plate、食材是 Ingredient/Pre-Ingredient。
            // 比在 Python 里拿名字猜("utensil_pot_01" 里有 pot)可靠得多。
            // onhas: 每样东西自己装了什么(盘子上的菜) —— 判"这个盘子是不是空的"。
            return "\"on\":[" + items + "],\"ontags\":[" + tags
                 + "],\"onhas\":[" + hases + "],\"n\":" + cnt;
        }

        /// <summary>箱子/生成器出什么食材(PickupItemSpawner.m_itemPrefab.name)。</summary>
        private static string ReadSpawn(GameObject go)
        {
            try
            {
                var st = FindType("PickupItemSpawner");
                if (st == null)
                    return "\"spawn\":\"\"";
                var sp = go.GetComponent(st);
                if (sp == null)
                    return "\"spawn\":\"\"";
                var f = st.GetField("m_itemPrefab");
                if (f == null)
                    return "\"spawn\":\"\"";
                var prefab = f.GetValue(sp) as UnityEngine.Object;
                return "\"spawn\":\"" + SafeName(prefab != null ? prefab.name : "") + "\"";
            }
            catch (Exception) { }
            return "\"spawn\":\"\"";
        }

        private static int CollectChildren(StringBuilder sb, StringBuilder tags, StringBuilder hases,
                                           Transform tr, int existing)
        {
            if (tr == null)
                return 0;
            int added = 0;
            int c = tr.childCount;
            for (int i = 0; i < c; i++)
            {
                var ch = tr.GetChild(i);
                if (ch == null)
                    continue;
                if (existing + added > 0)
                {
                    sb.Append(",");
                    tags.Append(",");
                }
                sb.Append("\"").Append(SafeName(ch.name)).Append("\"");
                string tg = "";
                try { tg = ch.tag; }
                catch (Exception) { }
                // 台面上的东西可能是"占位子物体"(attachPoint 下挂的模型), tag 取不到就退化成 Untagged
                tags.Append("\"").Append(SafeName(tg)).Append("\"");
                // onhas: 这个东西自己容器里装了什么(盘子上的菜)。
                // **关键**: 拿盘子去锅里取菜时, 盘子必须是**空的** ——
                // ServerPreparationContainer/ServerPlacementContainer 的规则是
                // "手持容器有内容 → 把内容倒进目标", 拿一个装了菜的盘子去锅边按交互,
                // 结果是把盘子里的菜倒进锅里, 正好反了。
                string has = "";
                try { has = ItemKnowledge.ContentsNames(ch.gameObject); }
                catch (Exception) { }
                if (existing + added > 0)
                    hases.Append(",");
                hases.Append("\"").Append(SafeName(has)).Append("\"");
                added++;
            }
            return added;
        }

        /// <summary>在已加载程序集里找类型(跨程序集 Type.GetType 需带程序集名)。
        /// public: ItemKnowledge 也要用; 结果缓存避免反复遍历程序集。</summary>
        public static Type FindType(string name)
        {
            Type cached;
            if (_typeCache.TryGetValue(name, out cached))
                return cached;
            var t = Type.GetType(name);
            if (t == null)
            {
                foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
                {
                    try
                    {
                        t = asm.GetType(name);
                        if (t != null)
                            break;
                    }
                    catch (Exception) { }
                }
            }
            // 只在命中时缓存: 缓存 null 会让"程序集尚未加载完"的时序问题永久化
            if (t != null)
                _typeCache[name] = t;
            return t;
        }

        private static readonly Dictionary<string, Type> _typeCache = new Dictionary<string, Type>();

        /// <summary>建/刷新**固定**台面缓存。贵的那 20 次 FindObjectsOfType 只在这里做。
        /// 易变类型(火/油渍/可推物体)不进缓存 —— 它们由 Scan() 每帧另扫。</summary>
        private static void EnsureStationCache(bool force)
        {
            string scene = "";
            try { scene = UnityEngine.SceneManagement.SceneManager.GetActiveScene().name; }
            catch (Exception) { }

            if (!force && _cache != null && _cacheScene == scene
                && Time.realtimeSinceStartup - _cacheBuiltAt < StaticRescanInterval)
                return;

            var list = new List<StationRef>();
            var seen = new Dictionary<int, int>();
            foreach (var tn in StationTypes)
            {
                if (IsVolatile(tn))
                    continue;                  // 每帧另扫, 见 Scan()
                var type = FindType(tn);
                if (type == null)
                    continue;
                try
                {
                    var objs = UnityEngine.Object.FindObjectsOfType(type);
                    foreach (var o in objs)
                    {
                        var go = GetGameObject(o);
                        if (go == null)
                            continue;
                        // 去重: 同一物体只归入优先级更高的那个类型
                        int iid = go.GetInstanceID();
                        if (seen.ContainsKey(iid))
                            continue;
                        seen[iid] = 1;
                        list.Add(MakeRef(go, tn));
                    }
                }
                catch (Exception) { }
            }
            _cache = list;
            _cacheScene = scene;
            _cacheBuiltAt = Time.realtimeSinceStartup;
        }

        /// <summary>作废静态缓存(换关卡/下一局时调)。下一次 ScanStations 会重建。</summary>
        /// <summary>把**缓存里**的台面物体交出来(给"交互足迹"用) —— 只读, 不触发重扫。
        ///
        /// 为什么要有这个口子: `StationRef` / `_cache` 都是 `private`, 而
        /// `LevelInfo` 要拿台面的 `GameObject` 才能取到 `Collider`, 进而调
        /// `InteractWithItemHelper.IsColliderInArc` 算足迹。
        ///
        /// ⚠ 调用方自己保证缓存是新的 —— 这一支**不重扫**, 拿到的可能是 5 秒前的
        ///   那一份(和 `ScanStations()` 的兜底节奏一致)。足迹是**按需**算的, 调用方
        ///   在同一个 `get_map` 里通常会先让 `Scan()` 跑一遍。
        /// ☠ 用三个平行 list 而不是返回 `StationRef` —— 那个类是 private,
        ///   而且 `iid`/`name` 才是足迹要的键, `go` 只是手段。
        /// </summary>
        public static void EachStation(List<GameObject> gos, List<int> iids, List<string> names)
        {
            gos.Clear();
            iids.Clear();
            names.Clear();
            var c = _cache;
            if (c == null)
                return;
            for (int i = 0; i < c.Count; i++)
            {
                var r = c[i];
                if (r == null || r.go == null)
                    continue;
                gos.Add(r.go);
                iids.Add(r.iid);
                names.Add(r.name);
            }
        }

        public static void InvalidateStationCache()
        {
            _cache = null;
            _cacheScene = "";
        }

        private static bool IsVolatile(string typeName)
        {
            for (int i = 0; i < VolatileTypes.Length; i++)
                if (VolatileTypes[i] == typeName)
                    return true;
            return false;
        }

        /// <summary>给一个台面物体建缓存记录 —— **只存结构事实**(引用 / iid / 名字 / 挂点)。
        ///
        /// ☠☠ **身份不在这里算** —— 那正是本轮改掉的东西。这里原来会
        ///   `r.staticJson = DescribeStatic(go, typeName)`, 于是身份跟着缓存一起吃 5 秒
        ///   (`StaticRescanInterval`), 而运行期真的会变: 见 `Reclassify` 的长注释。
        ///   现在身份由 `AppendRef` **每帧**重算。
        /// ⚠ 坐标本来就不存(锅会被端走、可推物体会动), 现在连身份也不存了 ——
        ///   这个类里剩下的全是"物体活着就不会变"的东西。
        /// </summary>
        private static StationRef MakeRef(GameObject go, string typeName)
        {
            var r = new StationRef { go = go, hint = typeName, name = go.name };
            try { r.iid = go.GetInstanceID(); } catch (Exception) { }
            r.attach = AttachPointOf(go);
            return r;
        }

        /// <summary>**这个物体现在是什么台面** —— 每帧重判, 不看缓存。
        ///
        /// ☠☠ 为什么"台面身份"不能缓存(2026-09-15 用户原话:
        ///   "**台面身份千万别缓冲, 这一块需要实时的**")。反编译里有**两处**确凿依据:
        ///
        ///   ① `ServerFlamethrowerSpray.cs:70` —— 火焰喷到一个台面上时
        ///      `attachStation.gameObject.AddComponent&lt;CookingStation&gt;()`,
        ///      紧接着 `:71` `stationSmouldering.Cooker.m_stationType =
        ///      CookingStationType.Flamethrower`
        ///      ⇒ 那个台面**当场从"普通台面"变成一台火炬灶**, 而缓存里还是老身份
        ///      (Python 侧按 `typeName`/`sub` 落到 `_STATION_SEM`, 于是把它当普通台面);
        ///   ② `ClientSessionInteractable.cs:101` `m_session = BuildSession(_avatar)` /
        ///      `:108` `m_session = null` —— **上车/下车**时赋值
        ///      ⇒ 遥感台那句"现在是不是正在驾驶"是**纯运行态**, 见 `DescribeIdentity`。
        ///
        /// 缓存 5 秒 ⇒ 这 5 秒里引擎**把平台当厨师开**(navigate 发移动键 → 平台乱跑、
        /// 卡住检测误判、侧移脱困 → 更乱)、**把火炬台当普通台面**。
        ///   这正是本项目最贵的那类错 —— "**假'能'的代价是执行一个错的物理动作**"。
        ///
        /// ⚠ **优先级顺序必须和 `EnsureStationCache` 那轮发现完全一致**
        ///   (`StationTypes` 的先后就是优先级: 派生类在前)。否则同一个物体会在
        ///   "发现时叫 A、每帧叫 B"之间来回抖, 而 `id` 前缀就是类型名 ⇒ **id 跟着抖**
        ///   ⇒ Python 侧按 id 建的索引(`_spot_now`/`assemble_spot`)整片失效。
        /// ⚠ **易变类型**(火/油渍/可推物体)在 `ScanStations` ② 里每帧另扫, 这里
        ///   **跳过**(和发现那轮一致) —— 它们认不出来, 落到 `hint` 兜底。
        /// ⚠ 代价: 每帧对**已经在手里的** ~N 个物体各做几次 `GetComponent`。
        ///   它和那轮发现**不是一个数量级** —— 发现是 20 次 `FindObjectsOfType`
        ///   (**全场景遍历**), 这里是已知物体上的**组件查找**。实测数见 `IdentityMs`。
        /// </summary>
        private static string Reclassify(GameObject go, string hint)
        {
            if (go == null)
                return hint;
            for (int i = 0; i < StationTypes.Length; i++)
            {
                string tn = StationTypes[i];
                if (IsVolatile(tn))
                    continue;                  // 每帧另扫, 见 ScanStations ②
                var type = FindType(tn);
                if (type == null)
                    continue;
                try
                {
                    if (go.GetComponent(type) != null)
                        return tn;
                }
                catch (Exception) { }
            }
            return hint;
        }

        /// <summary>按名字反射读一个字段, 读不到返回 null(不抛)。
        ///
        /// ⚠ 这里**不能用 `Func&lt;&gt;` / lambda** —— 本插件是用 `-nostdlib+` 显式引
        ///   .NET **2.0** 的 mscorlib 编译的(`build.bat`), 而 `Func&lt;T,TResult&gt;` 是
        ///   3.5 才进 mscorlib 的 ⇒ `error CS0246: 未能找到类型 Func&lt;,&gt;`(实测踩过)。
        ///   所以这类小工具一律写成方法。</summary>
        private static object ObjField(Type t, object comp, string name)
        {
            try
            {
                var f = t.GetField(name, BindingFlags.Instance
                                       | BindingFlags.Public | BindingFlags.NonPublic);
                return f == null ? null : f.GetValue(comp);
            }
            catch (Exception) { return null; }
        }

        /// <summary>同 ObjField, 但转成 float; 读不到返回 0。</summary>
        private static float NumField(Type t, object comp, string name)
        {
            try
            {
                object o = ObjField(t, comp, name);
                return o == null ? 0f : Convert.ToSingle(o);
            }
            catch (Exception) { return 0f; }
        }

        /// <summary>台面上内容物的挂点: Stack 优先(它可能被移动), 否则 AttachStation.m_attachPoint。
        /// 物品是挂在它下面的**子 Transform**, 不是字段 —— 所以只能看子物体。
        /// 这是"物品传递/接力(a 放台面 → b 接手)"唯一的观测手段。</summary>
        private static Transform AttachPointOf(GameObject go)
        {
            Transform ap = null;
            try
            {
                var at = FindType("AttachStation");
                if (at != null)
                {
                    var comp = go.GetComponent(at);
                    if (comp != null)
                    {
                        var f = at.GetField("m_attachPoint");
                        if (f != null)
                            ap = f.GetValue(comp) as Transform;
                    }
                }
                var st = FindType("Stack");
                if (st != null)
                {
                    var comp = go.GetComponent(st);
                    if (comp != null)
                    {
                        var gm = st.GetMethod("GetAttachPoint");
                        if (gm != null)
                        {
                            try
                            {
                                var tr = gm.Invoke(comp, new object[] { null }) as Transform;
                                if (tr != null && (ap == null || tr.childCount > 0))
                                    ap = tr;
                            }
                            catch (Exception) { }
                        }
                    }
                }
            }
            catch (Exception) { }
            return ap;
        }

        /// <summary>台面的**内容**部分 —— 每帧重算(用户提的"实时地图")。
        ///
        /// `n` / `on` / `ontags` 每帧都算(只是走一遍 Transform, 微秒级);
        /// `onhas` 走**反射**(ItemKnowledge.ContentsNames → GetContents() 的
        /// MethodInfo.Invoke), 按 OnhasInterval 节流并复用上一次的值。
        ///
        /// ⚠ ontags/onhas 必须和 on **同步、同长**(Python 侧按下标一一对应):
        ///   ontags = 游戏自己的 Unity Tag(锅=CookingUtensil / 盘=Plate)
        ///   onhas  = 这个东西容器里装了什么 —— 判"这个盘子是不是空的"。
        ///   拿一个装了菜的盘子去锅边按交互会把菜倒进锅里(方向正好相反),
        ///   所以取菜前必须能认出空盘。三个数组的长度必须一致。
        /// </summary>
        private static string DescribeDynamic(StationRef r, float now)
        {
            var sb = new StringBuilder();
            if (r.go == null)
                return "";

            // 台上/容器里的食材(CookableIngredient)
            //
            // ⚠ 原来用 `GetComponentInChildren` —— **只返回第一个**。一个盘子装三样菜时
            //   只报一样。改成收集**全部**, 用 "+" 拼起来(和 onhas 的拼法一致)。
            try
            {
                var ingType = FindType("CookableIngredient");
                if (ingType != null)
                {
                    var ings = r.go.GetComponentsInChildren(ingType);
                    if (ings != null && ings.Length > 0)
                    {
                        var f = ingType.GetField("m_ingredientOrderNode");
                        if (f != null)
                        {
                            var names = new StringBuilder();
                            int got = 0;
                            for (int k = 0; k < ings.Length && got < MaxOnShown; k++)
                            {
                                if (ings[k] == null)
                                    continue;
                                var node = f.GetValue(ings[k]);
                                if (node == null)
                                    continue;
                                var np = node.GetType().GetProperty("name");
                                if (np == null)
                                    continue;
                                var nm = (string)np.GetValue(node, null);
                                if (string.IsNullOrEmpty(nm))
                                    continue;
                                if (got > 0)
                                    names.Append("+");
                                names.Append(SafeName(nm));
                                got++;
                            }
                            if (got > 0)
                                sb.Append(string.Format(",\"ing\":\"{0}\"", names.ToString()));
                        }
                    }
                }
            }
            catch (Exception) { }

            // 台面上放着什么 + 数量
            try
            {
                var ap = r.attach;
                if (ap == null)
                    ap = r.attach = AttachPointOf(r.go);   // 缓存没了就补一次(台面被换过)
                if (ap != null)
                {
                    int c = ap.childCount;
                    sb.Append(string.Format(",\"n\":{0}", c));
                    if (c > 0)
                    {
                        // onhas 节流: 它走反射, 是动态层里最贵的一项
                        bool heavy = (OnhasInterval <= 0f) || (now - r.lastHasAt >= OnhasInterval);
                        var tags = new StringBuilder();
                        var hases = new StringBuilder();
                        sb.Append(",\"on\":[");
                        int shown = 0;
                        for (int i = 0; i < c && shown < MaxOnShown; i++)
                        {
                            var ch = ap.GetChild(i);
                            if (ch == null)
                                continue;
                            if (shown > 0)
                            {
                                sb.Append(",");
                                tags.Append(",");
                                hases.Append(",");
                            }
                            sb.Append("\"").Append(SafeName(ch.name)).Append("\"");
                            string tg = "";
                            try { tg = ch.tag; }
                            catch (Exception) { }
                            tags.Append("\"").Append(SafeName(tg)).Append("\"");
                            // ⚠ 按**子物体下标**存取 —— 见 `StationRef.lastHas` 那段:
                            //   存的取的是同一个下标, 节流(OnhasInterval>0)时也不会串号。
                            while (r.lastHas.Count <= i)
                                r.lastHas.Add("");
                            if (heavy)
                            {
                                string has = "";
                                try { has = ItemKnowledge.ContentsNames(ch.gameObject); }
                                catch (Exception) { }
                                r.lastHas[i] = has;
                            }
                            hases.Append("\"").Append(SafeName(r.lastHas[i])).Append("\"");
                            shown++;
                        }
                        sb.Append("],\"ontags\":[").Append(tags)
                          .Append("],\"onhas\":[").Append(hases).Append("]");
                        if (heavy)
                            r.lastHasAt = now;
                    }
                }
            }
            catch (Exception) { }

            return sb.ToString();
        }

        /// <summary>台面的**身份**部分(tag / sub / spawn / plate) —— 只在建缓存时算一次。
        /// 内容(`ing`/`n`/`on`/`ontags`/`onhas`)见 DescribeDynamic()。</summary>
        /// <summary>**身份**片段(tag/sub/spawn/plate/session…) —— **每帧现读, 不许缓存**。
        ///
        /// ☠☠ 它原来叫 `DescribeStatic`, 结果被塞进 `StationRef.staticJson` 缓存 5 秒。
        ///   改名的理由就是"名字别把意图写成结果": 这里面**混着运行态**
        ///   (`session` 是"现在是不是正在驾驶")。
        ///
        /// 为什么必须实时(用户 2026-09-15: "台面身份千万别缓冲, 这一块需要实时的"):
        ///   · `ServerFlamethrowerSpray.cs:70-71` —— 运行期给已有台面
        ///     `AddComponent&lt;CookingStation&gt;()` 并改 `m_stationType`
        ///     ⇒ **类型名和 `sub` 当场就变**(所以调用方得先 `Reclassify`);
        ///   · `ClientSessionInteractable.cs:101/108` —— 上车/下车写 `m_session`
        ///     ⇒ `session` 是纯运行态。
        ///   ⚠ 这个文件已经为**内容**(`ing`/`n`/`on`/`ontags`/`onhas`)做过同一次搬家
        ///     (见下面那段"已经搬到 DescribeDynamic"的注释) —— 本轮补的是**身份**那一半。
        ///
        /// ⚠ 这里剩下的 `exitPortal`/`land`/`arc`/`cooldown`/`recvDelay`/`spawn`/`plate`
        ///   在反编译里**没有**找到运行期写入(是 prefab/序列化配置), 但既然身份整体
        ///   改成每帧算了, 就**一起现读** —— 省得以后再判一次"这个到底会不会变"。
        /// </summary>
        private static string DescribeIdentity(GameObject go, string typeName)
        {
            var sb = new StringBuilder();

            // **游戏自己的角色分类就是 tag**。
            // 依据 GameUtils.cs:504-707 的一整套查找器, 它们全是"按 tag 找 + 按组件筛":
            //   GetAllIngredients     → tag "Pre-Ingredient" ∪ "Ingredient"
            //   GetIngredientCrates   → tag "Crate"
            //   FindEmptyContainers   → tag "Plate"
            //   GetPlayerHeldItems    → tag "Player"
            // 以及 ServerUtensilRespawnBehaviour.cs:123 用
            //   CompareTag("CookingStation") / ("PlateReturn") / ("PlateStation")
            //   加 RequestComponent<RubbishBin/ConveyorStation/WashingStation>() 区分台面角色。
            // 也就是说: 光看组件类型分不出"这个台面是灶台还是回收台", tag 才是权威。
            try
            {
                string tag = go.tag;
                if (!string.IsNullOrEmpty(tag) && tag != "Untagged")
                    sb.Append(string.Format(",\"tag\":\"{0}\"", SafeName(tag)));
            }
            catch (Exception) { }

            // ---- 传送门: **配对** ----
            //
            // 传送门是**两两配对**的, 而配对信息就是 `Teleportal` 上的一个**直接引用**:
            //   `Teleportal.cs:13-14`  `[SerializeField] public GameObject m_exitPortal;`
            // 我们原来只读名字+坐标 ⇒ 三扇门在管线里是**三个互不相干的点**, 谁也不通向谁,
            // 规划层就没法把"门"当成一条边用(只能当装饰)。
            //
            // 顺带把**落点**读出来: `m_teleportPoint`(Teleportal.cs:5-7) 才是真正把人
            // 放下的位置, 和门的视觉位置**不是一处** —— 实测 s_wizard_school_3_4 上
            // 占用表说门在 z=8.4 而物体在 z=7.28, 差约一格。拿视觉位置当落点会
            // "站在门外够不着"。
            //
            // 还有 `m_cooldownTime` / `m_receiveDelay`: 连着用会不会被冷却卡住,
            // 是规划时要考虑的时序。`m_teleportArc` 决定出来之后的朝向。
            // ---- 遥感/遥控驾驶台: **现在是不是正在驾驶** ----
            //
            // 机制(反编译, 见 `Terminal.cs` / `ServerTerminal.cs` / `ServerPilotMovement.cs`):
            //   走到控制台按交互 → 开始一个 session:
            //     · 厨师自己的 `PlayerControls.enabled = false`, 刚体转 kinematic(人定住)
            //     · 控制权交给被驾驶的物体(`AssignPlayer(ControlScheme)`)
            //   ⇒ **此后发的移动键驱动的是那块平台, 不是厨师**。
            //   会话在按下 拾取/交互/冲刺 任意一个键时结束。
            //
            // 为什么必须报出来: 引擎不知道这件事就会**把平台当厨师开** ——
            //   `navigate()` 发移动键 → 平台乱跑; 卡住检测判"厨师卡住" → 侧移脱困 → 更乱。
            //   而"退出"用的正是交互键, 所以 `interact()` 在会话中等于**踩刹车**。
            //
            // 信号是现成的公开属性: `ClientSessionInteractable.HasSession => m_session != null`。
            //
            // ☠☠ **这一条就是"身份不能缓存"最直接的证据** —— `m_session` 在
            //   `ClientSessionInteractable.cs:101`(BuildSession, 上车)与 `:108`(= null, 下车)
            //   被赋值, 是**纯运行态**。而它原来躺在 5 秒缓存的 `staticJson` 里
            //   ⇒ 用户按下交互键上了驾驶台之后, 引擎**最多 5 秒后才知道**,
            //   这 5 秒里它以为厨师还能走, 于是把平台当厨师开。
            //   (用户 2026-09-15: "台面身份千万别缓冲, 这一块需要实时的"。)
            if (typeName == "Terminal")
            {
                try
                {
                    var ct = SceneScanner.FindType("ClientTerminal");
                    var comp = ct != null ? go.GetComponent(ct) : null;
                    if (comp != null)
                    {
                        var prop = ct.GetProperty("HasSession");
                        bool has = prop != null && (bool)prop.GetValue(comp, null);
                        sb.Append(",\"session\":").Append(has ? "true" : "false");
                    }
                    // 顺便报出**它驾驶的是哪个物体** —— 便于把"台子"和"平台"对上。
                    var term = go.GetComponent(typeName);
                    if (term != null)
                    {
                        object po = ObjField(term.GetType(), term, "m_pilotableObject");
                        var pc = po as Component;
                        if (pc != null && pc.gameObject != null)
                            sb.Append(",\"pilots\":\"").Append(SafeName(pc.gameObject.name))
                              .Append("\"");
                    }
                }
                catch (Exception) { }
            }

            if (typeName == "Teleportal")
            {
                try
                {
                    var comp = go.GetComponent(typeName);
                    if (comp != null)
                    {
                        var t = comp.GetType();

                        var exit = ObjField(t, comp, "m_exitPortal") as GameObject;
                        if (exit != null)
                        {
                            Vector3 ep = exit.transform.position;
                            sb.Append(string.Format(
                                System.Globalization.CultureInfo.InvariantCulture,
                                ",\"exitPortal\":\"{0}\",\"exitX\":{1:F2},\"exitZ\":{2:F2}",
                                SafeName(exit.name), ep.x, ep.z));
                        }
                        var pt = ObjField(t, comp, "m_teleportPoint") as Transform;
                        if (pt != null)
                        {
                            Vector3 lp = pt.position;
                            sb.Append(string.Format(
                                System.Globalization.CultureInfo.InvariantCulture,
                                ",\"landX\":{0:F2},\"landZ\":{1:F2}", lp.x, lp.z));
                        }
                        sb.Append(string.Format(
                            System.Globalization.CultureInfo.InvariantCulture,
                            ",\"arc\":{0:F1},\"cooldown\":{1:F2},\"recvDelay\":{2:F2}",
                            NumField(t, comp, "m_teleportArc"),
                            NumField(t, comp, "m_cooldownTime"),
                            NumField(t, comp, "m_receiveDelay")));
                    }
                }
                catch (Exception) { }
            }

            // CookingStation.m_stationType (Hob/Oven/Fryer...)
            //
            // ⚠ 必须**连派生类一起**读。`m_stationType` 声明在基类 `CookingStation.cs:12`,
            //   而 `HeatedCookingStation : CookingStation`(HeatedCookingStation.cs:3)。
            //   原来只判 `typeName == "CookingStation"` ⇒ **HeatedCookingStation(烤箱/炸锅)
            //   的 sub 永远是空** ⇒ Python 落到 `_STATION_SEM.get("", "hob")` ⇒ 全被当成普通灶台。
            //   潜伏危害: 一旦遇到"灶台和烤箱并存"的关卡, 把需要 Oven 的菜放到 Hob 上会被
            //   游戏拒绝(CookingStation.cs:39 `GetRequiredStationType() != m_stationType`)。
            if (typeName == "CookingStation" || typeName == "HeatedCookingStation")
            {
                try
                {
                    var comp = go.GetComponent(typeName);
                    if (comp != null)
                    {
                        // 用 FindFieldUp 类的回溯找法: 字段在基类上, 派生类型 GetField 也能拿到 public,
                        // 但用 DeclaredOnly 之外的绑定再兜一层更稳。
                        var f = comp.GetType().GetField("m_stationType",
                            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                        if (f == null && comp.GetType().BaseType != null)
                            f = comp.GetType().BaseType.GetField("m_stationType",
                                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                        if (f != null)
                        {
                            var v = f.GetValue(comp);
                            sb.Append(string.Format(",\"sub\":\"{0}\"", SafeName(v == null ? "" : v.ToString())));
                        }
                    }
                }
                catch (Exception) { }
            }

            // PickupItemSpawner.m_itemPrefab.name → 箱子/生成器出什么食材
            try
            {
                var spawnerType = FindType("PickupItemSpawner");
                if (spawnerType != null)
                {
                    var sp = go.GetComponentInChildren(spawnerType);
                    if (sp != null)
                    {
                        var f = spawnerType.GetField("m_itemPrefab");
                        if (f != null)
                        {
                            var prefab = f.GetValue(sp) as UnityEngine.Object;
                            if (prefab != null)
                                sb.Append(string.Format(",\"spawn\":\"{0}\"", SafeName(prefab.name)));
                        }
                    }
                }
            }
            catch (Exception) { }

            // ⚠ 到这里为止的内容**全部每帧现读**(调用方 `AppendRef` 每帧调一次)。
            //   历史: `ing` / `n/on/ontags/onhas` 先是搬去了 `DescribeDynamic()`
            //   (它们是内容, 会每帧变); 本轮把**身份**(tag / sub / spawn / plate /
            //   session / 传送门那一组)也改成每帧 —— 依据见本函数的头注释。
            //   ⇒ 现在这个名字叫 `DescribeIdentity`, 不再叫 `DescribeStatic`:
            //     **名字别把意图写成结果**(这个文件里为这条踩过好几次)。

            // 盘子堆提供哪种容器(PlateStackBase.GetPlatingStep → 对比订单的 m_platingStep)
            if (typeName == "CleanPlateStack" || typeName == "DirtyPlateStack"
                || typeName == "PlateReturnStation")
            {
                try
                {
                    var comp = go.GetComponent(typeName);
                    if (comp != null)
                    {
                        var gm = comp.GetType().GetMethod("GetPlatingStep");
                        if (gm != null)
                        {
                            var ps = gm.Invoke(comp, null) as UnityEngine.Object;
                            if (ps != null)
                                sb.Append(string.Format(",\"plate\":\"{0}\"", SafeName(ps.name)));
                        }
                    }
                }
                catch (Exception) { }
            }

            return sb.ToString();
        }

        private static GameObject GetGameObject(object componentOrObj)
        {
            if (componentOrObj is GameObject go)
                return go;
            var t = componentOrObj.GetType().GetProperty("gameObject");
            return t?.GetValue(componentOrObj, null) as GameObject;
        }

        /// <summary>读厨师手里拿的物品名(ICarrierPlacement.InspectCarriedItem)。</summary>
        /// <summary>读厨师手上拿着什么。
        ///
        /// 依据(游戏自己的读法): GameUtils.cs:693-707 GetPlayerHeldItems()
        ///     ICarrier carrier = go.RequireInterface&lt;ICarrier&gt;();
        ///     GameObject item = carrier.InspectCarriedItem();
        ///   ICarrier : ICarrierPlacement, InspectCarriedItem() 定义在 ICarrierPlacement.cs:5。
        ///
        /// **必须优先读 Server 那一份**(用户指出的"拿了又放下"就出在这里):
        ///   ServerPlayerAttachmentCarrier.InspectCarriedItem()  (ServerPlayerAttachmentCarrier.cs:100)
        ///     读的是 m_carriedObjects[Default], **拾取时服务端立即写入** —— 权威值。
        ///   ClientPlayerAttachmentCarrier.InspectCarriedItem()  (ClientPlayerAttachmentCarrier.cs:69)
        ///     两边实现**一模一样**, 但客户端这份靠同步消息更新, 而环回消息要等
        ///     MultiplayerController.LateUpdate 的 Dispatch() 才送达 —— **会晚一帧**。
        ///   原来先读 Client: 刚拿到的一瞬间可能读成空 → 脚本判定"没拿到" → 重试时
        ///   **再按一次 pickup 把东西放下**。拿/放是同一个键的开关, 误判的代价是毁掉战果。
        ///
        /// 两份都返回, 便于发现"客户端滞后"这类问题。
        /// </summary>
        /// <summary>厨师手上拿着什么, 以及**手上那件容器里装了什么**。
        ///
        /// ☠☠ `heldHas` 是 2026-09-15 加的(用户实机描述的现象):
        ///   > "脚本拿着食材去盘子那, 放上盘子**被我拿着并且重新放一个空盘子**,
        ///   >  脚本会拿着**空盘子**去提交。"
        ///
        ///   根因: 插件原来只报 `held`(`equipment_plate_01 (3)`)—— **只有名字, 没有内容**
        ///   ⇒ Python 侧唯一能做的核对是 `_is_plate(held)`, 而**空盘和装好的菜
        ///   在它眼里一模一样**。于是"端起来之后"这一段完全没有判据可用:
        ///     · `_deliver_plate` 送出前只知道"手上是个盘子";
        ///     · 台面的 `onhas` 只覆盖**还在台面上**的那盘, 一端起来就看不见了。
        ///
        ///   复用**现成的** `ItemKnowledge.ContentsNames(go)` —— 台面 `onhas`
        ///   就是它算的(见 `CollectChildren`), 同一个盘子对象, 不另写一份。
        ///   判据(反编译)也是同一条: `ServerIngredientContainer.GetContents()`
        ///   → `m_composition` 递归(见 `ContentsNames` 的注释)。
        ///
        ///   ⚠ 取值口径与 `held` **一致**: 服务端权威优先, 客户端那份晚一帧。
        /// </summary>
        private static void ReadHeldItems(GameObject chefGo, out string serverHeld,
                                          out string clientHeld, out string heldHas)
        {
            serverHeld = "";
            clientHeld = "";
            heldHas = "";
            try
            {
                string ch, sh;
                clientHeld = ReadFromCarrier(chefGo, "ClientPlayerAttachmentCarrier", out ch);
                serverHeld = ReadFromCarrier(chefGo, "ServerPlayerAttachmentCarrier", out sh);
                heldHas = sh.Length > 0 ? sh : ch;      // 和 `held` 同一个取舍: 服务端优先
                if (serverHeld.Length == 0 && clientHeld.Length == 0)
                {
                    serverHeld = ReadFromCarrier(chefGo, "PlayerAttachmentCarrier", out sh);
                    heldHas = sh;
                }
            }
            catch (Exception) { }
        }

        private static volatile string _lastReflectErr = "";

        /// <summary>读厨师某个 carrier 上的东西 —— 返回**物体名**; `contents` 顺带给出
        /// **这件容器里装了什么**(如盘子上的菜)。
        ///
        /// `contents` 见 `ReadHeldItems` 的注释: 光盘子名分不出"空盘"和"装好的菜"。</summary>
        private static string ReadFromCarrier(GameObject chefGo, string typeName,
                                              out string contents)
        {
            contents = "";
            try
            {
                var ct = FindType(typeName);
                if (ct == null)
                    return "";
                var carrier = chefGo.GetComponentInChildren(ct);
                if (carrier == null)
                    return "";
                // ⚠ **必须显式给空参数列表**:
                //   这两个类各有**两个重载**
                //       ClientPlayerAttachmentCarrier.cs:69  InspectCarriedItem()
                //       ClientPlayerAttachmentCarrier.cs:74  InspectCarriedItem(PlayerAttachTarget)
                //       ServerPlayerAttachmentCarrier.cs:100 / :105 同上
                //   而 Type.GetMethod("名字") 在名字匹配到多个重载时会抛
                //   **AmbiguousMatchException** —— 之前这个异常被 catch(Exception){} 吞掉,
                //   于是 held 永远读成空串: 引擎以为"没拿到" -> 重试再按一次 pickup
                //   -> **把刚拿到的东西放回去**。这就是"拿了又放下"的真正根因,
                //   而且从第一版起就存在。
                var m = ct.GetMethod("InspectCarriedItem", Type.EmptyTypes);
                if (m == null)
                {
                    _lastReflectErr = typeName + ".InspectCarriedItem 找不到无参重载";
                    return "";
                }
                var item = m.Invoke(carrier, null) as UnityEngine.Object;
                if (item == null)
                    return "";
                // **容器内容** —— 复用台面 `onhas` 用的同一个原语(见 `CollectChildren`)。
                //   ⚠ 单独 try: 读内容失败不该把"手上拿着什么"一起弄丢 ——
                //     `held` 是很多判据的命根子(`_is_plate`/`verify_hold_change`)。
                try
                {
                    var igo = item as GameObject
                              ?? (item as Component != null ? (item as Component).gameObject : null);
                    if (igo != null)
                        contents = SafeName(ItemKnowledge.ContentsNames(igo));
                }
                catch (Exception) { }
                return SafeName(item.name);
            }
            catch (Exception ex)
            {
                // 不要再静默吞掉 —— 这个 bug 被 catch{} 藏了很久
                _lastReflectErr = typeName + ": " + ex.GetType().Name + " " + ex.Message;
                return "";
            }
        }

        /// <summary>把最近一次反射错误吐给桥, 便于在日志里看到(不再静默失败)。</summary>
        public static string LastReflectError()
        {
            return _lastReflectErr;
        }

        private static string ReadHeldItem(GameObject chefGo)
        {
            string s, c, h;
            ReadHeldItems(chefGo, out s, out c, out h);
            return s.Length > 0 ? s : c;
        }

        private static string SafeName(string n)
        {
            if (string.IsNullOrEmpty(n))
                return "";
            return n.Replace("\"", "'");
        }
    }
}
