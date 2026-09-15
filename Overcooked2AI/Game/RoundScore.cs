using System;
using System.Collections.Generic;
using System.Reflection;
using System.Text;
using UnityEngine;

namespace Overcooked2AI.Game
{
    /// <summary>**本局分数 / 星级 / 剩余时间** —— "赢没赢"的唯一权威来源。
    ///
    /// 为什么要有: 桥现在能报台面/厨师/食材/菜谱, 但**一个数字都没有** ——
    ///   交了几单、得几分、几颗星、还剩多少秒, 全得问游戏。用户要"让猫娘知道输赢",
    ///   那第一步得先有人把输赢**读出来**。
    ///
    /// 判据全部照抄游戏自己(规则1: 不猜):
    ///   · 总分    `TeamMonitor.TeamScoreStats.GetTotalScore()`
    ///             = `TotalBaseScore + TotalTipsScore - TotalTimeExpireDeductions`
    ///             (`TeamMonitor.cs:33-36`)
    ///   · 星级    `SceneDirectoryVarientEntry.GetStarForPoints(points, inNGP)`
    ///             (`SceneDirectoryData.cs:171-190`), 调用姿势照抄
    ///             `ClientCampaignFlowController.GetStarRating()` (`:148-155`)
    ///   · 下一颗星 `GetPointsForStar(stars+1)` (`SceneDirectoryData.cs:158-169`, 返回 -1 = 没有下一颗)
    ///   · **胜负    `stars > 0`** —— 游戏自己就是这么判的:
    ///             `BossLevelOutroFlowroutine.cs:27  m_succeeded = _setupData.StarsAwarded > 0`
    ///   · 失败单数 **没有独立字段**, 是推导值:
    ///             `TotalTimeExpireDeductions / GameConfig.RecipeTimeOutPointLoss`
    ///             (`CoopStarRatingUIController.cs:340` 就是这么算给结算界面显示的)
    ///
    /// ☠ **快照必须留在 C# 这一侧**(为什么不交给 Python 自己轮询):
    ///   引擎主循环的**焦点闸门在 `st = self.state()` 之前**(`neko/engine.py:5374`)——
    ///   用户切出游戏去干别的时, 那一整局结束 Python **一次状态都读不到**, 结果就永久丢了。
    ///   而这里的读取跟着 0.1 秒档跑, **不受焦点影响** ⇒ in-round 期间每次读都刷新
    ///   `_frozen`, 一出局就把它当成 `lastResult` 报出去, 谁来读都给最后那份。
    ///
    /// 计时器的坑(两个计时器语义**相反**, 见 `ClientRoundTimer.cs:21` /
    ///   `ClientModifiableRoundTimer.cs:29-37`): 战役用的是 `ClientRoundTimer`,
    ///   `m_roundTimer` 是**已用**时间(往上加), 剩余 = `m_timeLimit - m_roundTimer`;
    ///   而 Modifiable 那个 `m_roundTimer` 是**剩余**时间(往下减)。按类型名分流。
    /// </summary>
    internal static class RoundScore
    {
        private static bool _wasInRound;
        private static int _seq;

        /// <summary>最近一次的分数块(JSON)。in-round 时它就是"实时", 出局后它就是"结算"。</summary>
        private static string _frozen = "null";

        /// <summary>C# 报的总分(给 Python 判"有没有变化"用, 不做判据)。</summary>
        private static int _lastScore = -1;

        /// <summary>读不出来的原因。空串 = 读到了。
        ///
        /// ☠ **为什么要留着它**: 这一整块原来是"读不到就静默给我 null" ——
        ///   结果实机上只看到"这一版 DLL 没上报分数", 分不清是**没加载新 DLL**
        ///   还是**读失败**。静默失败是最贵的一种: 得让用户重启一次游戏才知道猜错没有。
        ///   现在每种失败都留一个短标签, 外面一眼看得出卡在哪一环。
        /// </summary>
        private static string _why = "";

        /// <summary>in-round 期间调用(挂在 0.1 秒那一档): 刷新快照。</summary>
        public static void CaptureRound(string scene)
        {
            if (!_wasInRound)
            {
                _seq++;                     // 新的一局
                _wasInRound = true;
            }
            try
            {
                // 每一环失败都**写清是哪一环** —— 反射链断在哪, 看 `roundWhy` 就知道。
                var ifc = GameUtils.GetFlowController();
                if (ifc == null)
                {
                    _why = "no-flow-controller";
                    return;
                }
                // ☠☠ **不能只认客户端那一支**(2026-09-15 实机 `s_rapids_2_1`, **Party** 模式):
                //   `roundWhy = flow-not-kitchen:ServerCampaignFlowController` ——
                //   那一局 `GetFlowController()` 给的是**服务端**那支, 而这里原来只
                //   往客户端强转 ⇒ 分数全 null。(正是上面那句注释预言的情形。)
                //   反编译依据(规则 1):
                //     · `ClientKitchenFlowControllerBase : ClientFlowControllerBase`(客户端)
                //     · `ServerKitchenFlowControllerBase : ServerFlowControllerBase`(服务端)
                //       —— **两支平行的类, 没有共同基类也没有共同接口**
                //       ⇒ 强转**只能命中一边**, 想两边都认只能反射;
                //     · 但两边 `GetMonitorForTeam(TeamID)` 拿到的 monitor 的 **`.Score`
                //       是同一个类型**(`TeamMonitor.TeamScoreStats` ——
                //       `ClientTeamMonitor.cs:13` / `ServerTeamMonitor.cs:11` 都是
                //       `public TeamMonitor.TeamScoreStats Score => m_score;`)
                //       ⇒ 反射只用在**取 monitor**这一步, 分数本身照旧强转。
                //   ⚠ `RoundTimer` 同理(两边各是一个接口: `IClientRoundTimer` /
                //     `IServerRoundTimer`), 见 `RoundTimeLeft`。
                //   ⚠ 反射句柄**按类型缓存** —— 这是每 0.1 秒都跑的路径
                //     (同 `ItemKnowledge.ContentsMethod` 那条教训)。
                var monitor = MonitorOf(ifc, TeamID.One);            // 战役固定 TeamID.One
                if (monitor == null)
                {
                    _why = "no-monitor:" + ifc.GetType().Name;
                    return;
                }
                var sc = ScoreOf(monitor);
                if (sc == null)
                {
                    _why = "no-score:" + monitor.GetType().Name;
                    return;
                }

                int baseScore = sc.TotalBaseScore;
                int tips = sc.TotalTipsScore;
                int expired = sc.TotalTimeExpireDeductions;
                int delivered = sc.TotalSuccessfulDeliveries;
                int combo = sc.TotalCombo;
                bool comboOk = sc.ComboMaintained;
                int score = sc.GetTotalScore();

                // 失败单数 = 超时扣分 / 每次超时扣多少(游戏自己就是这么算的)
                int perFail = 10;                                    // GameConfig.RecipeTimeOutPointLoss
                try
                {
                    var cfg = ifc.GetGameConfig();
                    if (cfg != null && cfg.RecipeTimeOutPointLoss > 0)
                        perFail = cfg.RecipeTimeOutPointLoss;
                }
                catch (Exception) { }
                int failed = expired / perFail;

                // 星级: 照抄 ClientCampaignFlowController.GetStarRating()
                int stars = -1, nextStar = -1, oneStar = -1;
                try
                {
                    var gs = GameUtils.GetGameSession();
                    var entry = (gs == null) ? null : gs.LevelSettings.SceneDirectoryVarientEntry;
                    if (entry != null)
                    {
                        bool ngp = false;
                        try
                        {
                            var sd = gs.Progress.SaveData;
                            ngp = sd != null && sd.IsNGPEnabledForLevel(GameUtils.GetLevelID())
                                  && sd.NewGamePlusDialogShown;
                        }
                        catch (Exception) { }
                        stars = entry.GetStarForPoints(score, ngp);
                        nextStar = entry.GetPointsForStar(stars + 1);   // -1 = 没有下一颗
                        oneStar = entry.GetPointsForStar(1);            // 过关线
                    }
                }
                catch (Exception) { }

                float timeLeft = RoundTimeLeft(ifc);

                var sb = new StringBuilder();
                sb.Append("{\"seq\":").Append(_seq)
                  .Append(",\"scene\":\"").Append(Esc(scene)).Append("\"")
                  .Append(",\"score\":").Append(score)
                  .Append(",\"base\":").Append(baseScore)
                  .Append(",\"tips\":").Append(tips)
                  .Append(",\"expiredDeduction\":").Append(expired)
                  .Append(",\"delivered\":").Append(delivered)
                  .Append(",\"failed\":").Append(failed)
                  .Append(",\"combo\":").Append(combo)
                  .Append(",\"comboMaintained\":").Append(comboOk ? "true" : "false");
                AppendNum(sb, "stars", stars);
                AppendNum(sb, "nextStarPoints", nextStar);
                AppendNum(sb, "oneStarScore", oneStar);
                if (timeLeft >= 0f)
                    sb.Append(",\"timeLeft\":").Append(timeLeft.ToString("0.0",
                              System.Globalization.CultureInfo.InvariantCulture));
                else
                    sb.Append(",\"timeLeft\":null");
                // **胜负**: 游戏自己的判据是"星数 > 0"(见类注释)。读不到星级时给 null, 不编。
                sb.Append(",\"passed\":").Append(stars < 0 ? "null"
                          : (stars > 0 ? "true" : "false"));
                sb.Append("}");

                _frozen = sb.ToString();
                _lastScore = score;
                _why = "";
            }
            catch (Exception ex)
            {
                _why = "ex:" + ex.GetType().Name;
            }
        }

        /// <summary>出局时调用(挂在"离开对局"那个分支): 只翻标志位, `_frozen` **冻住不动**。</summary>
        public static void NoteOutOfRound()
        {
            _wasInRound = false;
        }

        /// <summary>拼进 state 的两块: in-round 时报 `round`(实时), 出局后报 `lastResult`(冻结)。
        ///
        /// 同一个 `_frozen` 换个名字报 —— 这样"实时"和"结算"永远是**同一份读数**,
        /// 不会出现"局中看到的分数"和"赛后报的分数"对不上。
        /// </summary>
        public static string Json(bool inRound)
        {
            string live = inRound ? _frozen : "null";
            string last = inRound ? "null" : _frozen;
            // `roundWhy` 只在**读不出来**时非空 —— 外面据此区分
            // "没加载新 DLL"(连 round 键都没有) 和 "加载了但读失败"(键在、值为 null、有原因)。
            return "\"round\":" + (live ?? "null") + ",\"lastResult\":" + (last ?? "null")
                 + ",\"roundWhy\":\"" + Esc(_why) + "\"";
        }

        /// <summary>诊断用: 当前这一局是第几局(从插件加载起算)。</summary>
        public static int Seq { get { return _seq; } }

        // ---------------------------------------------------------------- 内部
        private static void AppendNum(StringBuilder sb, string name, int v)
        {
            sb.Append(",\"").Append(name).Append("\":");
            if (v < 0)
                sb.Append("null");
            else
                sb.Append(v);
        }

        // ------------------------------------------------------- 两支控制器共用的反射
        //
        // ☠ 客户端/服务端是**两支平行的类**(见 `CaptureRound` 里那段), 没有共同基类,
        //   所以"取 monitor / 取计时器"这两步只能反射。**句柄按类型缓存**
        //   —— 这个类是每 0.1 秒跑一次的路径, 不缓存就是拿帧率换新鲜度
        //   (同 `ItemKnowledge.ContentsMethod` 那条教训; null 也缓存,
        //    "这个类没有那个成员"同样是固定事实)。

        private static readonly Dictionary<Type, MethodInfo> _monitorM =
            new Dictionary<Type, MethodInfo>();
        private static readonly Dictionary<Type, PropertyInfo> _scoreP =
            new Dictionary<Type, PropertyInfo>();
        private static readonly Dictionary<Type, PropertyInfo> _timerP =
            new Dictionary<Type, PropertyInfo>();
        private static readonly Dictionary<Type, PropertyInfo> _elapsedP =
            new Dictionary<Type, PropertyInfo>();

        /// <summary>`flow.GetMonitorForTeam(team)` —— 两支控制器都认。</summary>
        private static object MonitorOf(object flow, TeamID team)
        {
            try
            {
                var t = flow.GetType();
                MethodInfo m;
                if (!_monitorM.TryGetValue(t, out m))
                {
                    try { m = t.GetMethod("GetMonitorForTeam", new Type[] { typeof(TeamID) }); }
                    catch (Exception) { m = null; }
                    _monitorM[t] = m;
                }
                return (m == null) ? null : m.Invoke(flow, new object[] { team });
            }
            catch (Exception) { return null; }
        }

        /// <summary>monitor 的 `.Score` —— **两边是同一个类型**, 所以能直接强转。</summary>
        private static TeamMonitor.TeamScoreStats ScoreOf(object monitor)
        {
            try
            {
                var t = monitor.GetType();
                PropertyInfo p;
                if (!_scoreP.TryGetValue(t, out p))
                {
                    p = t.GetProperty("Score");
                    _scoreP[t] = p;
                }
                return (p == null) ? null : (p.GetValue(monitor, null) as TeamMonitor.TeamScoreStats);
            }
            catch (Exception) { return null; }
        }

        /// <summary>`flow.RoundTimer` —— 两支各是一个接口(`IClientRoundTimer`/`IServerRoundTimer`)。</summary>
        private static object TimerOf(object flow)
        {
            try
            {
                var t = flow.GetType();
                PropertyInfo p;
                if (!_timerP.TryGetValue(t, out p))
                {
                    p = t.GetProperty("RoundTimer");
                    _timerP[t] = p;
                }
                return (p == null) ? null : p.GetValue(flow, null);
            }
            catch (Exception) { return null; }
        }

        /// <summary>剩余秒数; 读不到给 -1。</summary>
        private static float RoundTimeLeft(object flow)
        {
            try
            {
                var timer = TimerOf(flow);
                if (timer == null)
                    return -1f;
                // `TimeElapsed` 在两边接口上都有, 但**接口类型不同** ⇒ 同样按名字反射。
                var tt = timer.GetType();
                PropertyInfo pe;
                if (!_elapsedP.TryGetValue(tt, out pe))
                {
                    pe = tt.GetProperty("TimeElapsed");
                    _elapsedP[tt] = pe;
                }
                if (pe == null)
                    return -1f;
                float elapsed = Convert.ToSingle(pe.GetValue(timer, null));
                // ⚠ 两个计时器语义相反 —— 见类注释。按类型名分流, 别假设。
                bool countsDown = tt.Name.IndexOf("Modifiable", StringComparison.Ordinal) >= 0;
                if (countsDown)
                    return elapsed;                          // 它的 TimeElapsed 就是剩余
                var f = tt.GetField("m_timeLimit", BindingFlags.Instance
                                                 | BindingFlags.NonPublic | BindingFlags.Public);
                if (f == null)
                    return -1f;
                object v = f.GetValue(timer);
                if (v == null)
                    return -1f;
                float limit = Convert.ToSingle(v);
                float left = limit - elapsed;
                return left < 0f ? 0f : left;
            }
            catch (Exception)
            {
                return -1f;
            }
        }

        private static string Esc(string s)
        {
            if (string.IsNullOrEmpty(s))
                return "";
            return s.Replace("\\", "\\\\").Replace("\"", "\\\"");
        }
    }
}
