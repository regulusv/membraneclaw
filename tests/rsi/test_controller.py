import asyncio
import base64
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from membraneclaw_runtime.rsi.controller import Controller
from membraneclaw_runtime.rsi.fixtures import FixtureSolver,FixtureProposer,FixtureEvaluator
from membraneclaw_runtime.rsi.contracts import document,hashed
from membraneclaw_runtime.rsi.journal import Journal
from membraneclaw_runtime.rsi.report import summary,verify
from helpers import spec,run


class ControllerTests(unittest.TestCase):
    def test_two_rounds_accept_then_reject_and_parent_is_selected_version(self):
        with tempfile.TemporaryDirectory() as temp:
            report = run(Path(temp)/'run')
            self.assertEqual([d['outcome'] for d in report['decisions']],['accepted','rejected'])
            self.assertEqual(report['decisions'][1]['parent'],report['decisions'][0]['selected'])
            self.assertEqual(report['selected_version'],report['decisions'][0]['candidate'])
            self.assertEqual(report['ledger'],{'calls':26,'spent':26,'reserved':0})
            self.assertNotIn('PRIVATE_EVALUATOR_CANARY',json.dumps(report))

    def test_synthetic_run_never_opens_network_or_model_factory(self):
        with tempfile.TemporaryDirectory() as temp,patch('socket.create_connection',side_effect=AssertionError('network')),patch('agent.factory.runtime_session',side_effect=AssertionError('model')):
            self.assertEqual(run(Path(temp)/'run',spec(1))['status'],'completed')

    def test_invalid_return_is_captured_before_rejection(self):
        class Bad(FixtureSolver):
            async def solve(self,request):
                result = document(await super().solve(request));result['extra'] = float('nan');return result
        with tempfile.TemporaryDirectory() as temp,Journal(Path(temp)/'run',spec=spec(1)) as journal:
            asyncio.run(Controller(journal,solver=Bad()).run())
            self.assertEqual(journal.state['stop_reason'],'invalid_result')
            op = next(iter(journal.state['operations'].values()))
            self.assertEqual(op['status'],'invalid')
            envelope = json.loads(journal.store.read_payload(op['evidence']))
            self.assertIn(b'NaN',base64.b64decode(envelope['body']))
            self.assertEqual(journal.state['ledger']['spent'],1)
            verify(journal)

    def test_unknown_exception_retains_reservation_and_blocks_further_work(self):
        class Bad(FixtureSolver):
            async def solve(self,request):raise RuntimeError('private error detail')
        with tempfile.TemporaryDirectory() as temp,Journal(Path(temp)/'run',spec=spec(1)) as journal:
            asyncio.run(Controller(journal,solver=Bad()).run())
            self.assertEqual(journal.state['ledger'],{'calls':1,'spent':0,'reserved':1})
            self.assertEqual(len(journal.state['operations']),1)
            verify(journal)

    def test_budget_stops_and_observed_overrun_is_saved(self):
        class Costly(FixtureSolver):
            async def solve(self,request):return replace(await super().solve(request),units=3)
        with tempfile.TemporaryDirectory() as temp:
            report = run(Path(temp)/'limited',spec(1,operations=1))
            self.assertEqual(report['stop_reason'],'operation_count')
            report = run(Path(temp)/'overrun',spec(1),solver=Costly())
            self.assertEqual(report['ledger']['spent'],3)
            self.assertEqual(report['ledger']['calls'],1)

    def test_cancellation_retains_late_result_and_dispatches_nothing_else(self):
        class Late(FixtureSolver):
            async def solve(self,request):
                try:await asyncio.sleep(10)
                except asyncio.CancelledError:return await super().solve(request)
        async def exercise(journal):
            task = asyncio.create_task(Controller(journal,solver=Late()).run())
            while not any(op['status']=='started' for op in journal.state['operations'].values()):await asyncio.sleep(0)
            await asyncio.sleep(0);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        with tempfile.TemporaryDirectory() as temp,Journal(Path(temp)/'run',spec=spec(1)) as journal:
            asyncio.run(exercise(journal))
            self.assertEqual(journal.state['status'],'cancelled')
            self.assertEqual(journal.state['ledger']['calls'],1)
            self.assertEqual(next(iter(journal.state['operations'].values()))['status'],'completed')
            verify(journal)

    def test_incomplete_execution_cannot_be_promoted(self):
        class Incomplete(FixtureEvaluator):
            async def evaluate(self,request):return replace(await super().evaluate(request),status='incomplete')
        value = spec(1)
        with tempfile.TemporaryDirectory() as temp:
            report = run(Path(temp)/'run',value,evaluator=Incomplete(value['inputs']['references']))
            self.assertEqual(report['status'],'blocked');self.assertEqual(report['decisions'],[])

    def test_raw_credential_filter_runs_before_base64_capture(self):
        class Secret(FixtureSolver):
            async def solve(self,request):return replace(await super().solve(request),answer='synthetic-credential')
        with tempfile.TemporaryDirectory() as temp,Journal(Path(temp)/'run',spec=spec(1)) as journal:
            journal.store.policy.register_authentication_values(['synthetic-credential'])
            asyncio.run(Controller(journal,solver=Secret()).run())
            op = next(iter(journal.state['operations'].values()))
            self.assertIsNone(op['evidence'])
            self.assertEqual(journal.state['stop_reason'],'credential_rejected_before_persistence')
            self.assertEqual(journal.state['ledger']['reserved'],1)

    def test_proposer_gets_only_development_tasks_outputs_and_scores(self):
        captured=[]
        class Observe(FixtureProposer):
            async def propose(self,request):
                captured.append(document(request));return await super().propose(request)
        with tempfile.TemporaryDirectory() as temp:
            run(Path(temp)/'run',spec(1),proposer=Observe())
            evidence = captured[0]['development_evidence']
            self.assertIn('dev-mm',evidence);self.assertIn('answer',evidence)
            for private in ('PRIVATE_EVALUATOR_CANARY','val-exact','test-mm','expected','tolerance'):
                self.assertNotIn(private,json.dumps(captured))
