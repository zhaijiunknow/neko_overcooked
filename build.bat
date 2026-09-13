@echo off
setlocal

set ROOT=%~dp0
:: ⚠ 这三个路径是本机的实际位置, 换机器要改。
::   · CSC  : 原来写的 sdk\8.0.419 本机没装(只有 9.0.307/9.0.311)。
::            Roslyn 版本不影响产物 —— build.bat 用 -langversion:7.3 + -nostdlib+
::            显式引 .NET 2.0 的 mscorlib, 所以编译器本身跑在 .NET 9 上没问题。
::   · GAME : 原来写的 E:\SteamLibrary 这个盘不存在, 游戏实际在 D:\Steam。
::   · BEP  : 原来指向仓库里的 tools\BepInEx_x86\ —— 那个目录已被删(只剩 zip)。
::            直接用游戏里装好的 BepInEx\core, 更省事也更不会版本错配。
set CSC="C:\Program Files\dotnet\sdk\9.0.311\Roslyn\bincore\csc.dll"
set FW=C:\Windows\Microsoft.NET\Framework\v2.0.50727
set GAME=D:\Steam\steamapps\common\Overcooked! 2\Overcooked2_Data\Managed
set BEP=D:\Steam\steamapps\common\Overcooked! 2\BepInEx\core
set SRC=%ROOT%Overcooked2AI
set OUT=%ROOT%build
set DLL=%OUT%\Overcooked2AI.dll

if not exist "%OUT%" mkdir "%OUT%"

dotnet %CSC% -nologo -target:library -langversion:7.3 -platform:x86 -nostdlib+ ^
  -out:"%DLL%" ^
  -r:"%FW%\mscorlib.dll" ^
  -r:"%FW%\System.dll" ^
  -r:"%FW%\System.Xml.dll" ^
  -r:"%GAME%\Assembly-CSharp.dll" ^
  -r:"%GAME%\Assembly-CSharp-firstpass.dll" ^
  -r:"%GAME%\UnityEngine.dll" ^
  -r:"%GAME%\UnityEngine.CoreModule.dll" ^
  -r:"%GAME%\UnityEngine.UI.dll" ^
  -r:"%GAME%\UnityEngine.UIModule.dll" ^
  -r:"%GAME%\UnityEngine.IMGUIModule.dll" ^
  -r:"%GAME%\UnityEngine.InputModule.dll" ^
  -r:"%GAME%\UnityEngine.PhysicsModule.dll" ^
  -r:xinput="%GAME%\XInputDotNetPure.dll" ^
  -r:"%BEP%\BepInEx.dll" ^
  -r:"%BEP%\0Harmony20.dll" ^
  -r:"%BEP%\Mono.Cecil.dll" ^
  "%SRC%\Game\Plugin.cs" ^
  "%SRC%\Game\BridgeServer.cs" ^
  "%SRC%\Game\StateCollector.cs" ^
  "%SRC%\Game\SceneScanner.cs" ^
  "%SRC%\Game\OrderCapture.cs" ^
  "%SRC%\Game\RecipeReader.cs" ^
  "%SRC%\Game\ItemKnowledge.cs" ^
  "%SRC%\Game\NavPath.cs" ^
  "%SRC%\Game\LevelInfo.cs" ^
  "%SRC%\Game\InteractiveScan.cs" ^
  "%SRC%\Game\ActionExecutor.cs" ^
  "%SRC%\Game\VirtualGamepad.cs" ^
  "%SRC%\Game\VirtualInput.cs" ^
  "%SRC%\Game\MapOverlay.cs" ^
  "%SRC%\Game\GridInfo.cs" ^
  "%SRC%\Game\CellMap.cs" ^
  "%SRC%\Game\InteractDirect.cs"

if errorlevel 1 (
  echo BUILD FAILED
  exit /b 1
)
echo BUILD OK: %DLL%
