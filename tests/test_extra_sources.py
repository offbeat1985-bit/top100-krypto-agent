import unittest
from unittest.mock import MagicMock,patch
import agent
from test_agent import rows,NOW,CUTOFF,CachedTestCase

def ticker(exchange):
    return {'coin_id':'bitcoin','market':{'identifier':exchange},'base':'BTC','target':'USDT'}

class ExtraSources(CachedTestCase):
    def test_kucoin_ohlcv_order_and_open_exclusion(self):
        client=MagicMock()
        r=rows()+[[CUTOFF,400,500,300,450,999]]
        series=[[x[0],x[1],x[4],x[2],x[3],x[5],999] for x in reversed(r)]
        client.get.side_effect=[{'code':'200000','data':[{'baseCurrency':'BTC','quoteCurrency':'USDT','symbol':'BTC-USDT','enableTrading':True}]},
                               {'code':'200000','data':series}]
        d=agent.MarketData(client)
        with patch.object(d,'cg',return_value={'tickers':[ticker('kucoin')]}):
            candles,source=d.candles({'id':'bitcoin'},NOW)
        self.assertEqual(source,'kucoin:BTC-USDT')
        self.assertEqual(len(candles),300)
        self.assertEqual(candles[-1].close,400)
        self.assertEqual(candles[-1].high,401)
        self.assertEqual(candles[-1].volume,100)
        self.assertEqual(client.get.call_args.args[1]['type'],'4hour')
    def test_okx_utc_pagination_confirmation_and_volume(self):
        client=MagicMock()
        series=[[x[0]*1000,*x[1:6],999,999,'1'] for x in reversed(rows())]
        series.insert(0,[CUTOFF*1000,1,2,1,2,999,1,1,'0'])
        client.get.side_effect=[{'code':'0','data':[{'baseCcy':'BTC','quoteCcy':'USDT','instId':'BTC-USDT','state':'live'}]}]+[
            {'code':'0','data':series[i:i+100]} for i in range(0,len(series),100)]+[{'code':'0','data':[]}]
        d=agent.MarketData(client)
        with patch.object(d,'cg',return_value={'tickers':[ticker('okex')]}):
            candles,source=d.candles({'id':'bitcoin'},NOW)
        self.assertEqual(source,'okex:BTC-USDT');self.assertEqual(len(candles),300)
        self.assertEqual(candles[-1].volume,100)
        for call in client.get.call_args_list[1:]: self.assertEqual(call.args[1]['bar'],'4H')
        self.assertEqual(client.get.call_args_list[2].args[1]['after'],min(x[0] for x in series[:100]))
    def test_detailed_insufficient_history(self):
        client=MagicMock()
        client.get.side_effect=[{'code':'200000','data':[{'baseCurrency':'BTC','quoteCurrency':'USDT','symbol':'BTC-USDT','enableTrading':True}]},
                               {'code':'200000','data':[]}]
        with patch.object(agent.MarketData,'cg',return_value={'tickers':[ticker('kucoin')]}):
            with self.assertRaisesRegex(agent.DataError,'kucoin:BTC/USDT: Fewer than 260'):
                agent.MarketData(client).candles({'id':'bitcoin'},NOW)
    def test_provider_error_code_and_fallback(self):
        client=MagicMock()
        client.get.side_effect=[{'code':'50011','data':[]},
            {'code':'200000','data':[{'baseCurrency':'BTC','quoteCurrency':'USDT','symbol':'BTC-USDT','enableTrading':True}]},
            {'code':'200000','data':[[x[0],x[1],x[4],x[2],x[3],x[5],0] for x in rows()]}]
        with patch.object(agent.MarketData,'cg',return_value={'tickers':[ticker('okex'),ticker('kucoin')]}):
            _,source=agent.MarketData(client).candles({'id':'bitcoin'},NOW)
        self.assertEqual(source,'kucoin:BTC-USDT')
    def test_no_unverified_coin_mapping(self):
        t=ticker('kucoin');t['coin_id']='other'
        with patch.object(agent.MarketData,'cg',return_value={'tickers':[t]}):
            with self.assertRaisesRegex(agent.DataError,'no verified non-stale'):
                agent.MarketData(MagicMock()).candles({'id':'bitcoin'},NOW)
