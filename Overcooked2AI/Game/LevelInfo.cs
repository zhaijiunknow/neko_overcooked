using System;
using System.Collections.Generic;
using System.Text;
using UnityEngine;

namespace Overcooked2AI.Game
{
    /// <summary>关卡地图认知: 把**游戏自己那张网格**整张读出来交给脚本, 而不是让脚本猜障碍。
    ///
    /// 依据(反编译):
    ///   · GridManager.GetGridOccupant(GridIndex)        → 该格占用物(可读 tag)
    ///   · GridManager.GetPosFromGridLocation(GridIndex) → 该格世界坐标
    ///   · GridManager.GetGridHalfSize()                 → 索引范围 ±half
    ///   · GridNavSpace.Start() 只用 y=0 层建可走图: m_nodeMap[x,z] = (GetGridOccupant == null)
    ///   · InteractWithItemHelper.cs:46 说明占用物里有三种特殊 tag:
    ///       "Hazard" / "Travelator" / "MovingPlatform"
    ///   · 水面/岩浆/深渊是 RespawnCollider 触发器, **不是占用物**
    ///       ⇒ 原生 FindPath 会把水面当可走格, 厨师直接走进去淹死。
    ///
    /// 本模块补上原生寻路的两处盲区:
    ///   1) 把致命的 RespawnCollider 的 XZ 范围投到格子上 → 'H'
    ///   2) 对空格子向下打一条射线, 打不到地面 → 'V' (空洞, 会掉下去)
    ///
    /// 危险区判定的两条纪律(踩过坑之后加的):
    ///   · **按类型过滤**: LevelBounds 的 4 面边界墙 + KillPlane 也是 RespawnCollider,
    ///     全都当水面会让整张图变禁行。Drowning(水面/岩浆) 一律致命;
    ///     FallDeath 必须"与本地板同高"且"覆盖面积不过半"才算(KillPlane 在地板下方很远)。
    ///   · **覆盖率保险丝**: 单个 FallDeath 区覆盖超过一半可走格 → 判为边界盒, 忽略。
    ///
    /// 每格字符:
    ///   '.'  可走(空 + 有地面)          '#'  被普通占用物占住(墙/橱柜/台面/灶台)
    ///   'F'  被 "Hazard" 占用(火焰)     'P'  被 "MovingPlatform" 占用(会动, 能站)
    ///   'T'  被 "Travelator" 占用(传送带)'H'  危险区(水面/岩浆) —— 空着, 但踩上去会死
    ///   'V'  空洞(空着, 但脚下没地面)
    ///   'v'  地板太低(单向落差 / 正在下沉的平台, 例如会沉的荷叶)
    ///   'C'  台面传送带(ConveyorStation): 走不上去, 而且放上去的东西会被传走
    /// </summary>
    public static class LevelInfo
    {
        private static string _cache = "";
        private static string _cacheScene = "";
        private static float _cacheTime = -999f;

        private struct Haz
        {
            public string Name;
            public string Type;
            public float X0, X1, Z0, Z1, Y0, Y1;
            public bool KillPlane;
            public bool WorldVolume;   // 体积远大于整个网格 = 世界级体积(真·KillPlane), 不是本地危险区
            public bool Kills;         // 类型致命 且 与本地板同高 且 不是世界级体积
            public bool Use;           // 最终真的拿来当危险格
            public int Cells;          // 覆盖了多少个空格子
        }

        private static readonly List<Haz> _haz = new List<Haz>();

        /// <summary>RespawnCollider 的类型, 缓存给 FloorChar 用。</summary>
        private static Type _respawnType;

        /// <summary>诊断: 上一条**全层**射线命中的层号(-1 = 没打到)。见 FloorChar。</summary>
        private static int _probeLayer;
        /// <summary>诊断: 上一条全层射线打中的物体**是不是危险面**(有 RespawnCollider)。</summary>
        private static bool _probeHaz;
        /// <summary>诊断: 上一条全层射线打中的**物体名**(用来回答"那到底是什么")。</summary>
        private static string _probeName = "";
        /// <summary>诊断: 上一条全层射线打中的危险面的 `RespawnType`(Drowning/FallDeath/...)。</summary>
        private static string _probeRT = "";

        /// <summary>读 `RespawnCollider.m_respawnType`。</summary>
        private static string TypeName(Type rp, Component rc)
        {
            try
            {
                var f = rp.GetField("m_respawnType",
                    System.Reflection.BindingFlags.Instance
                    | System.Reflection.BindingFlags.Public
                    | System.Reflection.BindingFlags.NonPublic);
                object v = f != null ? f.GetValue(rc) : null;
                return v == null ? "" : v.ToString();
            }
            catch (Exception) { return ""; }
        }

        /// <summary>"地板会不会动"用到的两个组件类型(缓存)。见 FloorChar。</summary>
        private static Type _movingPlatType;
        private static Type _pilotType;

        /// <summary>ConveyorStation 的类型, 缓存给 OccupantChar 用。</summary>
        /// <summary>本关**所有**活跃的网格管理器 —— 占用判定要问遍它们。
        /// 为什么(实测 s_wizard_school_3_4 `活跃网格数=2`): 只问一个的话,
        /// 另一个网格管的区域整片查不到占用物 ⇒ 全判成空洞 ⇒ 13/27 台面够不着。
        /// 见 AllGridManagers() 的注释。</summary>
        private static List<GridManager> _allGms = new List<GridManager>();

        /// <summary>地面射线的参考高度 = 所有厨师的最高点(见 ReadChefFloorY)。
        /// `FloorChar` 从 `_floorRefY + 10` 往上起打, 覆盖 20 格, 装得下多平台落差。
        ///
        /// ☠ **地雷 (用户指出, 未拆)**: 这个值**源头是玩家位置** ——
        ///   取的是"所有厨师当前 y 的最大值", 所以**厨师一换层它就跳**
        ///   (实测: 走一趟传送门 `(20.3,-4.3)→(14.4,-4.1)` 就可能从 -1.84 跳到 0.00)。
        ///   而它不只当参考: 射线窗口(`FloorChar`)和**危险区同层判定**
        ///   (`CollectHazards` 的 `sameLevel = b.max.y >= floorY - 0.4`)都吃它。
        ///   ⇒ **一次传送/掉落就可能改变整张图的危险区归类** —— 这和"坑 4:
        ///     全局量 + 取样不确定, 伪装成地形问题"是同一个病。
        ///
        ///   现在之所以还没炸: 取 max 让它对 **FindObjectsOfType 的枚举顺序**免疫
        ///   (原来取 objs[0], 同一关两次读出 -1.84 / 0.00), 但**对"厨师在哪"依然敏感**。
        ///
        /// 🛠 **正确的拆法**(建议): 让射线窗口**与厨师无关** ——
        ///   从一个固定的高处往下打(比如由关卡几何/危险区顶部推出来的高度, 或一个
        ///   足够大的常量), 覆盖所有可能的地板层。危险区的"同层"判定同理,
        ///   应该拿**这一格自己的地板高度**去比(像 `BlockedBy` 那样已经改成按格取),
        ///   而不是拿一个全局量。`'v'`/`'^'` 的判定**已经不吃它了**(一律用厨师自己的 y),
        ///   所以拆掉之后不会影响那部分。</summary>
        private static float _floorRefY;

        private static Type _conveyorType;

        /// <summary>地面层掩码 = Ground | SlopedGround —— **游戏自己就是这么探地面的**。
        ///
        /// 层名来自游戏工程的 LayerManager(已从 globalgamemanagers 解析出来):
        ///   Default / TransparentFX / Ignore Raycast / Water / UI /
        ///   Players / Ground / Walls / Worktops / PlayersRespawn / AttachedBackpack /
        ///   Attachments / HeldAttachments / Beings / PlayerTriggerZone /
        ///   PlateStationBlock / CookingStationBlock / BinBlock / PushedObject /
        ///   PushedObjectBounds / TableBlock / Administration / SlopedGround /
        ///   Camera / **KillPlane** / PausableUI
        ///
        /// 关键点: **KillPlane 是独立的一层**。之前我的向下射线不带掩码, 会打到铺满全图的
        /// KillPlane, 于是每个格子都"有地面", 空洞检测形同虚设。带上 Ground|SlopedGround
        /// 之后 KillPlane 自然被排除 —— 这才是正统修法(组件判断只是兜底)。
        /// 这里**运行时用 NameToLayer 解析下标**, 不硬编码数字 —— 换版本/换平台都不会错。</summary>
        private static int _groundMask;

        private static int ResolveGroundMask()
        {
            try
            {
                int g = LayerMask.NameToLayer("Ground");
                int s = LayerMask.NameToLayer("SlopedGround");
                int m = 0;
                if (g >= 0 && g < 32)
                    m |= (1 << g);
                if (s >= 0 && s < 32)
                    m |= (1 << s);
                return m;
            }
            catch (Exception)
            {
                return 0;
            }
        }

        // ================= 物理可站判定 =================
        /// <summary>厨师身体的碰撞半径 —— "这一格挤不挤得过去"的判据。
        ///
        /// 为什么必须问物理(用户指出的问题: "到现在了还是分不清橱柜、地图物品"):
        ///   原来的可走判定只有一条 `GetGridOccupant(格) == null` ——
        ///   **不占格子的东西一律当空气**。而关卡里有大量装饰物(街景/花坛/栏杆)
        ///   **有碰撞体但不占格子**, 厨师撞得上去, A* 却认为能走。
        ///   表现就是日志里的 "卡住 / 超时(还差 N 格) / 路径点 (-1.2,3.6) 到不了"。
        /// 厨师的胶囊体在 prefab 上(离线 dump: "Player 1" 带 CapsuleCollider),
        /// 运行时读它的 radius 就是权威尺寸, 不用猜。
        /// </summary>
        private static float _bodyRadius;

        private static float ResolveBodyRadius()
        {
            try
            {
                var pcType = SceneScanner.FindType("PlayerControls");
                if (pcType == null)
                    return 0.35f;
                var objs = UnityEngine.Object.FindObjectsOfType(pcType);
                if (objs == null || objs.Length == 0)
                    return 0.35f;
                var comp = objs[0] as Component;
                if (comp == null)
                    return 0.35f;
                var cap = comp.GetComponent<CapsuleCollider>();
                if (cap == null)
                    return 0.35f;
                float r = cap.radius;
                var tr = cap.transform;
                float s = Mathf.Max(Mathf.Abs(tr.lossyScale.x), Mathf.Abs(tr.lossyScale.z));
                r *= (s > 0.01f ? s : 1f);
                return (r > 0.05f && r < 1.0f) ? r : 0.35f;
            }
            catch (Exception) { return 0.35f; }
        }

        /// <summary>这些层的碰撞体**不**算挡住路。
        /// 其余一律算挡路 —— 宁可多标几个不可走, 也别让 A* 规划出撞墙的路径。</summary>
        private static int _ignoreMask;

        private static int ResolveIgnoreMask()
        {
            int m = 0;
            string[] ignore = { "Ground", "SlopedGround", "Players", "KillPlane",
                                "PlayerTriggerZone", "Camera", "Water", "UI",
                                "PlayersRespawn", "Attachments", "HeldAttachments",
                                "AttachedBackpack", "Beings", "PausableUI",
                                "Administration" };
            foreach (var n in ignore)
            {
                try
                {
                    int L = LayerMask.NameToLayer(n);
                    if (L >= 0 && L < 32)
                        m |= (1 << L);
                }
                catch (Exception) { }
            }
            return m;
        }

        /// <summary>这一格厨师身体能不能站下。返回挡路物体的名字("" = 能站)。</summary>
        private static string BlockedBy(float x, float z, float y)
        {
            if (_bodyRadius <= 0.05f)
                return "";
            try
            {
                var hits = Physics.OverlapSphere(new Vector3(x, y + _bodyRadius * 0.9f, z),
                                                 _bodyRadius * 0.88f);
                if (hits == null)
                    return "";
                for (int i = 0; i < hits.Length; i++)
                {
                    var c = hits[i];
                    if (c == null)
                        continue;
                    int layer = c.gameObject.layer;
                    if ((_ignoreMask & (1 << layer)) != 0)
                        continue;
                    if (c.isTrigger)
                        continue;                 // 触发区不挡路
                    if (_respawnType != null && c.gameObject.GetComponent(_respawnType) != null)
                        continue;                 // 水面/死亡面另有标记
                    return c.gameObject.name;
                }
            }
            catch (Exception) { }
            return "";
        }

        /// <summary>桥 Job: kind="map", arg 可含 "force" 强制重建。</summary>
        public static string Snapshot(string arg)
        {
            bool force = !string.IsNullOrEmpty(arg) &&
                         arg.IndexOf("force", StringComparison.OrdinalIgnoreCase) >= 0;
            // 缓存最多能有多旧 —— 调用方可以用 `maxage=N` 覆盖默认的 5 秒。
            // 为什么必须能覆盖: 5 秒对人看地图够用, 但**寻路前**太旧了 ——
            //   限时平台升降、荷叶沉浮这类变化, 5 秒足够厨师走出 20 格,
            //   拿着旧图规划就是往海里走(`Engine.terrain` 那头配合用)。
            float maxAge = 5f;
            if (!string.IsNullOrEmpty(arg))
            {
                int k = arg.IndexOf("maxage=", StringComparison.OrdinalIgnoreCase);
                if (k >= 0)
                {
                    float v;
                    if (float.TryParse(arg.Substring(k + 7),
                            System.Globalization.NumberStyles.Float,
                            System.Globalization.CultureInfo.InvariantCulture, out v) && v >= 0f)
                        maxAge = v;
                }
            }
            string scene = "";
            try { scene = UnityEngine.SceneManagement.SceneManager.GetActiveScene().name; }
            catch (Exception) { }

            float now = Time.realtimeSinceStartup;
            if (!force && _cache.Length > 0 && _cacheScene == scene && now - _cacheTime < maxAge)
                return _cache;

            string json;
            try { json = Build(); }
            catch (Exception ex) { return "{\"error\":\"" + Safe(ex.Message) + "\"}"; }

            _cache = json;
            _cacheScene = scene;
            _cacheTime = now;
            return json;
        }

        /// <summary>某个世界点是不是致命危险区(脚本热路径用)。</summary>
        public static bool IsPointHazard(float x, float z)
        {
            for (int i = 0; i < _haz.Count; i++)
            {
                var hz = _haz[i];
                if (!hz.Use)
                    continue;
                if (x >= hz.X0 && x <= hz.X1 && z >= hz.Z0 && z <= hz.Z1)
                    return true;
            }
            return false;
        }

        private static string Build()
        {
            var gm = ResolveGridManager();
            if (gm == null)
                return "{\"error\":\"no GridManager\"}";

            // 占用判定要问遍**所有**活跃网格 —— 多网格关卡只问一个会漏掉一整片。
            _allGms = AllGridManagers();
            if (_allGms.Count == 0)
                _allGms.Add(gm);        // 兜底: 拿不到列表就至少用主 gm

            Point3 half = gm.GetGridHalfSize();
            int hx = half.X;
            int hz = half.Z;
            // 占用物的索引**带层号(Y)**, 查表时必须对上 —— 见下面"扫遍 y 层"的注释。
            int hy = half.Y;
            if (hx <= 0 || hz <= 0)
                return "{\"error\":\"bad grid half size\"}";

            int w = 2 * hx + 1;
            int h = 2 * hz + 1;
            int total = w * h;

            float floorY = ReadChefFloorY();
            _floorRefY = floorY;        // FloorChar 的射线起点要用它(见那里的注释)
            // 网格的世界尺寸 —— 用来识别"世界级体积"(铺满整图的 KillPlane)
            Vector3 gA = gm.GetPosFromGridLocation(new GridIndex(-hx, 0, -hz));
            Vector3 gB = gm.GetPosFromGridLocation(new GridIndex(hx, 0, hz));
            CollectHazards(floorY, Mathf.Abs(gB.x - gA.x), Mathf.Abs(gB.z - gA.z));

            // ================= 关键: 把网格扩到关卡边界 =================
            // GridManager.GetGridHalfSize() 只覆盖"厨房"那一小块。实测 s_sushi_4_1:
            //   网格只有 21x7 格 (x 12.8~36.8, z 1.2~8.4)
            //   但关卡边界墙在 x[7.9,39.9] z[-4.7,15.1], 地板(FloorCollision 一整块大板)
            //   更是一直铺到 x[2.1,41.4] z[-3.3,29.6]
            // 那一关的中央是一条从 z=1.2 到 8.4 连着的岛台(x=20), 左右两个厨房区在
            // **网格范围内根本不连通** —— 通路在网格外面(绕过岛台)。
            // 只用 GetGridHalfSize 建图, 寻路就会认为"厨师被困在 28 格里", 什么都干不了。
            // 所以这里按**危险区(边界墙)的世界范围**把索引范围扩大, 超出的格子靠
            // 向下射线判有没有地面来决定可走性。
            int gx0 = -hx, gx1 = hx, gz0 = -hz, gz1 = hz;
            ExpandRangeByHazards(gm, ref gx0, ref gx1, ref gz0, ref gz1, 20);

            w = gx1 - gx0 + 1;
            h = gz1 - gz0 + 1;
            total = w * h;
            if (total > 8000)
                return "{\"error\":\"grid too large\"}";

            // 缩略图: 原始网格矩形(便于说明"扩了多少")
            int natW = 2 * hx + 1, natH = 2 * hz + 1;
            // ==========================================================

            // ---- 第一遍: 只判占用物, 顺便数出"空格子"总数 ----
            _conveyorType = SceneScanner.FindType("ConveyorStation");
            _groundMask = ResolveGroundMask();
            // 物理可站判定要用到这两个(见 BlockedBy 的说明)
            _bodyRadius = ResolveBodyRadius();
            _ignoreMask = ResolveIgnoreMask();

            // ---- 预扫: 其它网格管理器的占用物 ----
            //
            // ⚠ **每个 GridManager 有自己的坐标空间**, 不能用主 gm 的 idx 去问别的 gm。
            //   实测 s_wizard_school_3_4(活跃网格数=2):
            //     网格0 origin=(13.2, 1.2, 12.0)   half=[10,2,4]
            //     网格1 origin=(19.2, 0.0,  6.0)   half=[ 7,1,4]
            //   同一个 idx 在两边对应**完全不同的世界坐标** —— 直接问就会取到别处的
            //   占用物, 变成**假阻挡**(比漏掉更糟: 会把本来能走的地方封死)。
            //
            //   所以: 让每个 gm 在**它自己的索引空间**里遍历, 拿到占用物后用**它自己**的
            //   `GetPosFromGridLocation` 换算成世界坐标, 再映射到统一网格的下标上。
            var occ = new char[total];
            // 每格的**地面高度**(NaN = 没地面)。B 方案: 交给 Python 拿厨师自己的 y 比。
            var floors = new float[total];
            // 每格**向下射线打中了什么**(名字|层|水平尺寸) —— 只给诊断用, 见 FloorChar。
            var floorHits = new string[total];
            // 每格的**地板会不会动**(会动 = 平台, 沉下去时是个洞) —— 见 FloorChar。
            var floorMovable = new bool[total];
            // 诊断: 每格"全层射线第一条命中在哪一层"(层号 0-31 → 一个 base36 字符)。
            var floorLayer = new int[total];
            for (int n = 0; n < total; n++)
                floorLayer[n] = -1;      // -1 = 没探到(区分于"命中 Default 层")
            var floorHaz = new bool[total];
            // 诊断: 每格全层射线打中的**物体名** —— 用"名字表 + 每格索引"传,
            // 不然 8000 格各自带一串名字会把桥塞满。
            var floorName = new string[total];
            var floorRT = new string[total];
            // 每格**地面射线的命中层**(-1 = 没打到) —— 用来认 SlopedGround(坡道)。
            var floorGLayer = new int[total];
            for (int n = 0; n < total; n++)
                floorGLayer[n] = -1;
            // 有多少个占用物是在 **y != 0 那一层**找到的。
            // 为什么值得单独报出来: "y 写死成 0" 这个 bug 修之前它是 0、修之后应当明显 > 0,
            // 所以这一个数字就能判定修复有没有生效 —— 不用再去数 '#' 的个数。
            int nOccOffY = 0;
            {
                Vector3 q00 = gm.GetPosFromGridLocation(new GridIndex(0, 0, 0));
                Vector3 q10 = gm.GetPosFromGridLocation(new GridIndex(1, 0, 0));
                Vector3 q01 = gm.GetPosFromGridLocation(new GridIndex(0, 0, 1));
                float cxs = q10.x - q00.x, czs = q01.z - q00.z;
                if (Mathf.Abs(cxs) > 1e-4f && Mathf.Abs(czs) > 1e-4f)
                {
                    for (int gi = 0; gi < _allGms.Count; gi++)
                    {
                        var g = _allGms[gi];
                        if (g == gm)
                            continue;               // 主 gm 由主循环处理
                        Point3 h2;
                        try { h2 = g.GetGridHalfSize(); }
                        catch (Exception) { continue; }
                        for (int jj = -h2.Z; jj <= h2.Z; jj++)
                        {
                            for (int ii = -h2.X; ii <= h2.X; ii++)
                            {
                                // ⚠ **扫遍 y 层**: 占用表的键是完整的三维索引, 层号也得对上。
                                //   原来这里和主循环都写死 y=0 —— 实测网格0 的 16 个占用物里
                                //   14 个在 y=-1、1 个在 y=-2, **只有 1 个在 y=0**。
                                for (int yy2 = -h2.Y; yy2 <= h2.Y; yy2++)
                                {
                                    var idx2 = new GridIndex(ii, yy2, jj);
                                    GameObject o2;
                                    try { o2 = g.GetGridOccupant(idx2); }
                                    catch (Exception) { continue; }
                                    if (o2 == null)
                                        continue;
                                    Vector3 p;
                                    try { p = g.GetPosFromGridLocation(idx2); }
                                    catch (Exception) { continue; }
                                    int ui = Mathf.RoundToInt((p.x - q00.x) / cxs) - gx0;
                                    int uj = Mathf.RoundToInt((p.z - q00.z) / czs) - gz0;
                                    if (ui < 0 || ui >= w || uj < 0 || uj >= h)
                                        continue;   // 落在统一网格外
                                    int n2 = uj * w + ui;
                                    if (occ[n2] == '\0')
                                        occ[n2] = OccupantChar(o2);
                                    if (yy2 != 0)
                                        nOccOffY++;
                                    break;          // 这一格已认领, 不再往上层看
                                }
                            }
                        }
                    }
                }
            }
            var xs = new float[total];
            var zs = new float[total];
            int freeCount = 0;
            for (int j = 0; j < h; j++)
            {
                for (int i = 0; i < w; i++)
                {
                    int n = j * w + i;
                    var idx = new GridIndex(gx0 + i, 0, gz0 + j);
                    Vector3 pos = gm.GetPosFromGridLocation(idx);
                    xs[n] = pos.x;
                    zs[n] = pos.z;
                    // 主 gm 的占用物 —— **只问它**(它认自己的 idx 空间)。
                    // 其它网格管理器的占用物已由上面的**预扫**按世界坐标填进 occ[],
                    // 这里不能覆盖掉(occ[n] 非 '\0' 就说明那块地方已经被认领了)。
                    //
                    // ⚠ **必须扫遍所有 y 层** —— 这里原来把 y 写死成 0, 是个真 bug:
                    //   `m_gridOccupancy` 是 `Dictionary<GridIndex, GameObject>`(GridManager.cs:9),
                    //   键是**完整三维索引**, 而 `GetGridOccupant` 是精确查表(:82-87) ——
                    //   **层号对不上就返回 null**。
                    //   实测 s_wizard_school_3_4 网格0 的 16 个占用物: y=-1 有 14 个、
                    //   y=-2 有 1 个、**y=0 只有 1 个**(Teleport_Moving)。
                    //   写死 y=0 ⇒ 那 15 个永远查不到 ⇒ 这些格子落到物理探测上变成 'x':
                    //     · `'#'` 和 `'x'` 都不能走, **寻路上看不出区别**(所以一直没被发现)
                    //     · 但 `OccupantChar` 会返回 'P'(移动平台)/'T'(传送带)/'C'/'F'
                    //       ⇒ 漏掉之后, 这些格子从"能站 / 能站但会动"变成"走不了",
                    //          **厨师永远上不了移动平台、也用不了传送带**。
                    GameObject occObj = null;
                    for (int yy = -hy; yy <= hy && occObj == null; yy++)
                    {
                        occObj = gm.GetGridOccupant(new GridIndex(gx0 + i, yy, gz0 + j));
                        if (occObj != null && yy != 0)
                            nOccOffY++;
                    }
                    if (occObj != null)
                        occ[n] = OccupantChar(occObj);
                    if (occ[n] == '\0')
                        freeCount++;
                }
            }

            // ---- 危险区定案: 数覆盖 + 上保险丝 ----
            for (int i = 0; i < _haz.Count; i++)
            {
                var hzr = _haz[i];
                int covered = 0;
                if (hzr.Kills)
                {
                    for (int n = 0; n < total; n++)
                    {
                        if (occ[n] != '\0')
                            continue;
                        if (xs[n] >= hzr.X0 && xs[n] <= hzr.X1 &&
                            zs[n] >= hzr.Z0 && zs[n] <= hzr.Z1)
                            covered++;
                    }
                }
                hzr.Cells = covered;
                if (!hzr.Kills)
                {
                    hzr.Use = false;
                }
                else if (hzr.Type == "Drowning")
                {
                    // 水面/岩浆基本一律致命, 但仍要留一道保险丝:
                    // s_sushi_4_5 实测那个"真 KillPlane"的类型就是 **Drowning**
                    // (x[-28,42] y[-1.50,-0.50] z[-30,40], 比整张图还大)。
                    // 覆盖超过 80% 可走格的水面不可能"绕过去", 那它就不是本地危险区。
                    hzr.Use = covered > 0 && covered * 5 <= freeCount * 4;
                }
                else
                {
                    // FallDeath: 覆盖过半基本就是边界盒, 不能让它把整张图判死
                    hzr.Use = covered > 0 && covered * 2 <= freeCount;
                }
                _haz[i] = hzr;
            }

            // ---- 第二遍: 出字符 ----
            var sb = new StringBuilder(total);
            int nBlkSample = 0, nPhys = 0;
            var _blkNames = new List<string>();
            int nFree = 0, nBlocked = 0, nHaz = 0, nVoid = 0, nVoidLow = 0,
                nPlat = 0, nTravel = 0, nFire = 0, nConveyor = 0;
            for (int n = 0; n < total; n++)
            {
                // **每一格都探**(放在字符判定之外) —— 见 ProbeCell 的注释:
                // 塞进 FloorChar 的话, 占用物/危险区那几类格子一格数据都没有,
                // 数组默认值 0 又会冒充"命中 Default 层"(实测读错过一次结论)。
                ProbeCell(xs[n], zs[n]);
                char ch;
                if (occ[n] != '\0')
                {
                    ch = occ[n];
                }
                else if (IsPointHazard(xs[n], zs[n]))
                {
                    ch = 'H';
                }
                else
                {
                    float cfy;
                    string hinfo;
                    bool fmov;
                    int gLayer;
                    char fc = FloorChar(xs[n], zs[n], out cfy, out hinfo, out fmov,
                                        out gLayer);
                    floorGLayer[n] = gLayer;
                    floors[n] = cfy;              // NaN = 没地面
                    floorHits[n] = hinfo;
                    floorMovable[n] = fmov;
                    ch = (fc == '\0') ? '.' : fc;
                    // **逐格射线判"水"**(用户实测指出: 平台之外那大片该是水, 原来全标成 'V')。
                    //   根因: `'H'` 原来是靠 `RespawnCollider` **范围投影**判的, 而那道投影里
                    //   有一句 `sameLevel = b.max.y >= floorY - 0.4f` —— **水面在平台下方**
                    //   ⇒ 判成"不同层" ⇒ 不标 H ⇒ 落到射线那条路 ⇒ 水面不是 Ground 层、
                    //   打不到"地面" ⇒ 标成 'V'。
                    //   射线是**逐格**的、不需要任何全局量: 打中的物体挂着 `RespawnCollider`
                    //   且类型是 `Drowning` ⇒ 这格就是水。
                    //   实测支撑: 两关的 'V' 格里 91%~96% 底下都是致命面。
                    if (_probeRT == "Drowning")
                        ch = 'H';
                    // **物理可站判定**: 有地面不代表站得下 —— 装饰物/栏杆/花坛
                    // 有碰撞体但不占格子, 原来一律当空气, A* 就会规划出撞墙的路径。
                    if (ch == '.')
                    {
                        // ⚠ 探测高度必须用**这一格自己的地板高度**, 不能用全局 floorY。
                        //   `BlockedBy` 把探测球心放在 `y + 半径*0.9`(:179) —— 传全局 floorY
                        //   就等于"**假设厨师站在 floorY 那一层**"去扫这一格; 对**不在那一层**
                        //   的格子, 球会悬空或者穿进楼板, 扫到的是**别的层**的东西。
                        //   而 floorY = "所有厨师 y 的最大值", 随厨师上下平台而变 ⇒
                        //   **同一关两次取图, 另一层的格子会大面积翻转**。
                        //   实测(s_wizard_school_3_4, 两次 mapview 对照): floorY 由 -1.84
                        //   变成 0.00 时, 上层 45 格在 'x' 与 '.' 之间翻转(可走 39 ↔ 84)。
                        //   `cfy` 就是上面 FloorChar 刚报出来的**这一格**的地板高度,
                        //   取不到(没地面)时才退回 floorY。
                        float probeY = float.IsNaN(cfy) ? floorY : cfy;
                        string blk = BlockedBy(xs[n], zs[n], probeY);
                        if (blk.Length > 0)
                        {
                            ch = 'x';                 // 物理阻挡(非格子占用)
                            if (nBlkSample < 6)
                                _blkNames.Add(blk);
                            nBlkSample++;
                        }
                    }
                }

                floorLayer[n] = _probeLayer;
                floorHaz[n] = _probeHaz;
                floorName[n] = _probeName;
                floorRT[n] = _probeRT;
                sb.Append(ch);
                switch (ch)
                {
                    case '.': nFree++; break;
                    case '#': nBlocked++; break;
                    case 'H': nHaz++; break;
                    case 'V': nVoid++; break;
                    case 'v': nVoidLow++; break;
                    case 'P': nPlat++; break;
                    case 'T': nTravel++; break;
                    case 'C': nConveyor++; break;
                    case 'x': nPhys++; break;
                    case 'F': nFire++; break;
                    default: nBlocked++; break;
                }
            }

            // ---- 世界坐标映射 ----
            // 用**扩展后**的索引范围(gx0..gz1), 不是原始的 ±half —— 否则 Python 侧
            // 按 ox + i*cell 反算出来的坐标会整体错位。
            Vector3 p00 = gm.GetPosFromGridLocation(new GridIndex(0, 0, 0));
            Vector3 p10 = gm.GetPosFromGridLocation(new GridIndex(1, 0, 0));
            Vector3 p01 = gm.GetPosFromGridLocation(new GridIndex(0, 0, 1));
            Vector3 pLast = gm.GetPosFromGridLocation(new GridIndex(gx1, 0, gz1));
            float cellX = p10.x - p00.x;
            float cellZ = p01.z - p00.z;
            Vector3 pMin = gm.GetPosFromGridLocation(new GridIndex(gx0, 0, gz0));
            float ox = pMin.x;
            float oz = pMin.z;
            bool regular = Mathf.Abs(ox + (w - 1) * cellX - pLast.x) < 0.05f
                        && Mathf.Abs(oz + (h - 1) * cellZ - pLast.z) < 0.05f;

            // ---- 地形版本号 ----
            // 给 Python 侧一个**便宜的"变了没有"判据**。
            // 为什么需要: `Engine.terrain()` 原来**按场景缓存、整局不刷新**,
            //   而地形是会变的 —— 限时平台升降 / 荷叶沉浮 / 火 / 潮水 / 传送带被按开。
            //   实测(s_wonderland_1_2 的限时平台, 1 分钟一换):
            //     66 格地板高度在 `0.00 ↔ -3.00` 之间循环, 而**字符网格一格都没变** ——
            //     平台降下时格子仍是 '.'(能站), 只有高度差出跨步上限(0.65)才判不可走。
            //   ⇒ "拿旧图寻路"的后果不是绕远, 是**直接走进海里**。
            //   有了版本号, 引擎就能低成本地问"还是不是我手里这份", 变了才重建。
            // 覆盖**所有影响可走性的输入**: 字符网格 + 每格地板高度。
            // 高度量化到 0.01, 免得浮点抖动把版本号抖成噪声。
            // FNV-1a: 够快, 不需要加密强度。
            uint ver = 2166136261u;
            for (int n = 0; n < sb.Length; n++)
            {
                ver ^= sb[n];
                ver *= 16777619u;
            }
            for (int n = 0; n < total; n++)
            {
                float f = floors[n];
                int q = float.IsNaN(f) ? int.MinValue : Mathf.RoundToInt(f * 100f);
                ver ^= (uint)q;
                ver *= 16777619u;
            }

            var o = new StringBuilder();
            o.Append("{");
            o.Append("\"w\":").Append(w);
            o.Append(",\"h\":").Append(h);
            o.Append(",\"hx\":").Append(hx);
            o.Append(",\"hz\":").Append(hz);
            o.Append(Inv(",\"ox\":", ox));
            o.Append(Inv(",\"oz\":", oz));
            o.Append(Inv(",\"cellx\":", cellX));
            o.Append(Inv(",\"cellz\":", cellZ));
            o.Append(Inv(",\"floorY\":", floorY));
            o.Append(",\"regular\":").Append(regular ? "true" : "false");
            o.Append(",\"grids\":").Append(GridManager.GetActiveCount());
            o.Append(",\"grid\":\"").Append(sb.ToString()).Append("\"");
            // 地形版本号(见上面算它的那段注释) —— 变了就说明"可走性"变了。
            o.Append(",\"ver\":\"").Append(ver.ToString("x8")).Append("\"");
            o.Append(",\"hazards\":[");
            for (int i = 0; i < _haz.Count; i++)
            {
                if (i > 0)
                    o.Append(",");
                var hzr = _haz[i];
                o.Append("{\"name\":\"").Append(Safe(hzr.Name)).Append("\"");
                o.Append(",\"type\":\"").Append(Safe(hzr.Type)).Append("\"");
                o.Append(Inv(",\"x0\":", hzr.X0));
                o.Append(Inv(",\"x1\":", hzr.X1));
                o.Append(Inv(",\"z0\":", hzr.Z0));
                o.Append(Inv(",\"z1\":", hzr.Z1));
                o.Append(Inv(",\"y0\":", hzr.Y0));
                o.Append(Inv(",\"y1\":", hzr.Y1));
                o.Append(",\"killPlane\":").Append(hzr.KillPlane ? "true" : "false");
                o.Append(",\"kills\":").Append(hzr.Kills ? "true" : "false");
                o.Append(",\"used\":").Append(hzr.Use ? "true" : "false");
                o.Append(",\"cells\":").Append(hzr.Cells);
                o.Append("}");
            }
            o.Append("]");
            o.Append(",\"counts\":{\"free\":").Append(nFree)
             .Append(",\"blocked\":").Append(nBlocked)
             .Append(",\"hazard\":").Append(nHaz)
             .Append(",\"void\":").Append(nVoid)
             .Append(",\"voidLow\":").Append(nVoidLow)
             .Append(",\"platform\":").Append(nPlat)
             .Append(",\"travelator\":").Append(nTravel)
             .Append(",\"conveyor\":").Append(nConveyor)
             .Append(",\"fire\":").Append(nFire).Append(",\"phys\":").Append(nPhys)
             // 「占用物是在 y != 0 那一层找到的」个数 —— **修 y 写死那个 bug 的验证信号**:
             // 修之前恒为 0, 修之后应当明显 > 0。用它判定修复有没有生效, 不用去数 '#'。
             .Append(",\"occOffY\":").Append(nOccOffY).Append("}");
            // 被物理挡住的格子, 给出几个挡路物体的名字 —— 便于核对"到底是谁挡的"
            o.Append(",\"physNames\":[");
            for (int i = 0; i < _blkNames.Count; i++)
            {
                if (i > 0)
                    o.Append(",");
                o.Append("\"").Append(Safe(_blkNames[i])).Append("\"");
            }
            o.Append("]");
            // ---- 诊断: "地板最低那批格子, 射线到底打中了什么" ----
            // 目的(用户要求的 Part 3: 别靠猜高度, 要让游戏告诉我们这格有没有**真地板**):
            //   平台收起来时高度会掉到 -9 那种值, 但"那底下到底是不是地板"用**高度答不了**
            //   —— 而 `FloorChar` 本来就打到了那个碰撞体, 名字/层/尺寸就在手边。
            //   这里**只采集、不下结论**: 先把"最低那批格子打中了什么"如实报出来,
            //   等实机数据到手再定规则。**猜一个阈值就是重蹈"±2 拍脑袋"的覆辙**
            //   (那一次把 s_space_6_2 的 2.40 层整片封死, 用户当场指出是错的)。
            // 每格地板"会不会动" —— 紧凑 0/1 串(和 grid 等长, 8000 格也就 8000 字符)。
            // 会动 = 平台: 它沉下去时**是一个洞**(用户实测: -1.65 不能走, 浮到 0.00 才能走);
            // 不会动 = 静止的地板, 那只是一个"层"。**这两者从高度上分不开** ——
            // 实测 1.65(该拦) 比 2.40(该放) 还小, 所以拿阈值调怎么调都错。
            o.Append(",\"movableFloor\":\"");
            for (int n = 0; n < total; n++)
                o.Append(floorMovable[n] ? '1' : '0');
            o.Append("\"");
            // 每格"全层射线第一条命中在哪一层" —— 紧凑 base36(0-9a-v = 层号 0-31, '-' = 没打到)。
            // 用来回答"水面能不能被射线打到"(见 FloorChar 里那段注释):
            // 如果水面格命中 `Water` 层, 那危险区判定就能换成**逐格射线**,
            // 顺带把 `CollectHazards` 里那颗吃 `floorY` 的地雷拆掉。
            o.Append(",\"probeLayer\":\"");
            for (int n = 0; n < total; n++)
            {
                int L = floorLayer[n];
                o.Append(L < 0 || L > 35 ? '-' : "0123456789abcdefghijklmnopqrstuvwxyz"[L]);
            }
            o.Append("\"");
            // 每格"全层射线打中的是不是危险面(RespawnCollider)" —— 紧凑 0/1。
            // 若危险区('H')那批**全是 1**, 就说明可以拿"逐格射线 + 组件判据"
            // **替掉现在那套范围投影**(那套里吃着 floorY 那颗地雷)。
            o.Append(",\"probeHaz\":\"");
            for (int n = 0; n < total; n++)
                o.Append(floorHaz[n] ? '1' : '0');
            o.Append("\"");
            // 名字表 + 每格索引(紧凑)。名字去重后按出现顺序编号, 索引用 base36 一个字符。
            o.Append(",\"probeNames\":[");
            {
                var idx = new System.Collections.Generic.Dictionary<string, int>();
                var names = new System.Collections.Generic.List<string>();
                var perCell = new int[total];
                for (int n = 0; n < total; n++)
                {
                    string nm = floorName[n];
                    if (string.IsNullOrEmpty(nm)) { perCell[n] = -1; continue; }
                    int k;
                    if (!idx.TryGetValue(nm, out k))
                    {
                        k = names.Count;
                        if (k < 60) { names.Add(nm); idx[nm] = k; }
                        else { perCell[n] = -1; continue; }
                    }
                    perCell[n] = k;
                }
                for (int k = 0; k < names.Count; k++)
                {
                    if (k > 0) o.Append(",");
                    o.Append("\"").Append(Safe(names[k])).Append("\"");
                }
                o.Append("],\"probeIdx\":\"");
                for (int n = 0; n < total; n++)
                    o.Append(perCell[n] < 0 ? '-' : "0123456789abcdefghijklmnopqrstuvwxyz"[perCell[n] % 36]);
                o.Append("\"");
            }
            // 每格危险面的 RespawnType —— 紧凑一个字符: '-' 无 / 'D' 水(Drowning)
            // / 'F' 深渊(FallDeath) / 'H' Hit / 'C' Car / '?' 其它。
            // 每格**地面射线命中层**: 'S' = SlopedGround(坡道), 'G' = 别的层, '-' = 没打到。
            // 只要这一个 bit: 判 `step_ok` 时"坡道不做跨步限制"要用它。
            o.Append(",\"groundKind\":\"");
            {
                int sloped = LayerMask.NameToLayer("SlopedGround");
                for (int n = 0; n < total; n++)
                {
                    int L = floorGLayer[n];
                    o.Append(L < 0 ? '-' : (L == sloped ? 'S' : 'G'));
                }
            }
            o.Append("\"");
            o.Append(",\"probeRT\":\"");
            for (int n = 0; n < total; n++)
            {
                string rt = floorRT[n];
                char c = '-';
                if (rt == "Drowning") c = 'D';
                else if (rt == "FallDeath") c = 'F';
                else if (rt == "Hit") c = 'H';
                else if (rt == "Car") c = 'C';
                else if (!string.IsNullOrEmpty(rt)) c = '?';
                o.Append(c);
            }
            o.Append("\"");
            o.Append(",\"lowHits\":[");
            try
            {
                float minF = float.MaxValue;
                for (int n = 0; n < total; n++)
                    if (!float.IsNaN(floors[n]) && floors[n] < minF)
                        minF = floors[n];
                if (minF < float.MaxValue)
                {
                    var seenHits = new Dictionary<string, int>();
                    for (int n = 0; n < total; n++)
                    {
                        if (float.IsNaN(floors[n]) || floors[n] > minF + 0.5f)
                            continue;
                        string k = floorHits[n];
                        if (string.IsNullOrEmpty(k))
                            continue;
                        int c;
                        seenHits.TryGetValue(k, out c);
                        seenHits[k] = c + 1;
                    }
                    int k2 = 0;
                    foreach (var kv in seenHits)
                    {
                        if (k2 >= 6)
                            break;
                        if (k2 > 0)
                            o.Append(",");
                        o.Append("{\"what\":\"").Append(Safe(kv.Key))
                         .Append("\",\"cells\":").Append(kv.Value)
                         .Append(Inv(",\"y\":", minF)).Append("}");
                        k2++;
                    }
                }
            }
            catch (Exception) { }
            o.Append("]");
            // 每格的**地面高度**(B 方案的核心数据) —— `null` = 没地面(空洞)。
            // Python 侧 `TerrainMap.walkable(at_y=...)` 拿**厨师自己的 y** 跟它比:
            //   落差超过 StepHeightMax ⇒ 这格站不下。
            // 这样"多平台"关卡里, 另一层的地板对这只厨师来说就是"过不去"(差太多),
            // 而自己那层的地板正常可走 —— 不再有一个全局 floorY 把整层判死。
            o.Append(",\"floors\":[");
            for (int n = 0; n < total; n++)
            {
                if (n > 0)
                    o.Append(",");
                if (float.IsNaN(floors[n]))
                    o.Append("null");
                else
                    o.Append(floors[n].ToString("F2",
                        System.Globalization.CultureInfo.InvariantCulture));
            }
            o.Append("]");
            o.Append("}");
            return o.ToString();
        }

        /// <summary>按危险区(关卡边界墙)的世界范围扩大索引范围。
        ///
        /// 为什么必须扩: `GridManager.GetGridHalfSize()` 只覆盖"厨房"那一小块,
        /// 而关卡的可走地板往往比它大得多(实测 s_sushi_4_1 的地板是一整块
        /// FloorCollision, x[2.1,41.4] z[-3.3,29.6], 而网格只有 21x7 格)。
        /// 不扩的话, 两个厨房区之间"绕岛台走"的通路就完全在网格外,
        /// 寻路会误判成"厨师被困住"。
        ///
        /// 扩出来的格子靠向下射线判地面 —— 网格外没有台面占用(字典里没这一项),
        /// 所以能不能走完全由地面决定, 这是对的。
        /// 同时用 margin 封顶, 避免某些关卡的危险区特别大时把网格撑爆。
        /// </summary>
        private static void ExpandRangeByHazards(GridManager gm, ref int gx0, ref int gx1,
                                                 ref int gz0, ref int gz1, int margin)
        {
            try
            {
                if (gm == null)
                    return;
                Vector3 p0 = gm.GetPosFromGridLocation(new GridIndex(0, 0, 0));
                Vector3 p1 = gm.GetPosFromGridLocation(new GridIndex(1, 0, 0));
                Vector3 p2 = gm.GetPosFromGridLocation(new GridIndex(0, 0, 1));
                float cx = p1.x - p0.x;
                float cz = p2.z - p0.z;
                if (Mathf.Abs(cx) < 0.01f || Mathf.Abs(cz) < 0.01f)
                    return;

                Point3 half = gm.GetGridHalfSize();
                int limX0 = -half.X - margin, limX1 = half.X + margin;
                int limZ0 = -half.Z - margin, limZ1 = half.Z + margin;

                // **兜底最小扩展**: 万一这一关没有可判定的边界墙(危险区全被判为世界体积),
                // 光靠危险区就一点也不扩, 又会退回"厨师被困住"。所以至少往外扩 6 格,
                // 多出来的格子能不能走由地面射线决定, 走不到的自成连通块会被自然排除。
                int minMargin = 6;
                int wx0 = gx0 - minMargin, wx1 = gx1 + minMargin;
                int wz0 = gz0 - minMargin, wz1 = gz1 + minMargin;
                for (int i = 0; i < _haz.Count; i++)
                {
                    var hzr = _haz[i];
                    if (!hzr.Kills)
                        continue;             // 只按真正的危险区(边界墙/水面)扩
                    int a = Mathf.FloorToInt((hzr.X0 - p0.x) / cx);
                    int b = Mathf.CeilToInt((hzr.X1 - p0.x) / cx);
                    int c = Mathf.FloorToInt((hzr.Z0 - p0.z) / cz);
                    int d = Mathf.CeilToInt((hzr.Z1 - p0.z) / cz);
                    if (a < wx0) wx0 = a;
                    if (b > wx1) wx1 = b;
                    if (c < wz0) wz0 = c;
                    if (d > wz1) wz1 = d;
                }
                gx0 = Mathf.Max(wx0, limX0);
                gx1 = Mathf.Min(wx1, limX1);
                gz0 = Mathf.Max(wz0, limZ0);
                gz1 = Mathf.Min(wz1, limZ1);
            }
            catch (Exception) { }
        }

        private static GridManager ResolveGridManager()
        {
            try
            {
                var nav = GameUtils.GetGridNavSpace();
                if (nav != null)
                {
                    var gm = GameUtils.GetGridManager(nav.transform);
                    if (gm != null)
                        return gm;
                }
            }
            catch (Exception) { }
            try
            {
                if (GridManager.GetActiveCount() > 0)
                    return GridManager.GetActive(0);
            }
            catch (Exception) { }
            return null;
        }

        /// <summary>**所有**活跃的网格管理器。
        ///
        /// ⚠ 为什么必须有这个(用户实测的关卡 s_wizard_school_3_4):
        ///   那一关 `活跃网格数 = 2` —— 关卡被切成两个网格区域。而原来只取
        ///   `GetActive(0)`, 于是**另一个区域里所有占用物都查不到**
        ///   (`GetGridOccupant` 只认自己那份字典) ⇒ 整片被判成"空洞"。
        ///   实测后果: 33x27 的图**只有 31 格可走**, 27 个台面里
        ///   **13 个够不着** —— 包括**食材箱**和**送餐口** ⇒ 这关一道菜都做不出来。
        ///
        /// `GetPosFromGridLocation` 各 gm 是一致的(同一个网格坐标空间), 所以坐标
        /// 仍用主 gm 算; 但**占用判定必须问遍所有**, 否则就等于没建图。
        /// </summary>
        private static List<GridManager> AllGridManagers()
        {
            var list = new List<GridManager>();
            try
            {
                int n = GridManager.GetActiveCount();
                for (int i = 0; i < n; i++)
                {
                    var g = GridManager.GetActive(i);
                    if (g != null)
                        list.Add(g);
                }
            }
            catch (Exception) { }
            return list;
        }

        private static char OccupantChar(GameObject occ)
        {
            string tag = "";
            try { tag = occ.tag; }
            catch (Exception) { }
            if (tag == "Hazard")
                return 'F';
            if (tag == "MovingPlatform")
                return 'P';
            if (tag == "Travelator")
                return 'T';
            // 台面传送带 (s_sushi_4_5 实测 83 个): 台面上放了东西会被**一格一格传走**,
            // 所以它既是障碍(走不上去), 又是"不能久放物品"的台面。
            // 依据 ConveyorStation.cs:3 的 RequireComponent(TabletopConveyenceReceiver, ...)
            // 与 ServerConveyorStation.ConveyTo —— 它搬的是**物品**, 不是厨师。
            if (_conveyorType != null && occ.GetComponent(_conveyorType) != null)
                return 'C';
            return '#';
        }

        /// <summary>这一格脚下有没有**能站**的地面。返回 '\0' 表示正常, 否则返回该格该用的字符。
        ///
        /// 两条判定, 第二条是踩过坑之后加的:
        ///   · 射线完全打不中 → 'V' 空洞(脚下什么都没有, 掉下去)
        ///   · 打中了但落点**明显更低** → 'v' 单向落差 / 正在下沉的平台
        ///
        /// 为什么必须看落点高度: **"会消失的平台"并不是被销毁的**。
        /// 以 DLC13 的荷叶为例 —— 全工程没有 LilyPad/Lotus 类, 唯一证据是 5 条音效枚举
        ///   DLC_13_LilyPad_{Lrg,Sml}_{Plunge,Pop,_Step}  (GameOneShotAudioTag.cs:317-321)
        /// "Plunge"(下沉) 与 "Pop"(浮起) 成对出现, 说明它是**踩下去再浮回来的循环体**,
        /// 物理上就是碰撞体跟着下沉动画走低。
        /// 所以对格子打向下射线**全程都会命中** —— 只判"命中与否"的脚本会一直认为这格能走,
        /// 直到最后一刻才发现, 而游戏给的反应窗口只有 m_timeBeforeFalling = 0.2s
        /// (PlayerControls.cs:270)。
        ///
        /// 落差阈值取 StepHeightMax = 0.65 (PlayerControls.cs:50):
        /// 比这更低就是"跳下去爬不回来"的落差, 对按格子走路的脚本等同不可走。
        /// </summary>
        /// <summary>这一格有没有地面, 并**把地面的实际高度报出来**。
        ///
        /// ⚠ 与旧版的区别(这是 B 方案的核心):
        ///   旧版拿一个**全局 floorY**(某只厨师当前位置)去判, 只从那个高度往下打 1.8 ——
        ///   多平台关卡里, 别的平台全落在窗口外 ⇒ 判成 `V`。
        ///   现在: **从高处往下打**, 取第一层地面, 把 `hit.point.y` 交出去;
        ///   "这只厨师站不站得下"由 Python 拿**厨师自己的 y**比
        ///   (见 `TerrainMap.walkable(at_y=...)`) —— 那本来就是相对量, 不该有全局值。
        /// 顺带: `'v'`(低地板/下沉平台) 的判定也搬到 Python 了, 因为它同样要跟**厨师**比。
        /// </summary>
        /// <summary>诊断探针: **每一格**都打一条全层射线, 报出"第一条命中在哪一层 /
        /// 是不是危险面 / 叫什么"。
        ///
        /// ⚠ **必须独立于 `FloorChar`**。一开始写在 FloorChar 里, 而 FloorChar
        ///   **只在"非占用、非危险区"那条分支被调用** ⇒ `'H'`(危险区)的格子
        ///   **一次都没打过**, 它们的层号是数组默认值 0(= Default) ——
        ///   于是"水面在 Default 层"这个结论是从**没跑过的数据**里读出来的(实测踩过)。
        /// </summary>
        private static void ProbeCell(float x, float z)
        {
            _probeLayer = -1;
            _probeHaz = false;
            _probeName = "";
            _probeRT = "";
            try
            {
                Vector3 from0 = new Vector3(x, _floorRefY + 10f, z);
                RaycastHit h0;
                if (!Physics.Raycast(from0, Vector3.down, out h0, 20f))
                    return;
                _probeLayer = h0.collider.gameObject.layer;
                _probeName = Safe(h0.collider.gameObject.name);
                // **连父级一起找** `RespawnCollider`(游戏自己标"碰到就死"的组件):
                // 水面/关底面往往挂在子物体上, 只看命中物体本身会漏。
                var rp = SceneScanner.FindType("RespawnCollider");
                if (rp != null)
                {
                    for (var cur2 = h0.collider.transform; cur2 != null; cur2 = cur2.parent)
                    {
                        var rc = cur2.GetComponent(rp);
                        if (rc == null)
                            continue;
                        _probeHaz = true;
                        // **读 RespawnType** —— 这是"水"和"深渊"的唯一区别
                        // (`RespawnCollider.cs:9`: Hit / Drowning / FallDeath / Car)。
                        // 用户实测指出: 平台之外那大片该是**水**, 图上却标成 `V`(空洞)。
                        // 两种都必须拦, 但**标出来的意思不同**, 而区别就在这个枚举上。
                        _probeRT = TypeName(rp, rc);
                        break;
                    }
                }
            }
            catch (Exception) { }
        }

        private static char FloorChar(float x, float z, out float cellFloorY,
                                      out string hitInfo, out bool movable,
                                      out int groundLayer)
        {
            cellFloorY = float.NaN;
            hitInfo = "";
            movable = false;
            groundLayer = -1;
            try
            {
                // 从参考高度再往上 10 格开始打, 覆盖 20 格 —— 够装下多平台的落差。
                // (参考高度本身已是"所有厨师的最高点", 见 ReadChefFloorY 的注释)
                //
                // ☠ 地雷(用户指出, 未拆): `_floorRefY` 源头是玩家位置, 见它的注释。
                //   这里的影响比 `CollectHazards` 那边**小一些**: 窗口只要覆盖住所有
                //   地板层, 起点高一点低一点结果一样 —— 但如果窗口**没罩住**某一层
                //   (比如厨师全在低处、而高台高出 +10 以上), 那一整层就会被判成 'V'。
                //   所以真要拆时: 把起点换成**与厨师无关的固定高处**。
                Vector3 from = new Vector3(x, _floorRefY + 10f, z);
                RaycastHit hit;
                // 只打地面层(Ground|SlopedGround) —— 游戏自己就是这么做的,
                // 顺带把 KillPlane 层排除掉。掩码为 0 时退回全层(再加组件兜底)。
                bool hitSomething = (_groundMask != 0)
                    ? Physics.Raycast(from, Vector3.down, out hit, 20f, _groundMask)
                    : Physics.Raycast(from, Vector3.down, out hit, 20f);
                if (!hitSomething)
                    return 'V';
                // 兜底: 万一掩码没生效(层名变了), 打到 RespawnCollider 也不算地面。
                // s_sushi_4_5 实测真 KillPlane 是 x[-28,42] y[-1.50,-0.50] z[-30,40],
                // 铺满整图且顶面 y=-0.5, 而射线从 floorY+0.6 往下 1.8 正好到 y=-1.2。
                if (_respawnType != null && hit.collider != null &&
                    hit.collider.gameObject.GetComponent(_respawnType) != null)
                    return 'V';
                cellFloorY = hit.point.y;
                // **这一格的地面在哪一层** —— 用来认 `SlopedGround`(坡道)。
                // 为什么必须认(用户实测): 坡道从 0.00 升到 1.85 跨三格, 每格采样
                // 落差 0.70/0.76 —— **刚好越过 0.65 的跨步上限**, 于是泛洪把整条坡道
                // 判成墙("台面左右那几格该能走却画成 V")。
                // 而坡道上厨师是**连续**走的(每帧 Δy 极小), 拿 1.2 格的采样差当
                // "台阶"比, 从一开始就不对 —— 层名就是区分它的凭据。
                try { groundLayer = hit.collider.gameObject.layer; } catch (Exception) { }
                // **这格的地板会不会动** —— 会动的是"平台", 静止的是"层"。
                // 为什么要分开(用户实测给出的反例): 会动的平台沉下去时**是一个洞**
                // (踩上去掉进去, 实测 -1.65 时不能走, 浮到 0.00 才能走);
                // 而静止的下层地板只是一个"层"(站上去照样走)。两者**从高度上分不开** ——
                // 1.65(该拦) 反而比 2.40(该放) 小, 所以拿阈值怎么调都是错的。
                try
                {
                    var go2 = hit.collider.gameObject;
                    if (_movingPlatType == null)
                        _movingPlatType = SceneScanner.FindType("MovingPlatform");
                    if (_pilotType == null)
                        _pilotType = SceneScanner.FindType("PilotMovement");
                    for (var cur = go2.transform; cur != null; cur = cur.parent)
                    {
                        if ((_movingPlatType != null && cur.GetComponent(_movingPlatType) != null)
                         || (_pilotType != null && cur.GetComponent(_pilotType) != null))
                        {
                            movable = true;
                            break;
                        }
                    }
                }
                catch (Exception) { }
                // 诊断用: **这一格打中的到底是什么**。
                // 为什么需要: "平台收起来"时字符不变、只有高度掉下去, 光看高度
                // 没法区分"那是别层的平台"(能走)和"那底下根本没地板"(掉下去会死)。
                // 名字 + 水平尺寸是最可能区分这两者的信号(本工程已经在别处用过
                // 同一条思路: `CollectHazards` 用"体积远大于整张网格"认世界级 KillPlane)。
                // 这里**先只采集、不下结论** —— 等实机数据出来再定规则。
                try
                {
                    var b = hit.collider.bounds;
                    hitInfo = string.Format(
                        System.Globalization.CultureInfo.InvariantCulture,
                        "{0}|layer{1}|{2:F1}x{3:F1}",
                        hit.collider.gameObject.name, hit.collider.gameObject.layer,
                        b.size.x, b.size.z);
                }
                catch (Exception) { }
                return '\0';
            }
            catch (Exception)
            {
                return '\0';   // 射线失败就别误判成空洞
            }
        }

        /// <summary>**整张图的参考高度** —— 取所有厨师里**最高**的那个。
        ///
        /// ⚠ 这里原来取的是 `objs[0].transform.position.y`(第一只厨师的**当前**位置),
        ///   有两个致命问题(实测 s_wizard_school_3_4):
        ///     ① **不确定**: `FindObjectsOfType` 的顺序 + 厨师在动 ⇒ 同一关两次读出
        ///        `floorY = -1.84` 和 `0.00`;
        ///     ② **只覆盖一层**: 两位厨师在**不同高度的平台**上时, 射线只从一个人那儿
        ///        往下打 1.8 格 ⇒ 另一个平台的地板全落在窗口外 ⇒ 判成 `V`(空洞)。
        ///        实测后果: 厨师1 站在自己平台上, 却被判成"站在空洞上", 可达格数 = 1。
        ///
        ///   改成取 **max**: 与枚举顺序无关(只要厨师集合不变结果就一样), 而且从最高处
        ///   往下打能覆盖所有平台。**逐格的地板高度由 FloorChar 自己报出来**, 到底站不站
        ///   得下交给 Python 拿"厨师自己的 y"去比(见 TerrainMap.walkable 的 at_y)。
        ///
        /// ☠☠ **地雷: 这个函数本身就是那颗雷的源头** (用户 2026-09-14 指出: 取玩家位置
        ///   来算 y, 感觉是地雷)。取 max 只解决了**枚举顺序**那一半 ——
        ///   **"厨师在哪"这一半没解决**: 取的是"所有厨师**当前** y 的最大值",
        ///   厨师一换层(传送门/掉落/电梯)它当场就跳。
        ///
        ///   而它的两个下游都不是"仅供参考":
        ///     · `FloorChar` 的射线起点窗口(`_floorRefY + 10`, 往下 20)
        ///     · `CollectHazards` 的**危险区同层判定**(`sameLevel = b.max.y >= floorY - 0.4`)
        ///   ⇒ 一次传送就可能**改变整张图的危险区归类**, 症状伪装成"地形读错了"。
        ///   这正是文档里"坑 4: 全局量 + 取样不确定"的同一个病, 只是换了个地方长出来。
        ///
        ///   已经不受它影响的: 每格地板高度(`floors[]`, 逐格射线)、`'v'`/`'^'` 的判定
        ///   (`walkable(at_y=)` 一律用厨师自己的 y)、`BlockedBy` 的探测高度(改成传 cfy)。
        ///   见 `_floorRefY` 的注释里有拆它的建议(把窗口换成与厨师无关的固定高处)。
        /// </summary>
        private static float ReadChefFloorY()
        {
            try
            {
                var pcType = SceneScanner.FindType("PlayerControls");
                if (pcType == null)
                    return 0f;
                var objs = UnityEngine.Object.FindObjectsOfType(pcType);
                if (objs == null || objs.Length == 0)
                    return 0f;
                float best = float.NegativeInfinity;
                for (int i = 0; i < objs.Length; i++)
                {
                    var c = objs[i] as Component;
                    if (c == null)
                        continue;
                    float y = c.transform.position.y;
                    if (y > best)
                        best = y;
                }
                return float.IsNegativeInfinity(best) ? 0f : best;
            }
            catch (Exception)
            {
                return 0f;
            }
        }

        /// <summary>收集所有 RespawnCollider。gridW/gridD 是整张网格的世界尺寸, 用来识别"世界级体积"。</summary>
        private static void CollectHazards(float floorY, float gridW, float gridD)
        {
            _haz.Clear();
            _respawnType = null;
            try
            {
                var hazType = SceneScanner.FindType("RespawnCollider");
                if (hazType == null)
                    return;
                _respawnType = hazType;
                var objs = UnityEngine.Object.FindObjectsOfType(hazType);
                if (objs == null)
                    return;
                float gridArea = Mathf.Max(1f, gridW * gridD);
                for (int i = 0; i < objs.Length; i++)
                {
                    var comp = objs[i] as Component;
                    if (comp == null)
                        continue;
                    var col = comp.GetComponent<Collider>();   // RespawnCollider 自身没有 Collider, 在同级
                    if (col == null)
                        continue;
                    Bounds b = col.bounds;

                    string type = "";
                    try
                    {
                        var f = hazType.GetField("m_respawnType");
                        if (f != null)
                        {
                            var v = f.GetValue(objs[i]);
                            if (v != null)
                                type = v.ToString();
                        }
                    }
                    catch (Exception) { }

                    string name = "";
                    try { name = comp.gameObject.name; }
                    catch (Exception) { }

                    // 名字**只作参考**。实测 s_sushi_4_5 里 4 面边界墙分别叫
                    // KillPlane / KillPlane (1) / (2) / (3), 真 KillPlane 反而叫 "KillPlane" ——
                    // 按名字一刀切会把边界墙也排除掉。
                    bool nameLooksKillPlane =
                        name.IndexOf("KillPlane", StringComparison.OrdinalIgnoreCase) >= 0;

                    // 只有 水面/岩浆(Drowning) 和 掉下去(FallDeath) 会弄死这一层的厨师。
                    // Hit / Car 不是地形危险, 不管。
                    bool deadlyType = type == "Drowning" || type == "FallDeath";
                    // 在关卡地板**下方**的(真 KillPlane 顶面 y=-0.5 而地板 y=0), 不算同层危险
                    //
                    // ☠ **这里是那颗地雷的引爆点** —— 判据里的 `floorY` 源头是
                    //   "所有厨师当前 y 的最大值"(见 `_floorRefY` 的注释)。
                    //   于是**厨师一换层(传送门/掉落), 这里就可能整片翻转**:
                    //   一张本来算"同层危险"的水面, 因为参考高度变了而变成"不算危险",
                    //   或者反过来。症状会伪装成"地形读错了", 极难查。
                    //   现在没炸只是因为取的是 max(对枚举顺序免疫), 对"厨师在哪"依然敏感。
                    bool sameLevel = b.max.y >= floorY - 0.4f;
                    // 体积远大于整张网格 ⇒ 是世界级体积(整图铺满的 KillPlane), 不是"能绕过去的本地危险区"
                    bool worldVolume = (b.size.x * b.size.z) > gridArea * 4f;

                    var hz = new Haz();
                    hz.Name = name;
                    hz.Type = type;
                    hz.X0 = b.min.x;
                    hz.X1 = b.max.x;
                    hz.Z0 = b.min.z;
                    hz.Z1 = b.max.z;
                    hz.Y0 = b.min.y;
                    hz.Y1 = b.max.y;
                    hz.KillPlane = nameLooksKillPlane;
                    hz.WorldVolume = worldVolume;
                    hz.Kills = deadlyType && sameLevel && !worldVolume;
                    hz.Use = false;
                    hz.Cells = 0;
                    _haz.Add(hz);
                }
            }
            catch (Exception) { }
        }

        private static string Inv(string key, float v)
        {
            return key + v.ToString("F3", System.Globalization.CultureInfo.InvariantCulture);
        }

        private static string Safe(string s)
        {
            if (string.IsNullOrEmpty(s))
                return "";
            return s.Replace("\\", "/").Replace("\"", "'");
        }
    }
}
