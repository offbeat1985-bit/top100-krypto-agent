from unittest.mock import patch
import agent
import test_agent
from test_agent import CachedTestCase, rows, NOW

class CandidateArchive(CachedTestCase):
    def test_archives_both_directions_even_low_or_ineligible(self):
        low={'direction':'LONG','score':1,'eligible':False}
        high={'direction':'SHORT','score':100,'eligible':True}
        with patch('agent.score',side_effect=[low,high]):
            evaluated=agent.candidate_scores([])
        self.assertEqual(evaluated,[low,high])
        self.assertEqual(agent.strong_signals([],evaluations=evaluated),[high])
    def test_zero_score_remains_reference_not_notification(self):
        evaluations=[{'direction':'LONG','score':0,'eligible':False},
                     {'direction':'SHORT','score':74,'eligible':True}]
        self.assertEqual(agent.strong_signals([],evaluations=evaluations),[])
    def test_real_score_components_retained(self):
        candles=agent.closed_candles(rows(),NOW)
        evaluations=agent.candidate_scores(candles)
        self.assertEqual([s['direction'] for s in evaluations],['LONG','SHORT'])
        for s in evaluations:
            self.assertEqual(sum(s['components'].values()),s['score'])
            self.assertIn('ema',s)
            self.assertIn('volume_ratio',s)
    def test_pipeline_archives_candidates_and_actual_deliveries(self):
        code,report,events,_=test_agent.Pipeline().execute()
        self.assertEqual(code,0)
        self.assertEqual(report['report_version'],2)
        self.assertEqual(len(report['candidate_scores']),100)
        self.assertEqual(len(report['universe']),100)
        self.assertEqual(len(report['confirmed_deliveries']),report['sent'])
        self.assertIn('evaluated_at',report['candidate_scores'][0])
        self.assertEqual(report['candidate_scores'][0]['price_reference'],'closed_candle_only_not_an_execution_price')
    def test_uncertain_delivery_is_not_api_confirmation(self):
        code,report,_,_=test_agent.Pipeline().execute(send_failure=True)
        self.assertEqual(code,1)
        self.assertEqual(report['confirmed_deliveries'],[])
        self.assertIn('uncertain_delivery',report)
