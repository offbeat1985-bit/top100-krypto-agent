"""Daily, read-only spot-market analysis. No trading endpoints are implemented."""
from __future__ import annotations
import argparse
import base64
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
import json
import math
import os
from pathlib import Path
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

UTC = timezone.utc
DAY = 86400
THRESHOLD = 75

class DataError(Exception):
    pass

class Client:
    def get(self, url, params=None, headers=None):
        if params:
            url += '?' + urllib.parse.urlencode(params)
        for attempt in range(5):
            try:
                request = urllib.request.Request(url, headers=headers or {})
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504):
                    raise DataError(f'HTTP {exc.code} from data provider') from None
                wait = min(60, max(2 ** (attempt + 1), int(exc.headers.get('Retry-After', '0'))))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                wait = 2 ** (attempt + 1)
            if attempt < 4:
                time.sleep(wait)
        raise DataError('Data provider unavailable after five attempts')

    def write(self, url, payload, headers):
        # Never retry writes: a timeout may mean the server accepted the request.
        request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={**headers, 'Content-Type': 'application/json'},
                                         method='PUT')
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except Exception:
            raise DataError('Persistent state write failed; notifications stopped') from None

@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

def closed_candles(rows, now):
    """Accept UTC 1D candles only; require contiguous, current, finite data."""
    cutoff = int(now.timestamp()) // DAY * DAY
    unique = {}
    for row in rows:
        c = Candle(int(row[0]), *(float(x) for x in row[1:6]))
        if c.timestamp >= cutoff:
            continue
        values = (c.open, c.high, c.low, c.close, c.volume)
        if c.timestamp % DAY or not all(math.isfinite(v) for v in values):
            raise DataError('Invalid daily candle')
        if min(values[:4]) <= 0 or c.volume < 0 or not (c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high):
            raise DataError('Invalid OHLCV values')
        if c.timestamp in unique and unique[c.timestamp] != c:
            raise DataError('Conflicting duplicate candle')
        unique[c.timestamp] = c
    candles = sorted(unique.values(), key=lambda c: c.timestamp)
    if len(candles) < 260:
        raise DataError('Fewer than 260 completed daily candles')
    if candles[-1].timestamp != cutoff - DAY:
        raise DataError('Stale daily candles')
    if any(b.timestamp - a.timestamp != DAY for a, b in zip(candles, candles[1:])):
        raise DataError('Missing daily candles')
    if sum(c.volume for c in candles[-21:-1]) <= 0:
        raise DataError('No usable volume')
    return candles

class MarketData:
    def __init__(self, client):
        self.client = client
        key = os.getenv('COINGECKO_API_KEY')
        self.cg_headers = {'x-cg-demo-api-key': key} if key else {}
        self.last_cg = 0.0
        self.binance_pairs = None
        self.kraken_pairs = None

    def cg(self, path, params):
        delay = 2.2 - (time.monotonic() - self.last_cg)
        if delay > 0:
            time.sleep(delay)
        self.last_cg = time.monotonic()
        return self.client.get('https://api.coingecko.com/api/v3/' + path, params, self.cg_headers)

    def top100(self):
        coins = self.cg('coins/markets', {'vs_currency': 'usd', 'order': 'market_cap_desc',
                                         'per_page': 100, 'page': 1, 'sparkline': 'false'})
        if not isinstance(coins, list) or len(coins) != 100:
            raise DataError('CoinGecko did not return exactly 100 coins')
        if len({c['id'] for c in coins}) != 100 or any(not c.get('market_cap') for c in coins):
            raise DataError('Invalid CoinGecko universe')
        return coins

    def candles(self, coin, now):
        # CoinGecko coin-ID tickers identify the asset; never guess from its symbol.
        tickers = self.cg('coins/' + urllib.parse.quote(coin['id'], safe='') + '/tickers',
                        {'order': 'volume_desc', 'page': 1, 'exchange_ids': 'binance,kraken'})['tickers']
        candidates = [t for t in tickers if t.get('coin_id') == coin['id']
                      and not t.get('is_anomaly') and not t.get('is_stale')
                      and t['market']['identifier'] in ('binance', 'kraken')
                      and t['target'] in ('USD', 'USDT', 'USDC')]
        candidates.sort(key=lambda t: float(t.get('converted_volume', {}).get('usd') or 0), reverse=True)
        errors = []
        for t in candidates[:8]:
            try:
                exchange = t['market']['identifier']
                base, quote = t['base'], t['target']
                if exchange == 'binance':
                    if self.binance_pairs is None:
                        info = self.client.get('https://api.binance.com/api/v3/exchangeInfo')
                        self.binance_pairs = {(p['baseAsset'], p['quoteAsset']): p['symbol']
                                              for p in info['symbols'] if p['status'] == 'TRADING'
                                              and p.get('isSpotTradingAllowed', True)}
                    pair = self.binance_pairs.get((base, quote))
                    if not pair:
                        continue
                    raw = self.client.get('https://api.binance.com/api/v3/klines',
                                          {'symbol': pair, 'interval': '1d', 'limit': 400})
                    rows = [[int(r[0]) // 1000, *r[1:6]] for r in raw]
                else:
                    if self.kraken_pairs is None:
                        result = self.client.get('https://api.kraken.com/0/public/AssetPairs')
                        if result.get('error'):
                            raise DataError('Kraken pair lookup failed')
                        self.kraken_pairs = {p.get('wsname'): key for key, p in result['result'].items()}
                    aliases = {'BTC': 'XBT', 'DOGE': 'XDG'}
                    pair = self.kraken_pairs.get(f'{aliases.get(base, base)}/{quote}')
                    if not pair:
                        continue
                    raw = self.client.get('https://api.kraken.com/0/public/OHLC',
                                          {'pair': pair, 'interval': 1440})
                    if raw.get('error'):
                        raise DataError('Kraken OHLC request failed')
                    series = [v for k, v in raw['result'].items() if k != 'last']
                    rows = [[r[0], *r[1:5], r[6]] for r in series[0]]
                return closed_candles(rows, now), f'{exchange}:{pair}'
            except (DataError, KeyError, IndexError, TypeError, ValueError) as exc:
                errors.append(type(exc).__name__)
        raise DataError('No verified USD/USDT/USDC market with valid 1D history' +
                        (f' ({", ".join(errors)})' if errors else ''))

def ema(values, period):
    if len(values) < period:
        raise DataError('Insufficient EMA history')
    result = [statistics.mean(values[:period])]
    alpha = 2 / (period + 1)
    for value in values[period:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result

def pivots(candles, high=True, radius=2):
    values = [c.high if high else c.low for c in candles]
    # Two right-side candles must already be closed: confirmed pivots only.
    return [(i, values[i]) for i in range(radius, len(values) - radius)
            if (values[i] > max(values[i-radius:i] + values[i+1:i+radius+1]) if high
                else values[i] < min(values[i-radius:i] + values[i+1:i+radius+1]))]

def score(candles, direction):
    long = direction == 'LONG'
    last = candles[-1]
    close = [c.close for c in candles]
    averages = {p: ema(close, p) for p in (20, 50, 100, 200)}
    ordered = [last.close] + [averages[p][-1] for p in (20, 50, 100, 200)]
    compare = (lambda a, b: a > b) if long else (lambda a, b: a < b)
    components = {'ema_alignment': 20 if all(compare(a, b) for a, b in zip(ordered, ordered[1:])) else 0,
                  'ema_slope': 10 if all(compare(averages[p][-1], averages[p][-6]) for p in (20, 50)) else 0}
    highs, lows = pivots(candles[-90:]), pivots(candles[-90:], False)
    components['structure'] = 20 if len(highs) >= 2 and len(lows) >= 2 and compare(highs[-1][1], highs[-2][1]) and compare(lows[-1][1], lows[-2][1]) else 0
    vol_ratio = last.volume / statistics.mean(c.volume for c in candles[-21:-1])
    components['volume'] = 15 if vol_ratio >= 1.5 else (8 if vol_ratio >= 1.2 else 0)
    history = candles[-21:-1]
    level = max(c.high for c in history) if long else min(c.low for c in history)
    breakout = compare(last.close, level) and compare(last.close, last.open)
    retest = False
    anchor = None
    # Recent breakout against the preceding 20-day liquidity boundary, then retest.
    for i in range(len(candles)-6, len(candles)-1):
        prior = candles[i-20:i]
        boundary = max(c.high for c in prior) if long else min(c.low for c in prior)
        if compare(candles[i].close, boundary) and not compare(candles[i-1].close, boundary):
            holds = all(compare(c.close, boundary * (0.995 if long else 1.005)) for c in candles[i+1:])
            touch = last.low <= boundary * 1.005 if long else last.high >= boundary * 0.995
            if holds and touch and compare(last.close, boundary) and compare(last.close, last.open):
                retest, anchor = True, boundary
    components['breakout'] = 20 if breakout else 0
    components['retest'] = 10 if retest else 0
    atr = statistics.mean(max(c.high-c.low, abs(c.high-prev.close), abs(c.low-prev.close))
                          for prev, c in zip(candles[-15:-1], candles[-14:]))
    relevant = lows if long else highs
    stop = relevant[-1][1] if relevant else (min(c.low for c in history) if long else max(c.high for c in history))
    risk = (last.close-stop) if long else (stop-last.close)
    components['liquidity_risk'] = 5 if 0.5 * atr <= risk <= 3 * atr else 0
    total = sum(components.values())
    # Strong trend alone is insufficient: an actionable closed breakout/retest is required.
    eligible = (breakout or retest) and risk > 0
    return {'direction': direction, 'score': total, 'eligible': eligible,
            'components': components, 'close': last.close, 'stop_reference': stop,
            'liquidity_level': anchor if retest else level, 'volume_ratio': round(vol_ratio, 3),
            'setup': 'retest' if retest else 'breakout', 'candle': last.timestamp,
            'ema': {str(p): averages[p][-1] for p in averages}}

def strong_signals(candles):
    return [s for d in ('LONG', 'SHORT') if (s := score(candles, d))['eligible'] and s['score'] >= THRESHOLD]

def scheduled_slot(schedule, now):
    local = now.astimezone(ZoneInfo('Europe/Zurich'))
    # Use cron identity, not runner wall-clock hour: GitHub may start jobs late.
    expected = '0 6 * * *' if local.utcoffset() == timedelta(hours=2) else '0 7 * * *'
    return schedule == expected

class StateStore:
    def __init__(self, client):
        self.client = client
        repo = os.environ['GITHUB_REPOSITORY']
        self.url = f'https://api.github.com/repos/{repo}/contents/.state/signals.json'
        self.headers = {'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
                        'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
        self.sha = None

    def load(self):
        # Direct request to distinguish a genuinely missing file from access failure.
        request = urllib.request.Request(self.url, headers=self.headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise DataError('Persistent state missing; restore it before sending') from None
            raise DataError('Cannot load persistent state') from None
        self.sha = payload['sha']
        state = json.loads(base64.b64decode(payload['content']))
        if state.get('version') != 1 or not isinstance(state.get('coins'), dict):
            raise DataError('Invalid persistent state')
        return state

    def save(self, state):
        payload = {'message': 'Persist signal notification reservations [skip ci]',
                   'content': base64.b64encode(json.dumps(state, sort_keys=True).encode()).decode(),
                   'sha': self.sha}
        result = self.client.write(self.url, payload, self.headers)
        self.sha = result['content']['sha']


def reserve(state, coin_id, signals, candle):
    """Notify only when a strong directional episode starts, once per closed day."""
    old = state['coins'].get(coin_id, {'candle': 0, 'active': []})
    if candle <= old['candle']:
        return []
    active = [s['direction'] for s in signals]
    new = [s for s in signals if s['direction'] not in old['active']]
    state['coins'][coin_id] = {'candle': candle, 'active': active}
    return new


def telegram(client, token, chat, message):
    # POST exactly once, never expose URL/token in logs, no trading integration.
    url = 'https://api.telegram.org/bot' + token + '/sendMessage'
    req = urllib.request.Request(url, data=json.dumps({'chat_id': chat, 'text': message,
                                  'disable_web_page_preview': True}).encode(),
                                  headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.load(response)
        if not payload.get('ok'):
            raise ValueError()
    except Exception:
        raise DataError('Telegram delivery failed or uncertain; reserved signal will not be resent') from None


def message(coin, signal, source):
    date = datetime.fromtimestamp(signal['candle'], UTC).date()
    reasons = ', '.join(f'{k}={v}' for k, v in signal['components'].items())
    return (f"{coin['name']} ({coin['symbol'].upper()}) {signal['direction']} {signal['score']}/100\n"
            f"Abgeschlossene UTC-Tageskerze: {date} | {source}\n"
            f"Setup: {signal['setup']} | Schluss: {signal['close']:.8g}\n"
            f"Liquiditätszone: {signal['liquidity_level']:.8g} | Stop-Referenz: {signal['stop_reference']:.8g}\n"
            f"Volumenfaktor: {signal['volume_ratio']}\n{reasons}\n"
            "Technisches Signal, keine Order. Short bezeichnet eine fallende Markterwartung.")


def run(dry_run=False):
    client = Client()
    now = datetime.now(UTC)
    report = {'started_at': now.isoformat(), 'dry_run': dry_run, 'universe_count': 0,
              'analyzed_count': 0, 'data_gaps': [], 'new_strong_signals': [], 'sent': 0}
    store = None
    state = {'version': 1, 'coins': {}}
    try:
        if not dry_run:
            if not os.getenv('TELEGRAM_BOT_TOKEN') or not os.getenv('TELEGRAM_CHAT_ID'):
                raise DataError('Required GitHub Secrets TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing')
            store = StateStore(client)
            state = store.load()
        data = MarketData(client)
        coins = data.top100()
        report['universe_count'] = len(coins)
        notices = []
        for coin in coins:
            try:
                candles, source = data.candles(coin, now)
                signals = strong_signals(candles)
                report['analyzed_count'] += 1
                for signal in reserve(state, coin['id'], signals, candles[-1].timestamp):
                    report['new_strong_signals'].append({'coin_id': coin['id'], 'source': source, **signal})
                    notices.append(message(coin, signal, source))
            except (DataError, KeyError, ValueError, TypeError) as exc:
                report['data_gaps'].append({'coin_id': coin['id'], 'reason': str(exc)})
        if report['analyzed_count'] < 50:
            raise DataError('Less than 50/100 assets analyzable; degraded scan, no notifications')
        if store:
            # Reserve the complete batch durably BEFORE contacting Telegram.
            store.save(state)
            for notice in notices:
                telegram(client, os.environ['TELEGRAM_BOT_TOKEN'], os.environ['TELEGRAM_CHAT_ID'], notice)
                report['sent'] += 1
        report['status'] = 'partial' if report['data_gaps'] else 'complete'
    except Exception as exc:
        # Unexpected exception messages may contain request URLs; do not print them.
        report['status'] = 'failed'
        report['error'] = str(exc) if isinstance(exc, DataError) else type(exc).__name__
    finally:
        Path('reports').mkdir(exist_ok=True)
        Path('reports/latest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('status', 'universe_count', 'analyzed_count', 'sent')}))
    return 1 if report['status'] == 'failed' else 0

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--check-schedule')
    args = parser.parse_args()
    if args.check_schedule:
        print('true' if scheduled_slot(args.check_schedule, datetime.now(UTC)) else 'false')
    else:
        raise SystemExit(run(args.dry_run))
