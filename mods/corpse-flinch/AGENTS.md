# AGENTS.md · 给 AI 代理的接手契约

你在一个**已发布的游戏运行时补丁**仓库里工作。这里的目标不是"能跑就行"，而是"别人解压、安装、就能用，且改坏了能被发现"。这份文件是契约，不是建议。

## 0. 这是什么

`corpse-flinch` 让 SF2 离线客户端的尸体对命中产生布娃娃物理反应。它不改游戏文件，复用客户端自带的桥（`runtime\tools\local_battle_bridge.py`）在进程内读数据、调用 `Engine.PrimitiveComponent.AddImpulse`。

- 目标平台：Windows x64，客户端自带便携 Python
- 交付形态：一个 zip，内含 `mods\corpse-flinch\...`（解压到游戏根目录 → 双击 `mods\corpse-flinch\安装.cmd` → 双击游戏根目录的启动器）
- 用户文档：[`README.md`](README.md)、[`docs/00-总目录.md`](docs/00-总目录.md)、[`docs/05-用户手册.md`](docs/05-用户手册.md)

## 1. 必读顺序（别跳）

1. [`docs/01-原理.md`](docs/01-原理.md) —— 数据从哪来，以及**哪三条路已经被证伪**
2. [`docs/02-数据结构与偏移.md`](docs/02-数据结构与偏移.md) —— 偏移、结构布局、标定数字
3. [`docs/03-AI改造指南.md`](docs/03-AI改造指南.md) §1 —— 八条硬性约定
4. [`docs/06-日志字段.md`](docs/06-日志字段.md) —— 判断"到底生效没有"的唯一可靠手段
5. 本文件 §3（不变量）、§5（陷阱）、§6（定义完成）

## 2. 仓库结构

```
corpse_flinch.py      CLI + Worker：主循环、触发、日志、异常兜底
config.json           全部可调参数（用户唯一需要改的文件）
sf2mod/attach.py      找游戏目录 / 找进程 / 装桥 / connect
sf2mod/game.py        偏移常量、Pawns、Player、camera、骨骼世界坐标
sf2mod/signals.py     读武器命中记录（枪的判据）
sf2mod/swing.py       读挥击状态位 / BurstFireCount（近战判据）
sf2mod/projectiles.py 跟踪投射物折线（投射物判据）
sf2mod/punch.py       AddImpulse 打包 + settle 测量
sf2mod/drag.py        E 键拖拽伺服（60 Hz PD）
sf2mod/keys.py        交互键状态位 / GetAsyncKeyState 回退
tools/install.py      安装（复制、写启动器、跑自检）
tools/launch.py       启动器逻辑（起游戏 + 起 worker）
tools/uninstall.py    卸载
tools/selftest.py     只读自检（偏移/函数/桥）
tools/unit_test.py    离线单测（不需要游戏）
tools/build_manifest.py  清单 / zip / --check
docs/00..07           文档
```

规则：**`sf2mod/` 只放机制，业务策略放 `Worker`**。新增效果 ≈ 新增模块 + 在 `run()` 里加一次 `try: self.<step>()`。

## 3. 不变量（违反即不可发布）

1. **不改游戏文件、不写注册表、不装服务**。唯一写入是桥的 9 字节跳转 + 命令块，且复用客户端自带的桥实现。
2. **只对 `Health <= 0` 的 pawn 施加冲量**。活人、AI、伤害数值一律不碰。
3. **判据必须来自游戏自己**（命中记录 / 状态位 / 投射物折线 / 按键状态位）。禁止引入"射线 + 距离"这类近似：它会同时产生假阳性与假阴性，且无法通过调参修好（见 `docs/01` §7）。
4. **每个探测器独立 `try`**。一个探测器抛 `OSError` 不能让其他探测器停摆（1.2.0 的教训：`try: self.melee(); self.arrows()` 让投掷武器完全失效）。
5. **兜底但不吞证据**：非 `OSError` 的异常记 `worker-error`（含 `where` 与 `error`）后继续；禁止裸 `except: pass`。
6. **日志单行 JSON**，字段小写、坐标/距离 1 位小数、地址 `"0x.."`。新增事件必须写进 [`docs/06-日志字段.md`](docs/06-日志字段.md)。
7. **`.cmd` 纯 ASCII**。cmd.exe 用 OEM 代码页解析批处理，中文会变成"不是内部或外部命令"。中文提示放 Python，路径用 `%~dp0`。
8. **Python 源文件用 UTF-8（无 BOM 也可）但要能被 `-X utf8` 读**；不要改动 `config.json` 的键名而不改文档与 `DEFAULTS`（三处必须一致）。
9. **改任何文件后重新生成清单**：`runtime\python\python.exe -X utf8 -B tools\build_manifest.py --zip corpse-flinch-<ver>.zip`。否则别人 `--check` 会报"被改"。
10. **不引入第三方依赖**。只用标准库 + 客户端自带 Python（3.x，`ctypes`/`struct`/`json` 可用）。

## 4. 环境事实

| 事实 | 值 / 做法 |
| --- | --- |
| 游戏目录 | 必须含 `runtime\tools\local_battle_bridge.py` 与 `runtime\python\python.exe` |
| 找游戏目录 | `install.py` 依次尝试 `--game` → 从自身往上找 → tkinter 弹窗；**运行时脚本不会猜目录**，独立脚本请设 `$env:SF2_GAME_DIR` 或显式传路径 |
| 桥 | `Bridge.install(main_thread(pid))`；`invoke(obj, fn, frame)` 需要游戏主线程推进（最小化会超时） |
| 桥的批量 | `Bridge` **没有** `invoke_many`（那是 `RouteBridge` 的方法）。需要批量时逐次 `invoke` |
| 地址读取 | `bridge.read(addr, n)` 一次最多拿 12~16 字节；解析结构时按 4/8/12 分批读 |
| worker 进程 | 文件是 `corpse_flinch.py`，解释器是客户端自带的 `pythonw.exe`（**和游戏启动器同名**） |
| 日志 | `mods\corpse-flinch\logs\corpse-flinch-YYYYMMDD.jsonl`；`ready` 行的 `pid` 是**游戏** PID，不是 worker PID |
| 版本确认 | 只认 `ready.version`；不要用文件时间或日志里的 `pid` 推断 worker 是否重启 |

## 5. 陷阱清单（都踩过）

1. **别按名字杀 `pythonw.exe`**：客户端的 `portable_runtime.py` 也是 `pythonw.exe`，杀掉它等于关游戏。按命令行精确匹配：
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
     Where-Object CommandLine -like '*corpse_flinch.py*' |
     ForEach-Object { Stop-Process -Id $_.ProcessId }
   ```
2. **`Get-Content` 读 UTF-8 无 BOM 文件在旧 PowerShell 里会显示乱码**，别据此判断文件损坏（用 `-Encoding utf8` 或 Python 读）。
3. **投掷物是穿透的**：打中尸体不停、不写 `VictimActor`、一直飞到地图外才销毁。任何"碰到就命中"的假设都会失败（`docs/01` §8.5）。
4. **actor 地址会被复用**：类名缓存必须以"新地址"为失效条件，否则投射物会"隐形"（1.8.1）。
5. **几何近似会给出假阳性**：别再用"射线离骨骼 X cm"。
6. **尸体骨骼读取很贵**：70 根骨 = 70 次跨进程调用。必须缓存（`BONE_TTL = 0.25`）。
7. **拖拽是伺服，不是约束**：目标点必须是三维（相机眼位 + forward × 距离），控制器频率不要超过 90 Hz，重力补偿要加在限速之后。
8. **`--dry` 必须只读**：dry 模式下不得调用 `AddImpulse`。
9. **换图要清状态**：`run()` 里 world 变化时清 `last` / 缓存 / 拖拽 / 投射物，否则会拿上一局的地址去推。
10. **`ready` 与 `DEFAULTS` 不一致会误导用户**：新增配置项必须同时进 `DEFAULTS`、`config.json`、文档参数表。
11. **一个游戏进程只能装一次桥**：`local_battle_bridge.install()` 第一行校验入口字节必须是**原始的**（`raise RuntimeError('Expected original Actor ProcessEvent entry')`），且桥永不还原。所以**不要在 worker 已挂上（或本局曾挂上）的进程上跑 `selftest.py`**——它会假失败。验证顺序：先停 worker、重开客户端，再跑 selftest；或只在 worker 的日志里看 `attached`/`ready`。
12. **worker 必须自己报告游戏退出**（1.10.3 已修，改这块时别退回旧行为）：游戏关掉后桥不会报错，它照样从死进程的内存里读数，`status` 只是 `loop_ms` 掉到 `0.0`；旧代码要 1 小时无武器才 `idle-exit`，于是留下一个空转 worker 占着 `worker.pid`，**无法挂到下一个游戏进程**。现在 `Worker.run()` 每秒 `attach.pid_alive(self.pid)`，游戏没了就写 `{"event":"game-exit","pid":N}` 并退出。
13. **双击这条路全是 `pythonw`：不能用 `is None` 判断 stdout**。`启动-鞭尸模式.cmd` → `pythonw launch.py` → 再 spawn `pythonw corpse_flinch.py`，第二层会继承**已失效的 std 句柄**：`sys.stdout` 能构造，但每次写都抛 `OSError: [Errno 22] Invalid argument`。1.10.1 之前 `ensure_streams()` 只在 `is None` 时替换流，于是 `make_logger.emit` 第一行 `print` 就把 worker 打死（退出码 120，pythonw 下完全静默）。现在用 `stream_ok()`（`write('')` + `flush()` 实测）兜底，并且 `launch.py` 给 worker 显式传 `stdout=DEVNULL, stderr=logs\worker-console.log`。**改启动链必须用 `Start-Process <启动器>.cmd` 复验**：`cmd /c` 有控制台，测不出这个 bug。
14. **`logs\worker.pid` 才是 worker 的身份**：`ready.pid` 是**游戏** PID。判重、停止、卸载前先读 pidfile（`alive()` = `OpenProcess(0x1000)` + `GetExitCodeProcess` == 259）；进程名/命令行匹配只当兜底。
15. **`docs\` 会被 Obsidian 当 vault 锁住**：卸载或覆盖安装前，`mods\corpse-flinch\docs` 常被 `Obsidian.exe "<...>\docs\00-总目录.md"` 打开，`Rename-Item`/`Remove-Item` 报 `The process cannot access the file ... because it is being used by another process`；`remove_tree` 的 20×0.2 s 重试挡不住这种长期占用，只能提示用户关掉 Obsidian（`uninstall.py` 会问"关掉后按回车重试，输入 n 放弃"，最多 3 轮）。
16. **不要用 `Get-Content -Raw` + `Set-Content` 改源码**：PowerShell 5.1 按 ANSI 读 UTF-8，中文字符串会变 mojibake，装到真机后 worker 报 `SyntaxError: unterminated string literal`。改文件用 `[System.IO.File]::ReadAllText/WriteAllText` + `UTF8Encoding($false)`，或直接编辑文本。

17. **启动器自己开游戏时，attach 一定撞上"还没加载完"**：`Reader.objects()` 读固定地址的 UObject 数组（`inspect_battle_runtime.py:94` 的 `if not 1000 < count <= capacity < 1000000`），刚创建进程时是空数组 → `ValueError: Unexpected object array`；随后 `local_battle_bridge.py:117` 又要求 `len(controllers) == 1` → `RuntimeError: Battle API incomplete: N functions, 0 controllers`。**这两种都是"再等等就好了"，不是致命错误**：1.10.2 起由 `connect_when_booted()` 兜住（`BOOT_GRACE = 3600.0`，2→5 s 退避，`waiting-boot` 同类错误每分钟一行、成功时 `booted`）。判"装完没反应"时先看 jsonl 有没有 `fatal`；只有 `waiting-boot` 说明 worker 活着在等。
18. **启动器里"游戏"必须先于"worker"**：1.10.2 的 `launch.py` 先 `if running_worker(): return 0`、后 `start_game()` —— 一个陈旧 worker 就能让每次双击都静默返回（`launcher.log` 只有 `worker 已经在跑，不重复启动`，没有 `启动游戏`）。现在是"确保游戏进程存在（必要时 `start_game()` 并等 15 s）→ 再处理 worker → 按 `worker_game_pid()` 与当前游戏 pid 比对、不一致就 `TerminateProcess` 重挂"。顺序再调之前先读 `docs/07` 的 1.10.3。


19. **崩溃残留的进程只剩一个 `Message` 框**：游戏崩了以后 `sf2.exe` 还活着，但主窗口已销毁（崩溃报告 `GameViewport == NULL!!! All Windows Closed`），屏幕上只剩引擎的 `Fatal error!` 框（窗口标题就是 `Message`）。`client_pid()` 照样找得到它，于是启动器只写 `游戏已经在运行` 就退出——用户看到的就是"双击又没反应"。1.10.4 起 `launch.py` 用 `windows_of(pid)`（`EnumWindows` + `GetWindowThreadProcessId` + `IsWindowVisible` + `GetWindowTextW`）判定"所有可见窗口标题都是 `Message`"＝崩溃残留，`TerminateProcess` 清掉再重开；**进程在跑但一个窗口都没有时不要杀**（那可能只是还在加载）。真机崩溃报告在 `<游戏>\runtime\test-client\SFGame\Logs\UE3AutoReportDump*.txt` + `unreal-*.dmp`：`D3D9Device.cpp:893 D3DERR_INVALIDCALL`、`GameViewport == NULL`、`Ran out of virtual memory`（32 位 4 GB）都是客户端自己的问题，别往补丁上归因。


## 6. 验证闭环（改完必跑）

```powershell
# 1) 语法
Get-ChildItem -Recurse -Filter *.py | ForEach-Object { python -m py_compile $_.FullName }

# 2) 离线单测（不需要游戏）
runtime\python\python.exe -X utf8 -B tools\unit_test.py
#    期望末行：all good: servo and key detection behave here.

# 3) 版本号
runtime\python\python.exe -X utf8 -B corpse_flinch.py --version
#    期望：corpse-flinch 1.10.4

# 4) 只读自检（仅限"本次启动尚未挂过 worker"的干净客户端，见 §5.11）
runtime\python\python.exe -X utf8 -B tools\selftest.py --game "E:\SF2-Offline-20261016"

# 5) 重新生成清单与发布包
runtime\python\python.exe -X utf8 -B tools\build_manifest.py --zip corpse-flinch-1.10.4.zip
runtime\python\python.exe -X utf8 -B tools\build_manifest.py --check
```

真机冒烟（必须做，不能只看单测）：

1. 安装到一份真实客户端（`install.py --game <游戏> --no-selftest`），**用 `Start-Process <游戏>\启动-鞭尸模式.cmd` 走双击路径**启动 —— `cmd /c` 有控制台，测不出 §5.13 的 bug。确认 `logs\launcher.log` 里有 `worker 已启动 pid=xxxxx`，并且 9 秒后该 pid 仍在
2. 进一局，确认日志依次出现 `attached` → `ready`（带 `version`）→ `world`（`corpses > 0`）
3. 依次验证：开枪打尸体（`corpse-hit` + `settled.moved > 1`）、打旁边地面（**没有** `corpse-hit`）、投掷（`projectile-hit`）、按 E（`drag-start` → `drag-end`）
4. 关掉游戏，确认 worker 自己退出：jsonl 末尾有 `{"event": "game-exit"}`，`logs\worker.pid` 已删除

## 7. 定义完成（DoD）

- [ ] `unit_test.py` 全绿；`--version` 正确
- [ ] 双击路径复验：`Start-Process <游戏>\启动-鞭尸模式.cmd` → `logs\launcher.log` 有 `worker 已启动 pid=`，9 秒后仍活（§5.13）
- [ ] 真机冒烟四项通过，日志有证据
- [ ] 新增/修改的配置项已进 `DEFAULTS` + `config.json` + `docs/05`
- [ ] 新增日志事件已进 `docs/06`
- [ ] `docs/07` 加一条（写清"改了什么、为什么"）
- [ ] `build_manifest.py --zip` 生成了新包，`--check` 通过
- [ ] 发布包内**没有**临时脚本、`logs/`、`__pycache__`、`.zip`
- [ ] 文档里出现的偏移、路径、函数名、版本号与代码一致

## 8. 卡住时

- 先跑 `tools\selftest.py`：它逐项报"函数/属性/结构"是否还对得上，比人肉猜快
- 判定类问题看日志（`docs/06`），几何类问题看 `docs/02` 的重测方法
- 不要在没有日志证据的情况下宣布"已修好"；这个项目里被证伪的假设比成立的方案多
