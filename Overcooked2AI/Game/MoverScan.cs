using System;
using System.Collections.Generic;
using System.Text;
using UnityEngine;

namespace Overcooked2AI.Game
{
    /// <summary>**会动的东西**: 路人 / 车辆 / 移动危险物 —— 地图建模漏掉的那一层。
    ///
    /// 为什么必须有(用户指出: "对于路人和车辆完全没有建模"):
    ///   地形图是一张**整局一次的静态快照**(Engine.terrain() 按场景缓存),
    ///   而这两类东西**会动**:
    ///     · 路人   —— 被 `物理阻挡` 扫到, 只是"那一格走不了"的静态标记。它一走, 标的地方就错了
    ///     · 车辆   —— 带 `RespawnCollider`(`RespawnCollider.cs:9` 的
    ///                 `enum RespawnType { Hit, Drowning, FallDeath, Car }`),
    ///                 被当**静态危险区**扫进去。**车会开,** 死亡区却冻在第一次扫到的位置。
    ///
    ///   后果不是"看不见", 而是**看见了错的东西**:
    ///     地图说 (12,5) 安全可走 ✅ —— 实际车已经开过来, 走到那儿就是死。
    ///   这比空白更糟, 因为它让人放心地走进去。
    ///
    /// **判据故意不依赖类名**: 反编译代码里根本没有 `NPC` 类
    ///   (`DLC03_NPC_02` 只是**物体名**), 也没有 Vehicle/Car 类 —— 所以:
    ///     ① `RespawnCollider` 按**组件**找(这是"会弄死你"的权威标记)
    ///     ② 其余按**名字模式**兜一层(路人没有专门组件)
    ///     ③ 每个都报 **moved**: 和上一次快照比, 位置变没变
    ///        —— 这才是"它是不是在动"的**通用判据**, 不依赖任何命名约定
    ///
    /// 成本: 只有 RespawnCollider + 名字命中的物体, 数量很少, 所以跟着 0.1 秒档跑。
    /// </summary>
    public static class MoverScan
    {
        /// <summary>名字里含这些词的物体也一起跟 —— 路人没有专门组件, 只能靠名字兜。</summary>
        private static readonly string[] NamePatterns =
        {
            "npc", "car", "vehicle", "pedestrian", "traffic", "crowd",
            "animal", "mouse", "rat", "cat", "dog", "bird", "ghost", "bull",
        };

        /// <summary>上一帧的位置: instanceID -> (x, y, z)。用来算 moved。</summary>
        private static readonly Dictionary<int, Vector3> _last = new Dictionary<int, Vector3>();
        private static float _lastAt;

        /// <summary>返回**裸数组** `[...]` —— 和 `ScanItems()` 一致。
        ///
        /// ⚠ 一开始写成了 `{"movers":[...],"count":N}`, 结果它被塞进 layout 的
        ///   `"movers":` 后面就成了一个**对象**; Python 那边按数组写的,
        ///   `for m in layout["movers"]` 迭代出来的是键(字符串),
        ///   直接 `AttributeError: 'str' object has no attribute 'get'`。
        ///   教训: 同一个 layout 里, 同类字段的**形状要统一**。
        /// </summary>
        public static string Snapshot()
        {
            var sb = new StringBuilder();
            sb.Append("[");
            int n = 0;
            var seen = new Dictionary<int, int>();

            // ① RespawnCollider —— "会弄死你"的权威标记(车辆就在这里)
            foreach (var c in Comps("RespawnCollider"))
            {
                if (c == null || c.gameObject == null)
                    continue;
                Append(sb, ref n, seen, c.gameObject, "RespawnCollider", DeathBy(c));
            }

            // ② 名字命中的(路人没有专门组件)
            try
            {
                var all = UnityEngine.Object.FindObjectsOfType(typeof(Collider));
                if (all != null)
                {
                    foreach (var o in all)
                    {
                        var col = o as Collider;
                        if (col == null || col.gameObject == null)
                            continue;
                        if (!NameHits(col.gameObject.name))
                            continue;
                        Append(sb, ref n, seen, col.gameObject, "ByName", "");
                    }
                }
            }
            catch (Exception) { }

            sb.Append("]");
            _lastAt = Time.realtimeSinceStartup;
            return sb.ToString();
        }

        private static bool Append(StringBuilder sb, ref int n, Dictionary<int, int> seen,
                                   GameObject go, string kind, string deathBy)
        {
            int iid;
            try { iid = go.GetInstanceID(); }
            catch (Exception) { return false; }
            if (seen.ContainsKey(iid))
                return false;
            seen[iid] = 1;

            Vector3 pos;
            float r = 0.6f;                 // 默认半径(一格 1.2 的半宽)
            try
            {
                pos = go.transform.position;
                var col = go.GetComponent<Collider>();
                if (col != null)
                    r = Mathf.Max(col.bounds.extents.x, col.bounds.extents.z);
            }
            catch (Exception) { return false; }

            // moved = 和上一帧比位置变了没有 —— **通用判据**, 不依赖命名
            bool moved = false;
            Vector3 prev;
            if (_last.TryGetValue(iid, out prev))
                moved = (prev - pos).sqrMagnitude > 0.01f;   // >0.1 格才算动(滤浮点抖)

            _last[iid] = pos;

            if (n > 0)
                sb.Append(",");
            sb.Append(string.Format(
                System.Globalization.CultureInfo.InvariantCulture,
                "{{\"name\":\"{0}\",\"kind\":\"{1}\",\"deathBy\":\"{2}\","
                + "\"x\":{3:F2},\"z\":{4:F2},\"r\":{5:F2},\"moved\":{6}}}",
                Safe(go.name), Safe(kind), Safe(deathBy),
                pos.x, pos.z, r, moved ? "true" : "false"));
            n++;
            return true;
        }

        private static bool NameHits(string name)
        {
            if (string.IsNullOrEmpty(name))
                return false;
            string n = name.ToLowerInvariant();
            for (int i = 0; i < NamePatterns.Length; i++)
                if (n.Contains(NamePatterns[i]))
                    return true;
            return false;
        }

        /// <summary>读 RespawnCollider.m_respawnType(`RespawnCollider.cs:12`)。</summary>
        private static string DeathBy(Component c)
        {
            try
            {
                var f = c.GetType().GetField("m_respawnType",
                    System.Reflection.BindingFlags.Instance
                    | System.Reflection.BindingFlags.Public
                    | System.Reflection.BindingFlags.NonPublic);
                var v = f != null ? f.GetValue(c) : null;
                return v == null ? "" : v.ToString();
            }
            catch (Exception) { return ""; }
        }

        private static Component[] Comps(string typeName)
        {
            var t = SceneScanner.FindType(typeName);
            if (t == null)
                return new Component[0];
            var objs = UnityEngine.Object.FindObjectsOfType(t);
            if (objs == null)
                return new Component[0];
            var list = new List<Component>();
            for (int i = 0; i < objs.Length; i++)
            {
                var c = objs[i] as Component;
                if (c != null)
                    list.Add(c);
            }
            return list.ToArray();
        }

        private static string Safe(string s)
        {
            if (string.IsNullOrEmpty(s))
                return "";
            return s.Replace("\\", "/").Replace("\"", "'");
        }
    }
}
