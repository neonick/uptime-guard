import copy
import json
import os
import unittest
from unittest.mock import patch

import monitor

T0 = '2026-10-12T00:00:00+00:00'
T1 = '2026-10-12T00:05:00+00:00'
T2 = '2026-10-12T00:10:00+00:00'


class MonitorTests(unittest.TestCase):
    def test_live_checks_reject_delivery_fault_before_loading_state(self):
        with patch.dict(os.environ, {'MONITOR_MODE': 'check', 'MONITOR_TEST_DELIVERY_FAULT': 'all'}), patch.object(
            monitor, 'Store') as store:
            with self.assertRaises(ValueError):
                monitor.run()
            store.assert_not_called()

    def test_synthetic_fault_requests_reject_channels_without_changing_credentials(self):
        env = {'MONITOR_MODE': 'test-down', 'MONITOR_TEST_DELIVERY_FAULT': 'all',
               'TELEGRAM_BOT_TOKEN': 'original-token', 'TELEGRAM_CHAT_ID': 'operator', 'MAIL_FROM': 'sender@example.com',
               'MAIL_TO': 'operator@example.com', 'MAIL_KEY_ID': 'key', 'MAIL_SECRET': 'secret'}
        calls = []
        def rejected(url, **kwargs):
            calls.append((url, kwargs))
            raise monitor.urllib.error.HTTPError(url, 400, 'rejected', {}, None)
        with patch.dict(os.environ, env), patch.object(monitor, 'request', side_effect=rejected):
            self.assertFalse(monitor.deliver('telegram', 'test'))
            self.assertFalse(monitor.deliver('email', 'test'))
            self.assertEqual(os.environ['TELEGRAM_BOT_TOKEN'], 'original-token')
            self.assertEqual(os.environ['MAIL_TO'], 'operator@example.com')
        self.assertNotIn('original-token', calls[0][0])
        self.assertEqual(calls[1][1]['body']['Destination']['ToAddresses'], ['not-an-email'])

    def test_health_requires_exact_http_200_and_json_status(self):
        class Response:
            def __init__(self, status, payload):
                self.status = status
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, limit):
                return json.dumps(self.payload).encode()
        for status, payload, healthy in [
            (200, {'status': 'ok'}, True),
            (201, {'status': 'ok'}, False),
            (202, {'status': 'ok'}, False),
            (204, {'status': 'ok'}, False),
            (200, {'status': 'error'}, False),
        ]:
            with self.subTest(status=status, payload=payload), patch.object(
                monitor.urllib.request, 'urlopen', return_value=Response(status, payload)):
                self.assertEqual(monitor.health('https://example.com/health') is None, healthy)

    def test_slow_russian_checks_leave_time_for_durable_state(self):
        clock = [100.0]
        saved = []
        nodes = ['ru-a', 'ru-b']
        class Response:
            status = 200
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit): return json.dumps(self.payload).encode()
        def urlopen(req, timeout):
            clock[0] += timeout
            if 'check-http?' in req.full_url:
                return Response({'ok': 1, 'request_id': 'id', 'nodes': dict.fromkeys(nodes)})
            if 'check-result/' in req.full_url:
                return Response(dict.fromkeys(nodes))
            return Response({'status': 'ok'})
        class FakeStore:
            def __init__(self, test): pass
            def load(self, bootstrap): return {'schema': 1, 'targets': {}}
            def save(self, state):
                self.assert_budget = clock[0] + 1 < monitor.DEADLINE
                if not self.assert_budget: raise TimeoutError('state budget exhausted')
                clock[0] += 1
                saved.append(copy.deepcopy(state))
        env = {'MONITOR_MODE': 'check', 'MONITOR_TARGETS': json.dumps([
            {'label': 'One', 'url': 'https://example.com/health'},
            {'label': 'Two', 'url': 'https://example.org/health'}])}
        with patch.dict(os.environ, env), patch.object(monitor, 'Store', FakeStore), patch.object(
            monitor, 'russian_nodes', return_value=nodes), patch.object(monitor, 'peer', return_value=None), patch.object(
            monitor.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(
            monitor.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), patch.object(
            monitor.urllib.request, 'urlopen', side_effect=urlopen):
            self.assertEqual(monitor.run(), 0)
        self.assertEqual(len(saved[0]['checks']), 5)
        self.assertTrue(saved[-1]['completed_at'])
        self.assertLess(clock[0] - 100, 165)

    def test_peer_age_already_counts_two_missed_periods(self):
        result = monitor.advance({}, 'cloud monitor silent', T0, threshold=1)
        self.assertEqual(result['pending'][0]['kind'], 'alert')
    def test_success_resets_consecutive_failures(self):
        first = monitor.advance({}, 'HTTP 502', T0)
        healthy = monitor.advance(first, None, T1)
        second = monitor.advance(healthy, 'HTTP 502', T2)
        self.assertEqual(second['failures'], 1)
        self.assertEqual(second['pending'], [])
        self.assertEqual(second['id'], T2)

    def test_pending_recovery_survives_new_incident(self):
        failure = monitor.advance(monitor.advance({}, 'HTTP 502', T0), 'HTTP 502', T1)
        failure['accepted'] = ['telegram']
        failure['pending'] = []
        recovery = monitor.advance(failure, None, T2)
        new = monitor.advance(recovery, 'HTTP 503', '2026-10-12T00:15:00+00:00')
        self.assertNotEqual(new['id'], failure['id'])
        self.assertEqual(new['pending'][0]['kind'], 'recovery')
        self.assertEqual(new['pending'][0]['channels'], ['telegram'])

    def test_only_accepted_channels_receive_recovery(self):
        failure = monitor.advance({}, 'HTTP 502', T0)
        failure['accepted'] = ['email']
        failure['pending'] = []
        recovery = monitor.advance(failure, None, T1)
        self.assertEqual(recovery['pending'][0]['channels'], ['email'])

    def test_reminder_respects_last_successful_send(self):
        old = monitor.advance({}, 'HTTP 502', T0)
        old.update(accepted=['telegram'], last_send=T0)
        self.assertFalse(monitor.advance(old, 'HTTP 502', T2)['pending'])
        new = monitor.advance(old, 'HTTP 502', '2026-10-12T00:31:00+00:00')
        self.assertEqual(new['pending'], [])

    def test_run_persists_and_retries_only_failed_channel(self):
        state = monitor.advance({}, 'TEST: synthetic HTTP failure', T0)
        database = {'schema': 1, 'targets': {'target-1': state}}
        persisted = []

        class FakeStore:
            def __init__(self, test):
                assert test

            def load(self, bootstrap):
                return copy.deepcopy(database)

            def save(self, value):
                persisted.append(copy.deepcopy(value))

        env = {'MONITOR_MODE': 'test-down', 'MONITOR_TARGETS': json.dumps(
            [{'label': 'Example', 'url': 'https://example.com/health'}])}
        with patch.dict(os.environ, env), patch.object(monitor, 'Store', FakeStore), patch.object(
            monitor, 'deliver', side_effect=lambda channel, text: channel == 'telegram') as send:
            self.assertEqual(monitor.run(), 1)
        self.assertEqual([call.args[0] for call in send.call_args_list], ['telegram', 'email'])
        self.assertEqual(persisted[0]['targets']['target-1']['pending'][0]['channels'], ['telegram', 'email'])
        database = persisted[-1]
        with patch.dict(os.environ, env), patch.object(monitor, 'Store', FakeStore), patch.object(
            monitor, 'deliver', return_value=True) as send:
            self.assertEqual(monitor.run(), 0)
        self.assertEqual([call.args[0] for call in send.call_args_list], ['email'])
        self.assertTrue(persisted[-1]['delivery_ok'])

    def test_russian_http_pending_and_mixed_are_not_healthy(self):
        nodes = ['ru-a', 'ru-b']
        start = {'ok': 1, 'request_id': 'id', 'nodes': dict.fromkeys(nodes)}
        responses = [start, {'ru-a': [[1, .1, 'OK', '200']], 'ru-b': [[0, .1, 'Bad', '503']]}]
        with patch.object(monitor, 'request', side_effect=responses):
            self.assertIn('DEGRADED', monitor.russian_http('https://example.com', nodes))
        with patch.object(monitor, 'request', side_effect=[start] + [dict.fromkeys(nodes)] * 6), patch.object(monitor.time, 'sleep'):
            self.assertIn('UNKNOWN', monitor.russian_http('https://example.com', nodes))

    def test_corrupt_state_is_not_overwritten(self):
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'example/monitor', 'GITHUB_TOKEN': 'fake'}):
            store = monitor.Store()
        with patch.object(monitor, 'request', return_value={'sha': 'one', 'content': 'e30='}):
            with self.assertRaisesRegex(ValueError, 'corrupt'):
                store.load(True)

    def test_grouped_checker_notice_retries_email_without_duplicating_telegram(self):
        failure = 'UNKNOWN: Russian HTTP HTTP 429'
        database = {'schema': 1, 'checks': {key: failure for key in ('target-1-ru', 'target-2-ru')},
                    'targets': {key: monitor.advance({}, failure, T0)
                                for key in ('target-1-ru', 'target-2-ru')}}
        persisted = []

        class FakeStore:
            def __init__(self, test):
                assert not test

            def load(self, bootstrap):
                return copy.deepcopy(database)

            def save(self, value):
                persisted.append(copy.deepcopy(value))

        env = {'MONITOR_MODE': 'check', 'MONITOR_TEST_DELIVERY_FAULT': 'none',
               'MONITOR_TARGETS': json.dumps([
                   {'label': 'Alpha', 'url': 'https://example.com/health'},
                   {'label': 'Beta', 'url': 'https://example.org/health'}])}
        rejected = monitor.urllib.error.HTTPError(
            'https://check-host.net/check-http?host=private-target.example', 429, 'limited', {}, None)
        with patch.dict(os.environ, env), patch.object(monitor, 'Store', FakeStore), patch.object(
            monitor, 'russian_nodes', return_value=['ru-a', 'ru-b']), patch.object(
            monitor, 'health', return_value=None), patch.object(monitor, 'peer', return_value=None), patch.object(
            monitor, 'russian_http', side_effect=rejected), patch.object(
            monitor, 'deliver', side_effect=lambda channel, text: channel == 'telegram') as send:
            self.assertEqual(monitor.run(), 1)
        self.assertEqual([call.args[0] for call in send.call_args_list], ['telegram', 'email'])
        for call in send.call_args_list:
            self.assertIn('Прямая проверка Alpha', call.args[1])
            self.assertIn('Прямая проверка Beta', call.args[1])
            self.assertNotIn('private-target.example', call.args[1])
        for key in ('ru-checker',):
            self.assertEqual(persisted[0]['targets'][key]['pending'][0]['channels'], ['telegram', 'email'])
            self.assertEqual(persisted[-1]['targets'][key]['pending'][0]['channels'], ['email'])
        database = persisted[-1]
        with patch.dict(os.environ, env), patch.object(monitor, 'Store', FakeStore), patch.object(
            monitor, 'russian_nodes', return_value=['ru-a', 'ru-b']), patch.object(
            monitor, 'health', return_value=None), patch.object(monitor, 'peer', return_value=None), patch.object(
            monitor, 'russian_http', side_effect=rejected), patch.object(
            monitor, 'deliver', return_value=True) as send:
            self.assertEqual(monitor.run(), 0)
        self.assertEqual([call.args[0] for call in send.call_args_list], ['email'])
        self.assertTrue(persisted[-1]['delivery_ok'])
        self.assertFalse(any(value.get('pending') for value in persisted[-1]['targets'].values()))

    def test_advisory_needs_four_timely_good_observations(self):
        old = monitor.advance({}, 'cloud monitor silent', T0, threshold=1)
        old.update(accepted=['telegram'], pending=[])
        first = monitor.advance(old, None, T1, advisory=True)
        self.assertEqual(first['pending'], [])
        second = monitor.advance(first, None, T2, advisory=True)
        self.assertEqual(second['pending'], [])
        third = monitor.advance(second, None, '2026-10-12T00:15:00+00:00', advisory=True)
        self.assertEqual(third['pending'], [])
        good = monitor.advance(third, None, '2026-10-12T00:20:00+00:00', advisory=True)
        self.assertEqual(good['pending'][0]['kind'], 'recovery')
        # Sparse successes never close the same incident or emit another alert.
        sparse = old
        for hour in range(1, 5):
            sparse = monitor.advance(sparse, None, f'2026-10-12T0{hour}:00:00+00:00', advisory=True)
            self.assertEqual(sparse['id'], old['id'])
            self.assertFalse(sparse['pending'])

    def test_checker_migration_preserves_failed_channel(self):
        legacy = monitor.advance({}, 'UNKNOWN: Russian HTTP HTTP 429', T0, threshold=1)
        legacy['accepted'] = ['telegram']
        legacy['pending'][0]['channels'] = ['email']
        state = {'checks': {'target-1-ru': 'UNKNOWN: Russian HTTP HTTP 429'},
                 'targets': {'target-1-ru': legacy}}
        monitor.migrate_checker(state, 'UNKNOWN: Russian HTTP TimeoutError')
        self.assertNotIn('target-1-ru', state['targets'])
        self.assertEqual(state['targets']['ru-checker']['accepted'], ['telegram'])
        self.assertEqual(state['targets']['ru-checker']['pending'][0]['channels'], ['email'])

    def test_checker_migration_keeps_confirmed_regional_failure(self):
        value = monitor.advance({}, 'Russian HTTP failure', T0, threshold=1)
        state = {'checks': {'target-1-ru': 'Russian HTTP failure'}, 'targets': {'target-1-ru': value}}
        monitor.migrate_checker(state, 'UNKNOWN: Russian HTTP HTTP 429')
        self.assertEqual(state['targets']['target-1-ru'], value)

    def test_unknown_does_not_count_as_second_confirmed_regional_failure(self):
        regional = monitor.advance({}, 'Russian HTTP failure', T0)
        database = {'schema': 1, 'checks': {'target-1-ru': 'Russian HTTP failure'},
                    'targets': {'target-1-ru': regional}}
        saved = []
        class FakeStore:
            def __init__(self, test): pass
            def load(self, bootstrap): return copy.deepcopy(database)
            def save(self, state): saved.append(copy.deepcopy(state))
        env = {'MONITOR_MODE': 'check', 'MONITOR_TEST_DELIVERY_FAULT': 'none',
               'MONITOR_TARGETS': json.dumps([{'label': 'Example', 'url': 'https://example.com/health'}])}
        for _ in range(3):
            with patch.dict(os.environ, env), patch.object(monitor, 'Store', FakeStore), patch.object(
                monitor, 'russian_nodes', return_value=[]), patch.object(
                monitor, 'health', return_value=None), patch.object(monitor, 'peer', return_value=None), patch.object(
                monitor, 'russian_http', return_value='UNKNOWN: Russian HTTP HTTP 429'), patch.object(
                monitor, 'deliver', return_value=True):
                self.assertEqual(monitor.run(), 0)
            database = saved[-1]
            self.assertEqual(database['targets']['target-1-ru']['failures'], 1)
            self.assertFalse(database['targets']['target-1-ru']['pending'])


if __name__ == '__main__':
    unittest.main()
