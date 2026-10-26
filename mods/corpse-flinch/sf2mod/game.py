"""Game memory layout: where the pawns, the player and the ragdolls live.

Every constant below was measured on the shipped client (build hash checked by the
runtime's Reader).  If the game is patched, these are the values to re-measure --
see docs/02-数据结构与偏移.md for how each one was found.
"""
import math
import struct

# --- global roots ---------------------------------------------------------
GWORLD = 0x179B2F4          # UWorld* of the current world
OBJECTS = 0x17882D0         # GObjects TArray (fallback only)
LEVEL = 0x50                # UWorld.PersistentLevel
ACTORS = 0x3C               # ULevel.Actors TArray

# --- actor / pawn fields --------------------------------------------------
LOCATION = 0x54             # AActor.Location       (FVector)
HEALTH = 0x338              # APawn.Health          (int)
PAWN = 0x228                # AController.Pawn
PLAYER = 0x3D4              # APlayerController.Player
CAMERA = 0x3D8              # APlayerController.PlayerCamera (ACamera*)
ROTATION = 0x60             # AActor.Rotation (FRotator); camera uses it too
MESH = 0x3CC                # APawn.Mesh            (USkeletalMeshComponent*)
WEAPON = 0x40C              # ASFPawn.Weapon        (ASFWeapon*)
AMMO_MGZ = 0x358            # ASFWeapon.AmmoCountInMgz (int)

# --- skeletal mesh --------------------------------------------------------
MESH_MATRIX = 0x170         # USkeletalMeshComponent.LocalToWorld (FMatrix)
MESH_ASSET = 0x1F8          # USkeletalMeshComponent.SkeletalMesh
SPACEBASES = 0x230          # USkeletalMeshComponent.SpaceBases (TArray<FBoneAtom>)

# --- the weapon's own hit record (see sf2mod/signals.py) -------------------
IMPACT_INFOS = 0xC90
PENETRATION_INFOS = 0xC9C
DAMAGE_SCALE = 0xCA8

# --- melee swing and projectiles ------------------------------------------
DOING_EXIGENCY = 0xED8      # ASFPawn_Player.DoingExigencyAttack (BoolProperty)
EXIGENCY_BIT = 8            # bit 3 of that byte while the F swing is running
VELOCITY = 0x180            # AActor.Velocity (FVector)
PROJECTILE_PREFIXES = ('SFCProjectile', 'SFProj_')

PAWN_PREFIX = ('SFPawn', 'SFCPawn', 'Pawn', 'SFCharacter')
SKIP_WORDS = ('BlockingVolume', 'SoundGroup', 'FX')
ACTOR_REFRESH = 100         # actor slots are recycled; drop cached class names


def u32(bridge, address):
    return struct.unpack('<I', bridge.read(address, 4))[0]


def i32(bridge, address):
    return struct.unpack('<i', bridge.read(address, 4))[0]


def matrix_translation(bridge, mesh):
    """World position of a skeletal mesh, or None."""
    if mesh < 0x10000:
        return None
    try:
        raw = bridge.read(mesh + MESH_MATRIX, 64)
    except OSError:
        return None
    return struct.unpack_from('<3f', raw, 48)


def vector(bridge, address):
    """An FVector stored at `address`, or None."""
    try:
        return struct.unpack('<3f', bridge.read(address, 12))
    except OSError:
        return None


def bones(bridge, mesh, limit=180):
    """World position of every bone of a skeletal mesh.

    SpaceBases holds one FBoneAtom (32 bytes, translation at +16) per bone in
    component space, so each point is multiplied by LocalToWorld.  Bone 0 is the
    root and sits at the mesh origin; the rest follow the ragdoll exactly.
    """
    if mesh < 0x10000:
        return []
    try:
        matrix = struct.unpack('<16f', bridge.read(mesh + MESH_MATRIX, 64))
        pointer, count, capacity = struct.unpack(
            '<3I', bridge.read(mesh + SPACEBASES, 12))
        if not 0 < count <= capacity < limit or pointer < 0x10000:
            return []
        raw = bridge.read(pointer, 32 * count)
    except OSError:
        return []
    out = []
    for index in range(count):
        x, y, z = struct.unpack_from('<3f', raw, 32 * index + 16)
        out.append((x * matrix[0] + y * matrix[4] + z * matrix[8] + matrix[12],
                    x * matrix[1] + y * matrix[5] + z * matrix[9] + matrix[13],
                    x * matrix[2] + y * matrix[6] + z * matrix[10] + matrix[14]))
    return out


def bone_world(bridge, mesh, index, limit=180):
    """World position of one bone (reads a single atom, not the whole skeleton)."""
    if mesh < 0x10000 or index < 0:
        return None
    try:
        matrix = struct.unpack('<16f', bridge.read(mesh + MESH_MATRIX, 64))
        pointer, count, capacity = struct.unpack(
            '<3I', bridge.read(mesh + SPACEBASES, 12))
        if not 0 < count <= capacity < limit or pointer < 0x10000 or index >= count:
            return None
        x, y, z = struct.unpack('<3f', bridge.read(pointer + 32 * index + 16, 12))
    except OSError:
        return None
    return (x * matrix[0] + y * matrix[4] + z * matrix[8] + matrix[12],
            x * matrix[1] + y * matrix[5] + z * matrix[9] + matrix[13],
            x * matrix[2] + y * matrix[6] + z * matrix[10] + matrix[14])


def bone_count(bridge, mesh, limit=180):
    """How many bones the mesh currently reports (0 when it is not readable)."""
    if mesh < 0x10000:
        return 0
    try:
        pointer, count, capacity = struct.unpack(
            '<3I', bridge.read(mesh + SPACEBASES, 12))
    except OSError:
        return 0
    if not 0 < count <= capacity < limit or pointer < 0x10000:
        return 0
    return count


def camera(bridge, controller):
    """(location, forward) of the player's camera, or (None, None)."""
    if controller < 0x10000:
        return None, None
    try:
        view = u32(bridge, controller + CAMERA)
    except OSError:
        return None, None
    if view < 0x10000:
        return None, None
    location = vector(bridge, view + LOCATION)
    try:
        pitch, yaw, _ = struct.unpack('<3i', bridge.read(view + ROTATION, 12))
    except OSError:
        return location, None
    pitch = pitch / 65536.0 * 6.283185307179586
    yaw = yaw / 65536.0 * 6.283185307179586
    cos_pitch = math.cos(pitch)
    return location, (cos_pitch * math.cos(yaw),
                      cos_pitch * math.sin(yaw),
                      math.sin(pitch))


def is_skeletal_mesh(bridge, mesh):
    """A live USkeletalMeshComponent (the only thing AddImpulse accepts here)."""
    if mesh < 0x10000 or mesh % 4:
        return False
    try:
        bridge.header(mesh)
        return 'SkeletalMeshComponent' in bridge.path(mesh)
    except (OSError, RecursionError):
        return False


class Pawns:
    """The persistent level's actor list, filtered down to pawns.

    One bulk read returns the whole level (a few hundred actors), so the live set
    is rebuilt on every poll.  An incremental GObjects cursor went blind once the
    object count dropped after a map change -- every address stayed "seen" while
    the array it came from had already been rebuilt -- which is why a brand new
    match once reported no pawns at all.  GObjects remains only as a fallback for
    the rare pawn that is not in the persistent level.
    """

    def __init__(self, bridge):
        self.bridge = bridge
        self.world = 0
        self.live = {}                 # actor address -> class name
        self.kinds = {}                # actor address -> class name, cached
        self.polls = 0
        self.level_addr = 0            # persistent level the kind cache belongs to
        self.seen = set()              # GObjects fallback cursor
        self.next_index = 0
        self.slots = set()             # actor addresses seen in the previous poll

    def level(self):
        world = u32(self.bridge, GWORLD)
        if world < 0x10000:
            return 0, []
        level = u32(self.bridge, world + LEVEL)
        if level != self.level_addr:
            # A new level reuses addresses: every cached class name is suspect.
            self.level_addr = level
            self.kinds.clear()
        if level < 0x10000:
            return world, []
        pointer, count, capacity = struct.unpack('<3I', self.bridge.read(level + ACTORS, 12))
        if not 0 < count <= capacity < 100000:
            return world, []
        raw = self.bridge.read(pointer, 4 * count)
        return world, [struct.unpack_from('<I', raw, 4 * i)[0] for i in range(count)]

    def kind(self, address):
        cached = self.kinds.get(address)
        if cached is not None:
            return cached
        try:
            name = self.bridge.object_name(self.bridge.header(address)[13])
        except (OSError, RecursionError):
            return None
        self.kinds[address] = name
        return name

    def poll(self):
        self.polls += 1
        try:
            world, actors = self.level()
        except OSError:
            return
        if world and world != self.world:
            self.world = world
            self.kinds.clear()
            self.live = {}
            self.seen.clear()
            self.next_index = 0
        if self.polls % ACTOR_REFRESH == 0:
            self.kinds.clear()
        # A slot that was empty last poll and holds an actor now is a brand new
        # object reusing a freed address.  The cached name there belongs to the
        # previous occupant, so a freshly thrown tomahawk could be invisible for
        # the whole second it was in the air.
        for address in actors:
            if address not in self.slots:
                self.kinds.pop(address, None)
        self.slots = set(actors)
        live = {}
        for address in actors:
            if address < 0x10000:
                continue
            name = self.kind(address)
            if not name or 'Default' in name or any(w in name for w in SKIP_WORDS):
                continue
            if name.startswith(PAWN_PREFIX):
                live[address] = name
        self.live = live
        if not actors:
            self.poll_objects()

    def poll_objects(self):
        try:
            pointer, count, capacity = struct.unpack('<3I', self.bridge.read(OBJECTS, 12))
        except OSError:
            return
        if not 0 < count <= capacity < 2000000:
            return
        if count < self.next_index:
            self.seen.clear()
            self.kinds.clear()
            self.next_index = 0
        bulk = min(count - self.next_index, 4096)
        if bulk <= 0:
            return
        try:
            raw = self.bridge.read(pointer + 4 * self.next_index, bulk * 4)
        except OSError:
            return
        for step in range(bulk):
            address = struct.unpack_from('<I', raw, step * 4)[0]
            if address < 0x10000 or address in self.seen:
                continue
            self.seen.add(address)
            name = self.kind(address)
            if not name or 'Default' in name or any(w in name for w in SKIP_WORDS):
                continue
            if name.startswith(PAWN_PREFIX):
                self.live[address] = name
        self.next_index += bulk

    # ------------------------------------------------------------------ views
    def corpses(self):
        return {a: n for a, n in self.live.items() if self.is_corpse(a)}

    def is_corpse(self, address):
        try:
            return i32(self.bridge, address + HEALTH) <= 0 and \
                is_skeletal_mesh(self.bridge, u32(self.bridge, address + MESH))
        except OSError:
            return False


class Player:
    """The local player controller and whatever it is holding."""

    def __init__(self, bridge, pawns):
        self.bridge = bridge
        self.pawns = pawns
        self.controller = 0

    def find(self):
        if self.controller:
            try:
                if u32(self.bridge, self.controller + PAWN) > 0x10000:
                    return self.controller
            except OSError:
                pass
            self.controller = 0
        for address in self._actors():
            try:
                if not (self.pawns.kind(address) or '').endswith('PlayerController'):
                    continue
                if u32(self.bridge, address + PLAYER) > 0x10000 \
                        and u32(self.bridge, address + PAWN) > 0x10000:
                    self.controller = address
                    return address
            except (OSError, RecursionError):
                continue
        return 0

    def _actors(self):
        try:
            return self.pawns.level()[1]
        except OSError:
            return []

    def pawn(self):
        controller = self.find()
        if not controller:
            return 0
        try:
            return u32(self.bridge, controller + PAWN)
        except OSError:
            return 0

    def weapon(self):
        pawn = self.pawn()
        if not pawn:
            return 0
        try:
            weapon = u32(self.bridge, pawn + WEAPON)
        except OSError:
            return 0
        return weapon if weapon > 0x10000 else 0

    def ammo(self, weapon):
        try:
            return i32(self.bridge, weapon + AMMO_MGZ)
        except OSError:
            return None
