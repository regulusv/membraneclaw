"""Serial, checkpointed improvement loop. All components are replaceable."""
import asyncio
import base64
import copy
import json
import time
from dataclasses import replace
from ..tracing.schema import Payload, digest
from ..tracing.policy import CredentialRejected
from .budget import Exhausted, reserve, settle
from .config import check_current_source
from .contracts import (Invalid, SolveRequest, ProposalRequest, EvaluationRequest,
                        document, normalize, hashed, validate_result)
from .datasets import public
from .fixtures import FixtureSolver, FixtureProposer, FixtureEvaluator
from .policy import decide
from .versions import apply_candidate, compile_agent
from .workspace import materialize, verify as verify_workspace


BUDGET_STOPS = {"observed_usage_exceeded_bound","operation_count","synthetic_units","wall_time"}


class Halt(Exception):
    pass


class Controller:
    def __init__(self, journal, *, solver=None, proposer=None, evaluator=None, recompute=False, clock=time.time):
        self.j = journal;self.spec = journal.spec;self.recompute = recompute;self.clock = clock
        self.tick_anchor = time.monotonic()
        started = journal.state.get('started_at')
        self.elapsed_anchor = max(journal.state.get('elapsed_floor',0), max(0,clock()-started) if started is not None else 0)
        self.components = {"solve":solver or FixtureSolver(),"propose":proposer or FixtureProposer(),
                           "evaluate":evaluator or FixtureEvaluator(self.spec['inputs']['references'])}
        for name,component in self.components.items():
            role = {"solve":"solver","propose":"proposer","evaluate":"evaluator"}[name]
            if (component.identity != self.spec['components'][role] or component.offline_pure is not True
                or type(component.bound) is not int or component.bound < 1):raise Invalid("unapproved offline adapter")

    def elapsed(self, state):
        wall = max(0,self.clock()-state['started_at']) if state['started_at'] is not None else 0
        return max(state.get('elapsed_floor',0),wall,self.elapsed_anchor+time.monotonic()-self.tick_anchor)

    def save(self, kind, state, **kwargs):
        state['elapsed_floor'] = self.elapsed(state)
        self.j.save(kind,state,**kwargs)
    def state(self):return copy.deepcopy(self.j.state)

    async def invoke(self, kind, logical, request):
        state = self.state()
        opid = state['logical'].get(logical)
        op = state['operations'].get(opid)
        if op and op['status'] == 'completed':return copy.deepcopy(op['result'])
        if op and op['status'] in {'started','unknown'}:
            op['status'] = 'unknown'
            self.save('operation.unknown',state)
            if not self.recompute:raise Halt('outcome_unknown_requires_explicit_offline_recompute')
            # Preserve the uncertain original reservation and charge a new attempt.
            op = None
        if op and op['status'] == 'invalid':raise Halt('invalid_result')
        component = self.components[kind]
        if op is None:
            attempt = sum(1 for v in state['operations'].values() if v['logical'] == logical)
            opid = hashed({'logical':logical,'attempt':attempt})
            request = replace(request,operation_id=opid)
            elapsed = self.elapsed(state)
            reserve(state['ledger'],self.spec['config']['limits'],component.bound,elapsed)
            op = {'id':opid,'logical':logical,'kind':kind,'input_digest':hashed(request),
                  'status':'prepared','bound':component.bound,'evidence':None,'result':None,
                  'attempt':attempt,'request':document(request)}
            state['operations'][opid] = op;state['logical'][logical] = opid
            self.save('operation.prepared',state)
        request = replace(request,operation_id=opid)
        if op['input_digest'] != hashed(request):raise Invalid('operation input changed')
        if op['status'] == 'prepared':
            # Time elapsed since reservation does not grant a fresh deadline.
            remaining = self.spec['config']['limits']['wall_seconds'] - self.elapsed(state)
            if remaining <= 0:raise Exhausted('wall_time')
            op['status'] = 'started';self.save('operation.started',state)
            cancelled = None
            task = asyncio.create_task(getattr(component,kind)(request))
            try:
                raw = await asyncio.wait_for(asyncio.shield(task),remaining)
                evidence_kind = 'returned'
            except (asyncio.CancelledError,TimeoutError) as error:
                task.cancel()
                while not task.done():
                    try:await asyncio.shield(task)
                    except asyncio.CancelledError:continue
                    except Exception:break
                try:raw = task.result();evidence_kind = 'returned'
                except BaseException as late:
                    raw = {'exception_type':type(late).__name__};evidence_kind = 'exception'
                cancelled = error
            except Exception as error:
                raw = {'exception_type':type(error).__name__};evidence_kind = 'exception'
            self.j.fault('operation.returned_before_capture')
            # Raw evidence is committed even if the returned JSON has NaN,
            # mismatched IDs, or an invalid schema. It is not a valid outcome yet.
            body = json.dumps(normalize(raw),ensure_ascii=True,allow_nan=True,
                              default=lambda value:{'unsupported_type':type(value).__name__}).encode()
            try:self.j.store.policy.check(body)
            except CredentialRejected:
                op['status'] = 'unknown';op['capture_error'] = 'credential_rejected'
                self.save('operation.capture_rejected',state)
                raise Halt('credential_rejected_before_persistence') from None
            evidence = {'kind':evidence_kind,'encoding':'base64','body':base64.b64encode(body).decode(),
                        'raw_digest':digest(body)}
            payload = Payload.json(evidence)
            op['evidence'] = payload.ref(self.j.ref);op['status'] = 'captured'
            self.save('operation.captured',state,evidence=evidence)
            if cancelled is not None:
                # Keep a valid late receipt when available, but never advance.
                try:self.validate_capture(opid,request)
                except Halt:pass
                raise cancelled
        return self.validate_capture(opid,request)

    def validate_capture(self, opid, request):
        state = self.state();op = state['operations'][opid]
        envelope = json.loads(self.j.store.read_payload(op['evidence']))
        raw = base64.b64decode(envelope['body'],validate=True)
        if digest(raw) != envelope['raw_digest']:raise Invalid('raw evidence mismatch')
        value = json.loads(raw)
        if envelope['kind'] == 'exception':
            op['status'] = 'unknown';self.save('operation.unknown',state)
            raise Halt('component_exception_outcome_unknown')
        observed = value.get('units') if type(value) is dict else None
        # Usage settlement survives other semantic validation failures.
        overrun = False
        if type(observed) is int and observed >= 0:
            overrun = settle(state['ledger'],op['bound'],observed)
            op['units_observed'] = observed
        try:
            validate_result(op['kind'],value,request)
        except (Invalid,ValueError,TypeError) as error:
            op['status'] = 'invalid';op['validation_error'] = type(error).__name__
            self.save('operation.invalid',state)
            raise Halt('invalid_result') from None
        op['status'] = 'completed';op['result'] = value
        self.save('operation.completed',state)
        if overrun:raise Exhausted('observed_usage_exceeded_bound')
        return value

    async def batch(self, version_id, split, label):
        state = self.j.state
        version = state['versions'][version_id]
        agent = compile_agent(version)
        workspace = self.j.directory/'workspaces'/hashed({'label':label,'version':version_id})
        if workspace.exists():verify_workspace(workspace,version)
        else:materialize(workspace,version,self.spec['config']['allowed_assets'])
        rows = []
        for task in self.spec['inputs']['tasks']:
            if task['split'] != split:continue
            for seed in self.spec['config']['seeds']:
                key = f'{label}/{version_id}/{task["id"]}/{seed}'
                solve = SolveRequest('',version_id,agent,public(task),seed)
                verify_workspace(workspace,version)
                result = await self.invoke('solve',key+'/solve',solve)
                verify_workspace(workspace,version)
                request = EvaluationRequest('',version_id,public(task),seed,result['answer'],result['status'])
                evaluation = await self.invoke('evaluate',key+'/evaluate',request)
                if evaluation['status'] != 'completed':raise Halt('incomplete_execution')
                rows.append(evaluation)
        return rows

    async def run(self):
        check_current_source(self.spec)
        if self.j.state['status'] == 'completed' or self.j.state['stop_reason'] in BUDGET_STOPS:return self.j.state
        with self.j.store.execution_guard(self.j.ref):
            state = self.state()
            if state['started_at'] is None:state['started_at'] = self.clock()
            state['status'] = 'running';state['stop_reason'] = None
            self.save('rsi.running',state)
            try:
                if self.spec['purpose'] == 'final_test':
                    rows = await self.batch(state['incumbent'],'test','final_test')
                    state = self.state();state['final_evaluation'] = rows
                else:
                    while self.j.state['round'] <= self.spec['config']['max_rounds']:
                        state = self.state();round_id = state['round'];parent = state['incumbent']
                        state['phase'] = 'baseline_development';self.save('rsi.phase',state)
                        development = await self.batch(parent,'development',f'round/{round_id}/development')
                        state = self.state();state['phase'] = 'baseline_validation';self.save('rsi.phase',state)
                        baseline = await self.batch(parent,'validation',f'round/{round_id}/incumbent')
                        state = self.state();state['phase'] = 'proposing';self.save('rsi.phase',state)
                        # Only development evidence crosses the proposer boundary.
                        development_records = []
                        for row in development:
                            task = next(t for t in self.spec['inputs']['tasks'] if t['id'] == row['task_id'])
                            solved = next(op['result'] for op in state['operations'].values()
                                          if op['kind'] == 'solve' and op['status'] == 'completed'
                                          and op['result']['version_id'] == parent
                                          and op['result']['task_id'] == row['task_id'] and op['result']['seed'] == row['seed'])
                            development_records.append({'task':document(public(task)),'answer':solved['answer'],
                                                        'execution_status':solved['status'],'evaluation':row})
                        evidence = json.dumps(development_records,sort_keys=True,separators=(',',':'))
                        req = ProposalRequest('',parent,tuple(sorted(state['versions'][parent]['assets'].items())),evidence,round_id)
                        candidate = await self.invoke('propose',f'round/{round_id}/proposal/{parent}',req)
                        state = self.state()
                        try:
                            child = apply_candidate(state['versions'][parent],candidate,self.spec['config']['allowed_assets'],
                                                    {v['asset_digest'] for v in state['versions'].values()
                                                     if not (state.get('active_candidate',{}).get('round') == round_id
                                                             and v['version_id'] == state['active_candidate']['version_id'])})
                        except Invalid as error:
                            decision = {'outcome':'rejected','reason':str(error),'round':round_id,'parent':parent,'candidate':None}
                        else:
                            if (state.get('active_candidate',{}).get('round') == round_id
                                and state['active_candidate']['version_id'] != child['version_id']):
                                raise Invalid('active candidate mismatch')
                            state['active_candidate'] = {'round':round_id,'version_id':child['version_id']}
                            state['versions'][child['version_id']] = child
                            state['phase'] = 'candidate_evaluating';self.save('candidate.checked',state,evidence=child)
                            results = await self.batch(child['version_id'],'validation',f'round/{round_id}/candidate')
                            decision = {**decide(baseline,results),'round':round_id,'parent':parent,'candidate':child['version_id']}
                        state = self.state()
                        if decision['outcome'] == 'accepted':state['incumbent'] = decision['candidate']
                        decision['selected'] = state['incumbent']
                        state['decisions'].append(decision);state['round'] += 1;state['phase'] = 'round_committed'
                        self.save('candidate.'+decision['outcome'],state,evidence=decision)
                        if decision['outcome'] == 'inconclusive':raise Halt('inconclusive_evaluation')
                    state = self.state()
                state['status'] = 'completed';state['phase'] = 'finished';state['stop_reason'] = 'completed'
                self.save('rsi.completed',state,terminal='completed')
            except (Halt,Exhausted,TimeoutError) as error:
                state = self.state();state['status'] = 'blocked';state['stop_reason'] = str(error) or type(error).__name__
                if state['phase'] == 'candidate_evaluating':
                    state['inconclusive'] = {'round':state['round'],'reason':state['stop_reason']}
                self.save('rsi.stopped',state,terminal='failed')
            except asyncio.CancelledError:
                state = self.state();state['status'] = 'cancelled';state['stop_reason'] = 'cancelled'
                self.save('rsi.cancelled',state,terminal='cancelled');raise
        return self.j.state
