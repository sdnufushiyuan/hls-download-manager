"""Offline policy and API checks; never contact QQ, Google or proxy services."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import hls_node_optimizer as opt


def ranking(a=(10, 10), b=(6, 6)):
    return opt.ranked({'old': [{'ok': True, 'seconds': s} for s in a],
                       'new': [{'ok': True, 'seconds': s} for s in b]})


class PolicyTests(unittest.TestCase):
    def test_requires_two_valid_independent_transfers(self):
        for rows in ([{'ok': True, 'seconds': 1}],
                     [{'ok': True, 'seconds': 1}, {'ok': False, 'seconds': 2}],
                     [{'ok': True, 'seconds': 0}, {'ok': True, 'seconds': 2}],
                     [{'ok': True, 'seconds': float('nan')}, {'ok': True, 'seconds': 2}]):
            self.assertEqual(opt.ranked({'bad': rows}), [])

    def test_requires_confirmation_across_cycles(self):
        target, state, reason = opt.choose('old', ranking(), {}, 10000)
        self.assertEqual((target, reason), ('old', 'confirm_next_cycle'))
        state['last_cycle'] = 10000
        target, state, reason = opt.choose('old', ranking(), state, 10300)
        self.assertEqual((target, reason), ('new', 'consistently_faster'))
        self.assertEqual(state['last_switch'], 10300)

    def test_short_term_speedup_or_one_bad_round_does_not_switch(self):
        for times in ((8, 8), (6, 9), (7.6, 7.6)):
            target, state, _ = opt.choose('old', ranking(b=times), {'challenger':'new','wins':5,'last_cycle':9900}, 10000)
            self.assertEqual(target, 'old')
            self.assertEqual(state['wins'], 0)

    def test_cooldown_prevents_oscillation(self):
        state = {'challenger':'new','wins':2,'last_cycle':9900,'last_switch':9900}
        target, _, reason = opt.choose('old', ranking(), state, 10000)
        self.assertEqual((target, reason), ('old', 'minimum_dwell'))

    def test_stale_challenger_confirmation_expires(self):
        state = {'challenger':'new','wins':2,'last_cycle':9000}
        target, updated, _ = opt.choose('old', ranking(), state, 10000)
        self.assertEqual(target, 'old')
        self.assertEqual(updated['wins'], 1)

    def test_no_valid_candidate_keeps_current(self):
        self.assertEqual(opt.choose('old', [], {}, 10000)[0], 'old')

    def test_current_not_measured_does_not_invent_speedup(self):
        self.assertEqual(opt.choose('missing', ranking(), {}, 10000)[0], 'missing')

    def test_dead_incumbent_can_use_twice_verified_alternative(self):
        target, _, reason = opt.choose('missing', ranking(), {'last_switch':9999}, 10000, unavailable=True)
        self.assertEqual((target, reason), ('new', 'verified_failover'))

    def test_current_best_clears_challenger(self):
        target, state, _ = opt.choose('old', ranking(b=(12, 12)), {'wins':5}, 10000)
        self.assertEqual(target, 'old')
        self.assertEqual(state['wins'], 0)

    def test_latency_filter_retains_current_and_explores_full_pool(self):
        names = ['old'] + [str(i) for i in range(70)]
        delays = {name:i+1 for i,name in enumerate(names)}
        cursor, seen = 0, set()
        for _ in range(75):
            selected, cursor = opt.candidates(names, delays, 'old', cursor)
            self.assertEqual(selected[0], 'old')
            self.assertLessEqual(len(selected), 5)
            self.assertEqual(len(selected), len(set(selected)))
            seen.update(selected)
        self.assertEqual(seen, set(names))

    def test_small_pool_and_invalid_current(self):
        self.assertEqual(opt.candidates(['old'], {}, 'old', 0), (['old'], 0))
        with self.assertRaises(ValueError):
            opt.candidates(['old'], {}, 'stranger', 0)

    def test_state_write_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            opt.atomic_json(path, {'selected':'old'})
            opt.atomic_json(path, {'selected':'new'})
            self.assertEqual(json.loads(path.read_text())['selected'], 'new')
            self.assertFalse(path.with_name('state.json.tmp').exists())

    def test_controller_is_loopback_only(self):
        with self.assertRaises(ValueError):
            opt.Controller({'api_secret':'fake','api_url':'http://example.invalid'})

    def test_switch_requires_api_readback(self):
        controller = opt.Controller({'api_secret':'fake','api_url':'http://127.0.0.1:19092'})
        controller.session = MagicMock()
        with patch.object(controller, 'get', return_value={'now':'old'}), self.assertRaises(RuntimeError):
            controller.select('new')
        controller.session.put.assert_called_once()


if __name__ == '__main__':
    unittest.main()
