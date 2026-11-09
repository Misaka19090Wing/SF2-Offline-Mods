"""Offline checks for the window maths and the lobby rule. Needs no game.

    python\\python.exe -X utf8 -B tools\\unit_test.py
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from sf2mod import lobby, window                     # noqa: E402
import lobby_resolution                              # noqa: E402

FAILURES = []


def check(label, condition, detail=''):
    print(('[OK ] ' if condition else '[FAIL] ') + label + ('' if condition else f' {detail}'))
    if not condition:
        FAILURES.append(label)


def test_window_maths():
    check('window: the frame is added to the client size', window.window_size(1920, 1080, 16, 39) == (1936, 1119),
          window.window_size(1920, 1080, 16, 39))
    check('window: a borderless window keeps the size', window.window_size(1920, 1080, 0, 0) == (1920, 1080))


def test_lobby_rule():
    check('lobby: SFEntryGame is the front end', lobby.is_lobby('SFEntryGame_0'))
    check('lobby: a match is not the front end',
          not lobby.is_lobby('SFAITeamDeathGame_0') and not lobby.is_lobby('SFSurvivalGame_0'))
    check('lobby: an unreadable class is not the front end', not lobby.is_lobby('') and not lobby.is_lobby(None))


def test_config():
    config = json.loads((ROOT / 'config.json').read_text('utf-8'))
    check('config: keys match DEFAULTS', set(config) == set(lobby_resolution.DEFAULTS),
          sorted(set(config) ^ set(lobby_resolution.DEFAULTS)))
    check('config: default 1920x1080',
          config['width'] == 1920 and config['height'] == 1080,
          (config['width'], config['height']))
    check('config: lobby_only defaults on', config['lobby_only'] is True)


for test in (test_window_maths, test_lobby_rule, test_config):
    test()
print()
if FAILURES:
    print(f'{len(FAILURES)} failed: ' + ', '.join(FAILURES))
    raise SystemExit(1)
print('all good: the window maths and the lobby rule behave here.')
