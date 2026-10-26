"""corpse-limit: control how many bodies the map keeps.

Three rules, all from config.json, all resolved without touching a game file:

    lifetime_seconds  a body is retired once it has been on the map this long
    max_distance      a body farther than this from the local player is retired
    max_corpses       beyond this many living bodies, the oldest ones go first

Retiring writes `Engine.Actor.LifeSpan = 0.0001`; the engine decrements it on its
next world tick and destroys the actor itself. No ProcessEvent call, no bridge, so
this mod coexists with every native-AI worker and with other memory-only mods.
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from sf2mod import attach, limit                      # noqa: E402
from sf2mod.game import Fields, Player                # noqa: E402

VERSION = '1.0.1'
DEFAULTS = {
    'enabled': True,
    'lifetime_seconds': 60.0,      # 0 = never retire on age
    'max_distance': 0.0,           # cm; 0 = keep distant bodies
    'max_corpses': 24,             # 0 = no cap; oldest go first above it
    'poll_seconds': 0.5,
    'dry_run': False,
}
BOOT_GRACE = 3600.0
STATUS_INTERVAL = 10.0


def load_config(path=None):
    config = dict(DEFAULTS)
    file = Path(path) if path else HERE / 'config.json'
    if file.is_file():
        config.update(json.loads(file.read_text('utf-8')))
    unknown = set(config) - set(DEFAULTS)
    if unknown:
        raise SystemExit('config.json 有未知项：' + ', '.join(sorted(unknown)))
    for key in ('lifetime_seconds', 'max_distance', 'max_corpses', 'poll_seconds'):
        value = config[key]
        if not isinstance(value, (int, float)) or value < 0:
            raise SystemExit(f'config.json 的 {key} 必须是非负数')
    return config


class Log:
    """One JSON object per line, the same style as the other mods' logs."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event, **fields):
        record = {'event': event, 'time': round(time.time(), 3)}
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False)
        try:
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(line + '\n')
        except OSError:
            pass
        print(line, flush=True)


def is_corpse(player, fields, address):
    """A dead pawn whose body is still on the map (health 0, HealthMax sane)."""
    try:
        health, maximum = fields.read(address)
        if maximum <= 0 or health > 0:
            return False
        # Already handed over by us (or the game): the timer is under a tick from
        # firing, and re-writing it every poll would only spam the log.
        span = fields.life_span_of(address)
        if 0 < span < 0.01:
            return False
        return 'Pawn' in player.kind(address)
    except (OSError, RecursionError):
        return False


class Worker:
    def __init__(self, bridge, pid, config, log, dry_run=False):
        self.bridge = bridge
        self.pid = pid
        self.config = config
        self.log = log
        self.dry = dry_run
        self.fields = Fields.wait(bridge, timeout=BOOT_GRACE,
                                  on_wait=lambda error: log.emit('waiting-boot', error=str(error)))
        self.player = Player(bridge, self.fields)
        self.seen = {}          # address -> first tick we saw it as a corpse
        self.retired = 0

    def step(self):
        now = time.monotonic()
        actors = self.player.actors()
        pawn = self.player.find()
        origin = None
        if pawn:
            try:
                origin = self.fields.location_of(pawn)
            except OSError:
                origin = None
        corpses = []
        alive = set()
        for address in actors:
            if not is_corpse(self.player, self.fields, address):
                continue
            alive.add(address)
            first = self.seen.setdefault(address, now)
            try:
                corpses.append((address, first, self.fields.location_of(address)))
            except OSError:
                continue
        for address in list(self.seen):          # forget bodies that left the map
            if address not in alive:
                del self.seen[address]
        expired = limit.decide(corpses, now,
                               lifetime=float(self.config['lifetime_seconds']),
                               max_distance=float(self.config['max_distance']),
                               max_corpses=int(self.config['max_corpses']),
                               player=origin)
        for address, reason in expired.items():
            age = round(now - self.seen.get(address, now), 1)
            if not self.dry:
                try:
                    self.fields.expire(address)
                except OSError as error:
                    self.log.emit('expire-failed', address=hex(address), error=str(error))
                    continue
            self.retired += 1
            self.log.emit('expire', address=hex(address), reason=reason,
                          age=age, corpses=len(corpses), retired=self.retired,
                          dry=self.dry)
        return len(corpses)

    def run(self, seconds=0.0, once=False):
        self.log.emit('ready', version=VERSION, pid=self.pid,
                      dry=self.dry, **{k: self.config[k] for k in DEFAULTS if k != 'dry_run'})
        self.log.emit('fields', **self.fields.offsets())
        end = time.monotonic() + seconds if seconds else None
        status = time.monotonic() + STATUS_INTERVAL
        while True:
            # Before any read: a dead process still answers reads for a while and
            # then raises OSError forever, and the OSError path below skips this.
            if self.pid and not attach.pid_alive(self.pid):
                self.log.emit('game-exit', pid=self.pid)
                return
            began = time.monotonic()
            try:
                corpses = self.step()
            except OSError:
                time.sleep(0.05)                 # actor vanished mid read
                continue
            except Exception as error:           # a bug here must not end the run
                self.log.emit('worker-error', error=f'{type(error).__name__}: {error}')
                time.sleep(0.5)
                continue
            if once:
                self.log.emit('once', corpses=corpses, retired=self.retired)
                return
            if time.monotonic() >= status:
                self.log.emit('status', corpses=corpses, retired=self.retired,
                              loop_ms=round((time.monotonic() - began) * 1000.0, 1))
                status = time.monotonic() + STATUS_INTERVAL
            if end and time.monotonic() >= end:
                return
            time.sleep(max(0.0, float(self.config['poll_seconds'])
                           - (time.monotonic() - began)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game', type=Path, help='game root (default: this mod)')
    parser.add_argument('--pid', type=int)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--duration', type=float, default=0.0)
    parser.add_argument('--once', action='store_true', help='run one pass and exit')
    parser.add_argument('--dry-run', action='store_true', help='log only, never write')
    parser.add_argument('--version', action='version', version=f'corpse-limit {VERSION}')
    args = parser.parse_args()
    config = load_config(args.config)
    if not config['enabled']:
        print('config.json 里 enabled = false，不启动。')
        return 0
    dry = bool(args.dry_run or config['dry_run'])
    game = (args.game or attach.game_dir(HERE)).resolve()
    pid = args.pid or attach.find_pid(game, timeout=30.0)
    if not pid:
        raise SystemExit('游戏没有运行：先启动游戏，再运行本程序。')
    log = Log(HERE / 'logs' / f"corpse-limit-{time.strftime('%Y%m%d')}.jsonl")
    bridge = attach.connect(game, pid)
    try:
        Worker(bridge, pid, config, log, dry_run=dry).run(args.duration, once=args.once)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
