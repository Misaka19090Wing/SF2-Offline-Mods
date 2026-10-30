# cod-health-regen · 类 CoD 呼吸回血

> 制作：DeepSeek ｜ 许可：MIT

SF2 离线客户端的**本地玩家**在停止受伤若干秒后，血量自动回复到上限。CoD 式：被打断后要重新等待，不是持续小回复。

- 不改游戏文件、不装服务、不写注册表、**不打代码补丁**（不装 ProcessEvent 钩子）。
- 只读写本进程内存里的两个字段：`Engine.Pawn.Health` / `Engine.Pawn.HealthMax`，前者是游戏自己扣血时改的那个 int。
- 只对**本地玩家**生效。机器人、怪物、英雄模式 bot 的血量一律不碰。
- 死亡状态绝不回血（`Health <= 0` 时不做任何写入），不会复活。

## 安装

把 `mods\cod-health-regen` 放到游戏目录下，然后双击 `mods\cod-health-regen\安装.cmd`。
安装只会：确认 mod 在 `<游戏>\mods\cod-health-regen`、在游戏根目录写一个 `启动-回血模式.cmd`、跑一次只读自检。游戏文件一个都不碰。

## 使用

双击游戏根目录的 `启动-回血模式.cmd`：游戏没开就先开游戏，再后台起 worker。
已经用别的方式开着游戏时，它也只会补一个 worker。

日志：`mods\cod-health-regen\logs\health-regen-YYYYMMDD.jsonl`

## 配置

`mods\cod-health-regen\config.json`，改完下次启动生效：

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `enabled` | `true` | 关掉回血（worker 仍会挂着并记录，但不写内存） |
| `regen_delay_seconds` | `4.0` | 最后一次受伤后等几秒才开始回血 |
| `regen_per_second` | `25.0` | 回血速度（点/秒），从 100 血算 4 秒回满 |
| `tick_hz` | `10.0` | 轮询频率；1–60 之间，越低越省 CPU |
| `log_damage` | `true` | 记录 `damage` 事件（受伤瞬间的血量） |
| `bots` | `false` | 机器人也按同样规则回血：只回**自己队**的活人，不含敌人 |
| `bots_pve_only` | `true` | 只在异形 PVE（生存/防守）里给机器人回血；团竞里队友不回，避免影响对抗平衡 |
| `dry_run` | `false` | 只算不写，用来确认"它想加多少" |

## 卸载

双击 `mods\cod-health-regen\卸载.cmd`（停 worker、删启动器）。加 `--purge` 再删整个 mod 目录。
游戏关掉时 worker 会自己退出，不用手动清理。

## 已知限制

- 数值回血走的是"游戏每帧读的那个 int"，不走引擎自带的 `HealDamage` 事件，所以没有治疗音效/特效。
- 进对局后的"扣血→停 4 秒→回满"已实机验收（2026-10-25，1.0.2）。1.0.0 / 1.0.1 两次实测都失败，根因是**名字表随地图加载变长导致类名读不出来**（见 `docs/01-原理.md` §6.3，已在实机复现并验证修复）；1.0.2 修好了重建名字表与不依赖类名的兜底。如果仍然不生效，`logs\health-regen-*.jsonl` 里的 `no-pawn` 事件会列出当时所有控制器，直接看 `candidates` 就知道卡在哪（见 `docs/01-原理.md` §6）。
- 玩家永远回血；`bots` 打开后**自己队的机器人**也回血，`bots_pve_only`（默认 `true`）把它限制在异形 PVE。
  已实机验收（2026-10-30，1.2.1）：`SFZombieSurvivalGame` / `SFDefenceGame` 里队友正常回血（`regen-bot` 287 次，队友 `HealthMax` 300 → 回满 300）；`SFAITeamDeathGame` 里 `bots-scan` 报 `pve: false`，一个队友都不回。
- 游戏最小化/暂停时不写（主线程不跑，但这里本来也不需要主线程，受影响的是游戏自己的帧推进）。
- 客户端 EXE 一旦更新，客户端的 `Reader` 哈希校验会拒绝连接——这是故意的，见 `docs/01-原理.md`。
