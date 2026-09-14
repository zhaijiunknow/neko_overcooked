using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using BepInEx.Logging;

namespace Overcooked2AI.Game
{
    /// <summary>TCP 桥 server。行协议: 每行一个 JSON。
    /// 请求: {"cmd":"state"} | {"cmd":"action","chef":0,"kind":"move_to","target":"board0","duration":0}
    /// 响应: state → 状态快照; action → {"ok":true,"pending":N}
    /// 跑在后台线程; 动作入队由主线程 ActionExecutor 消费。</summary>
    public sealed class BridgeServer
    {
        private readonly StateCollector _collector;
        private readonly ActionExecutor _executor;
        private readonly ManualLogSource _log;
        private TcpListener _listener;
        private Thread _thread;
        private volatile bool _running;

        public BridgeServer(StateCollector collector, ActionExecutor executor, ManualLogSource log)
        {
            _collector = collector;
            _executor = executor;
            _log = log;
        }

        public void Start(int port)
        {
            if (_running)
                return;
            _running = true;
            _listener = new TcpListener(IPAddress.Loopback, port);
            _listener.Start();
            _log?.LogInfo(string.Format("[Overcooked2AI] listener started on {0}", port));
            _thread = new Thread(AcceptLoop) { IsBackground = true, Name = "oc2ai-bridge" };
            _thread.Start();
            _log?.LogInfo(string.Format("[Overcooked2AI] accept thread started (alive={0})", _thread.IsAlive));
        }

        public void Stop()
        {
            _running = false;
            try { _listener?.Stop(); } catch { }
        }

        private void AcceptLoop()
        {
            _log?.LogInfo("[Overcooked2AI] accept loop running");
            while (_running)
            {
                TcpClient client = null;
                try
                {
                    client = _listener.AcceptTcpClient();
                }
                catch (Exception ex)
                {
                    _log?.LogWarning(string.Format("[Overcooked2AI] accept error: {0}", ex.Message));
                    break;
                }
                _log?.LogInfo(string.Format("[Overcooked2AI] accepted client from {0}", client.Client.RemoteEndPoint));
                Thread t = new Thread(() => HandleClient(client)) { IsBackground = true };
                t.Start();
            }
        }

        private void HandleClient(TcpClient client)
        {
            try
            {
                _log?.LogInfo("[Overcooked2AI] Python 已连接");
                NetworkStream stream = client.GetStream();
                byte[] buf = new byte[8192];
                StringBuilder sb = new StringBuilder();
                while (_running)
                {
                    int n = stream.Read(buf, 0, buf.Length);
                    if (n <= 0)
                        break; // 对端关闭
                    sb.Append(Encoding.UTF8.GetString(buf, 0, n));
                    // 按行切分处理(可能一次收多行/半行)
                    int idx;
                    while ((idx = sb.ToString().IndexOf('\n')) >= 0)
                    {
                        string line = sb.ToString().Substring(0, idx).Trim();
                        sb.Remove(0, idx + 1);
                        if (line.Length == 0)
                            continue;
                        string resp = HandleLine(line);
                        byte[] outBytes = Encoding.UTF8.GetBytes(resp + "\n");
                        stream.Write(outBytes, 0, outBytes.Length);
                        stream.Flush();
                    }
                }
            }
            catch (Exception ex)
            {
                _log?.LogWarning(string.Format("[Overcooked2AI] 连接结束: {0}", ex.Message));
            }
            _log?.LogInfo("[Overcooked2AI] Python 断开");
        }

        private string HandleLine(string line)
        {
            if (line.Contains("\"state\""))
                return _collector.Snapshot();
            if (line.Contains("\"orders\""))
                return OrderCapture.Snapshot();
            // 以下三项要在主线程扫 Unity 对象, 走请求-等待
            if (line.Contains("\"live\""))
                return _collector.RequestJob("live", 6000);
            if (line.Contains("\"raw\""))
                return _collector.RequestJob("raw", 6000);
            if (line.Contains("\"know\""))
                return _collector.RequestJob("know", 6000);
            // 寻路: 直接问游戏自己的 GridNavSpace(边界/橱柜/墙壁全算障碍)
            if (line.Contains("\"path\""))
            {
                int chef = GetInt(line, "chef", 0);
                float ptx = GetFloat(line, "tx", 0f);
                float ptz = GetFloat(line, "tz", 0f);
                string arg = string.Format(System.Globalization.CultureInfo.InvariantCulture,
                    "{0},{1},{2}", chef, ptx, ptz);
                return _collector.RequestJob("path", 6000, arg);
            }
            // 关卡地图: 整张原生网格 + 危险区(水面/岩浆/边界) + 空洞 + 平台
            if (line.Contains("\"map\""))
                return _collector.RequestJob("map", 8000, GetStr(line, "arg", ""));
            // 机关/陷阱: 按钮 / 传送带方向 / 触发机器 / 平台 / 火 / 关卡变形
            if (line.Contains("\"dyn\""))
                return _collector.RequestJob("dyn", 8000);
            // 灭火器诊断: 喷雾的两个触发字符串(只在 prefab 里, 静态读不到) + 组件清单。
            // 见 InteractiveScan.SprayDiag() 的注释。
            //
            // ⚠ 命令名是 `sprayinfo` **不是** `spray` —— 这里全是**子串匹配**,
            //   而直调动作用的是 `{"cmd":"direct","action":"spray"}`, 里面就含 `"spray"`,
            //   叫 `spray` 的话会把直调那条抢先拦下来(实测: spray 返回诊断 JSON、
            //   而 unspray 正常 —— 因为 "unspray" 里没有带引号的 "spray" 子串)。
            if (line.Contains("\"sprayinfo\""))
                return _collector.RequestJob("spray", 6000);
            // 会动的东西: 路人 / 车辆 / 移动危险物 —— 地形快照看不见的那一层。
            // 见 MoverScan 的注释。
            if (line.Contains("\"movers\""))
                return _collector.RequestJob("movers", 6000);
            // 游戏自己的网格: 格子↔世界坐标换算参数(m_origin/m_size/transform) + 占位表。
            // 依据 GridManager.cs:9,64-92 与 QuadGridManager.cs:28-38 —— 这是"解析地图"的权威依据,
            // 不是我们自己采样推断的那套。
            if (line.Contains("\"grid\""))
                return _collector.RequestJob("grid", 6000);
            // **对齐游戏格心的完整格子图**: kind(free/solid/hazard/tight/carry/void) + 占位表
            // + 台面传送带标记 + 交叉验证。这是地图解析的地基(CellMap.cs)。
            if (line.Contains("\"cells\""))
                return _collector.RequestJob("cells", 9000);
            if (line.Contains("\"pad\""))
                return HandlePad(line);
            // **直接调游戏自己的交互入口**(绕开输入层与客户端消息链):
            //   {"cmd":"direct","player":0,"action":"pickup"}
            // 依据 ServerPlayerControlsImpl_Default.ReceivePickUpEvent (:152-163, 无门)
            if (line.Contains("\"direct\""))
            {
                int pl = GetInt(line, "player", 0);
                string act = GetStr(line, "action", "pickup");
                return _collector.RequestJob("direct", 6000, pl + ":" + act);
            }
            // 内存级地图小地图(实时绘制): on/off/path
            if (line.Contains("\"overlay\""))
            {
                string act = GetStr(line, "action", "status");
                if (act == "on")
                    return MapOverlay.SetEnabled(true);
                if (act == "off")
                    return MapOverlay.SetEnabled(false);
                if (act == "toggle")
                {
                    MapOverlay.Toggle();
                    return MapOverlay.SetEnabled(MapOverlay.Enabled);
                }
                if (act == "path")
                    return MapOverlay.PushPath(GetStr(line, "pts", ""));
                return "{\"ok\":true,\"overlay\":" + (MapOverlay.Enabled ? "true" : "false") + "}";
            }
            if (line.Contains("\"action\""))
            {
                var msg = ParseAction(line);
                bool ok = msg != null && _executor.Enqueue(msg);
                return "{\"ok\":" + (ok ? "true" : "false") + ",\"pending\":" + _executor.PendingCount + "}";
            }
            if (line.Contains("\"ping\""))
                return "{\"pong\":true}";
            return "{\"error\":\"unknown cmd\"}";
        }

        /// <summary>虚拟手柄。
        ///
        /// 新机制(推荐): 带 action 字段 —— 直接替换 {chef} 的 PlayerControls.ControlSchemeData
        ///   {"cmd":"pad","action":"install","chef":0}        需要主线程(走 job)
        ///   {"cmd":"pad","action":"uninstall","chef":0}      需要主线程(走 job)
        ///   {"cmd":"pad","action":"drive","player":0,"x":0,"y":1,"pickup":0,"use":0,"dash":0}
        ///                                                    **不需要主线程** —— 只写我们自己的
        ///                                                    对象字段, 桥线程直调, 延迟最低
        ///   {"cmd":"pad","action":"release","player":0}
        ///   {"cmd":"pad","action":"status"}
        ///
        /// 旧机制(保留兼容): 不带 action —— 喂 VirtualGamepads(虚拟 InControl 设备)。
        ///   {"cmd":"pad","pad":0,"connected":1,...}</summary>
        private string HandlePad(string line)
        {
            string action = GetStr(line, "action", "");
            if (action.Length > 0)
                return HandlePadAction(line, action);

            int idx = GetInt(line, "pad", -1);
            if (idx < 0 || idx > 1)
                return "{\"ok\":false,\"error\":\"pad index\"}";
            var pad = VirtualGamepads.Pads[idx];
            pad.Connected = GetInt(line, "connected", 0) != 0;
            pad.A = GetInt(line, "A", 0) != 0;
            pad.B = GetInt(line, "B", 0) != 0;
            pad.X = GetInt(line, "X", 0) != 0;
            pad.Y = GetInt(line, "Y", 0) != 0;
            pad.LB = GetInt(line, "lb", 0) != 0;
            pad.RB = GetInt(line, "rb", 0) != 0;
            pad.Start = GetInt(line, "start", 0) != 0;
            pad.Back = GetInt(line, "back", 0) != 0;
            pad.DUp = GetInt(line, "du", 0) != 0;
            pad.DDown = GetInt(line, "dd", 0) != 0;
            pad.DLeft = GetInt(line, "dl", 0) != 0;
            pad.DRight = GetInt(line, "dr", 0) != 0;
            pad.LX = GetFloat(line, "lx", 0f);
            pad.LY = GetFloat(line, "ly", 0f);
            pad.RX = GetFloat(line, "rx", 0f);
            pad.RY = GetFloat(line, "ry", 0f);
            pad.LT = GetFloat(line, "lt", 0f);
            pad.RT = GetFloat(line, "rt", 0f);
            return "{\"ok\":true}";
        }

        /// <summary>虚拟手柄(新机制): install/uninstall 走主线程 job, drive/release/status 桥线程直调。
        /// 为什么 drive 能直调: 它只写 VirtualValue/VirtualButton 自己的 float/bool 字段,
        /// 不碰任何 Unity API —— 这样每帧喂值的延迟就是一次 TCP 往返, 而不是排队等主线程。</summary>
        private string HandlePadAction(string line, string action)
        {
            if (action == "status")
                return VirtualInput.Status();
            if (action == "install")
            {
                int chef = GetInt(line, "chef", 0);
                return _collector.RequestJob("pad", 6000, "install:" + chef);
            }
            if (action == "installplayer")
            {
                // 按玩家身份安装(0=Player.One) —— 双人首选, 不依赖对象枚举顺序
                int pl = GetInt(line, "player", 0);
                return _collector.RequestJob("pad", 6000, "installp:" + pl);
            }
            if (action == "installall")
                return _collector.RequestJob("pad", 8000, "installall");
            if (action == "uninstall")
            {
                int chef = GetInt(line, "chef", 0);
                return _collector.RequestJob("pad", 6000, "uninstall:" + chef);
            }
            int player = GetInt(line, "player", 0);
            if (action == "release")
            {
                VirtualInput.Release(player);
                return "{\"ok\":true}";
            }
            if (action == "releaseall")
            {
                VirtualInput.ReleaseAll();
                return "{\"ok\":true}";
            }
            if (action == "drive")
            {
                VirtualInput.Drive(player,
                    GetFloat(line, "x", 0f), GetFloat(line, "y", 0f),
                    GetInt(line, "pickup", 0) != 0, GetInt(line, "use", 0) != 0,
                    GetInt(line, "dash", 0) != 0, GetInt(line, "curse", 0) != 0);
                return "{\"ok\":true}";
            }
            return "{\"ok\":false,\"error\":\"unknown pad action: " + action + "\"}";
        }

        private ActionExecutor.ActionMsg ParseAction(string line)
        {
            // 极简 JSON 字段抽取(不引第三方): chef/kind/target/duration
            var msg = new ActionExecutor.ActionMsg();
            msg.Chef = GetInt(line, "chef", 0);
            msg.Kind = GetStr(line, "kind", "");
            msg.Target = GetStr(line, "target", "");
            msg.Duration = GetFloat(line, "duration", 0f);
            return msg.Kind == "" ? null : msg;
        }

        private static int GetInt(string s, string key, int dflt)
        {
            var v = GetStr(s, key, "");
            return int.TryParse(v, out var n) ? n : dflt;
        }

        private static float GetFloat(string s, string key, float dflt)
        {
            var v = GetStr(s, key, "");
            return float.TryParse(v, System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out var f) ? f : dflt;
        }

        private static string GetStr(string s, string key, string dflt)
        {
            int i = s.IndexOf("\"" + key + "\"", StringComparison.Ordinal);
            if (i < 0)
                return dflt;
            i = s.IndexOf(':', i);
            if (i < 0)
                return dflt;
            int j = i + 1;
            while (j < s.Length && (s[j] == ' ' || s[j] == '\t'))
                j++;
            if (j < s.Length && s[j] == '"')
            {
                int end = s.IndexOf('"', j + 1);
                return end < 0 ? dflt : s.Substring(j + 1, end - j - 1);
            }
            int k = j;
            while (k < s.Length && s[k] != ',' && s[k] != '}' && s[k] != ' ')
                k++;
            return s.Substring(j, k - j);
        }
    }
}
