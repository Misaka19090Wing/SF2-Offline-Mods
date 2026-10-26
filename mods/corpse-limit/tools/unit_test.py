"""Offline checks for the corpse rules. Needs no game.

    runtime\\python\\python.exe -X utf8 -B tools\\unit_test.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from sf2mod import limit                            # noqa: E402

FAILURES = []


def check(label, condition, detail=''):
    print(('[OK ] ' if condition else '[FAIL] ') + label + ('' if condition else f' {detail}'))
    if not condition:
        FAILURES.append(label)


def corpses(*rows):
    """(address, first_seen, location) with a default near the player."""
    return [(row[0], row[1], row[2] if len(row) > 2 else (0.0, 0.0, 0.0)) for row in rows]


def test_lifetime():
    picked = limit.decide(corpses((1, 0.0), (2, 50.0)), now=61.0, lifetime=60.0)
    check('lifetime: only the old body goes', picked == {1: 'lifetime'}, picked)
    check('lifetime: 0 disables the rule',
          limit.decide(corpses((1, 0.0)), now=1e6, lifetime=0) == {})


def test_distance():
    near, far = (0.0, 100.0, 0.0), (0.0, 9000.0, 0.0)
    picked = limit.decide(corpses((1, 0.0, near), (2, 0.0, far)), now=1.0,
                          max_distance=6000.0, player=(0.0, 0.0, 0.0))
    check('distance: only the far body goes', picked == {2: 'distance'}, picked)
    check('distance: 0 disables the rule',
          limit.decide(corpses((1, 0.0, far)), now=1.0, max_distance=0,
                       player=(0.0, 0.0, 0.0)) == {})
    check('distance: without a player nothing is too far',
          limit.decide(corpses((1, 0.0, far)), now=1.0, max_distance=6000.0, player=None) == {})


def test_count():
    bodies = corpses(*[(address, float(address)) for address in range(1, 6)])
    picked = limit.decide(bodies, now=10.0, max_corpses=2)
    check('count: the three oldest go', picked == {1: 'count', 2: 'count', 3: 'count'}, picked)
    check('count: at the cap nothing goes',
          limit.decide(bodies[:2], now=10.0, max_corpses=2) == {})
    check('count: 0 disables the rule',
          limit.decide(bodies, now=10.0, max_corpses=0) == {})
    same = corpses((7, 5.0), (3, 5.0), (9, 5.0))     # identical age: address decides
    check('count: ties break on address, stably',
          limit.decide(same, now=10.0, max_corpses=1) == {3: 'count', 7: 'count'},
          limit.decide(same, now=10.0, max_corpses=1))


def test_union():
    """A body caught by two rules keeps the first reason (lifetime wins)."""
    # 1..3 old and far, 4 fresh and far: only the fresh one may keep a distance/count reason
    bodies = corpses(*[(address, 0.0 if address < 4 else 61.0, (0.0, 9000.0, 0.0))
                       for address in range(1, 5)])
    picked = limit.decide(bodies, now=61.0, lifetime=60.0, max_distance=6000.0,
                          max_corpses=1, player=(0.0, 0.0, 0.0))
    check('union: nobody is forgotten', set(picked) == {1, 2, 3, 4}, picked)
    check('union: lifetime has priority', picked[1] == 'lifetime', picked)
    check('union: survivors keep their own reason',
          picked[4] in ('distance', 'count'), picked)


for test in (test_lifetime, test_distance, test_count, test_union):
    test()
print()
if FAILURES:
    print(f'{len(FAILURES)} failed: ' + ', '.join(FAILURES))
    raise SystemExit(1)
print('all good: the corpse rules behave here.')
