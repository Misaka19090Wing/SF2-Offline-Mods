"""Offline checks for drag.py and keys.py.

No game process is touched -- a fake bridge stands in for the game, so this can
run anywhere the patch lives:

    python tools\\unit_test.py
"""
import struct
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from sf2mod import drag as drag_mod, keys as keys_mod  # noqa: E402

ADD_IMPULSE = drag_mod.ADD_IMPULSE
GET_NAME = drag_mod.BONE_NAME
ADDRESSES = {ADD_IMPULSE: 0x1000, GET_NAME: 0x2000}   # what api.functions holds
FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'[OK ] {label}')
    else:
        print(f'[FAIL] {label} {detail}')
        FAILURES.append(label)


class FakeBridge:
    def __init__(self):
        self.calls = []

    def invoke(self, obj, function, frame, timeout=1.5):
        self.calls.append((obj, function, bytes(frame)))
        if function == ADDRESSES[GET_NAME]:
            index = struct.unpack_from('<i', frame, 0)[0]
            return struct.pack('<iII', index, 0x1234, 0)
        return b''

    def name(self, index):
        return 'Bip01-Spine'

    def pushes(self):
        return [call[2] for call in self.calls if call[1] == ADDRESSES[ADD_IMPULSE]]


def config(**over):
    base = {'drag_distance': 200.0, 'drag_lift': 0.0, 'drag_hz': 50.0,
            'drag_kp': 20.0, 'drag_kd': 9.0, 'drag_max_speed': 700.0,
            'drag_max_accel': 2500.0, 'drag_mass': 20.0, 'drag_impulse': 900.0,
            'drag_gravity': 1.0, 'drag_break': 600.0, 'drag_key': 0x45,
            'drag_reach': 300.0}
    base.update(over)
    return base


def make_api():
    return types.SimpleNamespace(functions=dict(ADDRESSES))


def fake_bone(points):
    """Patch bone_world to return (and, on each call, advance through) points."""
    state = {'point': tuple(points[0]), 'rest': list(points[1:])}

    def read(bridge, mesh, index, limit=180):
        return state['point']

    return state, read


def tick(drag, bridge, eye, forward, pawn=(0.0, 0.0, 0.0), dt=0.02):
    """Run one control tick: backdate the clock so the rate limiter opens.

    Backdate by one period plus a microsecond: `step` compares
    `now - prev_at < period`, and subtracting exactly one period can round to 3e-12
    *under* it at these magnitudes, which silently closes the gate. A larger margin
    would change the dt the servo maths sees, and the assertions encode dt=0.02.
    """
    drag.prev_at -= dt + 1e-6
    return drag.step(eye, forward, pawn)


def test_grab_and_push():
    bridge = FakeBridge()
    drag = drag_mod.Drag(bridge, make_api(), config())
    state, reader = fake_bone([(0.0, 0.0, 50.0)])
    original = drag_mod.game.bone_world
    drag_mod.game.bone_world = reader
    try:
        record = drag.grab(0xAAAA, 0xBBBB, 7, (0.0, 0.0, 50.0), (0.0, 0.0, 0.0))
        check('grab: bone name resolved', record['bone_name'] == 'Bip01-Spine', record)
        check('grab: lift kept from the pawn', record['lift'] == 50.0, record)
        check('grab: FName cached', drag.fname == (0x1234, 0), drag.fname)

        tick(drag, bridge, (0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        frame = bridge.pushes()[-1]
        impulse = struct.unpack_from('<3f', frame, 0)
        position = struct.unpack_from('<3f', frame, 12)
        name = struct.unpack_from('<2I', frame, 24)
        flag = frame[32]
        check('push: frame is 36 bytes', len(frame) == 36, len(frame))
        check('push: aims forward (+y)', impulse[1] > 50 and abs(impulse[0]) < 1,
              [round(v, 1) for v in impulse])
        check('push: in front of the camera, not the feet',
              150 < impulse[1] < 900, impulse)
        check('push: applied at the bone', position == (0.0, 0.0, 50.0), position)
        check('push: aimed at that bone', name == (0x1234, 0), name)
        check('push: bVelChange stays off', flag == 0, flag)

        # The rate limiter: a second call inside the same period does nothing.
        before = len(bridge.pushes())
        drag.step((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 0.0))
        check('rate: one impulse per control tick', len(bridge.pushes()) == before)

        # Sitting on the target: only the gravity term is left.
        state['point'] = (0.0, 200.0, 0.0)
        drag.prev = (0.0, 200.0, 0.0)                 # already still on the target
        tick(drag, bridge, (0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        impulse = struct.unpack_from('<3f', bridge.pushes()[-1], 0)
        check('hold: gravity compensated, no sideways pull',
              abs(impulse[2] - 20.0 * 980.0 * 0.02) < 1.0 and abs(impulse[1]) < 1.0,
              [round(v, 1) for v in impulse])
    finally:
        drag_mod.game.bone_world = original


def test_vertical_look():
    bridge = FakeBridge()
    drag = drag_mod.Drag(bridge, make_api(), config())
    state, reader = fake_bone([(0.0, 0.0, 0.0)])
    original = drag_mod.game.bone_world
    drag_mod.game.bone_world = reader
    try:
        drag.grab(0xAAAA, 0xBBBB, 7, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        drag.config['drag_gravity'] = 0.0                 # isolate the aim
        tick(drag, bridge, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0))   # looking straight up
        impulse = struct.unpack_from('<3f', bridge.pushes()[-1], 0)
        check('aim: looking up lifts the body', impulse[2] > 100 and abs(impulse[1]) < 1,
              [round(v, 1) for v in impulse])

        # Looking down must ask for a downward pull on a body above the aim.
        state['point'] = (0.0, 0.0, 300.0)
        drag.prev_at -= 0.02 + 1e-6
        drag.step((0.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 0.0, 0.0))
        impulse = struct.unpack_from('<3f', bridge.pushes()[-1], 0)
        check('aim: looking down lowers it', impulse[2] < -100,
              [round(v, 1) for v in impulse])
    finally:
        drag_mod.game.bone_world = original


def test_damping():
    bridge = FakeBridge()
    drag = drag_mod.Drag(bridge, make_api(), config(drag_gravity=0.0))
    state, reader = fake_bone([(0.0, 0.0, 0.0)])
    original = drag_mod.game.bone_world
    drag_mod.game.bone_world = reader
    try:
        drag.grab(0xAAAA, 0xBBBB, 7, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        # Body sits on the target but flies at 900 cm/s: the servo must brake.
        drag.prev = (0.0, 200.0 - 900.0 * 0.02, 0.0)
        state['point'] = (0.0, 200.0, 0.0)
        tick(drag, bridge, (0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        impulse = struct.unpack_from('<3f', bridge.pushes()[-1], 0)
        check('damping: overspeed is braked, not fed', impulse[1] < -50,
              [round(v, 1) for v in impulse])
    finally:
        drag_mod.game.bone_world = original


def test_mass_learning():
    bridge = FakeBridge()
    drag = drag_mod.Drag(bridge, make_api(), config(drag_gravity=0.0, drag_mass=20.0))
    original = drag_mod.game.bone_world
    drag_mod.game.bone_world = lambda b, mesh, index, limit=180: (0.0, 0.0, 0.0)
    try:
        drag.grab(0xAAAA, 0xBBBB, 7, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        # Tell the learner "an impulse of 200 gave 20 cm/s": m ≈ 10.
        drag.last_push = ((0.0, 200.0, 0.0), (0.0, 0.0, 0.0))
        drag._learn_mass((0.0, 0.0, 0.0), (0.0, 20.0, 0.0), 0.02)
        check('mass: learned towards J/dv', 10.0 < drag.mass < 20.0, drag.mass)

        drag.mass = 20.0
        drag.last_push = ((0.0, 200.0, 0.0), (0.0, 0.0, 0.0))
        drag._learn_mass((0.0, 0.0, 0.0), (0.2, 0.0, 0.0), 0.02)   # 1 cm/s: contact
        check('mass: a wall does not teach it', drag.mass == 20.0, drag.mass)

        # Pushed +y but the body slid +x: a joint moved it, not the impulse.
        drag.last_push = ((0.0, 200.0, 0.0), (0.0, 0.0, 0.0))
        drag._learn_mass((0.0, 0.0, 0.0), (20.0, 0.0, 0.0), 0.02)
        check('mass: sideways motion is not the impulse', drag.mass == 20.0, drag.mass)
    finally:
        drag_mod.game.bone_world = original


def test_center_bone():
    class Named(FakeBridge):
        MAP = {0: 'root', 1: 'Bip01-L-Hand', 2: 'Bip01-Pelvis',
               3: 'Bip01-Head', 4: 'Bip01-Spine'}

        def invoke(self, obj, function, frame, timeout=1.5):
            if function == ADDRESSES[GET_NAME]:
                self.calls.append((obj, function, bytes(frame)))
                index = struct.unpack_from('<i', frame, 0)[0]
                return struct.pack('<iII', index, index, 0)
            return super().invoke(obj, function, frame, timeout)

        def name(self, index):
            return self.MAP.get(index, 'bone')

    bridge = Named()
    drag = drag_mod.Drag(bridge, make_api(), config())
    points = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 0.0, 0.0),
              (3.0, 0.0, 0.0), (4.0, 0.0, 0.0)]
    check('center: the pelvis is the bone it pulls',
          drag.center_index(0xC000, points) == 2, drag.core)
    check('center: the pelvis is remembered by name',
          drag.core_name == 'Bip01-Pelvis', drag.core_name)
    before = len(bridge.calls)
    check('center: a second grab re-checks with one call',
          drag.center_index(0xC000, points) == 2
          and len(bridge.calls) - before == 1, len(bridge.calls) - before)
    check('center: other skeletons reuse the index',
          drag.center_index(0xD000, points) == 2)


def test_mass_jolt():
    bridge = FakeBridge()
    drag = drag_mod.Drag(bridge, make_api(), config())
    drag.mass = 20.0
    drag.last_push = ((0.0, 0.0, 100.0), (0.0, 0.0, 0.0))
    drag._learn_mass((0.0, 0.0, 100.0), (0.0, 0.0, 1.0), 0.02)
    check('mass: one crazy sample cannot double it', drag.mass <= 20.0, drag.mass)


def test_rebone():
    bridge = FakeBridge()
    records = []
    drag = drag_mod.Drag(bridge, make_api(), config(), emit=records.append)
    original = drag_mod.game.bone_world
    drag_mod.game.bone_world = lambda b, mesh, index, limit=180: (0.0, 0.0, 50.0)
    try:
        drag.grab(0xAAAA, 0xBBBB, 9, (0.0, 0.0, 50.0), (0.0, 0.0, 0.0))
        for _ in range(12):                       # a bone that never moves
            tick(drag, bridge, (0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        check('rebone: a dead bone falls back to the root body',
              drag.fallback and drag.index == 0 and drag.fname == (0, 0), drag.index)
        check('rebone: logged once',
              len([r for r in records if r.get('event') == 'drag-rebone']) == 1, records)
        before = len(records)
        tick(drag, bridge, (0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        check('rebone: does not keep logging',
              len([r for r in records[before:] if r.get('event') == 'drag-rebone']) == 0)
    finally:
        drag_mod.game.bone_world = original


def test_break_and_release():
    bridge = FakeBridge()
    drag = drag_mod.Drag(bridge, make_api(), config())
    original = drag_mod.game.bone_world
    drag_mod.game.bone_world = lambda b, mesh, index, limit=180: (0.0, 0.0, 50.0)
    try:
        drag.grab(0xAAAA, 0xBBBB, 3, (0.0, 0.0, 50.0), (0.0, 0.0, 0.0))
        tick(drag, bridge, (0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        check('push: one call counted', drag.calls == 1, drag.calls)

        # Way off target: the first tick only starts the timer.
        ended = tick(drag, bridge, (0.0, 5000.0, 0.0), (0.0, 1.0, 0.0))
        check('break: not yet released', ended is None and drag.active)
        drag.far_at = time.monotonic() - 0.31
        ended = tick(drag, bridge, (0.0, 5000.0, 0.0), (0.0, 1.0, 0.0))
        check('break: released after 0.3 s', ended and ended['reason'] == 'too-far', ended)
        check('break: inactive afterwards', not drag.active)

        record = drag.release('toggled')
        check('release: idle release is harmless', record['moved'] == 0.0, record)

        drag.grab(0xAAAA, 0xBBBB, 3, (0.0, 0.0, 50.0), (0.0, 0.0, 0.0))
        record = drag.release('toggled')
        check('release: reason kept', record['reason'] == 'toggled', record)
        check('release: mass reported', record['mass'] > 0, record)
    finally:
        drag_mod.game.bone_world = original


class KeyBridge:
    def __init__(self):
        self.word = 0

    def read(self, address, size):
        return struct.pack('<I', self.word)


def test_toggle_drag():
    """The E key path end to end: worker -> grab -> log record.

    This is the seam that once raised NameError inside the poll loop and took
    the whole worker down when the key was pressed, so it is exercised here.
    """
    import corpse_flinch

    records = []
    bridge = FakeBridge()
    servo = drag_mod.Drag(bridge, make_api(), config())
    worker = object.__new__(corpse_flinch.Worker)
    worker.config = config()
    worker.drag = servo
    worker.bridge = bridge
    worker.keys = types.SimpleNamespace(count=1, word=0x40)
    worker.player = types.SimpleNamespace(find=lambda: 0x1000, pawn=lambda: 0x2000)
    worker.pawns = types.SimpleNamespace(live={0xA0: 'SFCPawn'},
                                         is_corpse=lambda a: True)
    worker.name_of = lambda address: 'SFCPawn'
    worker.pawn_location = lambda: (0.0, 0.0, 0.0)
    worker.emit = lambda **fields: records.append(fields)
    points = [(0.0, 0.0, 100.0)] * 5
    worker.corpse_meshes = lambda force=False: [(0xA0, 0xB0, points)]
    worker.nearest_bone = lambda point, corpses: (100.0, 0xA0, 0xB0, 4, points[4])
    original = corpse_flinch.game.camera
    corpse_flinch.game.camera = lambda b, c: ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    try:
        worker.toggle_drag('test')
        start = records[-1]
        check('key: pressing E starts a drag',
              start.get('event') == 'drag-start', start)
        check('key: both the aim and the held bone are logged',
              start.get('gap') == 100.0 and start.get('held_at') == 100.0, start)
        check('key: the worker survives a press (no exception)', servo.active)

        worker.corpse_meshes = lambda force=False: []
        worker.toggle_drag('test')
        check('key: a second press lets go',
              records[-1].get('event') == 'drag-end'
              and records[-1].get('reason') == 'toggled', records[-1])

        worker.nearest_bone = lambda point, corpses: (900.0, 0xA0, 0xB0, 4, points[4])
        worker.toggle_drag('test')
        check('key: out of reach is logged as drag-none',
              records[-1].get('event') == 'drag-none' and records[-1].get('near') == 900.0,
              records[-1])
    finally:
        corpse_flinch.game.camera = original


def test_keys():
    bridge = KeyBridge()
    keys = keys_mod.Keys(bridge, {'drag_key': 0x45})
    keys.os_state = lambda: False                     # pretend E is idle
    check('keys: idle', keys.poll(0x1000) == [])

    bridge.word = keys_mod.USED_KEY                   # bPressingUsedKey down
    check('keys: native press is a tap', keys.poll(0x1000) == ['game'])
    check('keys: the raw word is kept for the log', keys.word == keys_mod.USED_KEY)
    check('keys: held key is not a second tap', keys.poll(0x1000) == [])
    bridge.word = 0
    check('keys: release', keys.poll(0x1000) == [])

    keys.last -= 1.0                                  # let the debounce expire
    bridge.word = keys_mod.USED_KEY
    check('keys: second press works', keys.poll(0x1000) == ['game'])

    bridge.word = keys_mod.USED_KEY | keys_mod.IGNORE_INPUT
    keys.last -= 1.0
    keys.native = False
    check('keys: bIgnoreInput swallows it', keys.poll(0x1000) == [])

    # The OS fallback only exists until the game flag has proven itself.
    keys = keys_mod.Keys(KeyBridge(), {'drag_key': 0x45})
    down = {'value': True}
    keys.os_state = lambda: down['value']
    check('keys: os fallback fires first', keys.poll(0x1000) == ['os'])
    keys.last -= 1.0
    keys.native_ok = True
    down['value'] = False
    keys.poll(0x1000)
    down['value'] = True
    check('keys: os ignored once the game answers', keys.poll(0x1000) == [])


if __name__ == '__main__':
    test_grab_and_push()
    test_vertical_look()
    test_damping()
    test_mass_learning()
    test_center_bone()
    test_mass_jolt()
    test_rebone()
    test_break_and_release()
    test_toggle_drag()
    test_toggle_drag()
    test_keys()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} failed: ' + ', '.join(FAILURES))
        raise SystemExit(1)
    print('all good: servo and key detection behave here.')
