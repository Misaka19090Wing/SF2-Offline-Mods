# 尸体数量控制 mod（corpse-limit 1.0.1）

> 制作：DeepSeek ｜ 许可：MIT

控制地图上保留多少尸体。三条规则都在 `config.json`，互不冲突、可以只开一条：

| 配置 | 默认 | 含义 |
| --- | --- | --- |
| `lifetime_seconds` | `60.0` | 尸体存在这么久后消失；`0` = 不按时间清理 |
| `max_distance` | `0.0` | 距离玩家超过这么多 cm 的尸体消失；`0` = 不按距离清理 |
| `max_corpses` | `24` | 最多保留多少具；超过时**最早出现的先消失**；`0` = 不限 |
| `poll_seconds` | `0.5` | 检查间隔 |
| `dry_run` | `false` | 只记日志不执行 |

改完保存即可**下一局生效**，不需要重装；改 `enabled: false` 就整个停用。

## 原理（为什么不装钩子）

三个需求都归约成"让这具尸体立刻消失"，而 UE3 引擎本身就会按 `Engine.Actor.LifeSpan`
倒计时销毁 actor。mod 把该值写成 0.0001，下一次世界 tick 引擎自己调用 `Destroy()`。
全程没有 ProcessEvent 调用、没有钩子，所以：

- 不需要和任何 AI worker 抢 `0x6008D0`（爆破/刀战/PVE 都能同时开）；
- 可以和回血 mod、鞭尸 mod 同时使用。

实机验证：给一个 pawn 写 `LifeSpan = 30`，1.6 秒后读回 **28.4**，证明引擎在递减这个字段。

**实机验收（2026-10-26，用户）**：三条规则都生效。日志证据 —— 一晚 63 次 `expire`，其中
`distance` 28 次、`lifetime` 2 次、`count` 33 次；两次会话的 `ready` 行分别写着
`lifetime_seconds 10.0 / max_distance 300.0` 与 `lifetime_seconds 0.0 / max_distance 1000.0`，
说明改 `config.json` 后重启 worker 即生效。

## 使用

1. `安装.cmd` —— 装进本目录上两级的游戏根目录，并在游戏目录生成 `启动-尸体清理.cmd`。
2. 启动游戏后双击 `启动-尸体清理.cmd`（或直接运行 `tools\launch.py`），worker 在后台运行，游戏关闭后自动退出。
3. 日志：`logs/corpse-limit-<日期>.jsonl`，每行一个 JSON，`expire` 事件记录被清理的尸体、
   原因（`lifetime` / `distance` / `count`）和存在时长。

## 边界

- 只认 `SFPawn` 且 `Health <= 0 && HealthMax > 0` 的 actor；大厅的 limbo pawn 不碰。
- 玩家自己的尸体也会按同样规则处理（不想让它消失就把 `lifetime_seconds` 设 0）。
- 尸体"数量"指仍在地图上的死亡 pawn；已被引擎销毁的不计入。
