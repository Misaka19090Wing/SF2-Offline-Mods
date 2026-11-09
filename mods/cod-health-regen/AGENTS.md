# AGENTS.md · 给 AI 代理的接手契约

本目录是 SF2 离线客户端的一个**运行时补丁 mod**（类 CoD 呼吸回血）。它和同级的 `corpse-flinch` 是同一套交付形态，但**更保守**：不装 ProcessEvent 钩子，只读写内存里的 `Pawn.Health`。

接手前先读：`docs/01-原理.md`（数据从哪来）、`docs/02-日志字段.md`（怎么判断生效）、本文件 §3。

## 1. 内容与边界

```
health_regen.py        CLI + Worker：连接、主循环、日志、异常兜底
config.json            全部可调参数（用户唯一需要改的文件）
sf2mod/attach.py       找游戏目录/进程、读写句柄（不装钩子）
sf2mod/reader.py       自带的 UE3 读写器（R10 封印后不再 import 客户端模块）
python/                mod 自带的精简 CPython 3.12（安装与运行都用它）
sf2mod/game.py         地址常量、Health/HealthMax 偏移的反射解析、本地玩家查找
sf2mod/regen.py        纯回血状态机（无游戏访问，离线单测）
tools/install.py       安装 + 写启动器 + 只读自检
tools/launch.py        起游戏（必要时）+ 后台起 worker
tools/uninstall.py     停 worker、删启动器、可选 --purge
tools/selftest.py      只读自检（连上、解析偏移、读一次血量）
tools/probe.py         只读诊断：两条认玩家的路都 dump 出来（关卡 actor / GObjects）
tools/unit_test.py     离线单测（不需要游戏）
tools/build_manifest.py 清单 --check / --zip
docs/00..02            文档
```

规则：**`sf2mod/` 只放机制，业务策略放 `Worker`**。新增效果 ≈ 新增纯逻辑模块 + 在 `Worker.step()` 加一次调用 + 在 `unit_test.py` 加一组断言。

## 2. 不变量（违反即不可发布）

1. **不改游戏文件、不写注册表、不装服务、不打代码补丁**。唯一写入是 `Pawn.Health` 这个 int。
2. **玩家永远回血；机器人只在 `bots` 打开时回**，且只回"与玩家同队"的活人（敌人、异形怪、无 PRI 的单位一律不碰）。`bots_pve_only`（默认 true）把机器人回血限制在异形 PVE。
3. **`Health <= 0` 时绝不写入**，永不复活。死亡/换图由 worker 的重置路径处理。
4. **只增不减、永不超上限**。读数不合理（`HealthMax <= 0`、`> 100000`、`Health > HealthMax`）时宁可不写。
5. **每个 tick 独立 `try`**：`OSError` 跳过本 tick；其他异常写 `worker-error` 后继续，不允许裸 `except: pass`。
6. **配置项三处一致**：`config.json` 的键 = `health_regen.DEFAULTS` 的键（单测检查），并在 `README.md` 的参数表里说明；未知键直接拒绝启动。
7. **日志单行 JSON**，新增事件必须写进 `docs/02-日志字段.md`。
8. **`.cmd` 纯 ASCII**（cmd.exe 用 OEM 代码页，中文会变成"不是内部或外部命令"）；中文提示放 Python。
9. **不引入第三方依赖**。只用标准库；解释器用 mod 自带的 `python/`（R10 起客户端自带 Python 被 `sf2guard` 封印，不能再用）。
10. **改完重新生成清单**：`runtime\python\python.exe -X utf8 -B tools\build_manifest.py`，否则 `--check` 会报"被改"。

## 3. 陷阱

1. **别把偏移写死**。四个字段全部走实时反射（`Fields`）；`docs/01` §2 的绝对地址只对当前客户端成立，由客户端 `Reader` 的 EXE 哈希校验兜底。
2. **别为了"更原生"去调 `Engine.Pawn.HealDamage`**：native、要传合法 `DamageType` class，收益只是治疗音效，风险是远程调用崩游戏。数值回血用 int 字段足够。
3. **别装 ProcessEvent 钩子**。装了会和 AI worker / corpse-flinch 抢 `0x6008D0`，安装顺序一变就报 `Expected original Actor ProcessEvent entry`。本 mod 的兼容性正是靠"不装钩子"。
4. **名字表会随地图加载变长**：`Reader` 只读一次名字表，`name()`/`header()`/`path()` 三个 `lru_cache` 都会把"当时读不到"的结果记住。加载地图后新名字的索引超出旧表 → 类名变成 `'<bad-name:N>'` → `*PlayerController` 判据全部失效 → **进图就认不到玩家**（1.0.1 实测症状：菜单 26 个 actor 认得出，进图 244 个 actor 认不出）。所以 `refresh_names()` 必须清 **`name` + `header` + `path` 三个缓存**，并在每次重扫前调用；另有不依赖类名的 `via='level-no-name'` 兜底。
5. **启动期连上会踩缓存坑**：`Reader` 只读一次名字表，`header()`/`path()` 按地址 `lru_cache`。启动早期扫描会把"名字还没加载"的地址缓存成错误路径，**永不自动恢复**：长寿 worker 一直报"缺少属性"，同一客户端上新开进程却立刻成功（已实测）。所以 `Fields.__init__` 必须先 `refresh_names()`。
6. **认玩家必须同时满足三条**：类名以 `PlayerController` 结尾、`Player != 0`、`HealthMax` 合理。少任何一条都出过事：
   - 只判 `Player != 0` → 26 个菜单 actor 里 9 个假阳性（实测）；
   - 不判 `HealthMax` → 菜单 limbo pawn（`Health=0, HealthMax=0`）被当成玩家，mod 静默空转。
7. **找不到 pawn 时不能提前 return**：控制器可能不在持久关卡 actor 列表里，必须继续走 GObjects 兜底（实测 0.16 秒/164k 对象，代价可以接受）。缓存也只在"有可用 pawn"时才算命中，否则阵亡/换图后永远卡住。
8. **永远不要静默吞掉 `OSError`**：1.0.0 就是靠 `except OSError: pass` 把整场失效藏了 165 秒。现在记 `read-error`。同理，认不到玩家没日志也必须补 `no-pawn` 诊断——否则下次还是只能猜。
9. **自己写的血量不能被当成受伤**。写入后 `regen.last_health` 已同步；`dry` 模式没写内存，worker 会手动把 `last_health` 拨回真实值。
10. **`death` 每次死亡只能记一条**。`regen.reset()` 会把 `last_health` 置 `None`，用 `None != 0` 当守卫会每 tick 刷一条（已踩）。
11. **别按名字杀 `pythonw.exe`**：客户端的 `portable_runtime.py` 也是 `pythonw.exe`，杀了等于关游戏。用 `logs\worker.json` 里的 pid。
12. **README/文档里的"验证状态"必须写实**：没实机确认过的就写未验证。

## 4. 验证闭环（改完必跑）

```bat
runtime\python\python.exe -X utf8 -B tools\unit_test.py
rem  期望：all good: 11 regen rule groups behave here.

runtime\python\python.exe -X utf8 -B health_regen.py --version
rem  期望：cod-health-regen 1.0.2

runtime\python\python.exe -X utf8 -B tools\selftest.py --game <游戏根目录>
rem  只读：连上客户端、打印两个偏移与实时血量

runtime\python\python.exe -X utf8 -B tools\build_manifest.py --check
```

实机冒烟（必须做，不能只看单测）：

1. `启动-回血模式.cmd`（或先 `开发工具.cmd play`，再跑 `tools\launch.py`）
2. 进一局，jsonl 依次出现 `ready` → `attached` → `fields` → `pawn`
3. 被打一枪出现 `damage`，停止受伤满 4 秒后开始出现 `regen`，直到 `full`
4. 血量条肉眼确认与日志数值一致（**这一步不能跳过**：日志只能证明内存值变了；已在 2026-10-25 的 1.0.2 实机对局中完成，见 `docs/01` §6）
5. 关游戏，jsonl 末尾 `game-exit`；`logs\worker.json` 不再更新

## 5. 卡住时

- 连不上：先 `tools\selftest.py`，它逐项报哪一步失败（进程 / 桥 / 偏移 / pawn）。
- 数值不涨：看有没有 `damage` 之后的 `regen`；没有 `regen` 就是被延迟或 `enabled`/`dry_run` 挡了。
- 涨了但屏幕不变：说明 HUD 不读这个字段——那是原理层面的错误，回到 `docs/01` §6 重新评估，不要靠调参硬凑。
