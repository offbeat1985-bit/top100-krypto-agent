import json
import threading
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import agent
from test_agent import CachedTestCase, rows, NOW, CUTOFF
from test_extra_sources import ticker

class FourHourUniverse(CachedTestCase):
    def test_stablecoin_categories_and_excludes_governance(self):
        d=agent.MarketData(MagicMock())
        categories=[{'category_id':'stablecoins','name':'Stablecoins'},
                    {'category_id':'yield-bearing-stablecoin','name':'Yield Bearing Stablecoins'},
                    {'category_id':'stablecoin-issuers','name':'Stablecoin Issuers'},
                    {'category_id':'stablecoin-protocols','name':'Stablecoin Protocols'}]
        with patch.object(d,'cg',side_effect=[categories,[{'id':'tether'}],[{'id':'yield-dollar'}]]) as cg:
            self.assertEqual(d.stablecoin_ids(),{'tether','yield-dollar'})
        self.assertEqual(cg.call_count,3)
    def test_classification_failure_stops_scan(self):
        d=agent.MarketData(MagicMock())
        with patch.object(d,'cg',return_value=[]), self.assertRaisesRegex(agent.DataError,'category missing'):
            d.top100()
    def test_empty_classification_is_not_silently_accepted(self):
        d=agent.MarketData(MagicMock())
        with patch.object(d,'cg',side_effect=[[{'category_id':'stablecoins'}],[]]), self.assertRaisesRegex(agent.DataError,'Empty stablecoin'):
            d.top100()
    def test_top100_refills_excluded_assets(self):
        d=agent.MarketData(MagicMock())
        coins=[{'id':str(i),'market_cap':1000-i} for i in range(130)]
        with patch.object(d,'stablecoin_ids',return_value={str(i) for i in range(20)}), patch.object(d,'cg',return_value=coins):
            result=d.top100()
        self.assertEqual(len(result),100)
        self.assertEqual(result[0]['id'],'20')
        self.assertEqual(result[-1]['id'],'119')
        self.assertEqual(d.excluded_stablecoins,20)
    def test_ticker_pagination_and_cache(self):
        d=agent.MarketData(MagicMock())
        with patch.object(d,'cg',side_effect=[{'tickers':[ticker('kraken')]*100},{'tickers':[ticker('kucoin')]}]) as cg:
            first=d.tickers({'id':'bitcoin'})
            second=d.tickers({'id':'bitcoin'})
        self.assertEqual(len(first),101);self.assertEqual(first,second)
        self.assertEqual(cg.call_count,2)
        self.assertEqual(cg.call_args.args[1]['page'],2)

class FourHourCache(CachedTestCase):
    def test_corrupt_cache_is_miss(self):
        cache=agent.JsonCache()
        cache.directory.mkdir(exist_ok=True)
        cache.path('bad').write_text('invalid-json')
        self.assertIsNone(cache.get('bad',100))
    def test_cache_ttl(self):
        cache=agent.JsonCache()
        with patch('time.time',return_value=100):cache.put('key',{'x':1})
        with patch('time.time',return_value=110):self.assertEqual(cache.get('key',20),{'x':1})
        with patch('time.time',return_value=121):self.assertIsNone(cache.get('key',20))
    def test_cache_does_not_mix_markets(self):
        d=agent.MarketData(MagicMock())
        d.cache.put('candles:4h:kucoin:BTC:USDT',rows())
        self.assertEqual(len(d.history('kucoin','BTC','USDT')),300)
        self.assertEqual(d.history('kraken','BTC','USD'),[])
    def test_incremental_fetch_retains_full_history(self):
        d=agent.MarketData(MagicMock())
        d.cache.put('candles:4h:kucoin:BTC:USDT',rows())
        raw_rows=[[x[0],x[1],x[4],x[2],x[3],x[5],0] for x in rows()[-3:]]
        d.client.get.side_effect=[{'code':'200000','data':[{'baseCurrency':'BTC','quoteCurrency':'USDT','symbol':'BTC-USDT','enableTrading':True}]},
                                  {'code':'200000','data':raw_rows}]
        with patch.object(d,'tickers',return_value=[ticker('kucoin')]):
            candles,_=d.candles({'id':'bitcoin'},NOW)
        self.assertEqual(len(candles),300)
        self.assertEqual(d.client.get.call_args.args[1]['startAt'],CUTOFF-3*agent.BAR)
    def test_incremental_stale_data_is_rejected(self):
        d=agent.MarketData(MagicMock())
        d.cache.put('candles:4h:kucoin:BTC:USDT',rows()[:-1])
        d.client.get.side_effect=[{'code':'200000','data':[{'baseCurrency':'BTC','quoteCurrency':'USDT','symbol':'BTC-USDT','enableTrading':True}]},
                                 {'code':'200000','data':[]}]
        with patch.object(d,'tickers',return_value=[ticker('kucoin')]), self.assertRaisesRegex(agent.DataError,'Stale 4H candles'):
            d.candles({'id':'bitcoin'},NOW)
    def test_endpoint_access_failure_is_not_retried_for_every_coin(self):
        client=agent.Client()
        error=urllib.error.HTTPError('url',451,'blocked',{},None)
        with patch('urllib.request.urlopen',side_effect=error) as request:
            for _ in range(2):
                with self.assertRaises(agent.DataError):client.get('https://api.binance.com/api/v3/exchangeInfo')
        self.assertEqual(request.call_count,1)

class FourHourTiming(CachedTestCase):
    def test_all_closed_4h_boundaries(self):
        for hour in (0,4,8,12,16,20):
            now=datetime(2026,10,15,hour,1,tzinfo=timezone.utc)
            cutoff=int(now.timestamp())//agent.BAR*agent.BAR
            r=[[cutoff-(300-i)*agent.BAR,100,102,99,101,100] for i in range(300)]
            c=agent.closed_candles(r+[[cutoff,1,2,1,2,100]],now)
            self.assertEqual(c[-1].timestamp,cutoff-agent.BAR)
            self.assertEqual(len(c),300)
    def test_same_day_multiple_bars_are_not_same_candle(self):
        state={'version':1,'coins':{}}
        s={'direction':'LONG'}
        self.assertEqual(agent.reserve(state,'btc',[s],agent.BAR),[s])
        self.assertEqual(agent.reserve(state,'btc',[s],2*agent.BAR),[])
        agent.reserve(state,'btc',[],3*agent.BAR)
        self.assertEqual(agent.reserve(state,'btc',[s],4*agent.BAR),[s])
        self.assertEqual(agent.reserve(state,'btc',[s],4*agent.BAR),[])
    def test_legacy_daily_state_is_preserved(self):
        state={'version':1,'coins':{'btc':{'candle':10,'active':['LONG']}}}
        self.assertEqual(agent.reserve(state,'btc',[{'direction':'LONG'}],100),[])
        self.assertIn('4h:btc',state['coins'])
        self.assertEqual(state['coins']['btc']['candle'],10)
    def test_timeframe_in_telegram_message(self):
        c=agent.closed_candles(rows(),NOW)
        s=agent.score(c,'LONG')
        text=agent.message({'name':'Bitcoin','symbol':'btc'},s,'kraken:XBTUSD')
        self.assertIn('4H-Kerze',text)
        self.assertIn('04:00 UTC',text)
