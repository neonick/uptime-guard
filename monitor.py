"""Generic standalone monitor. Configuration never goes into public state/logs."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from notifications import format_notification, notification_batches

DEADLINE = 0.0


def request(url: str, *, body: Any = None, headers: dict | None = None,
            method: str | None = None, expected_status: int | None = None) -> Any:
    remaining = DEADLINE - time.monotonic() if DEADLINE else 10
    if remaining <= 0:
        raise TimeoutError('monitor deadline')
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
        headers={'User-Agent': 'Independent-uptime/1.0', 'Accept': 'application/json',
                 'Content-Type': 'application/json', **(headers or {})})
    with urllib.request.urlopen(req, timeout=min(10, remaining)) as response:
        if expected_status is not None and response.status != expected_status:
            raise ValueError('unexpected HTTP status')
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError('oversize response')
    return json.loads(raw)


def signed_headers(url: str, key_id: str, secret: str, *, service: str,
                   body: Any = None) -> dict[str, str]:
    parsed = urllib.parse.urlsplit(url)
    now = datetime.now(timezone.utc)
    date, stamp = now.strftime('%Y%m%d'), now.strftime('%Y%m%dT%H%M%SZ')
    payload = json.dumps(body).encode() if body is not None else b''
    digest = hashlib.sha256(payload).hexdigest()
    names = 'host;x-amz-content-sha256;x-amz-date'
    canonical = '\n'.join(['POST' if body is not None else 'GET', parsed.path, '',
        f'host:{parsed.netloc}\nx-amz-content-sha256:{digest}\nx-amz-date:{stamp}\n',
        names, digest])
    scope = f'{date}/ru-central1/{service}/aws4_request'
    signing = ('AWS4' + secret).encode()
    for part in (date, 'ru-central1', service, 'aws4_request'):
        signing = hmac.new(signing, part.encode(), hashlib.sha256).digest()
    message = '\n'.join(['AWS4-HMAC-SHA256', stamp, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    signature = hmac.new(signing, message.encode(), hashlib.sha256).hexdigest()
    return {'Authorization': f'AWS4-HMAC-SHA256 Credential={key_id}/{scope}, '
            f'SignedHeaders={names}, Signature={signature}',
            'x-amz-date': stamp, 'x-amz-content-sha256': digest}


def reason(exc: Exception) -> str:
    # Never render request URLs: Telegram URL contains a secret.
    return f'HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__


class Store:
    def __init__(self, test: bool = False):
        repo = os.environ['GITHUB_REPOSITORY']
        self.url = f'https://api.github.com/repos/{repo}/contents/' + (
            'test-state.json' if test else 'state.json')
        self.headers = {'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
                        'X-GitHub-Api-Version': '2022-11-28'}
        self.sha = None

    def load(self, bootstrap: bool) -> dict:
        try:
            doc = request(self.url, headers=self.headers)
        except urllib.error.HTTPError as exc:
            if exc.code != 404 or not bootstrap:
                raise
            return {'schema': 1, 'targets': {}, 'checks': {}, 'completed_at': None}
        self.sha = doc['sha']
        state = json.loads(base64.b64decode(doc['content'], validate=False))
        if not isinstance(state, dict) or state.get('schema') != 1 or not isinstance(
            state.get('targets'), dict):
            raise ValueError('corrupt state retained')
        for value in state['targets'].values():
            if not isinstance(value, dict) or not isinstance(value.get('pending', []), list):
                raise ValueError('corrupt target retained')
        return state

    def save(self, state: dict) -> None:
        payload = {'message': 'Record completed monitor state',
            'content': base64.b64encode(json.dumps(state, sort_keys=True).encode()).decode()}
        if self.sha:
            payload['sha'] = self.sha
        response = request(self.url, body=payload, method='PUT', headers=self.headers)
        self.sha = response['content']['sha']


def russian_nodes() -> list[str]:
    doc = request('https://check-host.net/nodes/hosts')
    found = [(name, meta) for name, meta in doc['nodes'].items()
             if meta.get('location', [''])[0] == 'ru']
    result, asns = [], set()
    for name, meta in sorted(found):
        if meta.get('asn') not in asns:
            result.append(name)
            asns.add(meta.get('asn'))
        if len(result) == 2:
            print('Russian probes:', [(node, doc['nodes'][node].get('asn')) for node in result])
            return result
    raise ValueError('fewer than two Russian providers')


def russian_http(url: str, nodes: list[str]) -> str | None:
    query = urllib.parse.urlencode([('host', url)] + [('node', node) for node in nodes])
    start = request('https://check-host.net/check-http?' + query)
    if start.get('ok') != 1 or set(start.get('nodes', {})) != set(nodes):
        return 'UNKNOWN: requested Russian nodes unavailable'
    for _ in range(6):
        results = request('https://check-host.net/check-result/' + start['request_id'])
        if all(results.get(node) for node in nodes):
            good = []
            for node in nodes:
                row = results[node][0]
                good.append(isinstance(row, list) and len(row) > 3 and
                            row[0] == 1 and str(row[3]) == '200')
            if all(good):
                return None  # HTTP only; JSON is checked independently by runner.
            return 'DEGRADED: Russian HTTP paths disagree' if any(good) else 'Russian HTTP failure'
        remaining = DEADLINE - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('monitor deadline')
        time.sleep(min(3, remaining))
    return 'UNKNOWN: Russian HTTP result pending'


def health(url: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password:
            raise ValueError('invalid target')
        data = request(url, expected_status=200)
        return None if isinstance(data, dict) and data.get('status') == 'ok' else 'missing JSON status=ok'
    except Exception as exc:
        return reason(exc)


def peer() -> str | None:
    try:
        url = 'https://storage.yandexcloud.net/' + urllib.parse.quote(
            os.environ['PEER_BUCKET']) + '/heartbeat-uptime-state.json'
        data = request(url, headers=signed_headers(url, os.environ['PEER_S3_KEY_ID'],
            os.environ['PEER_S3_SECRET'], service='s3'))
        completed = datetime.fromisoformat(data['completed_at'].replace('Z', '+00:00'))
        age = (datetime.now(timezone.utc) - completed).total_seconds()
        if not completed.tzinfo or age < -60:
            return 'UNKNOWN: invalid cloud completion timestamp'
        return None if age <= 690 else (
            f'cloud monitor silent (over 690 seconds; last completion {completed.isoformat()})')
    except Exception as exc:
        return 'UNKNOWN: cloud heartbeat ' + reason(exc)


def deliver(channel: str, text: str) -> bool:
    try:
        fault = os.environ.get('MONITOR_TEST_DELIVERY_FAULT', 'none') if os.environ.get(
            'MONITOR_MODE') in {'test-down', 'test-recovery'} else 'none'
        if channel == 'telegram':
            token = '0:synthetic-invalid-token' if fault == 'all' else os.environ['TELEGRAM_BOT_TOKEN']
            result = request('https://api.telegram.org/bot' + token +
                '/sendMessage', body={'chat_id': os.environ['TELEGRAM_CHAT_ID'],
                'text': text, 'disable_web_page_preview': True})
            return isinstance(result, dict) and result.get('ok') is True
        body = {'FromEmailAddress': os.environ['MAIL_FROM'],
            'Destination': {'ToAddresses': ['not-an-email' if fault in {'email', 'all'}
                                          else os.environ['MAIL_TO']]},
            'Content': {'Simple': {'Subject': {'Data': text.splitlines()[0], 'Charset': 'UTF-8'},
                                  'Body': {'Text': {'Data': text, 'Charset': 'UTF-8'}}}}}
        url = 'https://postbox.cloud.yandex.net/v2/email/outbound-emails'
        result = request(url, body=body, headers=signed_headers(url, os.environ['MAIL_KEY_ID'],
            os.environ['MAIL_SECRET'], service='ses', body=body))
        return isinstance(result, dict) and bool(result.get('MessageId'))
    except Exception as exc:
        print('delivery', channel, reason(exc))
        return False


def advance(old: dict, failure: str | None, at: str, threshold: int = 2,
            *, advisory: bool = False) -> dict:
    pending = [dict(event, channels=list(event['channels'])) for event in old.get('pending', [])
               if event['kind'] != 'recovery']
    if failure is None:
        # Require sustained regional success before allowing a new regional incident.
        if advisory and old.get('accepted'):
            previous_good = old.get('healthy_at')
            timely = previous_good and 0 < (datetime.fromisoformat(at) -
                datetime.fromisoformat(previous_good)).total_seconds() <= 690
            count = old.get('healthy_count', 0) + 1 if timely else 1
            since_good = (old.get('healthy_since') if timely else None) or at
            span = (datetime.fromisoformat(at) - datetime.fromisoformat(since_good)).total_seconds()
            if count < 4 or span < 690:
                return {**old, 'healthy_count': count, 'healthy_at': at, 'healthy_since': since_good,
                        'pending': []}
        return {'pending': []}
    value = {key: old[key] for key in ('id', 'since', 'failures', 'accepted', 'last_send', 'last_failure') if key in old}
    value.setdefault('id', at)
    value.setdefault('since', at)
    value.setdefault('accepted', [])
    value['failures'] = value.get('failures', 0) + 1
    value['pending'] = pending
    # One alert and one recovery per incident. Retry unacknowledged channels,
    # but never repeat an accepted alert just because time has passed.
    if value['failures'] >= threshold and not value['accepted'] and not any(
        event['kind'] == 'alert' and event['id'] == value['id'] for event in pending):
        pending.append({'kind': 'alert', 'id': value['id'], 'at': at,
                        'channels': ['telegram', 'email'], 'reason': failure})
    return value


def migrate_checker(state: dict, failure: str | None) -> None:
    """Merge legacy per-site API outages without losing channel acknowledgements."""
    legacy = []
    for key, value in list(state['targets'].items()):
        previous_reason = state.get('checks', {}).get(key, '')
        if not previous_reason:
            previous_reason = next((e.get('reason', '') for e in value.get('pending', [])
                                    if e.get('kind') != 'recovery'), '')
        if key.endswith('-ru') and previous_reason and previous_reason != 'ok' and not previous_reason.startswith('UNKNOWN:'):
            value.setdefault('last_failure', previous_reason)
        if (key.endswith('-ru') and value.get('failures') and value.get('id')
                and not value.get('last_failure') and previous_reason.startswith('UNKNOWN:')):
            legacy.append(value)
            del state['targets'][key]
    if not legacy:
        return
    merged = dict(state['targets'].get('ru-checker') or min(legacy, key=lambda v: v.get('since', '')))
    all_values = [merged, *legacy]
    accepted = list(dict.fromkeys(c for v in all_values for c in v.get('accepted', [])))
    merged['accepted'] = accepted
    merged['pending'] = []
    missing = list(dict.fromkeys(c for v in all_values for e in v.get('pending', [])
                                for c in e['channels'] if c not in accepted))
    if missing and failure:
        merged['pending'].append({'kind': 'alert', 'id': merged['id'],
            'at': merged.get('since', merged['id']), 'channels': missing, 'reason': failure})
    state['targets']['ru-checker'] = merged


def run() -> int:
    global DEADLINE
    DEADLINE = time.monotonic() + 165
    mode = os.environ.get('MONITOR_MODE', 'check')
    if mode not in {'check', 'test-down', 'test-recovery'}:
        raise ValueError('invalid mode')
    fault = os.environ.get('MONITOR_TEST_DELIVERY_FAULT', 'none')
    if fault not in {'none', 'email', 'all'} or (mode == 'check' and fault != 'none'):
        raise ValueError('delivery faults require a synthetic run')
    test = mode != 'check'
    store = Store(test)
    state = store.load(os.environ.get('MONITOR_BOOTSTRAP') == 'true')
    for value in state['targets'].values():
        value['pending'] = [event for event in value.get('pending', [])
                            if event['kind'] != 'recovery']
    targets = json.loads(os.environ['MONITOR_TARGETS'])
    if not isinstance(targets, list) or not targets:
        raise ValueError('empty targets')
    run_deadline = DEADLINE
    observation_share = 80 / (len(targets) + 1)
    labels = {'peer': 'Cloud monitor'}
    checks = {}
    try:
        nodes = russian_nodes() if not test else []
        node_error = None
    except Exception as exc:
        nodes, node_error = [], 'UNKNOWN: Russian probes ' + reason(exc)
    for index, target in enumerate(targets, 1):
        key = 'target-' + str(index)
        labels[key] = target['label']
        if test:
            checks[key] = 'TEST: synthetic HTTP failure' if mode == 'test-down' else None
            continue
        DEADLINE = min(run_deadline, time.monotonic() + observation_share)
        try:
            checks[key] = health(target['url'])
            try:
                checks[key + '-ru'] = node_error or russian_http(target['url'], nodes)
            except Exception as exc:
                checks[key + '-ru'] = 'UNKNOWN: Russian HTTP ' + reason(exc)
        finally:
            DEADLINE = run_deadline
        labels[key + '-ru'] = target['label']
    if not test:
        DEADLINE = min(run_deadline, time.monotonic() + observation_share)
        try:
            checks['peer'] = peer()
        finally:
            DEADLINE = run_deadline
    at = datetime.now(timezone.utc).isoformat()
    # Keep raw per-target observations, but track a provider outage only once.
    raw_checks = {key: value or 'ok' for key, value in checks.items()}
    unknown_ru = [key for key, failure in checks.items()
                  if key.endswith('-ru') and failure and failure.startswith('UNKNOWN:')]
    shared_failure = checks[unknown_ru[0]] if unknown_ru else None
    checks['ru-checker'] = shared_failure
    labels['ru-checker'] = 'Проверка из России'
    for key in unknown_ru:
        # Do not erase a previously confirmed regional failure with UNKNOWN.
        checks[key] = (state['targets'].get(key, {}).get('last_failure') or None)
    migrate_checker(state, shared_failure)
    state['checks'] = raw_checks
    for key, failure in checks.items():
        if key in {'peer', 'ru-checker'}:
            # Observer outages remain in raw checks, never in the delivery queue.
            state['targets'].pop(key, None)
            continue
        if key in unknown_ru:
            continue  # UNKNOWN neither advances nor recovers a confirmed regional incident.
        state['targets'][key] = advance(state['targets'].get(key, {}), failure, at,
                                         advisory=key.endswith('-ru'))
        if failure and key.endswith('-ru') and not failure.startswith('UNKNOWN:'):
            state['targets'][key]['last_failure'] = failure
    state['completed_at'] = at
    state['status'] = 'ok'  # Completed observations, not a claim that targets are healthy.
    state['delivery_ok'] = False
    store.save(state)  # Durable pending before delivery; no false heartbeat on aborted checks.
    for channel, events in notification_batches(state['targets']):
        text = format_notification(events, labels, state['checks'], test=test)
        if deliver(channel, text):
            for key, event in events:
                value = state['targets'][key]
                event['channels'].remove(channel)
                if event['kind'] != 'recovery' and event['id'] == value.get('id'):
                    value['last_send'] = at
                    if channel not in value['accepted']:
                        value['accepted'].append(channel)
            store.save(state)
    for value in state['targets'].values():
        value['pending'] = [event for event in value.get('pending', []) if event['channels']]
    state['targets'] = {key: value for key, value in state['targets'].items()
                        if value.get('pending') or value.get('failures')}
    state['delivery_ok'] = not any(value.get('pending') for value in state['targets'].values())
    store.save(state)
    print('completed', at, 'checks', len(checks), 'delivery_ok', state['delivery_ok'])
    return 0 if state['delivery_ok'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(run())
    except Exception as error:
        print('monitor did not complete:', reason(error))
        raise SystemExit(1) from None
