using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Overcooked2AI.Game
{
    /// <summary>状态采集: 主线程每帧读游戏状态, 缓存 JSON。
    /// L1: scene/inRound/mode。L2: 对局中每 ~1s 扫一次台子布局。
    ///
    /// 线程纪律: FindObjectsOfType / GetComponent / FindGameObjectsWithTag 等 Unity API
    /// **只能在主线程**调用。桥是后台线程, 所以大扫描走"请求-等待"模式:
    /// 桥设置 kind, 主线程 Refresh() 里执行并写回结果, 桥轮询取走。</summary>
    public sealed class StateCollector
    {
        private readonly object _lock = new object();
        private string _snapshot = "";
        private float _lastLayoutScan = -10f;
        /// <summary>台面数组 —— **每帧**刷新(静态身份走 SceneScanner 的缓存, 位置/内容现读)。</summary>
        private string _stationsCache = "[]";
        /// <summary>烹饪进度 —— 0.1 秒刷新(1× FindObjectsOfType + 每口锅几次反射)。</summary>
        private string _cookCache = "[]";
        private float _lastCookScan = -10f;
        /// <summary>**全场景按 tag 找的食材** —— 0.1 秒刷新。
        /// 补的是"不在台面上的食材看不见"这个盲区(掉地上的/移动平台上的)。
        /// 依据: 游戏自己的 `GameUtils.GetAllIngredients()`(GameUtils.cs:504)。</summary>
        private string _itemsCache = "[]";
        private float _lastItemsScan = -10f;
        /// <summary>**会动的东西**: 路人 / 车辆 / 移动危险物 —— 0.1 秒刷新。
        /// 地形的静态快照看不见它们(moved 字段才是判据), 见 MoverScan 的注释。</summary>
        private string _moversCache = "[]";
        private float _lastMoverScan = -10f;

        // ---- 主线程任务(桥发起, 主线程执行) ----
        private string _jobKind = "";
        private string _jobArg = "";
        private int _jobWanted;
        private int _jobDone;
        private string _jobJson = "";
        private string _jobError = "";

        /// <summary>桥线程调用: 请求一次主线程扫描并等结果。kind: raw / know / live / path。</summary>
        public string RequestJob(string kind, int timeoutMs, string arg = "")
        {
            int want;
            lock (_lock)
            {
                _jobKind = kind;
                _jobArg = arg ?? "";
                _jobWanted++;
                _jobJson = "";
                _jobError = "";
                want = _jobWanted;
            }
            var sw = System.Diagnostics.Stopwatch.StartNew();
            while (sw.ElapsedMilliseconds < timeoutMs)
            {
                lock (_lock)
                {
                    if (_jobDone >= want && _jobJson.Length > 0)
                        return _jobJson;
                    if (_jobError.Length > 0)
                        return "{\"error\":\"" + _jobError + "\"}";
                }
                System.Threading.Thread.Sleep(15);
            }
            return "{\"error\":\"timeout(main thread busy)\",\"kind\":\"" + kind + "\"}";
        }

        /// <summary>主线程: 有请求就执行一次扫描。</summary>
        private void PumpJob()
        {
            string kind;
            string arg;
            int want;
            lock (_lock)
            {
                kind = _jobKind;
                arg = _jobArg;
                want = _jobWanted;
                if (kind.Length == 0 || want <= _jobDone)
                    return;
            }
            string json = "";
            try
            {
                if (kind == "raw")
                    json = SceneScanner.ScanRaw();
                else if (kind == "know")
                    json = ItemKnowledge.Snapshot();
                else if (kind == "live")
                    json = OrderCapture.LiveSnapshot();
                else if (kind == "path")
                    json = NavPath.PathFromArg(arg);
                else if (kind == "map")
                    json = LevelInfo.Snapshot(arg);
                else if (kind == "dyn")
                    json = InteractiveScan.Snapshot();
                else if (kind == "spray")
                    json = InteractiveScan.SprayDiag();
                else if (kind == "movers")
                    json = MoverScan.Snapshot();
                else if (kind == "grid")
                    json = GridInfo.Snapshot();
                else if (kind == "cells")
                    json = CellMap.Snapshot(arg);
                else if (kind == "direct")
                {
                    // arg = "<player>:<action>"
                    int pl = 0;
                    string act = "pickup";
                    try
                    {
                        var parts = (arg ?? "").Split(':');
                        if (parts.Length > 0) int.TryParse(parts[0], out pl);
                        if (parts.Length > 1) act = parts[1];
                    }
                    catch (Exception) { }
                    json = InteractDirect.Do(pl, act);
                }
                else if (kind == "pad")
                    json = VirtualInputJob(arg);
                else
                    json = "{\"error\":\"unknown job\"}";
            }
            catch (Exception ex)
            {
                json = "{\"error\":\"" + (ex.Message ?? "").Replace("\"", "'") + "\"}";
            }
            lock (_lock)
            {
                if (json.Length > 0)
                {
                    _jobJson = json;
                    _jobDone = want;
                }
                else
                {
                    _jobError = "empty result";
                }
            }
        }

        public void Refresh()
        {
            PumpJob();
            // 虚拟手柄: 万一游戏自己把控制方案改回去了(换人/重绑定), 立刻装回来。
            // 只在主线程做 —— 它要访问 Unity 对象。
            try { VirtualInput.Tick(); } catch (Exception) { }
            string scene = "";
            bool inRound = false;
            string mode = "";
            try
            {
                scene = SceneManager.GetActiveScene().name;
            }
            catch (Exception) { }
            try
            {
                var fc = GameUtils.GetFlowController();
                if (fc != null)
                    inRound = fc.InRound;
            }
            catch (Exception) { }
            try
            {
                mode = ClientGameSetup.Mode.ToString();
            }
            catch (Exception) { }

            // 对局中: 世界(台子)每 1 秒扫一次, 厨师位置每 0.1 秒扫一次。
            // 分开是因为导航是闭环: 位置读得慢, 按键就会用旧坐标算方向, 必然来回震。
            string layout = "{}";
            string recipePool = "[]";
            if (inRound)
            {
                float now = Time.realtimeSinceStartup;

                // **台面: 每帧**(用户提的"实时地图")。
                //   贵的部分(20 个类型各一次 FindObjectsOfType)已经挪进 SceneScanner 的
                //   静态缓存, 一关只做一次; 这里每帧的只是"走一遍缓存的引用, 读位置+内容"。
                //   ⚠ 坐标**不缓存** —— 用户指出本游戏没有绝对静态的台面(食材会放上去、
                //   锅会被端走), 所以位置每帧从 GameObject 现读。
                try
                {
                    _stationsCache = SceneScanner.ScanStations();
                }
                catch (Exception) { }

                // 烹饪进度: 0.1 秒(它自带 1× FindObjectsOfType + 每口锅几次反射)
                if (now - _lastCookScan >= 0.1f)
                {
                    _lastCookScan = now;
                    try
                    {
                        _cookCache = SceneScanner.ScanCooking();
                    }
                    catch (Exception) { }
                }

                // 会动的东西(路人/车辆/移动危险物): 0.1 秒 —— 地形快照看不见它们
                if (now - _lastMoverScan >= 0.1f)
                {
                    _lastMoverScan = now;
                    try
                    {
                        _moversCache = MoverScan.Snapshot();
                    }
                    catch (Exception) { }
                }

                // 全场景食材(按 tag): 0.1 秒 —— 补"不在台面上的食材"这个盲区
                if (now - _lastItemsScan >= 0.1f)
                {
                    _lastItemsScan = now;
                    try
                    {
                        _itemsCache = SceneScanner.ScanItems();
                    }
                    catch (Exception) { }
                }

                // 厨师位置: 0.1 秒(导航是闭环, 用旧坐标算方向必然来回震)
                if (now - _lastChefScan >= 0.1f)
                {
                    _lastChefScan = now;
                    try
                    {
                        _chefsCache = SceneScanner.ScanChefs();
                    }
                    catch (Exception) { }
                }

                // 本局分数/星级/剩余时间: 跟着 0.1 秒档, 且**必须在 C# 这一侧留快照**。
                //   理由见 `RoundScore` 的类注释: 引擎的焦点闸门在 `state()` 之前,
                //   用户切出游戏时 Python 一次都读不到, 结果就永久丢了。
                RoundScore.CaptureRound(scene);

                // 配方池: 1 秒(要读游戏对象, 有开销)
                if (now - _lastLayoutScan >= 1f)
                {
                    _lastLayoutScan = now;
                    try
                    {
                        _recipeCache = ReadRecipePool();
                    }
                    catch (Exception) { }
                    // 配方明细: 每对局只读一次(主线程; 树遍历有开销)
                    if (!_recipeDetailRead)
                    {
                        _recipeDetailRead = true;
                        try
                        {
                            _recipeDetailCache = ReadRecipeDetails();
                        }
                        catch (Exception) { }
                    }
                }

                // ⚠ `ScanCooking()` 返回的是**裸的元素列表**(没有外层方括号)。
                //   旧代码里那对方括号是 `ExtractArray(完整 Scan() 结果, "cooking")` 剥出来的,
                //   现在直接调它就必须自己补 —— 否则 `"cooking":{...},{...}` 是**非法 JSON**,
                //   整个 state 解析失败, 脚本第一步就崩。
                //   (这个 bug 一跑 mapview 就暴露了: JSONDecodeError char 9667。)
                layout = "{\"stations\":" + _stationsCache
                       + ",\"chefs\":" + _chefsCache
                       + ",\"items\":" + _itemsCache
                       + ",\"movers\":" + _moversCache
                       + ",\"cooking\":[" + _cookCache + "]}";
                recipePool = _recipeCache;
            }
            else
            {
                // 离开对局: 清缓存, 否则换关卡后拿到的还是上一关的配方/布局
                _recipeDetailRead = false;
                _recipeDetailCache = "[]";
                _recipeCache = "[]";
                _chefsCache = "[]";
                _stationsCache = "[]";
                _cookCache = "[]";
                _itemsCache = "[]";
                _moversCache = "[]";
                // 台面静态缓存的引用会指向已销毁的对象 —— 换关必须作废重建
                try { SceneScanner.InvalidateStationCache(); } catch (Exception) { }
                // 出局: **只翻标志位, 快照冻住** —— `lastResult` 靠它才能报出去。
                RoundScore.NoteOutOfRound();
            }

            string round = inRound ? "true" : "false";
            string app = "{}";
            try { app = VirtualInput.AppState(); } catch (Exception) { }
            lock (_lock)
            {
                // ⚠ 这两块挂在 `if (inRound)` **块外** —— 出局之后照样要报 `lastResult`,
                //   否则"上一局赢没赢"在对局结束那一刻就没了。
                _snapshot = string.Format(
                    "{{\"scene\":\"{0}\",\"inRound\":{1},\"mode\":\"{2}\",\"layout\":{3},\"recipes\":{4},\"details\":{5},\"app\":{6},{7},\"bridge\":\"ok\"}}",
                    scene, round, mode, layout, recipePool, _recipeDetailCache, app,
                    RoundScore.Json(inRound));
            }
        }

        /// <summary>主线程: 虚拟手柄的安装/卸载(要访问 Unity 对象, 不能从桥线程做)。
        /// arg 形如 "install:0" / "installp:1" / "installall" / "uninstall:0"。</summary>
        private static string VirtualInputJob(string arg)
        {
            string a = arg ?? "";
            int colon = a.IndexOf(':');
            string op = colon >= 0 ? a.Substring(0, colon) : a;
            int n = 0;
            if (colon >= 0)
            {
                int.TryParse(a.Substring(colon + 1), out n);
            }
            if (op == "install")
                return VirtualInput.Install(n);
            if (op == "installp")
                return VirtualInput.InstallByPlayer(n);
            if (op == "installall")
                return VirtualInput.InstallAll();
            if (op == "uninstall")
                return VirtualInput.Uninstall(n);
            return "{\"ok\":false,\"error\":\"bad pad arg: " + op + "\"}";
        }

        private string _recipeCache = "[]";
        private string _recipeDetailCache = "[]";
        private bool _recipeDetailRead;
        private string _chefsCache = "[]";
        private float _lastChefScan = -10f;

        /// <summary>从 {"stations":[...],"cooking":[...]} 里抠出某个数组原文。</summary>
        private static string ExtractArray(string json, string key)
        {
            if (string.IsNullOrEmpty(json))
                return "[]";
            string marker = "\"" + key + "\":[";
            int i = json.IndexOf(marker, StringComparison.Ordinal);
            if (i < 0)
                return "[]";
            int start = i + marker.Length - 1;   // 指向 '['
            int depth = 0;
            for (int j = start; j < json.Length; j++)
            {
                char c = json[j];
                if (c == '[')
                    depth++;
                else if (c == ']')
                {
                    depth--;
                    if (depth == 0)
                        return json.Substring(start, j - start + 1);
                }
            }
            return "[]";
        }

        /// <summary>读每道菜配方明细(主线程, 每对局一次, 订单池里全部配方)。</summary>
        private static string ReadRecipeDetails()
        {
            var sb = new System.Text.StringBuilder();
            sb.Append("[");
            try
            {
                var orders = ReadRecipeOrders();
                int n = 0;
                foreach (var order in orders)
                {
                    if (order == null)
                        continue;
                    if (n > 0)
                        sb.Append(",");
                    sb.Append(RecipeReader.Describe(order));
                    n++;
                }
            }
            catch (Exception) { }
            sb.Append("]");
            return sb.ToString();
        }

        private static System.Collections.Generic.List<object> ReadRecipeOrders()
        {
            var list = new System.Collections.Generic.List<object>();
            try
            {
                var lc = GameUtils.GetLevelConfig();
                if (lc == null)
                    return list;
                var grd = lc.GetType().GetMethod("GetRoundData");
                if (grd == null)
                    return list;
                var rd = grd.Invoke(lc, null);
                if (rd == null)
                    return list;
                var recipesField = rd.GetType().GetField("m_recipes");
                if (recipesField == null)
                    return list;
                var recipeList = recipesField.GetValue(rd);
                if (recipeList == null)
                    return list;
                var entriesField = recipeList.GetType().GetField("m_recipes");
                if (entriesField == null)
                    return list;
                var entries = entriesField.GetValue(recipeList) as Array;
                if (entries == null)
                    return list;
                foreach (var entry in entries)
                {
                    if (entry == null)
                        continue;
                    var orderField = entry.GetType().GetField("m_order");
                    if (orderField == null)
                        continue;
                    var order = orderField.GetValue(entry);
                    if (order != null)
                        list.Add(order);
                }
            }
            catch (Exception) { }
            return list;
        }

        /// <summary>读当前关卡菜谱池(GetLevelConfig → RoundData.m_recipes)。
        /// 只读每道菜 name, 不遍历配方树。</summary>
        private static string ReadRecipePool()
        {
            var sb = new System.Text.StringBuilder();
            sb.Append("[");
            try
            {
                var lc = GameUtils.GetLevelConfig();
                if (lc == null)
                    return "[]";
                // 调 GetRoundData() → RoundData
                var grd = lc.GetType().GetMethod("GetRoundData");
                if (grd == null)
                    return "[]";
                var rd = grd.Invoke(lc, null);
                if (rd == null)
                    return "[]";
                // 读 m_recipes (RecipeList) → m_recipes.m_recipes (Entry[])
                var recipesField = rd.GetType().GetField("m_recipes");
                if (recipesField == null)
                    return "[]";
                var recipeList = recipesField.GetValue(rd);
                if (recipeList == null)
                    return "[]";
                var entriesField = recipeList.GetType().GetField("m_recipes");
                if (entriesField == null)
                    return "[]";
                var entries = entriesField.GetValue(recipeList) as Array;
                if (entries == null)
                    return "[]";
                int n = 0;
                foreach (var entry in entries)
                {
                    if (entry == null)
                        continue;
                    var orderField = entry.GetType().GetField("m_order");
                    if (orderField == null)
                        continue;
                    var order = orderField.GetValue(entry);
                    if (order == null)
                        continue;
                    string name = "";
                    try
                    {
                        var p = order.GetType().GetProperty("name");
                        if (p != null)
                            name = (string)p.GetValue(order, null);
                    }
                    catch (Exception) { }
                    if (n > 0)
                        sb.Append(",");
                    sb.Append("\"" + SafeJson(name) + "\"");
                    n++;
                }
            }
            catch (Exception) { }
            sb.Append("]");
            return sb.ToString();
        }

        private static string SafeJson(string s)
        {
            if (string.IsNullOrEmpty(s))
                return "";
            return s.Replace("\"", "'").Replace("\\", "/");
        }

        public string Snapshot()
        {
            lock (_lock)
            {
                return _snapshot;
            }
        }

        /// <summary>给小地图用: 两个厨师的实时位置(主线程调用)。</summary>
        public struct ChefDot
        {
            public int id;
            public float x;
            public float z;
        }

        public static List<ChefDot> ChefsForOverlay()
        {
            var outList = new List<ChefDot>();
            try
            {
                var pcType = SceneScanner.FindType("PlayerControls");
                var idType = SceneScanner.FindType("PlayerIDProvider");
                if (pcType == null)
                    return outList;
                var objs = UnityEngine.Object.FindObjectsOfType(pcType);
                if (objs == null)
                    return outList;
                for (int i = 0; i < objs.Length; i++)
                {
                    var comp = objs[i] as Component;
                    if (comp == null)
                        continue;
                    var pos = comp.transform.position;
                    int id = i;
                    if (idType != null)
                    {
                        var idProv = comp.GetComponent(idType);
                        var m = idType.GetMethod("GetID", Type.EmptyTypes);
                        if (idProv != null && m != null)
                        {
                            try { id = Convert.ToInt32(m.Invoke(idProv, null)); }
                            catch (Exception) { }
                        }
                    }
                    outList.Add(new ChefDot { id = id, x = pos.x, z = pos.z });
                }
            }
            catch (Exception) { }
            return outList;
        }
    }
}
