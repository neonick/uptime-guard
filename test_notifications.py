import unittest

from notifications import format_notification, moscow_time, notification_batches

AT = '2026-10-13T09:17:33.636323+00:00'
SINCE = '2026-10-12T20:25:05.919956+00:00'
LABELS = {'target-1': 'Alpha', 'target-2': 'Beta'}
CHECKS = {'target-1': 'ok', 'target-2': 'ok'}


def event(reason, *, kind='reminder', channels=None):
    return {'kind': kind, 'reason': reason, 'id': SINCE, 'at': AT,
            'channels': ['telegram', 'email'] if channels is None else channels}


class NotificationTests(unittest.TestCase):
    def test_check_host_limit_is_one_notice_and_does_not_claim_site_failure(self):
        targets = {key: {'pending': [event('UNKNOWN: Russian HTTP HTTP 429')]}
                   for key in ('target-1-ru', 'target-2-ru')}
        batches = notification_batches(targets)
        self.assertEqual([channel for channel, _ in batches], ['telegram', 'email'])
        self.assertEqual([len(events) for _, events in batches], [2, 2])
        text = format_notification(batches[0][1], LABELS, CHECKS)
        self.assertIn('Проверка из России недоступна: Alpha, Beta', text)
        self.assertIn('Check-host ограничил запросы', text)
        self.assertIn('Прямая проверка Alpha: сайт отвечает нормально', text)
        self.assertIn('Прямая проверка Beta: сайт отвечает нормально', text)
        self.assertIn('Действие: Восстановить проверку из России', text)
        self.assertIn('13.10.2026 12:17 МСК', text)
        self.assertIn('12.10.2026 23:25 МСК', text)
        for raw in ('UNKNOWN', 'reminder:', 'Event:', 'UTC:', 'without JSON', '+00:00'):
            self.assertNotIn(raw, text)

    def test_failed_direct_check_is_visible_even_when_russian_checker_is_unknown(self):
        text = format_notification([('target-1-ru', event('UNKNOWN: Russian HTTP HTTP 429'))],
                                   LABELS, {'target-1': 'HTTP 502'})
        self.assertIn('Прямая проверка Alpha: не прошла', text)
        self.assertNotIn('сайт отвечает нормально', text)

    def test_a_site_rate_limit_is_distinct_from_a_checker_rate_limit(self):
        text = format_notification([('target-1', event('HTTP 429', kind='alert'))], LABELS, CHECKS)
        self.assertIn('Сайт ограничил частоту запросов', text)
        self.assertNotIn('Check-host', text)

    def test_cloud_silence_shows_last_report_and_current_site_checks(self):
        reason = 'cloud monitor silent (over 690 seconds; last completion 2026-10-12T20:24:10+00:00)'
        text = format_notification([('peer', event(reason))], LABELS, CHECKS)
        self.assertIn('Последний отчёт: 12.10.2026 23:24 МСК', text)
        self.assertIn('11 мин 30 сек', text)
        self.assertIn('Прямая проверка Alpha: сайт отвечает нормально', text)
        self.assertIn('Проверить запуск облачного монитора и его расписание', text)

    def test_unreadable_cloud_report_does_not_assert_a_stopped_scheduler(self):
        text = format_notification([('peer', event('UNKNOWN: cloud heartbeat HTTP 403'))], LABELS, CHECKS)
        self.assertIn('Не удалось прочитать его последний отчёт', text)
        self.assertNotIn('нет отчёта дольше', text)
        self.assertIn('Проверить доступ к отчётам', text)

    def test_peer_recovery_does_not_claim_scheduled_runs_are_repaired(self):
        text = format_notification([('peer', event('healthy', kind='recovery'))], LABELS, CHECKS)
        self.assertIn('регулярно проверяет', text)
        self.assertIn('четыре последовательных', text)
        self.assertNotIn('расписание восстановлено', text)

    def test_russian_recovery_and_disagreement_preserve_scope_of_observation(self):
        recovery = format_notification([('target-1-ru', event('healthy', kind='recovery'))], LABELS, CHECKS)
        self.assertIn('не всех функций приложения', recovery)
        disagreement = format_notification([('target-1-ru', event('DEGRADED: Russian HTTP paths disagree'))],
                                           LABELS, CHECKS)
        self.assertIn('У одного российского провайдера', disagreement)
        self.assertNotIn('Check-host ограничил', disagreement)

    def test_separate_incidents_and_channel_receipts_are_preserved(self):
        one = event('UNKNOWN: Russian HTTP HTTP 429', channels=['email'])
        two = event('UNKNOWN: Russian HTTP HTTP 429')
        batches = notification_batches({'target-1-ru': {'pending': [one]}, 'target-2-ru': {'pending': [two]}})
        self.assertEqual([(channel, len(events)) for channel, events in batches], [('email', 2), ('telegram', 1)])
        different = event('UNKNOWN: Russian HTTP TimeoutError')
        batches = notification_batches({'target-1-ru': {'pending': [one]}, 'target-2-ru': {'pending': [different]}})
        self.assertEqual(len(batches), 3)

    def test_moscow_date_changes_and_invalid_timestamps_stay_unknown(self):
        self.assertEqual(moscow_time('2026-10-12T23:59:00Z'), '13.10.2026 02:59 МСК')
        self.assertEqual(moscow_time('2026-10-12T23:59:00'), 'время неизвестно')
        self.assertEqual(moscow_time('invalid'), 'время неизвестно')

    def test_synthetic_delivery_has_a_clear_test_label_and_no_action(self):
        text = format_notification([('target-1', event('TEST: synthetic HTTP failure', kind='alert'))],
                                   LABELS, CHECKS, test=True)
        self.assertTrue(text.startswith('[ТЕСТ]'))
        self.assertIn('реальные сайты ради него не отключались', text)
        self.assertIn('Действие: Ничего делать не нужно', text)


if __name__ == '__main__':
    unittest.main()
