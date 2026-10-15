"""Four-hour, read-only spot-market analysis. No trading endpoints are implemented."""
from __future__ import annotations
import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import json
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import math
import os
from pathlib import Path
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request

UTC = timezone.utc
DAY = 86400
BAR = 4 * 3600
TIMEFRAME = '4h'
THRESHOLD = 75

class DataError(Exception):
    pass

class JsonCache:
    """Non-secret, disposable cache; corrupt entries are treated as misses."""
    def __init__(self, directory=None):
        self.directory = Path(directory or os.getenv('AGENT_CACHE_DIR', '.cache/crypto4h-v1'))
        self.lock = threading.RLock()

    def path(self, key):
        return self.directory / (hashlib.sha256(key.encode()).hexdigest() + '.json')

    def get(self, key, ttl):
        with self.lock:
            try:
                payload = json.loads(self.path(key).read_text())
                if 0 <= time.time() - payload['saved'] < ttl:
                    return payload['data']
            except (OSError, ValueError, TypeError, KeyError):
                pass
        return None

    def put(self, key, data):
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self.path(key)
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps({'saved': time.time(), 'data': data}))
            temporary.replace(target)

class Client:
    def __init__(self):
        self.cache = JsonCache()
        self.guard = threading.RLock()
        self.host_locks = {}
        self.next_request = {}
        self.blocked = {}

    def get(self, url, params=None, headers=None):
        if params:
            url += '?' + urllib.parse.urlencode(params)
        parsed = urllib.parse.urlsplit(url)
        host, endpoint = parsed.netloc, parsed.path
        with self.guard:
            lock = self.host_locks.setdefault(host, threading.RLock())
        interval = 2.2 if host == 'api.coingecko.com' else 0.4
        catalog = endpoint in ('/api/v3/exchangeInfo', '/0/public/AssetPairs',
                               '/api/v5/public/instruments', '/api/v2/symbols')
        cached = self.cache.get('catalog:' + url, 6 * 3600) if catalog else None
        if cached is not None:
            return cached
        last_error = 'unknown provider failure' 
        for attempt in range(5):
            try:
                with lock:
                    if endpoint in self.blocked:
                        raise DataError(self.blocked[endpoint])
                    # Recheck catalogue cache after another worker populated it.
                    cached = self.cache.get('catalog:' + url, 6 * 3600) if catalog else None
                    if cached is not None:
                        return cached
                    delay = self.next_request.get(host, 0) - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    self.next_request[host] = time.monotonic() + interval
                    request = urllib.request.Request(url, headers=headers or {})
                    with urllib.request.urlopen(request, timeout=20) as response:
                        result = json.load(response)
                    valid_catalog = (isinstance(result, dict)
                                     and not result.get('error')
                                     and result.get('code', '0') in ('0', '200000')
                                     and any(k in result for k in ('symbols', 'result', 'data')))
                    if catalog and valid_catalog:
                        self.cache.put('catalog:' + url, result)
                    return result
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 451):
                    with lock:
                        self.blocked[endpoint] = f'HTTP {exc.code}: endpoint unavailable for this run'
                if exc.code not in (429, 500, 502, 503, 504):
                    raise DataError(f'HTTP {exc.code} from data provider') from None
                last_error = f'HTTP {exc.code}'
                try:
                    retry_after = int(exc.headers.get('Retry-After', '0'))
                except (TypeError, ValueError):
                    retry_after = 0
                wait = min(60, max(2 ** (attempt + 1), retry_after))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = ('Invalid JSON response' if isinstance(exc, json.JSONDecodeError)
                              else 'Network connection failure or timeout')
                wait = 2 ** (attempt + 1)
            with lock:
                self.next_request[host] = max(self.next_request.get(host, 0), time.monotonic() + wait)
            if attempt < 4:
                time.sleep(wait)
        raise DataError(f'Data provider unavailable after five attempts: {last_error}')

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
    """Accept completed UTC 4H candles only; require contiguous, current, finite data."""
    cutoff = int(now.timestamp()) // BAR * BAR
    unique = {}
    for row in rows:
        c = Candle(int(row[0]), *(float(x) for x in row[1:6]))
        if c.timestamp >= cutoff:
            continue
        values = (c.open, c.high, c.low, c.close, c.volume)
        if c.timestamp % BAR or not all(math.isfinite(v) for v in values):
            raise DataError('Invalid 4H candle')
        if min(values[:4]) <= 0 or c.volume < 0 or not (c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high):
            raise DataError('Invalid OHLCV values')
        if c.timestamp in unique and unique[c.timestamp] != c:
            raise DataError('Conflicting duplicate candle')
        unique[c.timestamp] = c
    candles = sorted(unique.values(), key=lambda c: c.timestamp)
    if len(candles) < 260:
        raise DataError('Fewer than 260 completed 4H candles')
    if candles[-1].timestamp != cutoff - BAR:
        raise DataError('Stale 4H candles')
    if any(b.timestamp - a.timestamp != BAR for a, b in zip(candles, candles[1:])):
        raise DataError('Missing 4H candles')
    if sum(c.volume for c in candles[-21:-1]) <= 0:
        raise DataError('No usable volume')
    return candles

class MarketData:
    def __init__(self, client):
        self.client = client
        key = os.getenv('COINGECKO_API_KEY')
        self.cg_headers = {'x-cg-demo-api-key': key} if key else {}
        self.last_cg = 0.0
        self.cg_lock = threading.RLock()
        self.cache = JsonCache()
        self.excluded_stablecoins = 0
        self.binance_pairs = None
        self.kraken_pairs = None
        self.okx_pairs = None
        self.kucoin_pairs = None

    def cg(self, path, params):
        # Shared across workers: parallel scanning must not multiply CoinGecko rate.
        with self.cg_lock:
            delay = 2.2 - (time.monotonic() - self.last_cg)
            if delay > 0:
                time.sleep(delay)
            self.last_cg = time.monotonic()
            return self.client.get('https://api.coingecko.com/api/v3/' + path, params, self.cg_headers)

    def stablecoin_ids(self):
        categories = self.cg('coins/categories/list', {})
        if not isinstance(categories, list):
            raise DataError('CoinGecko category classification unavailable')
        ids = {c['category_id'] for c in categories
               if 'stablecoin' in c.get('category_id', '')
               and not any(word in (c.get('category_id', '') + ' ' + c.get('name', '')).lower()
                           for word in ('issuer', 'protocol', 'ecosystem', 'governance'))}
        if 'stablecoins' not in ids:
            raise DataError('Required stablecoin category missing; universe selection stopped')
        stable = set()
        for category in sorted(ids):
            for page in range(1, 11):
                rows = self.cg('coins/markets', {'vs_currency': 'usd', 'category': category,
                               'order': 'market_cap_desc', 'per_page': 250, 'page': page})
                if not isinstance(rows, list):
                    raise DataError('Stablecoin category membership unavailable')
                stable.update(c['id'] for c in rows)
                if len(rows) < 250:
                    break
            else:
                raise DataError('Stablecoin category pagination incomplete')
        if not stable:
            raise DataError('Empty stablecoin classification; universe selection stopped')
        return stable

    def top100(self):
        stable = self.stablecoin_ids()
        selected, seen = [], set()
        for page in range(1, 11):
            coins = self.cg('coins/markets', {'vs_currency': 'usd', 'order': 'market_cap_desc',
                                             'per_page': 250, 'page': page, 'sparkline': 'false'})
            if not isinstance(coins, list):
                raise DataError('Invalid CoinGecko universe')
            for c in coins:
                if c['id'] in seen:
                    continue
                seen.add(c['id'])
                if c['id'] in stable:
                    self.excluded_stablecoins += 1
                    continue
                if not c.get('market_cap'):
                    continue
                selected.append(c)
                if len(selected) == 100:
                    return selected
            if len(coins) < 250:
                break
        raise DataError('Fewer than 100 classified non-stablecoins available')

    def tickers(self, coin):
        key = 'tickers:' + coin['id']
        cached = self.cache.get(key, 6 * 3600)
        if isinstance(cached, list) and cached:
            return cached
        result = []
        for page in range(1, 4):
            batch = self.cg('coins/' + urllib.parse.quote(coin['id'], safe='') + '/tickers',
                            {'order': 'volume_desc', 'page': page,
                             'exchange_ids': 'binance,kraken,okex,kucoin'})['tickers']
            result.extend(batch)
            if len(batch) < 100:
                break
        # Do not persist empty results: allow temporary indexing gaps to recover.
        if result:
            self.cache.put(key, result)
        return result

    def history(self, exchange, base, quote):
        key = f'candles:4h:{exchange}:{base}:{quote}'
        rows = self.cache.get(key, 14 * DAY)
        if not isinstance(rows, list) or not rows:
            return []
        try:
            previous_cutoff = max(int(r[0]) for r in rows) + BAR
            closed_candles(rows, datetime.fromtimestamp(previous_cutoff, UTC))
            return rows
        except (DataError, ValueError, TypeError, IndexError):
            return []

    def candles(self, coin, now):
        # CoinGecko coin-ID tickers identify the asset; never guess from its symbol.
        tickers = self.tickers(coin)
        candidates = [t for t in tickers if t.get('coin_id') == coin['id']
                      and not t.get('is_anomaly') and not t.get('is_stale')
                      and t['market']['identifier'] in ('binance', 'kraken', 'okex', 'kucoin')
                      and t['target'] in ('USD', 'USDT', 'USDC')]
        candidates.sort(key=lambda t: float(t.get('converted_volume', {}).get('usd') or 0), reverse=True)
        errors = []
        # Round-robin exchanges so one venue cannot exhaust all fallback attempts.
        groups = {}
        for t in candidates:
            groups.setdefault(t['market']['identifier'], []).append(t)
        attempts = [group[i] for i in range(3) for group in groups.values() if len(group) > i]
        for t in attempts:
            try:
                exchange = t['market']['identifier']
                base, quote = t['base'], t['target']
                cached_rows = self.history(exchange, base, quote)
                since = max(int(r[0]) for r in cached_rows) - 2 * BAR if cached_rows else None
                if exchange == 'binance':
                    if self.binance_pairs is None:
                        info = self.client.get('https://api.binance.com/api/v3/exchangeInfo')
                        self.binance_pairs = {(p['baseAsset'], p['quoteAsset']): p['symbol']
                                              for p in info['symbols'] if p['status'] == 'TRADING'
                                              and p.get('isSpotTradingAllowed', True)}
                    pair = self.binance_pairs.get((base, quote))
                    if not pair:
                        raise DataError('Market absent from active spot catalogue')
                    params = {'symbol': pair, 'interval': '4h', 'limit': 400}
                    if since is not None:
                        params['startTime'] = since * 1000
                    raw = self.client.get('https://api.binance.com/api/v3/klines', params)
                    rows = [[int(r[0]) // 1000, *r[1:6]] for r in raw]
                elif exchange == 'kraken':
                    if self.kraken_pairs is None:
                        result = self.client.get('https://api.kraken.com/0/public/AssetPairs')
                        if result.get('error'):
                            raise DataError('Kraken pair lookup failed')
                        self.kraken_pairs = {p.get('wsname'): key for key, p in result['result'].items()}
                    aliases = {'BTC': 'XBT', 'DOGE': 'XDG'}
                    pair = self.kraken_pairs.get(f'{aliases.get(base, base)}/{quote}')
                    if not pair:
                        raise DataError('Market absent from active spot catalogue')
                    params = {'pair': pair, 'interval': 240}
                    if since is not None:
                        params['since'] = since
                    raw = self.client.get('https://api.kraken.com/0/public/OHLC', params)
                    if raw.get('error'):
                        raise DataError('Kraken OHLC request failed')
                    series = [v for k, v in raw['result'].items() if k != 'last']
                    rows = [[r[0], *r[1:5], r[6]] for r in series[0]]
                elif exchange == 'okex':
                    if self.okx_pairs is None:
                        raw = self.client.get('https://www.okx.com/api/v5/public/instruments', {'instType': 'SPOT'})
                        if raw.get('code') != '0':
                            raise DataError('OKX spot catalogue rejected request')
                        self.okx_pairs = {(p['baseCcy'], p['quoteCcy']): p['instId']
                                          for p in raw['data'] if p['state'] == 'live'}
                    pair = self.okx_pairs.get((base, quote))
                    if not pair:
                        raise DataError('Market absent from active spot catalogue')
                    rows = []
                    cursor = None
                    for _ in range(5):
                        params = {'instId': pair, 'bar': '4H', 'limit': 100}
                        if cursor is not None:
                            params['after'] = cursor
                        raw = self.client.get('https://www.okx.com/api/v5/market/history-candles', params)
                        if raw.get('code') != '0':
                            raise DataError('OKX historical candles rejected request')
                        batch = raw['data']
                        if not batch:
                            break
                        # confirm=1 means the exchange considers this candle complete.
                        rows.extend([[int(r[0]) // 1000, *r[1:6]] for r in batch if str(r[8]) == '1'])
                        oldest = min(int(r[0]) for r in batch)
                        if cursor is not None and oldest >= cursor:
                            raise DataError('OKX pagination did not advance')
                        cursor = oldest
                        if (since is not None and oldest // 1000 <= since) or len(rows) >= 400:
                            break
                elif exchange == 'kucoin':
                    if self.kucoin_pairs is None:
                        raw = self.client.get('https://api.kucoin.com/api/v2/symbols')
                        if raw.get('code') != '200000':
                            raise DataError('KuCoin spot catalogue rejected request')
                        self.kucoin_pairs = {(p['baseCurrency'], p['quoteCurrency']): p['symbol']
                                             for p in raw['data'] if p['enableTrading']}
                    pair = self.kucoin_pairs.get((base, quote))
                    if not pair:
                        raise DataError('Market absent from active spot catalogue')
                    end = int(now.timestamp()) // BAR * BAR
                    raw = self.client.get('https://api.kucoin.com/api/v1/market/candles',
                                          {'symbol': pair, 'type': '4hour', 'startAt': since if since is not None else end-401*BAR,
                                           'endAt': end})
                    if raw.get('code') != '200000':
                        raise DataError('KuCoin historical candles rejected request')
                    # KuCoin uses [time, open, close, high, low, volume, turnover].
                    rows = [[int(r[0]), r[1], r[3], r[4], r[2], r[5]] for r in raw['data']]
                merged = {int(r[0]): r for r in cached_rows}
                merged.update({int(r[0]): r for r in rows})
                candles = closed_candles(list(merged.values()), now)[-500:]
                self.cache.put(f'candles:4h:{exchange}:{base}:{quote}',
                               [[c.timestamp, c.open, c.high, c.low, c.close, c.volume] for c in candles])
                return candles, f'{exchange}:{pair}'
            except (DataError, KeyError, IndexError, TypeError, ValueError) as exc:
                detail = str(exc) if isinstance(exc, DataError) else 'Malformed provider response: ' + type(exc).__name__
                errors.append(f'{exchange}:{base}/{quote}: {detail}')
        raise DataError('No verified USD/USDT/USDC market with valid 4H history' +
                        (f' ({"; ".join(errors)})' if errors else ': no verified non-stale CoinGecko ticker on supported exchanges'))

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
    return schedule == '1 */4 * * *'


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
    key = f'{TIMEFRAME}:{coin_id}'
    old = state['coins'].get(key)
    if old is None:
        legacy = state['coins'].get(coin_id, {})
        old = {'candle': legacy.get('candle', 0), 'active': legacy.get('active', [])}
    if candle <= old['candle']:
        return []
    active = [s['direction'] for s in signals]
    new = [s for s in signals if s['direction'] not in old['active']]
    state['coins'][key] = {'candle': candle, 'active': active}
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
    date = datetime.fromtimestamp(signal['candle'], UTC).strftime('%Y-%m-%d %H:%M UTC')
    reasons = ', '.join(f'{k}={v}' for k, v in signal['components'].items())
    return (f"{coin['name']} ({coin['symbol'].upper()}) {signal['direction']} {signal['score']}/100\n"
            f"Abgeschlossene 4H-Kerze (Beginn): {date} | {source}\n"
            f"Setup: {signal['setup']} | Schluss: {signal['close']:.8g}\n"
            f"Liquiditätszone: {signal['liquidity_level']:.8g} | Stop-Referenz: {signal['stop_reference']:.8g}\n"
            f"Volumenfaktor: {signal['volume_ratio']}\n{reasons}\n"
            "Technisches Signal, keine Order. Short bezeichnet eine fallende Markterwartung.")


def run(dry_run=False):
    client = Client()
    now = datetime.now(UTC)
    report = {'started_at': now.isoformat(), 'timeframe': TIMEFRAME, 'dry_run': dry_run, 'universe_count': 0,
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
        report['excluded_stablecoins'] = data.excluded_stablecoins
        report['sources'] = {}
        def analyze(coin):
            error = None
            for attempt in range(3):
                try:
                    candles, source = data.candles(coin, now)
                    return coin, candles, source, None
                except (DataError, KeyError, ValueError, TypeError, IndexError) as exc:
                    error = str(exc) if isinstance(exc, DataError) else type(exc).__name__
                    # Exchange publication can lag the close; retry freshness only.
                    if 'Stale 4H candles' not in error or attempt == 2:
                        break
                    time.sleep(15)
            return coin, None, None, error
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(analyze, coin) for coin in coins]
            for future in as_completed(futures):
                coin, candles, source, error = future.result()
                if error:
                    report['data_gaps'].append({'coin_id': coin['id'], 'reason': error})
                    continue
                signals = strong_signals(candles)
                report['analyzed_count'] += 1
                venue = source.split(':')[0]
                report['sources'][venue] = report['sources'].get(venue, 0) + 1
                for signal in reserve(state, coin['id'], signals, candles[-1].timestamp):
                    report['new_strong_signals'].append({'coin_id': coin['id'], 'source': source, **signal})
                    notices.append(message(coin, signal, source))
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
        report['finished_at'] = datetime.now(UTC).isoformat()
        report['duration_seconds'] = round((datetime.now(UTC) - now).total_seconds(), 2)
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
