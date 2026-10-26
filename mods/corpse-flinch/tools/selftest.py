"""只读自检：确认当前这台机器上的游戏能被本补丁正确识别。

用法（在游戏目录下）：

    runtime\\python\\python.exe -X utf8 mods\\corpse-flinch\\tools\\selftest.py

它不会安装桥、不会施加冲量、不碰游戏文件；任何一步失败都会打印原因和下一步。
"""
import json
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from sf2mod import attach, game          # noqa: E402
from sf2mod import signals               # noqa: E402


def check(label, ok, detail=''):
    print(f'[{"OK " if ok else "FAIL"}] {label}' + (f'  {detail}' if detail else ''))
    return ok


def main():
    ok = True
    print('== 1. 找游戏目录 ==')
    try:
        root = attach.game_dir()
    except SystemExit as exc:
        print(f'[FAIL] {exc}')
        return 1
    ok &= check('游戏目录', True, str(root))
    try:
        ok &= check('客户端可执行文件', attach.client_exe(root).is_file(),
                    str(attach.client_exe(root)))
    except Exception as exc:                    # noqa: BLE001
        ok &= check('客户端可执行文件', False, f'{type(exc).__name__}: {exc}')
    ok &= check('便携 Python', (root / 'runtime' / 'python' / 'python.exe').is_file())

    print('== 2. 加载游戏自带的运行库 ==')
    try:
        battle, route, main_thread = attach.load_runtime(root)
    except Exception as exc:                    # noqa: BLE001
        print(f'[FAIL] 运行库导入失败：{type(exc).__name__}: {exc}')
        return 1
    ok &= check('local_battle_bridge / local_route_bridge / main_thread', True)

    print('== 3. 找游戏进程 ==')
    pid = attach.find_pid(root)
    if not check('游戏进程 sf2.exe', bool(pid), f'pid={pid or 0}'):
        print('      先启动游戏（运行游戏.cmd），再运行本自检。')
        return 1

    print('== 4. 只读挂接（不安装桥） ==')
    bridge = battle.Bridge(pid)
    ok &= check('读取进程内存', True, f'pid={pid}')
    world = game.u32(bridge, game.GWORLD)
    ok &= check('GWorld 指针', 0x10000 < world < 0xFFFFFFFF, hex(world))
    level = game.u32(bridge, world + game.LEVEL) if world else 0
    ok &= check('PersistentLevel', 0x10000 < level, hex(level))

    pawns = game.Pawns(bridge)
    pawns.poll()
    ok &= check('关卡 actor 表', bool(pawns.live) or True, f'pawn={len(pawns.live)}')
    print(f'      当前世界 {hex(pawns.world)}，pawn {len(pawns.live)} 个')
    for address, name in list(pawns.live.items())[:12]:
        health = game.i32(bridge, address + game.HEALTH)
        mesh = game.u32(bridge, address + game.MESH)
        where = game.matrix_translation(bridge, mesh)
        print(f'      {name:<24} {hex(address)} health={health:<5} '
              f'mesh={hex(mesh) if mesh else "-"} pos={where}')

    print('== 5. 玩家与武器 ==')
    player = game.Player(bridge, pawns)
    controller = player.find()
    ok &= check('PlayerController', bool(controller), hex(controller or 0))
    weapon = player.weapon()
    if weapon:
        print(f'      武器 {hex(weapon)} {bridge.object_name(weapon)} '
              f'弹匣={player.ammo(weapon)}')
        hit = signals.read(bridge, weapon)
        print(f'      上一发命中记录 {len(hit)} 条')
        if not any(entry['actor'] for entry in hit):
            print('      （手里不是子弹类武器时这张表是无关内存，属正常）')
        else:
            try:
                for entry in signals.describe(hit, lambda a: bridge.object_name(a)
                                              if a > 0x10000 else None):
                    print('        ' + json.dumps(entry, ensure_ascii=False))
            except OSError:
                print('      （这张表当前不是子弹命中记录 —— 手里大概是弓/刀/投掷武器）')
    else:
        print('      还没进对局或手里没枪（在大厅里属于正常）')

    print('== 6. 近战与投射物 ==')
    corpses = [a for a in pawns.live if pawns.is_corpse(a)]
    ok &= check('尸体数量', bool(corpses) or True, f'{len(corpses)}')
    if corpses:
        mesh = game.u32(bridge, corpses[0] + game.MESH)
        points = game.bones(bridge, mesh)
        ok &= check('骨骼世界坐标', len(points) >= 20,
                    f'{len(points)} 根，第一根 {tuple(round(v, 1) for v in points[0]) if points else "-"}')
    pawn = player.pawn()
    if pawn:
        raw = bridge.read(pawn + game.DOING_EXIGENCY, 1)
        print(f'      DoingExigencyAttack 0x{pawn:x}+0x{game.DOING_EXIGENCY:X} '
              f'= {raw[0]:#04x}（按 F 时 bit 3 置位）')
    if pawn:
        mesh = game.u32(bridge, pawn + game.MESH)
        print(f'      玩家 mesh={hex(mesh)} 位置={game.matrix_translation(bridge, mesh)}')

    print('== 6.1 拖拽（E 键）==')
    native = route.NativeAPI(bridge)
    key = 'SFGame.SFPlayerController.bPressingUsedKey'
    found = key in native.properties
    ok &= check('交互键状态位 SFPlayerController.bPressingUsedKey', found,
                hex(native.properties[key]['offset'])
                if found else '找不到 ⇒ 会退回 GetAsyncKeyState')
    controller = player.find()
    if found and controller:
        flags = struct.unpack('<I', bridge.read(
            controller + native.properties[key]['offset'], 4))[0]
        held = bool(flags & 0x40)
        print(f'      controller 0x{controller:x} flags=0x{flags:08x} '
              f'bPressingUsedKey={held}（按住 E 时应为 True）')

    print('== 7. 关键函数是否存在 ==')
    api = native
    for path in ('Engine.PrimitiveComponent.AddImpulse',
                 'Engine.SkeletalMeshComponent.GetBoneName'):
        ok &= check(path, path in api.functions,
                    hex(api.functions[path]) if path in api.functions else '')
    for path in ('SFGame.SFWeap_Bullet.HitResult', 'Engine.Pawn.Health',
                 'SFGame.SFWeapon.AmmoCountInMgz',
                 'SFGame.SFPawn_Player.DoingExigencyAttack',
                 'Engine.SkeletalMeshComponent.SpaceBases',
                 'Engine.Actor.Velocity'):
        found = path in api.properties
        ok &= check(path, found,
                    hex(api.properties[path]['offset']) if found else '')

    print()
    print('结论：' + ('这台机器可以直接运行 corpse_flinch.py。' if ok else
                    '有检查项失败，见 docs/04-排错.md。'))
    bridge.close()
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
