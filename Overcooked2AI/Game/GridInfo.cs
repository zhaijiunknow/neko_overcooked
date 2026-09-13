using System;
using System.Collections;
using System.Collections.Generic;
using System.Reflection;
using System.Text;
using UnityEngine;

namespace Overcooked2AI.Game
{
    /// <summary>游戏**自己的**网格: 格子↔世界坐标换算 + 占位表。
    ///
    /// 为什么必须读它, 而不是自己采样推断(用户指出的"解析不明白只能猜"):
    ///   反编译确认(GridManager.cs:9,64-92):
    ///       private Dictionary&lt;GridIndex, GameObject&gt; m_gridOccupancy   ← 游戏自己认为"谁占了哪个格子"
    ///       public GameObject GetGridOccupant(GridIndex)                ← 权威占用查询
    ///   格子↔世界坐标(QuadGridManager.cs:28-38):
    ///       pos  = transform.TransformPoint(m_origin + index * m_size)   ← **整数格 → 世界坐标**
    ///       idx  = Round((transform.InverseTransformPoint(p) - m_origin) / m_size)
    ///   也就是说: 原点 m_origin、步长 m_size、甚至网格自身的旋转缩放, 都是关卡里
    ///   [SerializeField] 写死的值 —— 我们以前拿"关卡包围盒 + 1.2 假设"去算格子中心和
    ///   相邻格, 属于**猜**, 于是才有"相邻格可走但人进不去/站不到"这类偏差。
    ///   还有 HexGridManager(六边形关卡), 换算规则不同, 所以这里把类型也报出来。
    ///
    /// 本类只读, 不改任何游戏状态。</summary>
    public static class GridInfo
    {
        public static string Snapshot()
        {
            var sb = new StringBuilder();
            sb.Append("{\"count\":");
            int n = 0;
            try
            {
                var gmType = SceneScanner.FindType("GridManager");
                if (gmType == null)
                    return "{\"error\":\"GridManager 类型未找到\"}";
                var getCount = gmType.GetMethod("GetActiveCount", BindingFlags.Public | BindingFlags.Static);
                var getActive = gmType.GetMethod("GetActive", BindingFlags.Public | BindingFlags.Static);
                if (getCount == null || getActive == null)
                    return "{\"error\":\"GridManager.GetActive* 未找到\"}";

                int count = Convert.ToInt32(getCount.Invoke(null, null));
                sb.Append(count).Append(",\"grids\":[");
                for (int i = 0; i < count; i++)
                {
                    object gm = getActive.Invoke(null, new object[] { i });
                    if (gm == null)
                        continue;
                    if (n > 0)
                        sb.Append(",");
                    sb.Append(One(gm, gmType));
                    n++;
                }
                sb.Append("]");
            }
            catch (Exception ex)
            {
                return "{\"error\":\"" + Safe(ex.GetType().Name + ": " + ex.Message) + "\"}";
            }
            sb.Append("}");
            return sb.ToString();
        }

        private static string One(object gm, Type gmType)
        {
            var sb = new StringBuilder();
            var comp = gm as Component;
            var t = gm.GetType();
            sb.Append("{\"type\":\"").Append(Safe(t.Name)).Append("\"");

            // 网格自身的 transform: 原点/步长都要经过它(可能有旋转与缩放)
            if (comp != null)
            {
                var p = comp.transform.position;
                var e = comp.transform.eulerAngles;
                var s = comp.transform.lossyScale;
                sb.Append(Fmt("pos", p.x, p.y, p.z));
                sb.Append(Fmt("rot", e.x, e.y, e.z));
                sb.Append(Fmt("scale", s.x, s.y, s.z));
            }

            // m_gridHalfSize: 格子编号范围是 [-half, +half]
            try
            {
                var f = gmType.GetField("m_gridHalfSize",
                    BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
                object hs = f != null ? f.GetValue(gm) : null;
                if (hs != null)
                {
                    float hx = Num(hs, "X"), hy = Num(hs, "Y"), hz = Num(hs, "Z");
                    sb.Append(",\"half\":[").Append(I(hx)).Append(",").Append(I(hy))
                      .Append(",").Append(I(hz)).Append("]");
                    // ⚠ 这里曾经输出 `"cellSpan":"["placeholder"]"` —— **非法 JSON**
                    //   (`placeholder` 是字面量, 值是字符串 `"["`, 解析器读到 `p` 就炸)。
                    //   一调 `grid` 命令就报 `Expecting ',' delimiter: char 156`。
                    //
                    //   为什么当时填不出来: 要算"整张网格覆盖的世界范围"需要格宽(`size`),
                    //   而 `size` 是在**下面另一个 try 块**里才读到的, 此刻还没有。
                    //
                    //   现在先输出合法的空数组 —— **别再造一个看起来像数据的东西**。
                    //   要真做的话: 把 size 的读取提到前面, 然后 span = (2*half+1) * size
                    //   (m_gridHalfSize 的注释说格子编号范围是 [-half, +half], 所以格数是 2*half+1)。
                    sb.Append(",\"cellSpan\":[]");
                }
            }
            catch (Exception) { }

            // QuadGridManager 的原点与步长 —— **权威的格子换算参数**
            try
            {
                float ox = 0f, oy = 0f, oz = 0f, sx = 0f, sy = 0f, sz = 0f;
                bool hasO = false, hasS = false;
                for (var cur = t; cur != null && cur != typeof(object); cur = cur.BaseType)
                {
                    var fo = cur.GetField("m_origin", BindingFlags.Instance | BindingFlags.NonPublic);
                    if (fo != null && !hasO)
                    {
                        object v = fo.GetValue(gm);
                        if (v is Vector3)
                        {
                            var vv = (Vector3)v;
                            ox = vv.x; oy = vv.y; oz = vv.z; hasO = true;
                        }
                    }
                    var fs = cur.GetField("m_size", BindingFlags.Instance | BindingFlags.NonPublic);
                    if (fs != null && !hasS)
                    {
                        object v = fs.GetValue(gm);
                        if (v is Vector3)
                        {
                            var vv = (Vector3)v;
                            sx = vv.x; sy = vv.y; sz = vv.z; hasS = true;
                        }
                    }
                }
                if (hasO)
                    sb.Append(Fmt("origin", ox, oy, oz));
                if (hasS)
                    sb.Append(Fmt("size", sx, sy, sz));
            }
            catch (Exception) { }

            // 占位表: **游戏自己认为哪些格子被占了、被谁占的**
            try
            {
                object dict = null;
                for (var cur = t; cur != null && cur != typeof(object); cur = cur.BaseType)
                {
                    var f = cur.GetField("m_gridOccupancy",
                        BindingFlags.Instance | BindingFlags.NonPublic);
                    if (f != null)
                    {
                        dict = f.GetValue(gm);
                        break;
                    }
                }
                var d = dict as IDictionary;
                if (d != null)
                {
                    sb.Append(",\"occupied\":").Append(d.Count);
                    sb.Append(",\"cells\":[");
                    int k = 0;
                    var byName = new Dictionary<string, int>();
                    foreach (DictionaryEntry en in d)
                    {
                        var idx = en.Key;
                        var gobj = en.Value as GameObject;
                        if (gobj == null)
                            continue;
                        string nm = Safe(gobj.name);
                        string tag = "";
                        int lay = gobj.layer;
                        try { tag = gobj.tag; } catch (Exception) { }
                        int num;
                        byName.TryGetValue(nm, out num);
                        byName[nm] = num + 1;
                        if (k < 400)      // 明细封顶, 免得一关几千格把桥塞满
                        {
                            if (k > 0)
                                sb.Append(",");
                            sb.Append("{\"x\":").Append(Num(idx, "X"))
                              .Append(",\"y\":").Append(Num(idx, "Y"))
                              .Append(",\"z\":").Append(Num(idx, "Z"))
                              .Append(",\"n\":\"").Append(nm)
                              .Append("\",\"tag\":\"").Append(Safe(tag))
                              .Append("\",\"layer\":").Append(lay).Append("}");
                        }
                        k++;
                    }
                    sb.Append("],\"shown\":").Append(Math.Min(k, 400));
                    sb.Append(",\"byName\":{");
                    int b = 0;
                    foreach (var kv in byName)
                    {
                        if (b > 0)
                            sb.Append(",");
                        sb.Append("\"").Append(kv.Key).Append("\":").Append(kv.Value);
                        b++;
                    }
                    sb.Append("}");
                }
            }
            catch (Exception) { }

            sb.Append("}");
            return sb.ToString();
        }

        private static string Fmt(string key, float a, float b, float c)
        {
            return string.Format(System.Globalization.CultureInfo.InvariantCulture,
                ",\"{0}\":[{1:F3},{2:F3},{3:F3}]", key, a, b, c);
        }

        private static string I(float v)
        {
            return ((int)v).ToString(System.Globalization.CultureInfo.InvariantCulture);
        }

        private static float Num(object o, string member)
        {
            try
            {
                if (o == null)
                    return 0f;
                var t = o.GetType();
                var p = t.GetProperty(member);
                if (p != null)
                    return Convert.ToSingle(p.GetValue(o, null));
                var f = t.GetField(member);
                if (f != null)
                    return Convert.ToSingle(f.GetValue(o));
            }
            catch (Exception) { }
            return 0f;
        }

        private static string Safe(string s)
        {
            return string.IsNullOrEmpty(s) ? "" : s.Replace("\"", "'").Replace("\\", "/");
        }
    }
}
