# corpse-flinch · 鞭尸补丁

> 原版：正直的普鲁士军官（bilibili）｜ 1.11.0–1.11.2 的修改：DeepSeek ｜ 原版未附许可
> 二次分发前请先确认原作者的许可，见仓库根目录 README 的「来源与许可」。

让尸体对子弹、飞斧、近战**真的产生物理反应**：方向、力度、着力点都来自游戏自己算出的那一枪，而不是"射线离骨骼够近就推一下"。

- 版本 **1.11.2** ｜ 适用：SF2 离线客户端（自带 `runtime\tools\local_battle_bridge.py` 的版本）
- 不修改任何游戏文件 ｜ 不写注册表 ｜ 不需要管理员 ｜ 不需要另装 Python
- 两个动作：**鞭尸**（打到尸体就动）与 **拖尸**（按 E 抓住，再按 E 放开，GMod 式）

| 你做了什么 | 你会看到 |
| --- | --- |
| 子弹打到尸体 | 中弹处受力，尸体晃一下/往后滑一小段；爆头、擦头、打手脚都有效 |
| 子弹打到尸体旁边的墙、地面、箱子 | **什么都不发生**（不会再把旁边的尸体带倒） |
| 飞斧 / 投掷刀 / 手雷命中 | 命中当帧推一把，方向沿投掷方向 |
| 弓 / 弩 | 走同一套投射物判据，**但没在真机验证过**（日志里没出现过箭矢类 actor），可能完全没反应 |
| F 挥击（近战武器） | 推开准星前最近的尸体 |
| 投掷武器的**左键近战** | 同上（这个动作没有专属状态位，靠弹药是否减少区分） |
| 3 米内对准尸体按 **E** | 抓住，尸体吊在准星前 200 cm；抬头它跟着抬；再按 E 放开 |

## 30 秒上手

1. 把 `corpse-flinch-1.11.2.zip` **解压到游戏根目录**（有 `sf2.exe` 和 `运行游戏.cmd` 的那一层），会得到 `<游戏>\mods\corpse-flinch\`
2. 双击 `<游戏>\mods\corpse-flinch\安装.cmd`
3. 装完它会问"现在就把鞭尸挂上吗"，**直接回车**（等于启动游戏并挂上 worker；不想现在启动就输 `n`）
4. 没现在启动的话，双击游戏根目录新出现的 **`启动-鞭尸模式.cmd`**
5. 进一局，对着尸体开枪

只想看判定、不想让尸体动：`runtime\python\python.exe -X utf8 -B mods\corpse-flinch\corpse_flinch.py --dry`

详细步骤、参数、排错见 **[`docs\00-总目录.md`](docs/00-总目录.md)**。

## 支持的武器

| 类别 | 判据来源 | 说明 |
| --- | --- | --- |
| 枪械（含霰弹） | 武器自己的命中记录 `RifleHitResult` | 命中点/法线/方向都是游戏算的，穿透的出口点会被跳过 |
| 弓 / 弩 | 投射物飞行折线（与飞斧同一套） | **未验证**：真实日志里从未出现箭矢类 actor，可能完全没反应 |
| 飞斧 / 投掷刀 / 手雷 | 同上 | 投掷物是穿透的，不会因为"碰到"就停下 |
| 近战（F 键） | `SFPawn.DoingExigencyAttack` 状态位 | 挥击起手就判定 |
| 投掷武器左键近战 | `SFWeapon.BurstFireCount` 上升沿 + 弹药是否减少 | 0.45 s 去重 |
| 拖拽（E 键） | `SFPlayerController.bPressingUsedKey` | 改键跟着走，聊天框里不误触 |

命中对象必须是**已经死亡**的 pawn（`Health <= 0`）；活人、AI 行为、伤害数值都不受影响。

## 常用参数

改 `<游戏>\mods\corpse-flinch\config.json`，重启补丁生效。完整说明见 [`docs\05-用户手册.md`](docs/05-用户手册.md) §6。

| 键 | 默认 | 作用 |
| --- | --- | --- |
| `strength` | `700.0` | 子弹冲量基准 |
| `melee_strength` | `700.0` | 近战冲量 |
| `projectile_strength` | `1080.0` | 投掷物冲量 |
| `projectile_radius` | `70.0` | 投掷物"命中"的判定半径（cm） |
| `drag_key` | `69` | 拖拽键的虚拟键码（69 = E） |
| `drag_distance` | `200.0` | 尸体被吊在准星前多远（cm） |
| `drag_kp` / `drag_kd` | `20.0` / `9.0` | PD 伺服刚度 / 阻尼 |
| `drag_max_speed` | `700.0` | 拖拽速度上限（cm/s） |
| `projectile` / `melee` / `drag` | `true` | 三个功能各自的开关 |

## 环境要求

- Windows 10/11 x64，SF2 离线客户端，单人对局
- 客户端自带 `runtime\python\python.exe` 与 `runtime\tools\local_battle_bridge.py`（本补丁复用它的桥，不额外打洞）
- 游戏必须在前台渲染才能挂上（最小化时桥的命令无法完成）

## 文档地图

| 文档 | 给谁 | 内容 |
| --- | --- | --- |
| [`docs\00-总目录.md`](docs/00-总目录.md) | 所有人 | 按角色给出的阅读路线 + 全部文档索引 |
| [`docs\05-用户手册.md`](docs/05-用户手册.md) | 玩家 | 安装、启动、玩法、参数、日志、调参、FAQ、限制 |
| [`docs\06-日志字段.md`](docs/06-日志字段.md) | 玩家 / 开发者 | 每一条日志事件的字段与含义，可用它自证"到底判没判中" |
| [`docs\01-原理.md`](docs/01-原理.md) | 逆向 / 开发者 | 游戏怎么算这一枪、我们为什么不用射线、被否掉的方案 |
| [`docs\02-数据结构与偏移.md`](docs/02-数据结构与偏移.md) | 开发者 | 偏移表、结构布局、冲量标定、换版本后怎么重测 |
| [`docs\03-AI改造指南.md`](docs/03-AI改造指南.md) | AI / 开发者 | 硬性约定、模块地图、常见改造、陷阱、交付自查 |
| [`docs\04-排错.md`](docs/04-排错.md) | 所有人 | 症状 → 原因 → 处理 |
| [`docs\07-版本历史.md`](docs/07-版本历史.md) | 所有人 | 每个版本改了什么、为什么改 |
| [`AGENTS.md`](AGENTS.md) | AI 代理 | 接手本项目的契约：必读顺序、不变量、验证命令、定义完成 |

## 卸载

双击 `<游戏>\mods\corpse-flinch\卸载.cmd`（会先停掉 worker），或手动删除 `<游戏>\mods\corpse-flinch\` 与 `<游戏>\启动-鞭尸模式.cmd`。删不掉时先关 Obsidian/编辑器——它们常把 `docs\` 占着，关掉后在卸载窗口按回车即可重试。没有任何注册表/系统目录残留。

## 安全与边界

- **不修改游戏文件**：全部行为在运行时内存里，退出即消失；唯一写入是桥本来提供的 9 字节跳转 + 命令块。
- 只对 `Health <= 0` 的 pawn 施加冲量；只对**本地玩家自己**打出的命中生效。
- 投掷物必须在销毁点离尸体骨骼 `projectile_radius` 以内；还在手里的采样不算；起飞后 0.15 s 内只认正前方。
- 拖拽超过 `drag_break`（600 cm）且持续 0.3 s 自动松手，不会隔墙硬拽。
- 单人/离线使用。联机服务器上使用可能违反服务器规则，与本补丁无关。
- 游戏更新导致偏移变化时，现象是"打尸体只出现 `shot-miss`、从不出现 `corpse-hit`"或挂接直接失败——按 [`docs\02`](docs/02-数据结构与偏移.md) §7 重测。

## 文件地图

```
corpse-flinch\
  corpse_flinch.py          CLI + Worker.run()：轮询、触发、日志、兜底
  config.json               全部可调参数（唯一需要改的文件）
  安装.cmd / 卸载.cmd        纯 ASCII 薄壳，逻辑在 tools\install.py / uninstall.py
  README.md                 本文件
  AGENTS.md                 给 AI 代理的接手契约
  MANIFEST.sha256           每个文件的 sha256（--check 核对）
  sf2mod\attach.py          找游戏目录、找进程、装桥、连接
  sf2mod\game.py            偏移表、pawn/尸体/玩家/武器读取
  sf2mod\signals.py         读武器自己的命中记录（枪的判据）
  sf2mod\swing.py           读挥击状态位 / BurstFireCount（近战判据）
  sf2mod\projectiles.py     跟踪箭/投掷刀的飞行折线（投射物判据）
  sf2mod\punch.py           AddImpulse 打包与位移测量
  sf2mod\drag.py            E 键拖拽：60 Hz 临界阻尼 PD 伺服
  sf2mod\keys.py            读交互键状态位 / 退回 GetAsyncKeyState
  tools\install.py          复制文件、生成启动器、跑自检
  tools\launch.py           启游戏 + 后台挂 worker
  tools\uninstall.py        删文件、删启动器
  tools\selftest.py         只读自检（挂不上时先跑它）
  tools\unit_test.py        离线单测：拖拽伺服 + 按键识别（不用开游戏）
  tools\build_manifest.py   重新生成清单 / 打 zip / --check 核对
  docs\00..07               文档（见上面的文档地图）
```

> `.cmd` 全部纯 ASCII：cmd.exe 用系统 OEM 代码页解析批处理，中文写进去会变成"不是内部或外部命令"。中文提示放 Python 里，路径用 `%~dp0`。改这个补丁请保持这条规矩。

## 版本

当前 **1.10.4**。1.10.1 修「装完没反应」（两层 pythonw 继承失效的 std 句柄，worker 第一行日志就把自己打死），并重做了安装/卸载链路；1.10.2 修 1.10.1 带出的新竞态：启动器自己开游戏时，worker 会在游戏加载完成前 attach，旧代码直接 `fatal` 退出——现在改为等待（日志先 `waiting-boot`，进游戏后 `booted` → `ready`）；1.10.3 修「双击启动器没反应」：以前"已有 worker 就直接退出"排在"开游戏"前面，游戏关掉后遗留的空转 worker 会把每次双击都吞掉——现在先开游戏、再处理 worker，worker 自己也会在游戏退出时写 `game-exit` 并结束。同版本 zip 顶层改成了 `mods\corpse-flinch\`，解压到游戏根目录就行。1.10.4 修「崩溃后启动器不动」：上一局崩了、进程只剩一个 `Fatal error` 框时，启动器会认出它（所有可见窗口都叫 `Message`）并清掉重开，不再傻等。功能代码与 1.9.3 完全一致。逐版改动（含每次的根因）见 [`docs\07-版本历史.md`](docs/07-版本历史.md)。

## 分享与改造

可以自由分发、改代码、改参数。改造后请：
1. 跑 `tools\unit_test.py`（离线）与 `tools\selftest.py`（只读，需要游戏开着）
2. 跑 `tools\build_manifest.py --zip corpse-flinch-<新版本>.zip` 重新生成清单与发布包
3. 在 [`docs\07-版本历史.md`](docs/07-版本历史.md) 加一条，写清楚"改了什么、为什么"
