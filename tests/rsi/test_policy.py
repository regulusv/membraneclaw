import unittest
from membraneclaw_runtime.rsi.policy import decide


def rows(scores):
    return [{'task_id':str(i),'seed':0,'status':'completed','scale':1,'score':score} for i,score in enumerate(scores)]


class Promotion(unittest.TestCase):
    def test_equal_and_regressing_cases_rejected(self):
        self.assertEqual(decide(rows([0,1]),rows([0,1]))['outcome'],'rejected')
        self.assertEqual(decide(rows([1,0,0]),rows([0,1,1]))['outcome'],'rejected')

    def test_incomplete_duplicate_missing_nonfinite_and_scale_mismatch_inconclusive(self):
        good = rows([0,1])
        bads = [rows([0]),rows([0,float('nan')]),rows([0,2])]
        bads.append([{**r,'scale':2} for r in good])
        bads.append([{**r,'status':'failed'} for r in good])
        for bad in bads:
            with self.subTest(bad=bad):self.assertEqual(decide(good,bad)['outcome'],'inconclusive')
        self.assertEqual(decide([good[0],good[0]],[good[0],good[0]])['outcome'],'inconclusive')

    def test_positive_paired_gain_has_explicit_means_and_denominator(self):
        result = decide(rows([0,1]),rows([1,1]))
        self.assertEqual(result['outcome'],'accepted')
        self.assertEqual(result['parent_mean'],.5);self.assertEqual(result['candidate_mean'],1)
        self.assertEqual(result['mean_gain'],.5)
        self.assertEqual(len(result['paired_cases']),2)
