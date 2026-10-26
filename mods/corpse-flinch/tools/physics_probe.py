"""Find out where a live ragdoll's velocity actually lives.

Plan C for the R6 AI clash is "apply the flinch without the ProcessEvent bridge",
i.e. write the ragdoll's physics state instead of calling AddImpulse. Every class
involved is script-visible, so no offset has to be hard coded:

    Pawn.Mesh                                     USkeletalMeshComponent
    SkeletalMeshComponent.PhysicsAssetInstance    UPhysicsAssetInstance
    PhysicsAssetInstance.Bodies                   TArray<URB_BodyInstance*>
    PhysicsAssetInstance.RootBodyIndex            int
    PhysicsAsset.BodySetup                        TArray<FBodySetup> (u32[0]=RB_BodySetup)
    RB_BodySetup.BoneName                         FName
    RB_BodyInstance.Velocity / PreviousVelocity / BodyData / BodyIndex

What is NOT known is which of those is authoritative: `Velocity` is a mirror the
engine refreshes from PhysX, and `BodyData` is the native body struct. This probe
answers that with real corpses instead of guesses.

Read-only by default. `--impulse` additionally fires one AddImpulse through the
bridge and diffs the bodies before/after, so the field the engine touched becomes
visible. It refuses to fire unless this session's bridge is corpse-flinch's own
single-call one -- writing single-call data into the R6 batch bridge's block would
damage the match (see docs/01 原理 §9).

    runtime\\python\\python.exe -X utf8 -B tools\\physics_probe.py [--game <root>] [--impulse]
"""
import argparse
import json
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACK = HERE.parent
sys.path.insert(0, str(PACK))

from sf2mod import attach                       # noqa: E402

PROPERTIES = (
    'Engine.Pawn.Health',
    'Engine.Pawn.Mesh',
    'Engine.Actor.Physics',
    'Engine.SkeletalMeshComponent.PhysicsAsset',
    'Engine.SkeletalMeshComponent.PhysicsAssetInstance',
    'Engine.SkeletalMeshComponent.FramesPhysicsAsleep',
    'Engine.PhysicsAsset.BodySetup',
    'Engine.PhysicsAssetInstance.Bodies',
    'Engine.PhysicsAssetInstance.RootBodyIndex',
    'Engine.PhysicsAssetInstance.Owner',
    'Engine.RB_BodySetup.BoneName',
    'Engine.RB_BodyInstance.Velocity',
    'Engine.RB_BodyInstance.PreviousVelocity',
    'Engine.RB_BodyInstance.BodyData',
    'Engine.RB_BodyInstance.BodyIndex',
    'Engine.RB_BodyInstance.OwnerComponent',
)
BODY_FIELDS = ('Velocity', 'PreviousVelocity', 'BodyData', 'BodyIndex', 'OwnerComponent')
PHYS_RIGID_BODY = 10
ADD_IMPULSE = 'Engine.PrimitiveComponent.AddImpulse'


def connect(game, pid):
    """A read/write handle from the client's own runtime; never installs the hook.

    Note: this must NOT call `sf2mod.attach.connect()` -- that one takes
    (game, battle, route, main_thread) and exists for the worker, not for tools.
    """
    parts = ('tools/runtime', 'src/runtime', 'tools') if (game / 'sf2-project.json').is_file() \
        else ('runtime', 'runtime/tools')
    for part in parts:
        path = str(game / part)
        if (game / part).is_dir() and path not in sys.path:
            sys.path.insert(0, path)
    import local_battle_bridge as battle
    return battle.Bridge(pid)


def u32(bridge, address):
    return struct.unpack('<I', bridge.read(address, 4))[0]


def actors_of(bridge):
    world = u32(bridge, 0x179B2F4)
    if world < 0x10000:
        return 0, []
    level = u32(bridge, world + 0x50)
    if level < 0x10000:
        return world, []
    pointer, count, capacity = struct.unpack('<3I', bridge.read(level + 0x3C, 12))
    if not 0 < count <= capacity < 100000:
        return world, []
    raw = bridge.read(pointer, 4 * count)
    return world, [struct.unpack_from('<I', raw, 4 * i)[0] for i in range(count)]


class Refs:
    """Property offsets resolved once from live reflection."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.offset = {}
        self.size = {}
        self.kind = {}
        by_name = {path.rsplit('.', 1)[1]: path for path in PROPERTIES}
        for address, name, cls in bridge.objects():
            path = by_name.get(name)
            if path is None or not cls.endswith('Property'):
                continue
            if bridge.path(address) != path:
                continue
            self.offset[path] = struct.unpack('<I', bridge.read(address + 0x64, 4))[0]
            _, self.size[path] = struct.unpack('<2I', bridge.read(address + 0x44, 8))
            self.kind[path] = cls
        missing = [p for p in PROPERTIES if p not in self.offset]
        if missing:
            raise RuntimeError('属性缺失：' + ', '.join(missing))

    def get(self, obj, path):
        raw = self.bridge.read(obj + self.offset[path], self.size[path])
        kind = self.kind[path]
        if kind == 'IntProperty':
            return struct.unpack('<i', raw)[0]
        if kind == 'FloatProperty':
            return struct.unpack('<f', raw)[0]
        if kind == 'ByteProperty':
            return raw[0]
        if kind == 'StructProperty' and len(raw) == 12:
            return list(struct.unpack('<3f', raw))
        if kind == 'StructProperty':
            return raw
        return struct.unpack('<I', raw)[0]

    def offsets(self):
        return {path: hex(value) for path, value in sorted(self.offset.items())}


def tarray(bridge, obj, offset, element_size=None):
    pointer, count, _ = struct.unpack('<3I', bridge.read(obj + offset, 12))
    if not 0 < count < 512 or pointer < 0x10000:
        return pointer, 0
    return pointer, count


def setup_stride(refs, asset):
    """Discover the real element size of PhysicsAsset.BodySetup by validating entries."""
    bridge = refs.bridge
    offset = refs.offset['Engine.PhysicsAsset.BodySetup']
    pointer, count = tarray(bridge, asset, offset)
    if not count:
        return None, 0, []
    candidates = [refs.size['Engine.PhysicsAsset.BodySetup'], 12, 16, 8, 4]
    best = (0, None, [])
    for stride in dict.fromkeys(c for c in candidates if c):
        try:
            raw = bridge.read(pointer, stride * count)
        except OSError:
            continue
        hits, rows = 0, []
        for i in range(count):
            setup = struct.unpack_from('<I', raw, i * stride)[0]
            if setup < 0x10000:
                rows.append(None)
                continue
            try:
                name = bridge.path(setup)
            except (OSError, RecursionError):
                rows.append(None)
                continue
            if name.endswith('RB_BodySetup'):
                hits += 1
                rows.append(setup)
            else:
                rows.append(None)
        if hits > best[0]:
            best = (hits, stride, rows)
    return best[1], best[0], best[2]


def describe(refs, body, bone=None):
    row = {'address': hex(body), 'bone': bone}
    for field in BODY_FIELDS:
        path = 'Engine.RB_BodyInstance.' + field
        try:
            value = refs.get(body, path)
        except (OSError, struct.error):
            row[field] = '<unreadable>'
            continue
        if isinstance(value, (bytes, bytearray)):
            row[field] = value.hex()
        elif isinstance(value, list):
            row[field] = [round(v, 2) for v in value]
        elif field in ('BodyIndex', 'OwnerComponent'):
            row[field] = hex(value) if value > 0xFFFF else value
        else:
            row[field] = value
    return row


def bodies_of(refs, mesh):
    bridge = refs.bridge
    asset = refs.get(mesh, 'Engine.SkeletalMeshComponent.PhysicsAsset')
    bones, stride, setups = [], None, []
    if asset > 0xFFFF:
        stride, _, setups = setup_stride(refs, asset)
        for setup in setups:
            if not setup:
                bones.append(None)
                continue
            try:
                bones.append(bridge.name(refs.get(setup, 'Engine.RB_BodySetup.BoneName')))
            except (OSError, struct.error):
                bones.append(None)
    instance = refs.get(mesh, 'Engine.SkeletalMeshComponent.PhysicsAssetInstance')
    bodies = []
    if instance > 0xFFFF:
        pointer, count = tarray(bridge, instance, refs.offset['Engine.PhysicsAssetInstance.Bodies'])
        if count:
            raw = bridge.read(pointer, 4 * count)
            bodies = [struct.unpack_from('<I', raw, 4 * i)[0] for i in range(count)]
    return asset, stride, bones, instance, bodies


def scan_triples(bridge, base, size, target, tolerance=0.75):
    """Offsets inside [base, base+size) that hold `target` as three floats."""
    if base < 0x10000 or size <= 0:
        return []
    try:
        raw = bridge.read(base, size)
    except OSError:
        return []
    hits = []
    for offset in range(0, len(raw) - 11, 4):
        value = struct.unpack_from('<3f', raw, offset)
        if all(abs(value[i] - target[i]) <= tolerance for i in range(3)):
            hits.append(offset)
    return hits


def scan_pointers(bridge, base, size, low=0x10000, high=0x7FFFFFFF):
    try:
        raw = bridge.read(base, size)
    except OSError:
        return []
    out = []
    for offset in range(0, len(raw) - 3, 4):
        value = struct.unpack_from('<I', raw, offset)[0]
        if low < value < high:
            out.append((offset, value))
    return out


def mesh_position(bridge, mesh):
    try:
        raw = bridge.read(mesh + 0x170, 64)
    except OSError:
        return None
    return struct.unpack_from('<3f', raw, 48)


def locate_velocity(bridge, refs, mesh, bodies, root_index, tolerance=1.5, rounds=12):
    """Where does the velocity triple live? Vote across frames.

    The mirror is refreshed once per frame, so a single read can be one frame
    behind the native value. Reading mirror+native together many times and only
    trusting offsets that match over and over removes both the frame skew and
    coincidental float triples.
    """
    out = {}
    if not bodies:
        return out
    if not 0 <= root_index < len(bodies):
        root_index = 0
    body = bodies[root_index]
    native = struct.unpack('<I', refs.bridge.read(
        body + refs.offset['Engine.RB_BodyInstance.BodyData'], 4))[0]
    out['native_body'] = hex(native)
    if native < 0x10000:
        out['note'] = 'BodyData 指针无效'
        return out
    children = [(offset, pointer) for offset, pointer in
                scan_pointers(refs.bridge, native, 0x300)[:64]]
    out['child_pointers'] = len(children)
    votes = {}
    rounds_seen = 0
    for _ in range(rounds):
        velocity = refs.get(body, 'Engine.RB_BodyInstance.Velocity')
        if not any(abs(v) > 1.0 for v in velocity):
            time.sleep(0.05)
            continue
        rounds_seen += 1
        for offset in scan_triples(refs.bridge, native, 0x300, velocity, tolerance):
            votes['native+%s' % hex(offset)] = votes.get('native+%s' % hex(offset), 0) + 1
        for pointer_offset, pointer in children:
            for offset in scan_triples(refs.bridge, pointer, 0x300, velocity, tolerance):
                key = 'child %s+%s' % (hex(pointer), hex(offset))
                votes[key] = votes.get(key, 0) + 1
        time.sleep(0.06)
    out['rounds_with_motion'] = rounds_seen
    out['votes'] = dict(sorted(votes.items(), key=lambda kv: -kv[1])[:8])
    out['candidates'] = {key: value for key, value in votes.items() if value >= max(2, rounds_seen // 3)}
    if not out['candidates']:
        out['note'] = '没有稳定命中：镜头/物理在这段时间内没动，或速度三元组不是连续 float3'
    return out


def candidate_addresses(refs, located):
    """(name, address) for every stable offset the vote produced."""
    out = []
    native = int(located.get('native_body', '0x0'), 16)
    for key in located.get('candidates', {}):
        if key.startswith('native+'):
            out.append((key, native + int(key.split('+')[1], 16)))
        elif key.startswith('child '):
            pointer, offset = key[6:].split('+')
            out.append((key, int(pointer, 16) + int(offset, 16)))
    return out


def try_write(bridge, refs, mesh, bodies, root_index, located, push=(0.0, 0.0, 400.0)):
    """Write a bigger velocity at each candidate; the engine mirror tells the truth.

    If physics actually reads the field we wrote, the mirror the engine refreshes
    from PhysX will show our value on the next frames. Measuring mesh movement is
    useless here because the ragdoll is already ballistic.
    """
    results = []
    if not 0 <= root_index < len(bodies):
        root_index = 0
    body = bodies[root_index] if bodies else 0
    for name, address in candidate_addresses(refs, located)[:4]:
        try:
            saved = bridge.read(address, 12)
            current = list(struct.unpack('<3f', saved))
        except OSError as error:
            results.append({'candidate': name, 'error': str(error)})
            continue
        wanted = [current[i] + push[i] for i in range(3)]
        try:
            bridge.write(address, struct.pack('<3f', *wanted))
        except OSError as error:
            results.append({'candidate': name, 'error': str(error)})
            continue
        mirror = []
        for _ in range(3):
            time.sleep(0.06)
            try:
                mirror.append([round(v, 1) for v in refs.get(
                    body, 'Engine.RB_BodyInstance.Velocity')])
            except OSError:
                mirror.append(None)
        try:
            bridge.write(address, saved)
        except OSError:
            pass
        adopted = any(m and all(abs(m[i] - wanted[i]) < 60 for i in range(3)) for m in mirror)
        results.append({'candidate': name, 'address': hex(address),
                        'wrote': [round(v, 1) for v in wanted],
                        'engine_mirror_after': mirror, 'adopted': adopted})
    return results


def impulse_diff(bridge, refs, mesh, bodies, bones):
    """One real AddImpulse, then show which body fields the engine changed."""
    try:
        bridge.active()                  # raises unless corpse-flinch owns the entry
    except Exception as error:           # noqa: BLE001
        return {'skipped': '本会话的桥不是 corpse-flinch 的单调用桥（%s），拒绝发冲量' % error}
    if not bodies:
        return {'skipped': '没有可用的 body 列表'}
    try:
        from local_route_bridge import NativeAPI
        api = NativeAPI(bridge)
    except Exception as error:           # noqa: BLE001
        return {'skipped': '无法建立 NativeAPI（%s）' % error}
    function = api.functions.get(ADD_IMPULSE)
    if not function:
        return {'skipped': '找不到 ' + ADD_IMPULSE}
    before = [describe(refs, body, bones[i] if i < len(bones) else None)
              for i, body in enumerate(bodies)]
    frame = struct.pack('<3f3fQ?3x', 0.0, 0.0, 700.0, 0.0, 0.0, 0.0, 0, False)
    bridge.invoke(mesh, function, frame, timeout=1.5)
    time.sleep(0.15)
    after = [describe(refs, body, bones[i] if i < len(bones) else None)
             for i, body in enumerate(bodies)]
    changed = []
    for old, new in zip(before, after):
        fields = sorted(k for k in old if k not in ('address', 'bone') and old[k] != new[k])
        if fields:
            changed.append({'address': new['address'], 'bone': new['bone'], 'fields': fields,
                            'before': {k: old[k] for k in fields},
                            'after': {k: new[k] for k in fields}})
    return {'changed_bodies': len(changed), 'detail': changed[:12],
            'before': before[:6], 'after': after[:6]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game', type=Path)
    parser.add_argument('--pid', type=int, default=0)
    parser.add_argument('--wait', type=float, default=0.0)
    parser.add_argument('--impulse', action='store_true',
                        help='通过桥打一发 AddImpulse 并对比刚体字段（仅限单调用桥会话）')
    parser.add_argument('--locate', action='store_true',
                        help='打一发冲量后，扫出速度三元组在原生结构里的真实偏移（只读扫描）')
    parser.add_argument('--try-write', action='store_true',
                        help='在扫出的偏移上试写一个速度并测量布娃娃是否移动（会写内存，随后还原）')
    parser.add_argument('--output', type=Path, default=None,
                        help='把结果写成 UTF-8 JSON 到这里，避免 PowerShell 重定向变成 UTF-16')
    args = parser.parse_args()
    game = (args.game or attach.game_dir(HERE)).resolve()
    out = {'game': str(game), 'read_only': not args.impulse}
    bridge = None
    try:
        pid = args.pid or attach.find_pid(game, timeout=args.wait)
        if not pid:
            raise SystemExit('游戏没有运行：先进一局，让场上有尸体再跑本探针。')
        bridge = connect(game, pid)
        refs = Refs(bridge)
        out['offsets'] = refs.offsets()
        world, actors = actors_of(bridge)
        out['world'] = hex(world)
        out['actors'] = len(actors)
        rows = []
        for actor in actors:
            if actor < 0x10000:
                continue
            try:
                health = refs.get(actor, 'Engine.Pawn.Health')
                mesh = refs.get(actor, 'Engine.Pawn.Mesh')
                physics = refs.get(actor, 'Engine.Actor.Physics')
            except (OSError, struct.error):
                continue
            if mesh <= 0xFFFF:
                continue
            try:
                instance = refs.get(mesh, 'Engine.SkeletalMeshComponent.PhysicsAssetInstance')
            except (OSError, struct.error):
                instance = 0
            rows.append({'pawn': hex(actor), 'health': health, 'physics': physics,
                         'mesh': hex(mesh), 'instance': hex(instance)})
        out['corpses'] = [row for row in rows if row['health'] <= 0]
        ragdolls = [row for row in out['corpses']
                    if row['physics'] == PHYS_RIGID_BODY and int(row['instance'], 16) > 0xFFFF]
        out['ragdolls'] = len(ragdolls)
        if not ragdolls:
            out['note'] = '暂无"真布娃娃"尸体（Actor.Physics==10 且已建物理实例）；死后约 1 秒内才是热的'
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        target = ragdolls[0]
        mesh = int(target['mesh'], 16)
        asset, stride, bones, instance, bodies = bodies_of(refs, mesh)
        out['target'] = {'pawn': target['pawn'], 'mesh': target['mesh'], 'asset': hex(asset),
                         'setup_stride': stride, 'instance': hex(instance),
                         'root_body_index': refs.get(instance, 'Engine.PhysicsAssetInstance.RootBodyIndex'),
                         'instance_owner': hex(refs.get(instance, 'Engine.PhysicsAssetInstance.Owner')),
                         'frames_asleep': refs.get(mesh, 'Engine.SkeletalMeshComponent.FramesPhysicsAsleep')}
        out['bodies_count'] = len(bodies)
        out['bodies'] = [describe(refs, body, bones[i] if i < len(bones) else None)
                         for i, body in enumerate(bodies[:24])]
        if len(bodies) > 24:
            out['bodies_note'] = '只打印前 24 个 body；总数见 bodies_count'
        if args.impulse or args.locate or args.try_write:
            out['impulse'] = impulse_diff(bridge, refs, mesh, bodies, bones)
            out['read_only'] = not (args.impulse or args.try_write)
        if args.locate or args.try_write:
            root_index = out['target'].get('root_body_index') or 0
            out['locate'] = locate_velocity(bridge, refs, mesh, bodies, root_index)
            if args.try_write:
                out['try_write'] = try_write(bridge, refs, mesh, bodies, root_index,
                                             out['locate'])
                out['read_only'] = False
    except (SystemExit, OSError, RuntimeError, ValueError, struct.error) as error:
        out['error'] = '%s: %s' % (type(error).__name__, error)
    finally:
        if bridge is not None:
            bridge.close()
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + '\n', encoding='utf-8')
        print('written: %s' % args.output)
    else:
        print(text)
    return 0 if 'error' not in out else 1


if __name__ == '__main__':
    raise SystemExit(main())
