using System;
using System.Collections.Generic;
using System.Reflection;
using System.Text;
using UnityEngine;

namespace Overcooked2AI.Game
{
    /// <summary>食材知识表: 把"这个食材要经过什么加工"从游戏数据里挖出来, 交给 Python 推导完整流程。
    ///
    /// 依据(反编译):
    ///   · Unity Tag 本身就区分加工阶段 —— Pre-Ingredient(需加工的生料) / Ingredient(已加工成品) / Crate(食材箱)
    ///   · WorkableItem.m_nextPrefab   → 切完之后变成什么(有这个组件 = 可以切)
    ///   · WorkableItem.m_stages       → 切片数
    ///   · CookingHandler.m_stationType→ 要哪种灶(Hob 煮锅/Oven 烤箱/DeepFatFryer 炸锅/FirePit/Barbeque...)
    ///   · CookingHandler.m_cookingtime→ 熟的时间; 超过 2 倍就烧焦(GetCookedOrderState)
    ///   · PickupItemSpawner.m_itemPrefab → 箱子出什么
    /// 这些都是游戏自己的 public 字段/方法, 不猜测。</summary>
    public static class ItemKnowledge
    {
        private static readonly string[] Tags =
        {
            "Pre-Ingredient", "Ingredient", "Crate", "Utensil",
        };

        public static string Snapshot()
        {
            var sb = new StringBuilder();
            int n = 0;
            var seen = new System.Collections.Generic.Dictionary<int, int>();

            // 1) 场景里的实例(有位置、有 Unity Tag)
            foreach (var tag in Tags)
            {
                GameObject[] objs;
                try
                {
                    objs = GameObject.FindGameObjectsWithTag(tag);
                }
                catch (Exception)
                {
                    continue; // 该关卡没有这个 tag
                }
                if (objs == null)
                    continue;
                foreach (var go in objs)
                {
                    if (go == null || seen.ContainsKey(go.GetInstanceID()))
                        continue;
                    seen[go.GetInstanceID()] = 1;
                    if (n > 0)
                        sb.Append(",");
                    sb.Append(One(go, tag, false));
                    n++;
                }
            }

            // 2) 已加载的 prefab 资源。
            //    必需: 食材还在箱子里时场景里根本没有它的实例, 但"要用什么灶、煮多久、切几片"
            //    这些加工参数只存在于 prefab 上, 不扫就查不到(煮这一步会直接失败)。
            ScanPrefabs(sb, ref n, seen, "CookableIngredient");
            ScanPrefabs(sb, ref n, seen, "WorkableItem");
            ScanPrefabs(sb, ref n, seen, "IngredientPropertiesComponent");

            return string.Format("{{\"items\":[{0}],\"count\":{1}}}", sb, n);
        }

        private static void ScanPrefabs(StringBuilder sb, ref int n,
            System.Collections.Generic.Dictionary<int, int> seen, string typeName)
        {
            try
            {
                var type = SceneScanner.FindType(typeName);
                if (type == null)
                    return;
                var objs = Resources.FindObjectsOfTypeAll(type);
                if (objs == null)
                    return;
                foreach (var o in objs)
                {
                    var comp = o as Component;
                    if (comp == null)
                        continue;
                    var go = comp.gameObject;
                    if (go == null)
                        continue;
                    // 只要不在场景里的(= prefab 资源), 实例已在第 1 步收过
                    try
                    {
                        if (go.scene.IsValid())
                            continue;
                    }
                    catch (Exception) { continue; }
                    int iid = go.GetInstanceID();
                    if (seen.ContainsKey(iid))
                        continue;
                    seen[iid] = 1;
                    string json = One(go, "Prefab", true);
                    if (json == null)
                        continue;
                    if (n > 0)
                        sb.Append(",");
                    sb.Append(json);
                    n++;
                }
            }
            catch (Exception) { }
        }

        private static string One(GameObject go, string tag, bool isPrefab)
        {
            var pos = go.transform.position;
            var sb = new StringBuilder();
            sb.Append("{\"tag\":\"").Append(Safe(tag)).Append("\"");
            sb.Append(",\"name\":\"").Append(Safe(go.name)).Append("\"");
            sb.Append(string.Format(",\"x\":{0:F2},\"z\":{1:F2}", pos.x, pos.z));
            sb.Append(isPrefab ? ",\"prefab\":true" : ",\"prefab\":false");
            string ing = IngredientName(go);
            sb.Append(",\"ing\":\"").Append(Safe(ing)).Append("\"");

            // 可切? 切完是什么?
            string next = "";
            int stages = 0;
            try
            {
                var wt = SceneScanner.FindType("WorkableItem");
                if (wt != null)
                {
                    var wi = go.GetComponent(wt);
                    if (wi != null)
                    {
                        var gm = wt.GetMethod("GetNextPrefab");
                        if (gm != null)
                        {
                            var np = gm.Invoke(wi, null) as GameObject;
                            if (np != null)
                            {
                                next = IngredientName(np);
                                if (next.Length == 0)
                                    next = np.name;
                            }
                        }
                        var sf = wt.GetField("m_stages");
                        if (sf != null)
                            stages = (int)sf.GetValue(wi);
                    }
                }
            }
            catch (Exception) { }
            sb.Append(",\"next\":\"").Append(Safe(next)).Append("\",\"stages\":").Append(stages);

            // 可煮? 用什么灶? 多久熟?
            string station = "";
            float cookTime = 0f;
            try
            {
                var ct = SceneScanner.FindType("CookingHandler");
                if (ct != null)
                {
                    var ch = go.GetComponent(ct);
                    if (ch != null)
                    {
                        var stf = ct.GetField("m_stationType");
                        if (stf != null)
                        {
                            var v = stf.GetValue(ch);
                            if (v != null)
                                station = v.ToString();
                        }
                        var tf = ct.GetField("m_cookingtime");
                        if (tf != null)
                            cookTime = (float)tf.GetValue(ch);
                    }
                }
            }
            catch (Exception) { }
            sb.Append(",\"station\":\"").Append(Safe(station)).Append("\"");
            sb.Append(string.Format(",\"cookTime\":{0:F1}", cookTime));

            // ☠☠ **这个食材允许进哪些容器**(加热方式) —— "米进锅、肉进平底锅"那条规矩的
            //    权威来源, 也是游戏**拒收**时的判据。
            //
            // 依据(反编译, 规则 1):
            //   `CookableContainer.cs:46-47`
            //       var cp = _object.RequestComponent<CookableProperties>();
            //       if (cp == null || !cp.AllowsCookingStep(_handler.AccessCookingType)) return false;
            //   `CookableProperties.cs:11-13`  `AllowedCookingSteps` 是 `CookingStepData[]`,
            //       判据是 **比 `m_uID`**(`x.m_uID == _stepData.m_uID`)。
            //   ⇒ 把这张表报出来, Python 侧就能拿它跟 `ScanCooking` 报的
            //     `容器.cookId` 对上 —— **不再靠"离我最近"猜哪口锅**。
            //   没有这个组件(生料/成品) ⇒ 空数组, 调用方退回老行为。
            // ⚠ `m_uID` 是 `[SelfAssignID]` 的运行时整数: **只在同一局内可比值** ——
            //   而我们两边都现读, 正好够用(不需要跨会话稳定)。
            // ⚠ 名字只给日志/离线看; **判据只用 id**(名字可能重复或为空)。
            sb.Append(",\"cookSteps\":[");
            try
            {
                var cpt = SceneScanner.FindType("CookableProperties");
                if (cpt != null)
                {
                    var cp = go.GetComponent(cpt);
                    if (cp != null)
                    {
                        var f = cpt.GetField("AllowedCookingSteps",
                            System.Reflection.BindingFlags.Instance
                            | System.Reflection.BindingFlags.Public
                            | System.Reflection.BindingFlags.NonPublic);
                        var arr = f != null ? f.GetValue(cp) as Array : null;
                        if (arr != null)
                        {
                            int m = 0;
                            foreach (var e in arr)
                            {
                                if (e == null)
                                    continue;
                                var so = e as UnityEngine.Object;
                                string nm = so != null ? Safe(so.name) : "";
                                int id = 0;
                                try
                                {
                                    var idf = e.GetType().GetField("m_uID");
                                    if (idf != null)
                                        id = Convert.ToInt32(idf.GetValue(e));
                                }
                                catch (Exception) { }
                                if (m > 0)
                                    sb.Append(",");
                                sb.Append(string.Format(
                                    "{{\"id\":{0},\"name\":\"{1}\"}}", id, nm));
                                m++;
                            }
                        }
                    }
                }
            }
            catch (Exception) { }
            sb.Append("]");

            // 箱子出什么
            string spawn = "";
            string spawnIng = "";    // 箱子直接出的东西的食材名(直接可用)
            string spawnNext = "";   // 若出的是需切生料: 切完变成的食材名
            int spawnStages = 0;     // 生料的切片数(WorkableItem.m_stages)
            try
            {
                var pt = SceneScanner.FindType("PickupItemSpawner");
                if (pt != null)
                {
                    var sp = go.GetComponent(pt);
                    if (sp != null)
                    {
                        var f = pt.GetField("m_itemPrefab");
                        if (f != null)
                        {
                            var prefab = f.GetValue(sp) as GameObject;
                            if (prefab != null)
                            {
                                spawn = prefab.name;
                                spawnIng = IngredientName(prefab);
                                var wt2 = SceneScanner.FindType("WorkableItem");
                                var wi2 = wt2 != null ? prefab.GetComponent(wt2) : null;
                                if (wi2 != null)
                                {
                                    var sf = wt2.GetField("m_stages");
                                    if (sf != null)
                                        spawnStages = (int)sf.GetValue(wi2);
                                    var gm2 = wt2.GetMethod("GetNextPrefab");
                                    if (gm2 != null)
                                    {
                                        var np2 = gm2.Invoke(wi2, null) as GameObject;
                                        if (np2 != null)
                                            spawnNext = IngredientName(np2);
                                    }
                                }
                            }
                        }
                    }
                }
            }
            catch (Exception) { }
            sb.Append(",\"spawn\":\"").Append(Safe(spawn)).Append("\"");
            sb.Append(",\"spawnIng\":\"").Append(Safe(spawnIng)).Append("\"");
            sb.Append(",\"spawnNext\":\"").Append(Safe(spawnNext)).Append("\"");
            sb.Append(",\"spawnStages\":").Append(spawnStages);

            sb.Append("}");
            return sb.ToString();
        }

        /// <summary>容器里装了什么(用逗号拼起来)。**锅的关键信息** ——
        /// ScanCooking 读的是 CookingHandler, 而锅身上那个 handler 只知道"熟没熟",
        /// 不知道"煮的是什么", 所以 ItemKnowledge.IngredientName(锅) 永远是空串。
        /// 要知道锅里是米饭还是别的, 只能读容器的 m_contents。
        ///
        /// ⚠ 不能用 node.GetEnumerator() 遍历: IngredientAssembledNode.GetEnumerator 是
        ///    `yield return this`(无限自递归), CompositeAssembledNode.GetEnumerator 只吐孙辈。
        ///    只能读数组字段 m_composition 递归。</summary>
        public static string ContentsNames(GameObject go)
        {
            if (go == null)
                return "";
            try
            {
                var arr = GetContents(go, "ServerIngredientContainer");
                if (arr == null || arr.Length == 0)
                    arr = GetContents(go, "ClientIngredientContainer");
                if (arr == null || arr.Length == 0)
                    return "";
                var sb = new StringBuilder();
                foreach (var n in arr)
                    AppendNodeName(sb, n);
                return sb.ToString();
            }
            catch (Exception) { }
            return "";
        }

        private static Array GetContents(GameObject go, string typeName)
        {
            var ct = SceneScanner.FindType(typeName);
            if (ct == null)
                return null;
            var comp = go.GetComponent(ct);
            if (comp == null)
                return null;
            var m = ContentsMethod(ct);
            if (m == null)
                return null;
            return m.Invoke(comp, null) as Array;
        }

        /// <summary>`GetContents` 的 MethodInfo, **按类型缓存**。
        ///
        /// ⚠ 原来这里每次调用都 `ct.GetMethod(...)` —— 而 `ContentsNames` 是
        ///   **每个台面的每个子物体每帧**都要跑的(`SceneScanner.DescribeDynamic`),
        ///   50 个台面 × 1~2 个子物体 × 60fps ⇒ 每秒几千次**没必要的**反射查找。
        ///   `Type` 一旦解析出来, 它的方法表就是固定的 ⇒ 可以安全地缓存(连 null 一起,
        ///   因为"这个类没有 GetContents"也是固定事实 —— 和 `FindType` 那条
        ///   "程序集可能还没加载完"不一样)。
        ///
        /// 为什么非做不可: 用户 2026-09-15 定的规矩是"**每次都使用最新的地图**,
        /// 本身地图就小, 占用无关紧要" —— 于是 `onhas` 不再节流、每帧都算。
        /// 不先把这次查找缓存掉, 那条规矩就是在拿帧率换新鲜度。
        /// </summary>
        private static MethodInfo ContentsMethod(Type ct)
        {
            MethodInfo m;
            if (_contentsMethods.TryGetValue(ct, out m))
                return m;
            try { m = ct.GetMethod("GetContents", Type.EmptyTypes); }
            catch (Exception) { m = null; }
            _contentsMethods[ct] = m;
            return m;
        }

        private static readonly Dictionary<Type, MethodInfo> _contentsMethods =
            new Dictionary<Type, MethodInfo>();

        /// <summary>把一个订单节点还原成人类可读的名字(递归, 只读数组字段)。</summary>
        private static void AppendNodeName(StringBuilder sb, object node)
        {
            if (node == null)
                return;
            var t = node.GetType();
            var ing = t.GetField("m_ingriedientOrderNode");     // 食材叶子(注意游戏里就是拼错的)
            var item = t.GetField("m_itemOrderNode");           // 物品叶子(盘子/锅一类)
            var field = ing ?? item;
            if (field != null)
            {
                var on = field.GetValue(node);
                if (on != null)
                {
                    var p = on.GetType().GetProperty("name");
                    var s = p != null ? (string)p.GetValue(on, null) : null;
                    if (!string.IsNullOrEmpty(s))
                    {
                        if (sb.Length > 0)
                            sb.Append("+");
                        sb.Append(s);
                    }
                }
                return;
            }
            var comp = t.GetField("m_composition");             // 复合节点 → 递归
            if (comp == null)
                return;
            var arr = comp.GetValue(node) as Array;
            if (arr == null)
                return;
            foreach (var e in arr)
                AppendNodeName(sb, e);
        }

        /// <summary>物体代表什么食材(IngredientPropertiesComponent.GetOrderComposition → 食材名)。</summary>
        public static string IngredientName(GameObject go)
        {
            if (go == null)
                return "";
            try
            {
                var it = SceneScanner.FindType("IngredientPropertiesComponent");
                if (it == null)
                    return "";
                var comp = go.GetComponent(it);
                if (comp == null)
                    return "";
                var gm = it.GetMethod("GetOrderComposition");
                if (gm == null)
                    return "";
                var node = gm.Invoke(comp, null);
                if (node == null)
                    return "";
                var f = node.GetType().GetField("m_ingriedientOrderNode");
                if (f == null)
                    return "";
                var on = f.GetValue(node);
                if (on == null)
                    return "";
                var p = on.GetType().GetProperty("name");
                return p != null ? (string)p.GetValue(on, null) : "";
            }
            catch (Exception) { }
            return "";
        }

        private static string Safe(string s)
        {
            if (string.IsNullOrEmpty(s))
                return "";
            return s.Replace("\"", "'").Replace("\\", "/");
        }
    }
}
