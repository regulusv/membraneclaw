import asyncio
import json
import socket
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from membraneclaw_runtime.gateway import Budget
from membraneclaw_runtime.providers.keydesk import KeyDeskGeminiAdapter, start_keydesk_gateway
from membraneclaw_runtime.providers.types import ModelRequest, ContentBlock, ConfigurationError, TransportError, ProviderHTTPError
from membraneclaw_runtime.runtime import AgentRuntime
from membraneclaw_runtime.tools import ToolRegistry
from membraneclaw_runtime.tracing import FileTraceStore
from membraneclaw_runtime.tracing.validation import validate
from membraneclaw_runtime.tracing.replay import execute_replay
from membraneclaw_runtime.tracing.export import export_bundle, restore_bundle

RAW = json.dumps({'candidates':[{'content':{'role':'model','parts':[{'text':'1250','thoughtSignature':'opaque-signature'}]},'finishReason':'STOP'}], 'usageMetadata':{'promptTokenCount':18,'candidatesTokenCount':4,'totalTokenCount':22},'futureField':{'keep':'中文 😀'}})

class KeyDeskContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=FileTraceStore(self.tmp.name+'/trace');self.calls=[]

    async def invoke(self, args):
        self.calls.append(args)
        events=self.store.read_episode(self.gateway.recorder.episode).events
        self.assertEqual(events[-1]['type'],'keydesk.requested')
        self.assertEqual(events[-1]['data']['request_id'],args['request_id'])
        req=next(e for e in events if e['type']=='attempt.started')
        body=json.loads(self.store.read_payload(req['payload_ref']))
        self.assertEqual(body['contents'][0]['parts'][0]['text'],args['prompt'])
        return {'credential_id':args['credential_id'],'request_id':args['request_id'],'state':'completed',
                'request':{'url':'https://generativelanguage.googleapis.com/v1beta/models/'+args['model']+':generateContent','method':'POST','body':body},
                'result':{'http_status':200,'raw_response':RAW}}

    async def setup_runtime(self, invoke=None, timeout=10):
        self.gateway=await start_keydesk_gateway(self.store,'credential-id',invoke or self.invoke,
                         budget=Budget(max_requests=1,attempt_timeout_seconds=timeout))
        req=ModelRequest(self.gateway.route,(ContentBlock.text_block('Convert 1.25 litres. 中文 😀'),),max_output_tokens=256)
        return AgentRuntime(self.gateway,ToolRegistry(),req)

    async def test_real_store_capture_reopen_export_restore_and_network_free_replay(self):
        runtime=await self.setup_runtime();result=await runtime.run()
        self.assertEqual(result.text,'1250');self.assertEqual(len(self.calls),1)
        episode=self.gateway.recorder.episode;store=FileTraceStore(self.tmp.name+'/trace')
        self.assertEqual(validate(store,episode).status,'completed')
        response=next(e for e in store.read_episode(episode).events if e['type']=='attempt.response')
        self.assertEqual(store.read_payload(response['payload_ref']),RAW.encode())
        bundle=self.tmp.name+'/trace.zip';export_bundle(store,episode,bundle,mode='private-replay')
        restored,ref=restore_bundle(bundle,self.tmp.name+'/restored')
        with patch.object(socket,'create_connection',side_effect=AssertionError('network forbidden')):
            replay=await execute_replay(restored,ref)
        self.assertEqual(replay['text'],'1250');self.assertFalse(replay['network'])
        self.assertEqual(replay['model_requests'],1)

    async def test_unknown_record_is_not_retried(self):
        async def unknown(args):
            record=await self.invoke(args);record['state']='unknown';return record
        runtime=await self.setup_runtime(unknown)
        with self.assertRaises(TransportError):await runtime.run()
        self.assertEqual(len(self.calls),1)
        ep=self.store.read_episode(self.gateway.recorder.episode)
        self.assertEqual(ep.completeness['external_outcome'],'unknown')
        self.assertTrue(any(e['type']=='keydesk.requested' for e in ep.events))

    async def test_changed_request_is_rejected(self):
        async def wrong(args):
            record=await self.invoke(args);record['request']['method']='GET';return record
        runtime=await self.setup_runtime(wrong)
        with self.assertRaises(TransportError):await runtime.run()
        self.assertFalse(any(e['type']=='model.completed' for e in self.store.read_episode(self.gateway.recorder.episode).events))

    async def test_durable_start_failure_prevents_call(self):
        runtime=await self.setup_runtime()
        original=self.store.commit
        def fail(*args,**kwargs):
            if any(e.type=='attempt.started' for e in kwargs.get('events',[])):
                raise OSError('injected disk failure')
            return original(*args,**kwargs)
        with patch.object(self.store,'commit',side_effect=fail):
            with self.assertRaises(Exception):await runtime.run()
        self.assertEqual(self.calls,[])

    async def test_429_is_captured_without_retry(self):
        async def quota(args):
            r=await self.invoke(args);r['state']='failed';r['result']={'http_status':429,'raw_response':'{"error":"quota"}'};return r
        runtime=await self.setup_runtime(quota)
        with self.assertRaises(ProviderHTTPError):await runtime.run()
        self.assertEqual(len(self.calls),1)
        response=next(e for e in self.store.read_episode(self.gateway.recorder.episode).events if e['type']=='attempt.response')
        self.assertEqual(response['data']['status'],429)

    async def test_timeout_keeps_durable_remote_id(self):
        async def slow(args):
            self.calls.append(args);await asyncio.Event().wait()
        runtime=await self.setup_runtime(slow,0.05)
        with self.assertRaises(TransportError):await runtime.run()
        self.assertEqual(len(self.calls),1)
        self.assertTrue(any(e['type']=='keydesk.requested' for e in self.store.read_episode(self.gateway.recorder.episode).events))

    def test_profile_fails_closed(self):
        a=KeyDeskGeminiAdapter();state=a.initial_state('gemini-3.5-flash-lite')
        req=ModelRequest(state.route,(ContentBlock.text_block('hi'),),max_output_tokens=256)
        for changed in (replace(req,system_instruction='hidden'),replace(req,temperature=0.2),replace(req,max_output_tokens=1025),replace(req,content=(ContentBlock.text_block('😀'*4001),))):
            with self.assertRaises(ConfigurationError):a.prepare(changed,state)

    async def test_response_storage_failure_never_commits_success(self):
        runtime=await self.setup_runtime()
        original=self.store.commit
        def fail(*args,**kwargs):
            if any(e.type=='attempt.response' for e in kwargs.get('events',[])):
                raise OSError('injected response storage failure')
            return original(*args,**kwargs)
        with patch.object(self.store,'commit',side_effect=fail):
            with self.assertRaises(Exception):await runtime.run()
        self.assertEqual(len(self.calls),1)
        ep=self.store.read_episode(self.gateway.recorder.episode)
        self.assertNotEqual(ep.status,'completed')
        self.assertFalse(any(e['type']=='model.completed' for e in ep.events))

    async def test_cancellation_keeps_id_and_no_second_call(self):
        started=asyncio.Event()
        async def slow(args):
            self.calls.append(args);started.set();await asyncio.Event().wait()
        runtime=await self.setup_runtime(slow)
        task=asyncio.create_task(runtime.run());await started.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(len(self.calls),1)
        ep=self.store.read_episode(self.gateway.recorder.episode)
        self.assertNotEqual(ep.status,'completed')
        self.assertTrue(any(e['type']=='keydesk.requested' for e in ep.events))
