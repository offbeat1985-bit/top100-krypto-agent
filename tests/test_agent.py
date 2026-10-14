import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock
import urllib.error
import agent

NOW = datetime(2026, 10, 14, 8, tzinfo=timezone.utc)
CUTOFF = int(NOW.timestamp()) // agent.DAY * agent.DAY

def rows():
    return [[CUTOFF - (300-i)*agent.DAY, 100+i, 102+i, 99+i, 101+i, 100] for i in range(300)]

class Candles(unittest.TestCase):
    def test_excludes_open_and_future_daily_candles(self):
        r = rows() + [[CUTOFF, 400, 99999, 1, 99999, 99999], [CUTOFF+agent.DAY, 1, 2, 1, 2, 100]]
        c = agent.closed_candles(r, NOW)
        self.assertEqual(len(c), 300)
        self.assertEqual(c[-1].timestamp, CUTOFF-agent.DAY)
    def test_rejects_missing_stale_short_nonfinite_and_invalid(self):
        bad = [rows()[:-1], rows()[1:250], rows()[:150]+rows()[151:]]
        for column, value in ((2, math.inf), (5, -1), (3, 900), (0, CUTOFF-1)):
            r = rows(); r[-1][column] = value; bad.append(r)
        for r in bad:
            with self.subTest(case=r[-1]), self.assertRaises(agent.DataError):
                agent.closed_candles(r, NOW)
    def test_duplicates(self):
        r = rows()
        self.assertEqual(len(agent.closed_candles(r + [r[-1]], NOW)), 300)
        other = r[-1].copy(); other[5] = 55
        with self.assertRaises(agent.DataError): agent.closed_candles(r+[other], NOW)

class Indicators(unittest.TestCase):
    def test_ema_known_values(self):
        self.assertEqual(agent.ema([1,2,3,4,5], 3), [2,3,4])
        for p in (20,50,100,200):
            self.assertAlmostEqual(agent.ema([7]*300, p)[-1], 7)
    def test_confirmed_pivots_only(self):
        candles = [agent.Candle(i,1,h,1,1,1) for i,h in enumerate([2,3,8,3,2,4,10])]
        self.assertEqual(agent.pivots(candles), [(2,8)])
    def test_score_and_filter(self):
        c = agent.closed_candles(rows(), NOW)
        for d in ('LONG','SHORT'):
            s = agent.score(c,d)
            self.assertEqual(s['score'],sum(s['components'].values()))
            self.assertTrue(0 <= s['score'] <= 100)
        with patch('agent.score', side_effect=[{'eligible':True,'score':74},{'eligible':False,'score':100}]):
            self.assertEqual(agent.strong_signals(c), [])
        with patch('agent.score', side_effect=[{'eligible':True,'score':75},{'eligible':True,'score':100}]):
            self.assertEqual(len(agent.strong_signals(c)),2)
    def test_flat_market_no_signal(self):
        c = [agent.Candle(CUTOFF-(300-i)*agent.DAY,100,101,99,100,100) for i in range(300)]
        self.assertEqual(agent.strong_signals(c), [])
    def test_long_and_short_breakouts(self):
        # Rising/falling stair waves create confirmed HH/HL or LH/LL pivots.
        for direction, sign in [('LONG',1),('SHORT',-1)]:
            c=[]
            for i in range(300):
                price=500 + sign*(i*.6+math.sin(i*.7)*4)
                c.append(agent.Candle(CUTOFF-(300-i)*agent.DAY,price-sign*.2,price+1,price-1,price,100))
            boundary = max(x.high for x in c[-21:-1]) if sign == 1 else min(x.low for x in c[-21:-1])
            last=boundary+sign*2
            c[-1]=agent.Candle(c[-1].timestamp,last-sign,last+1,last-1,last,200)
            s=agent.score(c,direction)
            self.assertTrue(s['eligible'])
            self.assertEqual(s['components']['breakout'],20)
            self.assertEqual(s['components']['structure'],20)
            self.assertGreaterEqual(s['score'],75)

class Dedup(unittest.TestCase):
    def test_episode_and_restart(self):
        state={'version':1,'coins':{}}; signal={'direction':'LONG'}
        self.assertEqual(agent.reserve(state,'btc',[signal],1),[signal])
        state=json.loads(json.dumps(state))
        self.assertEqual(agent.reserve(state,'btc',[signal],1),[])
        self.assertEqual(agent.reserve(state,'btc',[signal],2),[])
        agent.reserve(state,'btc',[],3)
        self.assertEqual(agent.reserve(state,'btc',[signal],4),[signal])
    def test_failed_data_does_not_reset_state(self):
        state={'version':1,'coins':{'btc':{'candle':10,'active':['LONG']}}}
        self.assertEqual(agent.reserve(state,'btc',[{'direction':'SHORT'}],9),[])
        self.assertEqual(state['coins']['btc']['active'],['LONG'])
    def test_telegram_failure_redacts_secrets(self):
        with patch('urllib.request.urlopen',side_effect=RuntimeError('https://secret-token')):
            with self.assertRaises(agent.DataError) as error:
                agent.telegram(None,'secret-token','chat','text')
        self.assertNotIn('secret-token',str(error.exception))
    def test_state_write_failure_redacted(self):
        with patch('urllib.request.urlopen',side_effect=RuntimeError('token')):
            with self.assertRaises(agent.DataError): agent.Client().write('https://api.github.com',{}, {})

class Sources(unittest.TestCase):
    def test_top100_requires_exact_universe(self):
        d=agent.MarketData(MagicMock())
        with patch.object(d,'cg',return_value=[{'id':str(i),'market_cap':100-i} for i in range(100)]):
            self.assertEqual(len(d.top100()),100)
        with patch.object(d,'cg',return_value=[]), self.assertRaises(agent.DataError): d.top100()
    def test_binance_mapping_and_volume(self):
        client=MagicMock()
        raw=[[r[0]*1000,*r[1:]] for r in rows()]
        client.get.side_effect=[{'symbols':[{'baseAsset':'BTC','quoteAsset':'USDT','symbol':'BTCUSDT','status':'TRADING'}]},raw]
        d=agent.MarketData(client)
        t={'coin_id':'bitcoin','market':{'identifier':'binance'},'base':'BTC','target':'USDT'}
        with patch.object(d,'cg',return_value={'tickers':[t]}) as cg:
            candles,source=d.candles({'id':'bitcoin'},NOW)
            self.assertEqual(cg.call_args.args[1]['exchange_ids'], 'binance,kraken,okex,kucoin')
        self.assertEqual(source,'binance:BTCUSDT')
        self.assertEqual(candles[-1].volume,100)
    def test_kraken_fallback(self):
        client=MagicMock()
        kr=[[r[0],*r[1:5],0,r[5],99] for r in rows()]
        client.get.side_effect=[agent.DataError('Binance unavailable'),
                               {'result':{'XXBTZUSD':{'wsname':'XBT/USD'}}},
                               {'result':{'XXBTZUSD':kr,'last':1}}]
        d=agent.MarketData(client)
        ticks=[{'coin_id':'bitcoin','market':{'identifier':x},'base':'BTC','target':q,'converted_volume':{'usd':v}}
               for x,q,v in [('binance','USDT',2),('kraken','USD',1)]]
        with patch.object(d,'cg',return_value={'tickers':ticks}):
            c,s=d.candles({'id':'bitcoin'},NOW)
        self.assertEqual(s,'kraken:XXBTZUSD'); self.assertEqual(c[-1].volume,100)
    def test_same_symbol_wrong_coin_rejected(self):
        d=agent.MarketData(MagicMock())
        t={'coin_id':'different','market':{'identifier':'binance'},'base':'BTC','target':'USDT'}
        with patch.object(d,'cg',return_value={'tickers':[t]}), self.assertRaises(agent.DataError):
            d.candles({'id':'bitcoin'},NOW)
    def test_http_retry(self):
        response=MagicMock(); response.__enter__.return_value=response
        response.read.return_value=b'{"ok":true}'
        with patch('urllib.request.urlopen',side_effect=[urllib.error.HTTPError('url',429,'rate limit',{},None),response]), patch('time.sleep'):
            self.assertEqual(agent.Client().get('https://example.com'),{'ok':True})

class Workflow(unittest.TestCase):
    def test_switzerland_summer_winter_and_transitions(self):
        for date,expected in [('2026-01-10','0 7 * * *'),('2026-07-10','0 6 * * *'),
                              ('2026-03-29','0 6 * * *'),('2026-10-25','0 7 * * *')]:
            # A delayed runner at noon still selects the correct scheduled invocation.
            now=datetime.fromisoformat(date+'T12:00:00+00:00')
            for cron in ('0 6 * * *','0 7 * * *'):
                self.assertEqual(agent.scheduled_slot(cron,now),cron==expected)
    def test_workflow_safety_contract(self):
        text=Path('.github/workflows/signals.yml').read_text()
        for required in ("cron: '0 6 * * *'", "cron: '0 7 * * *'", 'cancel-in-progress: false',
                         'secrets.TELEGRAM_BOT_TOKEN','secrets.TELEGRAM_CHAT_ID','--dry-run','if: always()',
                         'contents: write','unittest discover','workflow_dispatch:'):
            self.assertIn(required,text)
        self.assertEqual(json.loads(Path('.state/signals.json').read_text())['version'],1)


class Pipeline(unittest.TestCase):
    def execute(self, count=100, dry=False, save_failure=False, send_failure=False):
        import os
        import tempfile
        candle = agent.Candle(CUTOFF-agent.DAY,100,110,90,105,200)
        signal = {'direction':'LONG','score':90}
        events=[]
        store=MagicMock()
        store.load.return_value={'version':1,'coins':{}}
        def save(state):
            events.append('reserve')
            if save_failure: raise agent.DataError('State unavailable')
        store.save.side_effect=save
        data=MagicMock()
        data.top100.return_value=[{'id':str(i),'name':str(i),'symbol':'T'} for i in range(100)]
        def candles(coin,now):
            if int(coin['id']) >= count: raise agent.DataError('No market')
            return [candle], 'verified-market'
        data.candles.side_effect=candles
        def send(*args):
            events.append('send')
            if send_failure: raise agent.DataError('Telegram uncertain')
        with tempfile.TemporaryDirectory() as directory:
            old=os.getcwd()
            try:
                os.chdir(directory)
                with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'test-secret','TELEGRAM_CHAT_ID':'test-chat'}), \
                     patch('agent.StateStore',return_value=store), patch('agent.MarketData',return_value=data), \
                     patch('agent.strong_signals',return_value=[signal]), patch('agent.message',return_value='strong signal'), \
                     patch('agent.telegram',side_effect=send), patch('builtins.print'):
                    result=agent.run(dry)
                report=json.loads(Path('reports/latest.json').read_text())
            finally:
                os.chdir(old)
        return result,report,events,store
    def test_durable_reservation_precedes_every_send(self):
        result,report,events,_=self.execute()
        self.assertEqual(result,0); self.assertEqual(report['sent'],100)
        self.assertEqual(events,['reserve']+['send']*100)
    def test_failed_state_write_never_sends(self):
        result,report,events,_=self.execute(save_failure=True)
        self.assertEqual(result,1); self.assertEqual(events,['reserve'])
        self.assertEqual(report['sent'],0)
    def test_uncertain_delivery_stops_without_retry(self):
        result,report,events,_=self.execute(send_failure=True)
        self.assertEqual(result,1); self.assertEqual(events,['reserve','send'])
    def test_degraded_scan_does_not_reserve_or_send(self):
        result,report,events,_=self.execute(count=49)
        self.assertEqual(result,1); self.assertEqual(events,[])
        self.assertEqual(len(report['data_gaps']),51)
    def test_partial_scan_explicit(self):
        result,report,events,_=self.execute(count=50)
        self.assertEqual(result,0); self.assertEqual(report['status'],'partial')
        self.assertEqual(report['analyzed_count'],50)
    def test_dry_run_does_not_access_persistent_store_or_send(self):
        result,report,events,store=self.execute(dry=True)
        self.assertEqual(result,0); self.assertEqual(events,[])
        store.load.assert_not_called(); store.save.assert_not_called()

class Retest(unittest.TestCase):
    def test_closed_breakout_then_retest_in_both_directions(self):
        for direction, sign in [('LONG',1),('SHORT',-1)]:
            c=[]
            for i in range(300):
                price=500+sign*(i*.6+math.sin(i*.7)*4)
                c.append(agent.Candle(CUTOFF-(300-i)*agent.DAY,price-sign*.2,price+1,price-1,price,100))
            i=len(c)-2
            level=max(x.high for x in c[i-20:i]) if sign==1 else min(x.low for x in c[i-20:i])
            price=level+sign*3
            c[i]=agent.Candle(c[i].timestamp,price-sign,price+1,price-1,price,200)
            price=level+sign*1
            c[-1]=agent.Candle(c[-1].timestamp,price-sign*.5,price+1,price-1,price,180)
            s=agent.score(c,direction)
            self.assertEqual(s['components']['retest'],10)
            self.assertEqual(s['setup'],'retest')
            self.assertAlmostEqual(s['liquidity_level'],level)
            self.assertTrue(s['eligible'])

if __name__ == '__main__': unittest.main()
