import copy
import json
import unittest
from membraneclaw_runtime.rsi.contracts import Invalid,document
from membraneclaw_runtime.rsi.datasets import audit,public
from helpers import spec


class Datasets(unittest.TestCase):
    def test_private_references_never_enter_public_projection(self):
        data = spec()['inputs']
        projected = json.dumps([document(public(t)) for t in data['tasks']])
        self.assertNotIn('PRIVATE_EVALUATOR_CANARY',projected)
        self.assertNotIn('expected',projected)

    def test_group_text_and_identifier_leakage_are_rejected(self):
        for key in ('id','group','text'):
            value = spec()['inputs'];value['tasks'][2][key] = value['tasks'][0][key]
            with self.subTest(key=key),self.assertRaises(Invalid):audit(value['tasks'],value['references'])

    def test_missing_reference_and_nonfinite_values_fail(self):
        value = spec()['inputs'];del value['references']['dev-mm']
        with self.assertRaises(Invalid):audit(value['tasks'],value['references'])
        value = spec()['inputs'];value['references']['dev-mm']['tolerance'] = -1
        with self.assertRaises(Invalid):audit(value['tasks'],value['references'])
