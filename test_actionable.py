import copy
import json
import os
import unittest
from unittest.mock import patch

import monitor


class ActionableTests(unittest.TestCase):
    def cycle(self, database, *, site=None, regional='UNKNOWN: Russian probes HTTP 429',
              peer='cloud monitor silent'):
        saved = []

        class FakeStore:
            def __init__(self, test): pass
            def load(self, bootstrap): return copy.deepcopy(database)
            def save(self, state): saved.append(copy.deepcopy(state))

        env = {'MONITOR_MODE': 'check', 'MONITOR_TEST_DELIVERY_FAULT': 'none',
               'MONITOR_TARGETS': json.dumps([{'label': 'Example', 'url': 'https://example.com/health'}])}
        with patch.dict(os.environ, env), patch.object(monitor, 'Store', FakeStore), patch.object(
            monitor, 'health', return_value=site), patch.object(
            monitor, 'russian_nodes', return_value=['ru-a', 'ru-b']), patch.object(
            monitor, 'russian_http', return_value=regional), patch.object(
            monitor, 'peer', return_value=peer), patch.object(monitor, 'deliver', return_value=True) as send:
            self.assertEqual(monitor.run(), 0)
        return saved[-1], send

    def test_observer_alerts_and_old_recoveries_are_silent(self):
        for kind in ('alert', 'recovery'):
            with self.subTest(kind=kind):
                legacy = {'id': '2026-10-18T12:00:00+00:00', 'failures': 4,
                          'accepted': ['telegram'], 'pending': [
                              {'kind': kind, 'id': '2026-10-18T12:00:00+00:00',
                               'at': '2026-10-18T12:00:00+00:00', 'reason': 'healthy',
                               'channels': ['email']}]}
                state = {'schema': 1, 'targets': {'peer': legacy, 'ru-checker': legacy}}
                for _ in range(6):
                    state, send = self.cycle(state)
                    send.assert_not_called()
                    self.assertTrue(state['delivery_ok'])
                    self.assertIn('cloud monitor silent', state['checks']['peer'])
                    self.assertIn('UNKNOWN:', state['checks']['target-1-ru'])
                state, send = self.cycle(state, peer=None, regional=None)
                send.assert_not_called()

    def test_real_site_failure_alerts_once_and_recovers_silently(self):
        state = {'schema': 1, 'targets': {}}
        state, send = self.cycle(state, site='HTTP 502')
        send.assert_not_called()
        state, send = self.cycle(state, site='HTTP 502')
        self.assertEqual(send.call_count, 2)
        self.assertIn('🔴 Example', send.call_args.args[1])
        state, send = self.cycle(state, site='HTTP 502')
        send.assert_not_called()
        state, send = self.cycle(state)
        send.assert_not_called()
        _, send = self.cycle(state)
        send.assert_not_called()

    def test_regional_failure_is_not_hidden_by_direct_success(self):
        state = {'schema': 1, 'targets': {}}
        state, _ = self.cycle(state, regional='Russian HTTP failure')
        state, send = self.cycle(state, regional='Russian HTTP failure')
        self.assertEqual(send.call_count, 2)
        self.assertIn('из России', send.call_args.args[1])
        _, send = self.cycle(state)
        send.assert_not_called()  # UNKNOWN does not recover a confirmed regional outage.
