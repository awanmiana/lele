"""Time must be an input, not an accident of when the code ran.

The library used to read the system clock deep inside functions, which made two
things true at once: the same stored data analysed tomorrow gave a different
answer, and a test written against a fixed date quietly changed meaning when
that date was reached. The suite had already been bitten by this — one test
began failing on the day its hard-coded date aged out.

These tests pin the contract: `clock.now()` is the only source of time,
`freeze()` substitutes an instant for a block and always restores it, and the
functions that judge a bound against "now" accept it as an argument so a
historical run can be reproduced exactly.

No network.
"""
import os
import unittest
from datetime import datetime, timedelta, UTC

from lele.analysis import moves, signals
from lele.core import clock

INSTANT = datetime(2026, 1, 31, 12, 0, 0, tzinfo=UTC)


class ClockTests(unittest.TestCase):

    def test_now_is_aware_utc(self):
        moment = clock.now()
        self.assertIsNotNone(moment.tzinfo)
        self.assertEqual(moment.utcoffset(), timedelta(0))

    def test_freeze_substitutes_and_restores(self):
        before = clock.now()
        with clock.freeze(INSTANT) as frozen:
            self.assertEqual(clock.now(), INSTANT)
            self.assertEqual(frozen, INSTANT)
            self.assertTrue(clock.is_frozen())
        self.assertFalse(clock.is_frozen())
        self.assertNotEqual(clock.now(), INSTANT)
        self.assertGreaterEqual(clock.now(), before)

    def test_freeze_restores_after_an_exception(self):
        """A failing test must not leave the process pinned to a past date."""
        with self.assertRaises(RuntimeError):
            with clock.freeze(INSTANT):
                raise RuntimeError("boom")
        self.assertFalse(clock.is_frozen())
        self.assertNotEqual(clock.now(), INSTANT)

    def test_freeze_nests_and_unwinds_in_order(self):
        with clock.freeze(INSTANT):
            with clock.freeze(INSTANT + timedelta(days=1)):
                self.assertEqual(clock.now(), INSTANT + timedelta(days=1))
            self.assertEqual(clock.now(), INSTANT)

    def test_freeze_accepts_an_iso_string(self):
        with clock.freeze("2026-01-31T12:00:00Z"):
            self.assertEqual(clock.now(), INSTANT)
        with clock.freeze("2026-01-31T14:00:00+02:00"):
            self.assertEqual(clock.now(), INSTANT, "a stated offset must be honoured")

    def test_freeze_refuses_a_nonsense_instant(self):
        for bad in ("not a date", "2026-13-01T00:00:00Z", "", "31/01/2026"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                with clock.freeze(bad):
                    pass

    def test_the_environment_can_pin_the_clock(self):
        os.environ["LELE_NOW"] = "2026-02-01T00:00:00+00:00"
        self.addCleanup(os.environ.pop, "LELE_NOW", None)
        self.assertEqual(clock.now(), datetime(2026, 2, 1, tzinfo=UTC))
        self.assertEqual(clock.frozen_at(), datetime(2026, 2, 1, tzinfo=UTC))
        del os.environ["LELE_NOW"]
        self.assertIsNone(clock.frozen_at())

    def test_advance(self):
        self.assertEqual(clock.advance(INSTANT, 3600), INSTANT + timedelta(hours=1))

    def test_an_explicit_instant_makes_the_result_reproducible(self):
        """The contract: pass `now` and the ambient clock stops mattering.

        Without an explicit instant the answer is time-dependent by design, so
        this compares the same call made under three different frozen clocks and
        requires one result, not three.
        """
        stored = [
            {"id": 1, "start_time": (INSTANT - timedelta(hours=200)).isoformat()},
            {"id": 2, "start_time": (INSTANT - timedelta(hours=30)).isoformat()},
            {"id": 3, "start_time": (INSTANT - timedelta(hours=5)).isoformat()},
        ]
        results = []
        for frozen in (INSTANT, INSTANT + timedelta(days=365),
                       INSTANT + timedelta(days=3650)):
            with clock.freeze(frozen):
                requested, servable = moves.windows(stored, 24, 72, now=INSTANT)
                results.append(([w["move_id"] for w in requested],
                                [w["move_id"] for w in servable]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])
        self.assertEqual(results[0][0], [1, 2, 3])
        self.assertEqual(results[0][1], [2, 3],
                         "a window is servable when it starts inside the reach: "
                         "-54h and -29h are inside 72h, -224h is not")

    def test_without_an_explicit_instant_the_clock_decides(self):
        """Which is the behaviour the argument exists to remove, and it is real."""
        # the pre-move window for a move 30h back opens 54h before the instant
        recent = [{"id": 1, "start_time": (INSTANT - timedelta(hours=30)).isoformat()}]
        with clock.freeze(INSTANT):
            _, near = moves.windows(recent, 24, 72)
        with clock.freeze(INSTANT + timedelta(days=365)):
            _, later = moves.windows(recent, 24, 72)
        self.assertEqual(len(near), 1)
        self.assertEqual(later, [], "a year later the same window is out of reach")


class ExplicitNowTests(unittest.TestCase):

    def test_the_signature_takes_now(self):
        import inspect
        self.assertIn("now", inspect.signature(signals.news_signals).parameters)
        self.assertIn("now", inspect.signature(moves.windows).parameters)

    def test_a_window_predating_the_reach_is_not_servable(self):
        start = INSTANT - timedelta(hours=100)
        with clock.freeze(INSTANT + timedelta(days=1)):
            requested, servable = moves.windows(
                [{"id": 9, "start_time": start.isoformat()}], 24, 72)
        self.assertEqual(len(requested), 1, "the request is still reported")
        self.assertEqual(servable, [],
                         "nothing is servable when the window predates the reach")


if __name__ == "__main__":
    unittest.main()
