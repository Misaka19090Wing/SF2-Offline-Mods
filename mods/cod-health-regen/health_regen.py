"""cod-health-regen: Code-style (CoD-like) health regeneration for SF2 offline.

Reads the local player's Engine.Pawn.Health/HealthMax every tick and writes Health
back once `regen_delay_seconds` have passed since the last damage.  Never heals a
dead pawn, never exceeds HealthMax, never touches bots, never modifies game files.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from sf2mod import attach                      # noqa: E402
from sf2mod.game import Fields, Player         # noqa: E402
from sf2mod.regen import Regen, bot_targets, is_pve                 # noqa: E402

VERSION = '1.3.0'
DEFAULTS = {
    'enabled': True,
    'regen_delay_seconds': 4.0,
    'regen_per_second': 25.0,
    'tick_hz': 10.0,
    'log_damage': True,
    'bots': False,               # heal living teammates with the same rule
    'bots_pve_only': True,       # ... but only in the alien survival/defence modes
    'dry_run': False,
}
BOOT_GRACE = 3600.0
BOT_SCAN_SECONDS = 0.5   # shortcut: teammates are scanned twice a second, raise if they ever feel laggy      # the client needs a while before GObjects is readable
REGEN_LOG_INTERVAL = 1.0


def load_config(path=None):
    config = dict(DEFAULTS)
    file = HERE / 'config.json'
    if path:
        file = Path(path)
    if file.is_file():
        config.update(json.loads(file.read_text('utf-8')))
    unknown = set(config) - set(DEFAULTS)
    if unknown:
        raise SystemExit('config.json 有未知项：' + ', '.join(sorted(unknown)))
    return config


class Log:
    """One JSON object per line, same field style as corpse-flinch's logs."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event, **fields):
        record = {'event': event, 'time': round(time.time(), 3)}
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, sort_keys=False)
        try:
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(line + '\n')
        except OSError:
            pass
        try:
            print(line, flush=True)
        except (OSError, ValueError):
            pass                                # pythonw: no usable stdout


class Worker:
    def __init__(self, game, config, log, pid=0, dry=False):
        self.game = game
        self.config = config
        self.log = log
        self.pid = pid
        self.dry = bool(dry or config['dry_run'])
        self.regen = Regen(config['regen_delay_seconds'], config['regen_per_second'])
        self.pawn = 0
        self.last_regen_log = 0.0
        self.at_full = False
        self.announced = 0.0
        self.no_pawn_since = 0.0
        self.last_diagnostic = 0.0
        self.read_errors = 0
        self.last_read_error = 0.0
        self.dead = False
        self.bot_regens = {}
        self.bots_healed = 0
        self.last_bot_log = 0.0
        self.last_bot_scan = 0.0
        self.last_bot_report = 0.0

    # ------------------------------------------------------------------ attach
    def waiting_boot(self, error):
        """One line a minute while the client is still starting, never a fatal."""
        now = time.monotonic()
        if now - self.announced >= 60.0:
            self.announced = now
            self.log.emit('waiting-boot', pid=self.pid, error=str(error))

    def attach(self, wait=0.0):
        """Wait for the client to finish booting, then take a read/write handle."""
        while True:
            self.pid = self.pid or attach.find_pid(self.game, timeout=wait)
            if not self.pid:
                raise SystemExit('游戏没有运行：先启动游戏，再运行本程序。')
            try:
                bridge = attach.connect(self.game, self.pid)
            except (ValueError, OSError, RuntimeError) as error:
                self.waiting_boot(error)         # wait, but say so once a minute
                time.sleep(3.0)
                continue
            self.log.emit('attached', pid=self.pid, exe=str(attach.client_exe(self.game)),
                          layout=attach.layout(self.game), dry=self.dry)
            return bridge

    # -------------------------------------------------------------------- run
    def run(self):
        self.log.emit('ready', version=VERSION, pid=self.pid if self.pid else 0,
                      mode='dry' if self.dry else 'run', enabled=self.config['enabled'],
                      regen_delay_seconds=self.config['regen_delay_seconds'],
                      regen_per_second=self.config['regen_per_second'],
                      tick_hz=self.config['tick_hz'], bots=self.config['bots'])
        bridge = self.attach(wait=0.0 if self.pid else 60.0)
        try:
            # GObjects is empty for a while after start-up: keep waiting, do not die.
            fields = Fields.wait(bridge, on_wait=self.waiting_boot)
        except (RuntimeError, OSError, ValueError) as error:
            self.log.emit('fatal', error=str(error))
            raise SystemExit(str(error))
        self.log.emit('fields', **fields.offsets())
        player = Player(bridge, fields, on_event=self.log.emit)
        period = 1.0 / max(1.0, min(60.0, float(self.config['tick_hz'])))
        while True:
            if not attach.pid_alive(self.pid):
                self.log.emit('game-exit', pid=self.pid)
                self.clear_state()
                return 0
            started = time.monotonic()
            try:
                self.step(player, fields)
            except OSError as error:
                # A read raced a map change. Never swallow it silently: a worker
                # that quietly reads nothing looks exactly like a broken worker.
                self.read_errors += 1
                if self.read_errors == 1 or time.monotonic() - self.last_read_error >= 60.0:
                    self.last_read_error = time.monotonic()
                    self.log.emit('read-error', where='step', error=str(error),
                                  total=self.read_errors)
            except Exception as error:        # noqa: BLE001 - keep the worker alive
                self.log.emit('worker-error', where='step', error=repr(error))
            time.sleep(max(0.0, period - (time.monotonic() - started)))

    def clear_state(self):
        """Drop our own pid file so the next launch never trusts this run."""
        state = HERE / 'logs' / 'worker.json'
        try:
            if json.loads(state.read_text('utf-8')).get('worker') == os.getpid():
                state.unlink()
        except (OSError, ValueError):
            pass

    def diagnose(self, player, now):
        """Say why no pawn is visible, twice a minute at most."""
        if not self.no_pawn_since:
            self.no_pawn_since = now
        if now - self.no_pawn_since < 5.0 or now - self.last_diagnostic < 30.0:
            return
        self.last_diagnostic = now
        self.log.emit('no-pawn', seconds=round(now - self.no_pawn_since, 1),
                      **player.summary())

    def step_bots(self, player, fields, now):
        """Heal teammates with the same delay/rate; only this life's own pawn is fed."""
        if not self.config['bots'] or not self.config['enabled']:
            return
        if now - self.last_bot_scan < BOT_SCAN_SECONDS:
            return
        self.last_bot_scan = now
        origin = fields.team_of(self.pawn) if self.pawn else None
        world = fields.world()                   # not player.world: actors() has not run yet
        game = fields.game_of(world)
        kind = player.kind(game) if game else ''
        pve = is_pve(kind)
        if origin is None or (self.config['bots_pve_only'] and not pve):
            self.bot_regens.clear()
            self.report_bots(now, origin, pve, kind, [], 0)
            return
        rows = []
        for address in player.actors():
            try:
                if 'Pawn' not in player.kind(address):
                    continue
                health, maximum = fields.read(address)
                rows.append((address, fields.team_of(address), health, maximum))
            except OSError:
                continue                         # actor vanished mid scan
        health_of = {row[0]: (row[2], row[3]) for row in rows}
        targets = bot_targets(rows, self.pawn, origin)
        for address in targets:
            health, maximum = health_of[address]
            state = self.bot_regens.get(address)
            if state is None:
                state = self.bot_regens[address] = Regen(self.config['regen_delay_seconds'],
                                                         self.config['regen_per_second'])
            target = state.update(now, health, maximum)
            if target is None:
                continue
            if not self.dry:
                try:
                    fields.write(address, target)
                except OSError:
                    continue
            self.bots_healed += 1
            if now - self.last_bot_log >= REGEN_LOG_INTERVAL:
                self.last_bot_log = now
                self.log.emit('regen-bot', bot=hex(address), health=target,
                              max=maximum, bots=self.bots_healed, dry=self.dry)
        for address in list(self.bot_regens):    # forget pawns that left the map
            if address not in health_of:
                del self.bot_regens[address]
        self.report_bots(now, origin, pve, kind, rows, len(targets))

    def report_bots(self, now, team, pve, kind, rows, teammates):
        """One line every 10 s: why the teammate pass did (or did not) heal anyone."""
        if now - self.last_bot_report < 10.0:
            return
        self.last_bot_report = now
        self.log.emit('bots-scan', team=team, pve=pve, mode=kind or None,
                      pawns=len(rows), teammates=teammates, bots=self.bots_healed,
                      sample=[{'at': hex(address), 'team': team_of, 'health': health}
                              for address, team_of, health, _ in rows[:4]])

    def step(self, player, fields):
        now = time.monotonic()
        try:
            self.step_bots(player, fields, now)
        except Exception as error:       # noqa: BLE001 - the player's regen must not depend on this
            self.log.emit('worker-error', where='bots', error=repr(error))
        pawn = player.find(now)
        if not pawn:
            if self.pawn:
                self.log.emit('pawn-lost', pawn=(hex(self.pawn)))
            self.pawn = 0
            self.regen.reset(now)
            self.at_full = False
            self.diagnose(player, now)
            return
        self.no_pawn_since = 0.0
        self.last_diagnostic = 0.0
        if pawn != self.pawn:
            self.log.emit('pawn', pawn=hex(pawn), via=player.via)
            self.pawn = pawn
            self.regen.reset(now)
            self.at_full = False
            self.dead = False
        if not self.config['enabled']:
            return
        health, maximum = fields.read(pawn)
        if self.config['log_damage'] and self.regen.last_health is not None \
                and health < self.regen.last_health:
            self.log.emit('damage', health=health, max=maximum)
            self.at_full = False
        if health <= 0:
            if not self.dead:                  # one line per death, not 10 per second
                self.dead = True
                self.log.emit('death', health=health)
            self.regen.reset(now)
            self.at_full = False
            return
        self.dead = False
        target = self.regen.update(now, health, maximum)
        if target is None:
            if not self.at_full and health >= maximum > 0:
                self.at_full = True
                self.log.emit('full', health=health, max=maximum)
            return
        if self.dry:
            self.regen.last_health = health  # nothing was written: do not read it as damage
            self.log.emit('regen-would', health=health, target=target, max=maximum)
            return
        fields.write(pawn, target)
        if now - self.last_regen_log >= REGEN_LOG_INTERVAL:
            self.last_regen_log = now
            self.log.emit('regen', health=target, max=maximum)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version='cod-health-regen ' + VERSION)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--log', type=Path,
                        default=HERE / 'logs' / time.strftime('health-regen-%Y%m%d.jsonl'))
    parser.add_argument('--pid', type=int, default=0)
    parser.add_argument('--dry', action='store_true', help='只读：算出目标血量但不写入')
    parser.add_argument('--once', action='store_true', help='只读一次血量并打印')
    args = parser.parse_args()

    config = load_config(args.config)
    game = attach.game_dir(HERE)
    log = Log(args.log)
    if args.once:
        bridge = Worker(game, config, log, args.pid, dry=True).attach(wait=60.0)
        fields = Fields.wait(bridge, timeout=60.0)
        player = Player(bridge, fields, on_event=log.emit)
        pawn = player.find()
        health, maximum = fields.read(pawn) if pawn else (None, None)
        world = fields.world()
        game = fields.game_of(world)
        print(json.dumps({'pid': bridge.pid, 'pawn': hex(pawn), 'health': health,
                          'health_max': maximum, 'via': player.via,
                          'world': hex(world), 'game': hex(game),
                          'mode': player.kind(game) if game else '',
                          'offsets': fields.offsets()}, ensure_ascii=False))
        bridge.close()
        return 0
    worker = Worker(game, config, log, args.pid, dry=args.dry)
    return worker.run()


if __name__ == '__main__':
    raise SystemExit(main())
