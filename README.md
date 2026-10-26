# SF2-Offline-Mods

上海滩（SF2）离线客户端的 3 个进程内 mod。三者都**不修改任何游戏文件、不写注册表、不需要管理员**，
也不需要另装 Python —— 用客户端自带的 `runtime\python`。

| Mod | 版本 | 作用 | 需要 ProcessEvent 钩子 |
| --- | --- | --- | --- |
| [cod-health-regen](mods/cod-health-regen/) | 1.0.2 | 类 CoD 呼吸回血：受伤后停几秒自动回满 | 否 |
| [corpse-flinch](mods/corpse-flinch/) | 1.11.2 | 子弹 / 近战 / 飞斧把尸体真的打飞，方向力度来自游戏自己算出的命中 | 是，与原生 AI worker 分时让位 |
| [corpse-limit](mods/corpse-limit/) | 1.0.1 | 控制尸体存在时长、距离玩家多远的清掉、地图上最多留几具 | 否 |

## 安装

每个 mod 目录下都有 `安装.cmd`，或手动：

```
<游戏>\runtime\python\python.exe -X utf8 -B <mod>\tools\install.py --game <游戏根目录>
```

装完后游戏根目录会多一个启动用的 `.cmd`（例如 `启动-回血模式.cmd`）。先开游戏，再双击它；
worker 在后台运行，游戏关闭后自行退出。

各 mod 的配置都在自己目录下的 `config.json`，改完**下一局生效**，不需要重装。

## 兼容性

`0x6008D0` 上的 ProcessEvent 钩子每个进程只能有一个，而原生 AI worker（爆破/刀战/异形 PVE/英雄/
路线图团队竞技）也要用它。corpse-flinch 用 `ai_priority` 做分时让位：检测到 AI 模式就把入口的原始
字节放回去，AI 局结束后再收回来。另外两个 mod 不装钩子，任何时候都能和其它 mod 同时开。

## 适用版本

离线客户端需自带 `runtime\tools\local_battle_bridge.py`（R6 及之后）。换版本后偏移可能变，
各 mod 的 `docs/` 里写了解析方式和重测步骤。
