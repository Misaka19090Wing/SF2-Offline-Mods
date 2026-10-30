"""Offline unit tests for the regen rules. Needs no game.

    runtime\\python\\python.exe -X utf8 -B tools\\unit_test.py
"""
import json
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from sf2mod.game import Fields                      # noqa: E402
from sf2mod.regen import Regen, bot_targets, is_pve  # noqa: E402
import health_regen                                # noqa: E402


def tick(regen, start, end, health, maximum, step=0.1):
    """Feed `regen` every `step` seconds; return the last value it asked for."""
    out = None
    t = start
    while t <= end + 1e-9:
        value = regen.update(t, health, maximum)
        if value is not None:
            out = value
            health = value                          # a real client would have written it
        t += step
    return out, health


def first_write(regen, start, end, health, maximum, step=0.1):
    """Time and value of the first tick that asks for a write."""
    t = start
    while t <= end + 1e-9:
        value = regen.update(t, health, maximum)
        if value is not None:
            return round(t, 3), value
        t += step
    return None, None


def test_delay_gates_the_first_heal():
    r = Regen(delay=4.0, per_second=25.0)
    assert r.update(0.0, 40, 100) is None
    assert r.update(3.9, 40, 100) is None, '恢复必须在受伤后 4 秒才开始'
    value = r.update(4.1, 40, 100)
    assert value is not None and value > 40


def test_damage_restarts_the_delay():
    r = Regen(delay=4.0, per_second=25.0)
    tick(r, 0.0, 6.0, 100, 100)                     # full health: nothing happens
    r.update(10.0, 40, 100)                         # took damage at t=10
    assert r.update(13.8, 40, 100) is None
    assert r.update(14.2, 40, 100) is not None


def test_never_exceeds_max_and_never_lowers():
    r = Regen(delay=0.0, per_second=1000.0)
    r.update(0.0, 99, 100)
    assert r.update(0.1, 99, 100) == 100
    assert r.update(0.2, 100, 100) is None
    assert r.update(0.3, 120, 100) is None, '超过上限的读数不得被改写'
    assert r.update(0.4, 100, 0) is None, 'HealthMax 无效时不得写入'


def test_dead_pawn_is_never_healed():
    r = Regen(delay=4.0, per_second=1000.0)
    for step in range(21):                       # 2 s of being dead
        assert r.update(step * 0.1, 0, 100) is None, '死亡状态绝不能被写入'
    t, value = first_write(r, 2.1, 7.0, 50, 100)
    assert t is not None and t >= 4.0, '复活后要重新等满延迟，实际 %r' % (t,)


def test_slow_rate_accumulates_fractional_health():
    r = Regen(delay=0.0, per_second=0.5)
    r.update(0.0, 10, 100)
    written = 0
    for step in range(1, 41):
        t = step * 0.1
        value = r.update(t, 10 + written, 100)
        if value is not None:
            written += value - (10 + written)
    assert written == 2, '0.5 点/秒 在 4 秒内应只加 2 点，实际 %r' % written


def test_own_write_is_not_read_as_damage():
    r = Regen(delay=0.0, per_second=1000.0)
    r.update(0.0, 60, 100)
    assert r.update(0.1, 60, 100) == 100
    assert r.update(0.2, 100, 100) is None          # nothing left to do


def test_stall_does_not_produce_a_burst():
    r = Regen(delay=0.0, per_second=100.0)
    r.update(0.0, 10, 100)
    value = r.update(5.0, 10, 100)                  # 5 s gap (game paused)
    assert value is None or value <= 10 + 50, '停顿后不得一次性跳满'


def test_bad_reflection_is_rejected():
    r = Regen(delay=0.0, per_second=1000.0)
    assert r.update(0.0, 50, None) is None
    assert r.update(0.1, None, 100) is None
    assert r.update(0.2, 50, 10 ** 7) is None, '异常 HealthMax 不得写入'


def test_reset_forgets_the_previous_pawn():
    r = Regen(delay=0.0, per_second=1000.0)
    r.update(0.0, 10, 100)
    assert r.update(0.1, 10, 100) is not None
    r.reset(0.2)
    assert r.update(0.2, 10, 100) is None, '换 pawn 后要先重新计时'


def test_boot_wait_retries_instead_of_dying():
    class Cache:
        def cache_clear(self):
            pass

    class Booting:
        """GObjects empty for a while, then properties missing (version check)."""
        def __init__(self):
            self.calls = 0
            self.name = Cache()
            self.header = Cache()
            self.path = Cache()

        def read(self, address, size):
            if size == 12:
                return struct.pack('<3I', 0x10000000, 2000, 4000)   # sane name table
            return b'\0' * size

        def objects(self):
            self.calls += 1
            if self.calls < 3:
                raise ValueError('Unexpected object array')
            raise RuntimeError('客户端缺少属性：Engine.Pawn.Health')

    bridge = Booting()
    started = time.monotonic()
    message = ''
    try:
        Fields.wait(bridge, timeout=0.3, interval=0.01)
    except RuntimeError as error:
        message = str(error)
    assert 'Engine.Pawn.Health' in message, '属性缺失必须最终报错，不能静默'
    assert bridge.calls >= 3, '开机期必须重试'
    assert time.monotonic() - started < 2.0, '不能无限等待'


def test_bot_targets():
    """Only living teammates are healed; the player and unknown teams are not."""
    rows = [
        (0xA1, 0, 80, 100),     # teammate, hurt        -> heal
        (0xA2, 1, 80, 100),     # enemy                 -> no
        (0xA3, None, 80, 100),  # no PRI (PVE monster)  -> no
        (0xA4, 0, 0, 100),      # teammate but dead     -> no
        (0xA5, 0, 50, 0),       # bad reflection        -> no
        (0xA7, 0, 120, 100),    # health above max      -> no
        (0xA6, 0, 40, 100),     # player's own pawn     -> no
    ]
    picked = bot_targets(rows, player_pawn=0xA6, player_team=0)
    assert picked == [0xA1], picked
    assert bot_targets(rows, player_pawn=0xA6, player_team=None) == []
    assert bot_targets(rows, player_pawn=0xA6, player_team=1) == [0xA2]
    assert bot_targets([], player_pawn=0xA6, player_team=0) == []
    print('[OK ] bots: only living teammates are healed')


def test_pve_predicate():
    assert is_pve('SFSurvivalGame_0') and is_pve('SFZombieSurvivalGame_1')
    assert is_pve('SFDefenceGame_2')
    assert not is_pve('SFAITeamDeathGame_0') and not is_pve('') and not is_pve(None)
    print('[OK ] pve: survival/defence are PVE, team deathmatch is not')


def _bots_worker(config, log):
    """A Worker with a stubbed Player/Fields: enough to drive step_bots offline."""
    return health_regen.Worker(Path('.'), dict(health_regen.DEFAULTS, **config), log)


class _Log:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


class _Fields:
    def __init__(self, rows, teams):
        self.rows, self.teams, self.written = rows, teams, {}

    def read(self, address):
        return self.rows[address]

    def team_of(self, address):
        return self.teams.get(address)

    def world(self):
        return 0x1234

    def game_of(self, world):
        return 0x999

    def write(self, address, value):
        self.written[address] = value


class _Player:
    def __init__(self, addresses, game_class='SFSurvivalGame_0'):
        self.addresses, self.game_class = addresses, game_class

    def actors(self):
        return self.addresses

    def kind(self, address):
        return self.game_class if address == 0x999 else 'SFPawn'


def _drive(config, game_class='SFSurvivalGame_0'):
    log = _Log()
    worker = _bots_worker(dict(bots=True, dry_run=False, regen_delay_seconds=0.0,
                               regen_per_second=50.0, **config), log)
    worker.pawn = 0xA6
    fields = _Fields({0xA1: (30, 100), 0xA2: (30, 100), 0xA6: (100, 100)},
                     {0xA1: 0, 0xA2: 1, 0xA6: 0})
    player = _Player([0xA1, 0xA2, 0xA6], game_class)
    worker.step_bots(player, fields, 1000.0)
    worker.last_bot_scan = 0.0
    worker.step_bots(player, fields, 1000.2)
    return fields, log


def test_bots_pass_runs():
    """step_bots must execute and heal exactly the teammates."""
    fields, log = _drive({'bots_pve_only': False})
    assert 0xA1 in fields.written and fields.written[0xA1] > 30, fields.written
    assert 0xA2 not in fields.written, 'the other team was healed'
    assert 0xA6 not in fields.written, 'the player was healed by the bots pass'
    assert any(event == 'regen-bot' for event, _ in log.events), log.events
    print('[OK ] bots: PVE gate off -> only teammates are healed')


def test_bots_pve_gate():
    """bots_pve_only keeps teammates unhealed outside the PVE modes."""
    fields, log = _drive({'bots_pve_only': True}, game_class='SFAITeamDeathGame_0')
    assert not fields.written, fields.written
    scan = [f for e, f in log.events if e == 'bots-scan']
    assert scan and scan[-1]['pve'] is False, scan
    fields, _ = _drive({'bots_pve_only': True}, game_class='SFSurvivalGame_0')
    assert 0xA1 in fields.written, 'PVE mode should heal teammates'
    print('[OK ] bots: bots_pve_only heals teammates in PVE only')


def test_config_matches_defaults():
    config = json.loads((ROOT / 'config.json').read_text('utf-8'))
    assert set(config) == set(health_regen.DEFAULTS), 'config.json 与 DEFAULTS 的键必须一致'
    assert config['regen_delay_seconds'] >= 0
    assert config['regen_per_second'] > 0
    assert config['tick_hz'] > 0
    assert health_regen.VERSION.count('.') == 2


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith('test_')]
    for test in tests:
        test()
    print('all good: %d regen rule groups behave here.' % len(tests))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
