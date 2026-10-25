import unittest
from membraneclaw_runtime.rsi.budget import *


class Budgets(unittest.TestCase):
    def test_exact_fit_and_no_release_of_unknown_reservation(self):
        ledger = {'calls':0,'spent':0,'reserved':0};limits = {'operations':2,'units':2,'wall_seconds':10}
        reserve(ledger,limits,1,0)
        self.assertEqual(ledger['reserved'],1)
        reserve(ledger,limits,1,0)
        with self.assertRaises(Exhausted):reserve(ledger,limits,1,0)
        self.assertEqual(ledger,{'calls':2,'spent':0,'reserved':2})

    def test_usage_overrun_is_retained_unclamped(self):
        ledger = {'calls':1,'spent':0,'reserved':1}
        self.assertTrue(settle(ledger,1,4))
        self.assertEqual(ledger['spent'],4)

    def test_deadline_and_invalid_usage_do_not_reset_budget(self):
        ledger = {'calls':0,'spent':0,'reserved':0};limits = {'operations':2,'units':2,'wall_seconds':10}
        with self.assertRaises(Exhausted):reserve(ledger,limits,1,10)
        reserve(ledger,limits,1,0)
        with self.assertRaises(Invalid):settle(ledger,1,True)
        self.assertEqual(ledger['reserved'],1)
