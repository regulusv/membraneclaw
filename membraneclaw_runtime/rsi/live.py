"""Live RSI components executed by the native AgentRuntime and ModelGateway.

Key Desk is a text-only remote transport. Native traces capture the canonical
submitted JSON and remote response text, not independently observed wire bytes.
"""
import asyncio
import copy
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from ..gateway import ModelGateway, Budget, RetryPolicy
from ..providers.gemini import GeminiAdapter
from ..providers.types import ModelRequest, ContentBlock, NativeResponse, TransportError
from ..runtime import AgentRuntime
from ..state import RuntimeLimits
from ..tools import ToolRegistry
from ..tracing import FileTraceStore
from ..tracing.schema import EpisodeRef, Payload, canonical_json
from ..tracing.validation import validate
from .contracts import Invalid, SolverResult, document, hashed

MODEL = 'gemini-3.1-flash-lite'
IDENTITIES = {'solver': 'native_gemini_solver_v1', 'proposer': 'native_gemini_proposer_v1',
              'evaluator': 'numeric_fixture_v1'}


def atomic_json(path, value):
    path = Path(path)
    if path.exists():raise Invalid('refusing to overwrite bridge evidence')
    temp = path.with_suffix(path.suffix+'.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(canonical_json(value));stream.flush();os.fsync(stream.fileno())
    os.replace(temp, path)


class KeyDeskTransport:
    authentication_values = ()  # Credential remains on the remote service.

    def __init__(self, directory, request_id):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.request_id = request_id

    async def exchange(self, request, *, before_send, after_receive, timeout):
        body = json.loads(request.body)
        try:
            prompt = body['contents'][0]['parts'][0]['text']
            tokens = body['generationConfig']['maxOutputTokens']
        except (KeyError, IndexError, TypeError):
            raise Invalid('request outside Key Desk text profile') from None
        expected = {'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
                    'generationConfig': {'maxOutputTokens': tokens, 'candidateCount': 1}}
        if (body != expected or type(prompt) is not str or type(tokens) is not int or not 1 <= tokens <= 1024
            or request.method != 'POST' or request.route.model != MODEL
            or request.path != f'/v1beta/models/{MODEL}:generateContent'):
            raise Invalid('request outside Key Desk text profile')
        await before_send(request)
        envelope = {'request_id': self.request_id, 'operation': 'generate', 'model': MODEL,
                    'prompt': prompt, 'max_output_tokens': tokens}
        # Publication occurs only after the native attempt.started commit.
        atomic_json(self.directory/(self.request_id+'.request.json'), envelope)
        response_path = self.directory/(self.request_id+'.response.json')
        deadline = time.monotonic()+timeout
        while not response_path.exists():
            if time.monotonic() >= deadline:raise TransportError('key_desk_outcome_unknown')
            await asyncio.sleep(0.1)
        receipt = json.loads(response_path.read_text())
        if receipt.get('request_id') != self.request_id or receipt.get('state') not in {'completed','failed'}:
            raise TransportError('key_desk_outcome_unknown')
        submitted = receipt.get('request', {})
        actual = submitted.get('body')
        if isinstance(actual, str):actual = json.loads(actual)
        if (actual != body or submitted.get('method') != request.method
            or submitted.get('url') != 'https://generativelanguage.googleapis.com'+request.path):
            raise TransportError('key_desk_request_mismatch')
        result = receipt.get('result', {})
        if type(result.get('http_status')) is not int or type(result.get('raw_response')) is not str:
            raise TransportError('key_desk_receipt_invalid')
        response = NativeResponse(result['http_status'], {}, result['raw_response'].encode())
        await after_receive(response)  # Raw response captured before provider parsing.
        return response


def component_prompt(role, request):
    if role == 'solver':
        return (request.agent.prompt+'\n'+'\n'.join(t for _,t in request.agent.skills)
                +'\nTask:\n'+request.task.text)
    return ('Improve the supplied agent using development evidence only. Return a JSON object '
            'with edits (list of {path,before,after}) and rationale (string). before is the supplied '
            'preimage hash; after is full replacement text. Make one generalizable text edit, '
            'never hardcode answers. Keep the response concise.\n'+json.dumps({
                'assets': {p: {'text': t, 'before': hashed(t)} for p,t in request.assets},
                'development_evidence': json.loads(request.development_evidence)}, sort_keys=True))


def parse_proposal(text):
    text = text.strip()
    if text.startswith('```') and text.endswith('```'):
        text = '\n'.join(text.splitlines()[1:-1])
    value = json.loads(text)
    if type(value) is not dict or set(value) != {'edits','rationale'}:raise Invalid('proposal fields')
    return value


class LiveComponent:
    offline_pure = False
    bound = 1  # One operation unit; tokens are separately observed in model traces.

    def __init__(self, journal, transport_factory=None):
        self.j = journal
        self.transport_factory = transport_factory or (lambda rid: KeyDeskTransport(journal.directory/'bridge', rid))

    async def invoke(self, request):
        adapter = GeminiAdapter()
        route = adapter.initial_state(MODEL).route
        store = FileTraceStore(self.j.directory/'model-traces')
        rid = 'rsi-'+hashed({'run': self.j.ref.run_id, 'operation': request.operation_id})[:40]
        gateway = await ModelGateway.start(store, adapter, self.transport_factory(rid), route,
            role=self.role, run_id=self.j.ref.run_id,
            budget=Budget(max_requests=1, max_elapsed_seconds=960, attempt_timeout_seconds=900),
            retry=RetryPolicy(max_attempts=1), context={
                'rsi_operation_id': request.operation_id, 'rsi_request_digest': hashed(request),
                'rsi_root_run': self.j.ref.run_id,
                'version_id': getattr(request, 'version_id', getattr(request, 'parent_version', None)),
                'task_id': request.task.id if self.role == 'solver' else None,
                'transport': 'key_desk_bridge', 'external_request_id': rid,
                'capture_boundary': 'canonical_submitted_json_and_remote_response_text'})
        state = copy.deepcopy(self.j.state)
        state['operations'][request.operation_id]['model_episode'] = gateway.recorder.episode.key
        self.j.save('rsi.model_linked', state)
        model_request = ModelRequest(route, (ContentBlock.text_block(component_prompt(self.role, request)),),
                                     max_output_tokens=1024, provider_options={'gemini': {'candidateCount': 1}})
        async with gateway.execution():
            await gateway.record_event('rsi.operation', payload=Payload.json(document(request)))
            result = await AgentRuntime(gateway, ToolRegistry([]), model_request,
                                        limits=RuntimeLimits(max_steps=1)).run()
        episode = store.read_episode(gateway.recorder.episode)
        if result.status != 'completed':raise Invalid('native runtime did not complete')
        return result.text, {'episode': episode.ref.key, 'head': episode.commits[-1]['hash']}


class LiveSolver(LiveComponent):
    identity = IDENTITIES['solver']
    role = 'solver'

    async def solve(self, request):
        text, trace = await self.invoke(request)
        return {**document(SolverResult(request.operation_id, request.version_id, request.task.id,
                                       request.seed, 'completed', text)), 'model_trace': trace}


class LiveProposer(LiveComponent):
    identity = IDENTITIES['proposer']
    role = 'proposer'

    async def propose(self, request):
        text, trace = await self.invoke(request)
        return {**parse_proposal(text), 'operation_id': request.operation_id,
                'parent_version': request.parent_version, 'evidence_digest': hashed(request.development_evidence),
                'units': 1, 'model_trace': trace}


def live_spec():
    from .cli import demo_spec
    spec = demo_spec()
    spec['mode'] = spec['config']['mode'] = 'live_gemini'
    spec['components'] = dict(IDENTITIES)
    spec['config']['max_rounds'] = 1
    spec['config']['limits'] = {'operations': 30, 'units': 30, 'wall_seconds': 7200}
    spec['inputs']['assets']['prompt.txt'] = 'Solve the supplied quantity task. Return only a JSON object with a numeric value field.'
    spec['inputs']['source_files']['assets'] = dict(spec['inputs']['assets'])
    spec['inputs']['source_files']['configuration'] = json.dumps(spec['config'], sort_keys=True)
    spec['config_digest'] = hashed(spec['config'])
    spec['dataset_digest'] = hashed(spec['inputs'])
    return spec


def verify_model_links(journal):
    """Join immutable native episodes to RSI requests/results, including failed calls."""
    from types import SimpleNamespace
    from .contracts import CompiledAgent
    store = FileTraceStore(journal.directory/'model-traces')
    rows = []
    for op in journal.state['operations'].values():
        if op['kind'] not in {'solve','propose'}:continue
        key = op.get('model_episode')
        if key is None:
            if op['status'] == 'completed':raise Invalid('completed live operation has no trace')
            continue
        ep = store.read_episode(EpisodeRef(*key.split('/')))
        check = validate(store, ep.ref)
        context = ep.metadata['context']
        if (context['rsi_operation_id'] != op['id'] or context['rsi_request_digest'] != op['input_digest']
            or context['rsi_root_run'] != journal.ref.run_id):raise Invalid('model/RSI provenance mismatch')
        starts = [e for e in ep.events if e['type'] == 'attempt.started']
        responses = [e for e in ep.events if e['type'] == 'attempt.response']
        completed = [e for e in ep.events if e['type'] == 'model.completed']
        if op['status'] == 'completed':
            if (check.status != 'completed' or check.completeness['capture'] != 'complete'
                or len(starts) != 1 or len(responses) != 1 or len(completed) != 1
                or op['result'].get('model_trace') != {'episode': key, 'head': ep.commits[-1]['hash']}):
                raise Invalid('incomplete or mismatched native model trace')
            req = copy.deepcopy(op['request'])
            if op['kind'] == 'solve':
                req['agent'] = CompiledAgent(**req['agent']);req['task'] = SimpleNamespace(**req['task'])
            request = SimpleNamespace(**req)
            native = json.loads(store.read_payload(starts[0]['payload_ref']))
            if native['contents'][0]['parts'][0]['text'] != component_prompt(ep.metadata['role'], request):
                raise Invalid('native request differs from RSI request')
            snapshot = json.loads(store.read_payload(ep.checkpoint_ref))
            parts = snapshot['provider_state']['native_history'][-1]['parts']
            text = ''.join(p.get('text','') for p in parts if not p.get('thought'))
            if op['kind'] == 'solve' and text != op['result']['answer']:raise Invalid('solver output differs from trace')
            if op['kind'] == 'propose' and parse_proposal(text) != {k:op['result'][k] for k in ('edits','rationale')}:
                raise Invalid('proposal differs from trace')
        snapshot = json.loads(store.read_payload(ep.checkpoint_ref)) if ep.checkpoint_ref else {}
        rows.append({'operation_id': op['id'], 'kind': op['kind'], 'episode': key,
                     'head': ep.commits[-1]['hash'] if ep.commits else None,
                     'status': ep.status, 'completeness': check.completeness,
                     'tokens_observed': snapshot.get('tokens_observed', 0),
                     'usage_unknown': snapshot.get('usage_unknown', True)})
    return rows
