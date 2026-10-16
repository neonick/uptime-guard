"""Operator-facing notices; technical reasons and delivery ACKs stay in state."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

MOSCOW = timezone(timedelta(hours=3))


def moscow_time(value: str) -> str:
    try:
        at = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if at.tzinfo is None:
            raise ValueError('missing timezone')
        return at.astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M МСК')
    except (ValueError, TypeError, AttributeError):
        return 'время неизвестно'


def notification_batches(targets: dict) -> list[tuple[str, list[tuple[str, dict]]]]:
    """Combine matching checker failures, preserving each event's channel ACK."""
    batches: dict[tuple, list[tuple[str, dict]]] = {}
    for key, value in targets.items():
        for event in value.get('pending', []):
            for channel in event['channels'][:]:
                group = 'ru-checker' if key.endswith('-ru') and event['reason'].startswith(
                    'UNKNOWN:') else key
                identity = (channel, group, event['kind'], event['reason'], event['at'])
                batches.setdefault(identity, []).append((key, event))
    return [(identity[0], events) for identity, events in batches.items()]


def _site_reason(reason: str) -> str:
    codes = {
        '401': 'Сайт потребовал авторизацию',
        '403': 'Сайт отклонил доступ',
        '404': 'Адрес проверки не найден',
        '429': 'Сайт ограничил частоту запросов',
        '500': 'Приложение ответило внутренней ошибкой',
        '502': 'Веб-сервер не получил нормальный ответ приложения',
        '503': 'Сервер сообщил о временной недоступности',
        '504': 'Веб-сервер не дождался ответа приложения',
    }
    if reason.startswith('HTTP '):
        code = reason.removeprefix('HTTP ')
        return codes.get(code, 'Сайт ответил ошибкой') + f' (HTTP {code}).'
    if reason == 'missing JSON status=ok':
        return 'Сервер ответил, но приложение не подтвердило нормальную работу.'
    if reason in {'TimeoutError', 'URLError', 'ConnectionError', 'OSError'}:
        return 'Проверка не смогла получить ответ сайта: ошибка соединения или ожидания.'
    return 'Проверка не подтвердила нормальную работу сайта.'


def _checker_reason(reason: str) -> str:
    if 'HTTP 429' in reason:
        return 'Check-host ограничил запросы проверки (HTTP 429 — лимит частоты).'
    if 'result pending' in reason:
        return 'Check-host не успел вернуть результаты проверки.'
    if 'nodes unavailable' in reason or 'Russian probes' in reason:
        return 'Не удалось получить доступные российские узлы Check-host.'
    return 'Не удалось получить результат от Check-host, сервиса проверки из России.'


def format_notification(events: list[tuple[str, dict]], labels: dict[str, str],
                        checks: dict[str, str], *, test: bool = False) -> str:
    key, event = events[0]
    kind, reason = event['kind'], event['reason']
    recovery = kind == 'recovery'
    russian = key.endswith('-ru')
    checker = key == 'ru-checker'
    keys = [item_key.removesuffix('-ru') for item_key, _ in events]
    names = ', '.join(dict.fromkeys(labels.get(item_key, 'Сайт') for item_key in keys))
    body: list[str] = []
    if key == 'peer':
        if recovery:
            title = '✅ Облачный монитор снова регулярно проверяет сайты'
            body.append('Получены четыре последовательных своевременных результата проверки.')
            action = 'Срочных действий нет.'
        elif reason.startswith('cloud monitor silent'):
            title = '🟡 Облачный монитор: нет свежих проверок'
            body.append('От облачного монитора нет отчёта дольше допустимых 11 мин 30 сек.')
            if '; last completion ' in reason:
                completed = reason.split('; last completion ', 1)[1].removesuffix(')')
                body.append('Последний отчёт: ' + moscow_time(completed) + '.')
            action = 'Проверить запуск облачного монитора и его расписание.'
        else:
            title = '🟡 Не удалось проверить облачный монитор'
            body.append('Не удалось прочитать его последний отчёт. Работает ли он, неизвестно.')
            action = 'Проверить доступ к отчётам мониторинга; это не подтверждение падения сайтов.'
        keys = [item_key for item_key in checks if item_key != 'peer' and not item_key.endswith('-ru')]
    elif checker:
        keys = [item_key for item_key in checks if item_key != 'peer' and not item_key.endswith('-ru')]
        if recovery:
            title = '✅ Проверка из России снова работает'
            body.append('Сервис внешних проверок снова регулярно возвращает результаты.')
        else:
            title = '🟡 Проверка из России временно недоступна'
            body.append(_checker_reason(reason))
            body.append('Это сбой проверки, а не подтверждённое падение сайтов.')
        action = 'При ответах сайтов срочных действий нет. Если сбой затянется, проверить сервис наблюдения.'
    elif russian:
        if recovery:
            title = f'✅ {names}: проверка из России снова проходит'
            body.append('Российские узлы снова получили нормальный ответ сайта.')
            body.append('Это проверка доступности, а не всех функций приложения.')
            action = 'Срочных действий по этой проверке нет.'
        elif reason.startswith('UNKNOWN:'):
            title = f'🟡 Проверка из России недоступна: {names}'
            body.append(_checker_reason(reason))
            body.append('Доступность сайтов из России по этой проверке неизвестна.')
            action = 'Восстановить проверку из России. Эта ошибка сама по себе не требует перезапуска сайтов.'
        elif reason.startswith('DEGRADED:'):
            title = f'⚠️ {names}: доступность из России различается'
            body.append('У одного российского провайдера сайт отвечает, у другого проверка не прошла.')
            action = 'Открыть сайт из российской сети; при повторной ошибке проверить сетевую доступность.'
        else:
            title = f'🔴 {names}: сайт не прошёл проверку из России'
            body.append('Российские узлы не подтвердили доступность сайта.')
            action = 'Открыть сайт из российской сети; если ошибка подтверждается, проверить сервер.'
    elif recovery:
        title = f'✅ {names}: проверка сайта снова проходит'
        body.append('Сайт ответил нормально, приложение подтвердило работоспособность.')
        action = 'Срочных действий по этому событию нет.'
    else:
        title = f'🔴 {names}: проверка сайта не прошла'
        body.append(_site_reason(reason))
        action = 'Открыть сайт. Если ошибка подтверждается, нужна проверка приложения и сервера.'

    if kind == 'reminder':
        body.append('Повторное уведомление: проблема остаётся.')
    if not test and (russian or checker or key == 'peer'):
        for item_key in dict.fromkeys(keys):
            if item_key in checks:
                status = 'сайт отвечает нормально' if checks[item_key] == 'ok' else 'не прошла'
                body.append(f'Прямая проверка {labels.get(item_key, "сайта")}: {status}.')
    body.append('Результат проверки: ' + moscow_time(event['at']) + '.')
    if not recovery:
        starts = {item['id'] for _, item in events}
        if len(starts) == 1:
            body.append('Проблема впервые замечена: ' + moscow_time(event['id']) + '.')
        else:
            for item_key, item in events:
                name = labels.get(item_key.removesuffix('-ru'), 'Сайт')
                body.append(f'Проблема {name} замечена: {moscow_time(item["id"])}.')
    if test:
        title = '[ТЕСТ] ' + title
        body.append('Это тест доставки; реальные сайты ради него не отключались.')
        action = 'Ничего делать не нужно.'
    return '\n'.join([title, '', *body, '', 'Действие: ' + action])
