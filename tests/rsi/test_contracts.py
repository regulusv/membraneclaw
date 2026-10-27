import copy
import json
import unittest
from dataclasses import asdict, replace
from membraneclaw_runtime.rsi.contracts import *
from membraneclaw_runtime.rsi.config import verify_spec, check_current_source
from membraneclaw_runtime.rsi.versions import version,compile_agent
from helpers import spec


class Contracts(unittest.TestCase):
    def test_hash_rejects_nan_and_is_canonical(self):
        self.assertEqual(hashed({'b':1,'a':2}),hashed({'a':2,'b':1}))
        with self.assertRaises(ValueError):hashed({'score':float('nan')})

    def test_request_is_frozen_and_has_no_private_reference(self):
        request = SolveRequest('op','version',CompiledAgent('prompt',()),TaskInput('task','public'),0)
        with self.assertRaises(Exception):request.seed = 1
        self.assertNotIn('expected',document(request))

    def test_mismatched_ids_extra_fields_and_bool_usage_are_rejected(self):
        request = SolveRequest('op','version',CompiledAgent('prompt',()),TaskInput('task','public'),0)
        good = document(SolverResult('op','version','task',0,'completed','answer'))
        for patch in ({'operation_id':'other'},{'version_id':'other'},{'task_id':'other'},{'extra':True},{'units':True},{'seed':False}):
            with self.subTest(patch=patch),self.assertRaises(Invalid):validate_result('solve',{**good,**patch},request)

    def test_config_and_component_mutation_is_rejected(self):
        for mutate in (lambda s:s.update(schema=2),lambda s:s['components'].update(solver='different'),
                       lambda s:s['inputs']['assets'].update({'prompt.txt':'changed'})):
            value = spec();mutate(value)
            with self.assertRaises(Invalid):verify_spec(value)

    def test_changed_implementation_prevents_resume(self):
        value = spec();value['source']['files']['rsi/cli.py'] += '\n# changed'
        value['source']['digest'] = hashed(value['source']['files'])
        with self.assertRaisesRegex(Invalid,'source changed'):check_current_source(value)

    def test_raw_snapshot_and_semantic_input_must_agree(self):
        value = spec()
        value['inputs']['source_files']['tasks'] = '[]'
        value['dataset_digest'] = hashed(value['inputs'])
        with self.assertRaisesRegex(Invalid,'raw input bytes'):verify_spec(value)
