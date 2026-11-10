"""POSIX JSON-lines bridge for an authenticated agent to run a live trace check.

stdout emits gemini_call arguments; stdin accepts that MCP result (or its parsed
record). No key crosses this interface. Run only with an authorized MCP caller.
"""
import argparse
import asyncio
import json
import socket
import sys
from pathlib import Path
from unittest.mock import patch

from ..gateway import Budget
from ..providers.keydesk import start_keydesk_gateway
from ..providers.types import ModelRequest, ContentBlock
from ..runtime import AgentRuntime
from ..tools import ToolRegistry
from .file_store import FileTraceStore
from .validation import validate
from .export import export_bundle, restore_bundle
from .replay import execute_replay

async def main(args):
    reader=asyncio.StreamReader()
    pipe,_=await asyncio.get_running_loop().connect_read_pipe(lambda:asyncio.StreamReaderProtocol(reader),sys.stdin)
    records=[]
    async def invoke(arguments):
        print(json.dumps({'tool':'gemini_call','arguments':arguments}),flush=True)
        line=await reader.readline()
        if not line:raise RuntimeError('MCP caller disconnected')
        value=json.loads(line)
        if 'content' in value:
            if value.get('isError'):raise RuntimeError('MCP tool failed')
            value=json.loads(value['content'][0]['text'])
        records.append(value)
        return value
    store=FileTraceStore(args.trace_dir)
    gateway=await start_keydesk_gateway(store,args.credential_id,invoke,
        budget=Budget(max_requests=1,max_elapsed_seconds=300,attempt_timeout_seconds=240))
    episode=gateway.recorder.episode
    print(json.dumps({'episode':episode.key}),flush=True)
    request=ModelRequest(gateway.route,(ContentBlock.text_block('Convert 1.25 litres to millilitres. Reply only with the number.'),),max_output_tokens=256)
    try:
        result=await AgentRuntime(gateway,ToolRegistry(),request).run()
    finally:
        pipe.close()
    assert result.status=='completed' and result.text.strip()=='1250', result
    reopened=FileTraceStore(args.trace_dir)
    report=validate(reopened,episode)
    events=reopened.read_episode(episode).events
    request_event=next(e for e in events if e['type']=='attempt.started')
    response_event=next(e for e in events if e['type']=='attempt.response')
    assert json.loads(reopened.read_payload(request_event['payload_ref']))==records[0]['request']['body']
    assert reopened.read_payload(response_event['payload_ref'])==records[0]['result']['raw_response'].encode('utf-8')
    bundle=Path(args.trace_dir)/(episode.episode_id+'.zip')
    export_bundle(reopened,episode,bundle,mode='private-replay')
    restored,ref=restore_bundle(bundle,Path(args.trace_dir)/('restored-'+episode.episode_id))
    with patch.object(socket,'create_connection',side_effect=AssertionError('network forbidden during replay')):
        replay=await execute_replay(restored,ref)
    assert replay['text']==result.text and replay['network'] is False
    summary={'episode':episode.key,'status':report.status,'event_count':len(events),
        'text':result.text,'remote_request_id':gateway.transport.request_ids[0],
        'usage':gateway._last_output.usage.raw,'raw_response_matches':True,
        'export_restore_replay':replay,'bundle':str(bundle)}
    Path(args.trace_dir,'live-smoke-report.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credential-id',required=True)
    parser.add_argument('--trace-dir',default='var/traces/keydesk-live')
    asyncio.run(main(parser.parse_args()))
