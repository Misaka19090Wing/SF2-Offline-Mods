"""Drag a ragdoll around with the use key, gravity-gun style.

The engine exposes AddImpulse(mesh, Impulse, Position, BoneName, bVelChange) and
this build ignores bVelChange (see punch.py), so the drag has to be built out of
ordinary impulses.  What makes the difference between "pulled along" and "twitch
like a landed fish" is the controller:

  * the target is a point in front of the *camera* (eye + forward * distance), so
    looking up lifts the body and looking down lowers it;
  * one impulse per control tick (default 60 Hz, not per 4 ms poll), so the
    physics has time to integrate between pushes;
  * a critically damped PD law in the velocity domain, i.e. the impulse asks for
    a velocity change, not for a position jump:
        accel  = kp * error - kd * velocity + gravity compensation
        impulse = mass * accel * dt
    with kd = 2*sqrt(kp) the body glides in instead of bouncing around the target;
  * the bone's mass is measured on the fly (see `_learn_mass`), because a calf
    and a pelvis need very different impulses for the same 30 cm/s.

Tuning knobs (config.json): drag_distance, drag_kp, drag_kd, drag_hz,
drag_max_speed, drag_max_accel, drag_mass, drag_impulse, drag_gravity,
drag_break, drag_lift.
"""
import math
import re
import struct
import time

from . import game

ADD_IMPULSE = 'Engine.PrimitiveComponent.AddImpulse'
BONE_NAME = 'Engine.SkeletalMeshComponent.GetBoneName'

GRAVITY = 980.0               # cm/s^2, UE3 default
MIN_PUSH = 4.0                # impulses below this only jitter the physics
MASS_RANGE = (1.0, 400.0)     # sanity clamp for the learned bone mass
CORE = re.compile(r'(Pelvis|Spine|Torso|Hips)', re.I)


def distance(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def length(v):
    return math.sqrt(sum(value * value for value in v))


class Drag:
    def __init__(self, bridge, api, config, emit=print):
        self.bridge = bridge
        self.config = config
        self.emit = emit
        self.api = api
        self.add_impulse = api.functions.get(ADD_IMPULSE)
        self.get_bone_name = api.functions.get(BONE_NAME)
        self.active = False
        self.actor = 0
        self.mesh = 0
        self.index = -1
        self.fname = (0, 0)
        self.label = ''
        self.mass = float(config.get('drag_mass', 20.0))
        self.mass_samples = 0
        self.core = None                 # cached index of the pelvis
        self.core_name = ''
        self.prev = None
        self.prev_at = 0.0
        self.velocity = (0.0, 0.0, 0.0)
        self.start = None
        self.started = 0.0
        self.ticks = 0
        self.calls = 0
        self.travelled = 0.0
        self.failures = 0
        self.error = 0.0
        self.far_at = 0.0
        self.fallback = False
        self.period = 1.0 / max(1.0, float(config.get('drag_hz', 60.0)))

    # ------------------------------------------------------------------ naming
    def name_of(self, mesh, index):
        """The bone's FName as '<name>#<number>', plus the raw FName pair."""
        if not self.get_bone_name or index < 0:
            return '', (0, 0)
        frame = struct.pack('<i', index) + b'\0' * 8
        try:
            out = self.bridge.invoke(mesh, self.get_bone_name, frame, timeout=2.0)
            name_index, number = struct.unpack_from('<2I', out, 4)
        except (OSError, struct.error):
            return '', (0, 0)
        try:
            text = self.bridge.name(name_index)
        except (OSError, RecursionError):
            text = f'name#{name_index}'
        return (f'{text}#{number}' if number else text), (name_index, number)

    def bone_label(self, index):
        return self.name_of(self.mesh, index)

    def center_index(self, mesh, points):
        """Index of the pelvis: the bone at the centre of mass.

        The atom under the crosshair is a hand or a head as often as not, and a
        push there is almost all torque -- the body flips out of the level
        instead of following you.  The pelvis is close to the centre of mass,
        so the same impulse becomes a (nearly) pure translation.  Bone names
        are identical for every instance of a skeleton, so the index is found
        once and re-checked with a single call afterwards.
        """
        if self.core is not None and self.core < len(points):
            label = self.name_of(mesh, self.core)[0]
            if label == self.core_name:
                return self.core
            self.core = None
        chosen = None
        for index in range(len(points)):
            label = self.name_of(mesh, index)[0] or ''
            if not CORE.search(label):
                continue
            near = 0 if 'pelvis' in label.lower() else 1
            if chosen is None or near < chosen[0]:
                chosen = (near, index, label)
            if near == 0:
                break
        if chosen is None:
            return None
        self.core, self.core_name = chosen[1], chosen[2]
        return self.core
    # ------------------------------------------------------------------- grab
    def grab(self, actor, mesh, index, bone, pawn=None):
        """Take hold of one bone of one corpse. Returns the log record."""
        if not self.add_impulse:
            raise SystemExit('cannot find Engine.PrimitiveComponent.AddImpulse')
        self.actor, self.mesh, self.index = actor, mesh, index
        label, self.fname = self.bone_label(index)
        self.label = label
        self.prev, self.prev_at = tuple(bone), time.monotonic()
        self.velocity = (0.0, 0.0, 0.0)
        self.start = tuple(bone)
        self.started = time.monotonic()
        self.ticks = self.calls = self.failures = 0
        self.travelled = 0.0
        self.error = 0.0
        self.far_at = 0.0
        self.fallback = False
        self.active = True
        return {'bone': index, 'bone_name': label or None,
                'mass': round(self.mass, 1),
                'lift': round(bone[2] - pawn[2], 1) if pawn and len(pawn) == 3 else 0.0}

    # ------------------------------------------------------------------- step
    def target_of(self, eye, forward):
        """Where the grabbed bone is being pulled to: in front of the camera."""
        span = length(forward) or 1.0
        reach = self.config['drag_distance']
        lift = self.config.get('drag_lift', 0.0)
        return (eye[0] + forward[0] / span * reach,
                eye[1] + forward[1] / span * reach,
                eye[2] + forward[2] / span * reach + lift)

    def step(self, eye, forward, pawn=None):
        """One control tick. Returns a record only when the drag ended."""
        if not self.active:
            return None
        now = time.monotonic()
        if now - self.prev_at < self.period:
            return None                     # let the physics integrate
        point = game.bone_world(self.bridge, self.mesh, self.index)
        if point is None:
            return self.release('mesh-lost')
        if pawn is None:
            return self.release('no-player')
        dt = now - self.prev_at
        travel = distance(point, self.prev)
        raw = tuple((point[i] - self.prev[i]) / dt for i in range(3))
        # At 60 Hz a physics bone's finite difference is noisy; damping on raw
        # noise is exactly what turned the old servo into a shaking fish.
        velocity = tuple(0.5 * raw[i] + 0.5 * self.velocity[i] for i in range(3))
        self.prev, self.prev_at = point, now
        self.velocity, self.ticks = velocity, self.ticks + 1
        self.travelled += travel
        # A finger or a toe often has no physics body of its own: the impulse
        # goes nowhere, the atom never moves, and the servo keeps pushing at
        # full strength forever.  After ~0.2 s of no motion at all, drop back
        # to the root body (no bone name), which is always simulated.
        if not self.fallback and self.ticks >= 12 and self.travelled < 1.0:
            self.fallback = True
            self.emit({'event': 'drag-rebone', 'bone': self.index,
                       'bone_name': self.label or None, 'to': 'root',
                       'why': 'bone did not move', 'cm': round(self.travelled, 2)})
            self.index, self.fname, self.label = 0, (0, 0), ''

        if eye is None or forward is None:
            return self.release('no-player')
        target = self.target_of(eye, forward)
        error = tuple(target[i] - point[i] for i in range(3))
        gap = length(error)
        self.error = gap
        break_reach = self.config['drag_break']
        if gap > break_reach:
            if not self.far_at:
                self.far_at = now
            elif now - self.far_at > 0.30:
                return self.release('too-far')
        else:
            self.far_at = 0.0

        kp = float(self.config['drag_kp'])
        kd = float(self.config['drag_kd'])
        accel = [kp * error[i] - kd * velocity[i] for i in range(3)]
        limit = float(self.config['drag_max_accel'])
        scale = length(accel)
        if scale > limit:
            accel = [value * limit / scale for value in accel]
        # Keep the *speed* the body is allowed to pick up while closing in.
        wanted = [value * dt for value in accel]
        vmax = float(self.config.get('drag_max_speed', 700.0)) * dt
        if length(wanted) > vmax:
            wanted = [value * vmax / length(wanted) for value in wanted]
        # Gravity is compensated *after* the speed clamp: it is not closing
        # a gap, and it must survive when the body hangs still on the target.
        wanted[2] += GRAVITY * float(self.config.get('drag_gravity', 1.0)) * dt
        impulse = [self.mass * value for value in wanted]
        cap = float(self.config['drag_impulse'])
        size = length(impulse)
        if size > cap:
            impulse = [value * cap / size for value in impulse]
            size = cap
        if size < MIN_PUSH:
            return None
        self._learn_mass(impulse, velocity, dt)

        frame = struct.pack('<3f3fII?3x', *impulse, *point,
                            self.fname[0], self.fname[1], False)
        try:
            self.bridge.invoke(self.mesh, self.add_impulse, frame, timeout=1.5)
        except OSError:
            self.failures += 1
            if self.failures >= 5:
                return self.release('invoke-failed')
            return None
        self.failures = 0
        self.calls += 1
        self.last_impulse = size
        return None

    # ------------------------------------------------------------------ mass
    def _learn_mass(self, impulse, velocity, dt):
        """Rough m = |J| / |dv| from the previous tick's push and its effect.

        Only used as a starting scale: the PD law is stable for a factor of two
        either way, and a wrong mass only shows up as "a bit sluggish" or "a bit
        eager".  Clamped, because a single tick can be dominated by contacts.
        """
        last = getattr(self, 'last_push', None)
        if not last:
            self.last_push = (tuple(impulse), tuple(velocity))
            return
        prev_impulse, prev_velocity = last
        delta = tuple(velocity[i] - prev_velocity[i] for i in range(3))
        dv = length(delta)
        self.last_push = (tuple(impulse), tuple(velocity))
        if dv < 5.0:
            return                          # blocked by the ground / a wall
        size = length(prev_impulse)
        if size < 1e-3:
            return
        # Only believe the measurement when the body went where it was pushed;
        # otherwise it is a joint or a contact doing the moving, not the impulse.
        dot = sum(prev_impulse[i] * delta[i] for i in range(3)) / (size * dv)
        if dot < 0.7:
            return
        estimate = size / dv
        if not (MASS_RANGE[0] <= estimate <= MASS_RANGE[1]):
            return
        # One tick is far too noisy to set the gain from, and a jump in mass is
        # felt as a jolt: never move it more than 15% per sample, then ease in.
        estimate = min(max(estimate, self.mass * 0.85), self.mass * 1.15)
        self.mass = 0.9 * self.mass + 0.1 * estimate
        self.mass_samples += 1

    # ---------------------------------------------------------------- release
    def release(self, reason):
        if not self.active:
            return {'reason': reason, 'seconds': 0.0, 'moved': 0.0, 'path': 0.0,
                    'ticks': 0, 'calls': 0}
        here = game.bone_world(self.bridge, self.mesh, self.index) or self.prev
        record = {'reason': reason,
                  'seconds': round(time.monotonic() - self.started, 2),
                  'moved': round(distance(here, self.start), 1) if here and self.start else 0.0,
                  'path': round(self.travelled, 1),
                  'ticks': self.ticks, 'calls': self.calls,
                  'mass': round(self.mass, 1),
                  'error': round(self.error, 1),
                  'bone': self.index, 'bone_name': self.label or None}
        self.active = False
        self.mesh = 0
        self.actor = 0
        self.prev = None
        self.last_push = None
        return record
