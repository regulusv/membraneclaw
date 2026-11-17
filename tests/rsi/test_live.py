import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

from membraneclaw_runtime.rsi.live import (live_spec, LiveSolver, LiveProposer, KeyDeskTransport,
                                         atomic_json, MODEL)
from membraneclaw_runtime.rsi.controller import Controller
from membraneclaw_runtime.rsi.journal import Journal
from membraneclaw_runtime.rsi.report import summary
from membraneclaw_runtime.rsi.contracts import Invalid
from membraneclaw_runtime.providers.types import NativeResponse, TransportError, ModelRequest, ContentBlock
from membraneclaw_runtime.providers.gemini import GeminiAdapter
from membraneclaw_runtime.tracing.schema import canonical_json, EpisodeRef
from membraneclaw_runtime.tracing import FileTraceStore
from membraneclaw_runtime.tracing.replay import execute_replay


class FakeTransport:
    authentication_values = ()
    calls = 0

    async def exchange(self, request, *, before_send, after_receive, timeout):
        await before_send(request)
        type(self).calls += 1
        prompt = json.loads(request.body)['contents'][0]['parts'][0]['text']
        if prompt.startswith('Improve the supplied agent'):
            assets = json.loads(prompt.split('\n',1)[1])['assets']
            text = json.dumps({'edits': [{'path': 'prompt.txt', 'before': assets['prompt.txt']['before'],
                                         'after': 'RAW_JSON. Convert units and honor requested precision.'}],
                               'rationale': 'Use strict JSON.'})
        else:
            task = json.loads(prompt.split('Task:\n')[1])
            v = task['quantity']/1000 if task.get('output_unit') == 'm' else task['quantity']
            if task.get('digits') is not None:v = 1.24
            text = json.dumps({'value': v})
            if 'RAW_JSON' not in prompt:text = '```json\n'+text+'\n```'
        response = NativeResponse(200, {}, canonical_json({'candidates': [{'content': {'role':'model','parts':[{'text':text}]},
                           'finishReason':'STOP'}], 'usageMetadata': {'promptTokenCount':10,'candidatesTokenCount':10,'totalTokenCount':20}}))
        await after_receive(response)
        return response


class LiveTests(unittest.TestCase):
    def test_controller_native_runtime_trace_links_replay_and_completed_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'run'
            with Journal(root, spec=live_spec()) as j:
                factory = lambda rid: FakeTransport()
                asyncio.run(Controller(j, solver=LiveSolver(j,factory), proposer=LiveProposer(j,factory)).run())
                report = summary(j)
                self.assertEqual(report['status'],'completed')
                self.assertEqual(report['decisions'][0]['outcome'],'accepted')
                self.assertEqual(len(report['model_traces']),7)
                self.assertEqual(report['actual_model_tokens'],140)
                before = FakeTransport.calls
                asyncio.run(Controller(j,solver=LiveSolver(j,factory),proposer=LiveProposer(j,factory)).run())
                self.assertEqual(before,FakeTransport.calls)
                store = FileTraceStore(root/'model-traces')
                for row in report['model_traces']:
                    replay = asyncio.run(execute_replay(store,EpisodeRef(*row['episode'].split('/'))))
                    self.assertFalse(replay['network'])
                    self.assertEqual(replay['status'],'completed')
                self.assertEqual(before,FakeTransport.calls)
                with self.assertRaises(Invalid):Controller(j,recompute=True)
                # Corrupt a result/trace join: verification must not trust a report.
                state = copy.deepcopy(j.state)
                op = next(o for o in state['operations'].values() if o['kind']=='solve')
                op['model_episode'] = report['model_traces'][-1]['episode']
                j.state = state
                from membraneclaw_runtime.rsi.live import verify_model_links
                with self.assertRaises(Invalid):verify_model_links(j)

    def test_bridge_capture_order_and_request_mismatch(self):
        async def scenario(temp, mismatch=False):
            adapter = GeminiAdapter();state = adapter.initial_state(MODEL)
            request = adapter.prepare(ModelRequest(state.route,(ContentBlock.text_block('hello'),),
                max_output_tokens=1024,provider_options={'gemini':{'candidateCount':1}}),state)
            transport = KeyDeskTransport(temp,'test')
            seen = []
            async def before(native):
                self.assertFalse((Path(temp)/'test.request.json').exists());seen.append('before')
            async def after(response):seen.append('after')
            async def deliver():
                while not (Path(temp)/'test.request.json').exists():await asyncio.sleep(.01)
                body = json.loads(request.body)
                if mismatch:body['contents'][0]['parts'][0]['text']='changed'
                atomic_json(Path(temp)/'test.response.json',{'request_id':'test','state':'completed',
                    'request':{'url':'https://generativelanguage.googleapis.com'+request.path,'method':'POST','body':body},
                    'result':{'http_status':200,'raw_response':'{}'}})
            delivery = asyncio.create_task(deliver())
            try:
                if mismatch:
                    with self.assertRaises(TransportError):
                        await transport.exchange(request,before_send=before,after_receive=after,timeout=2)
                    self.assertEqual(seen,['before'])
                else:
                    await transport.exchange(request,before_send=before,after_receive=after,timeout=2)
                    self.assertEqual(seen,['before','after'])
            finally:await delivery
        for mismatch in [False,True]:
            with tempfile.TemporaryDirectory() as temp:asyncio.run(scenario(temp,mismatch))

    def test_trace_write_failure_never_publishes_bridge_request(self):
        async def scenario(temp):
            adapter=GeminiAdapter();state=adapter.initial_state(MODEL)
            native=adapter.prepare(ModelRequest(state.route,(ContentBlock.text_block('hi'),),max_output_tokens=32,
                                  provider_options={'gemini':{'candidateCount':1}}),state)
            async def fail(_):raise OSError('disk full')
            async def after(_):self.fail('must not receive')
            with self.assertRaises(OSError):
                await KeyDeskTransport(temp,'test').exchange(native,before_send=fail,after_receive=after,timeout=1)
            self.assertEqual(list(Path(temp).iterdir()),[])
        with tempfile.TemporaryDirectory() as temp:asyncio.run(scenario(temp))
