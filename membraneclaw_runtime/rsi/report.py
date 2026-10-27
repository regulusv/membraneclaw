"""Read-only verification and private-answer-free derived reports."""
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from ..tracing.schema import digest
from .config import verify_spec
from .contracts import Invalid, hashed, integer, validate_result
from .versions import verify_version, version
from .policy import decide


def verify(journal):
    journal._resolve()
    verify_spec(journal.spec)
    state = journal.state
    required = set('format status phase round incumbent initial versions operations logical decisions ledger started_at elapsed_floor stop_reason'.split())
    if (not required <= set(state) or set(state)-required-{'active_candidate','inconclusive','final_evaluation'}
        or state['format'] != 1 or state['status'] not in {'running','completed','blocked','cancelled'}):
        raise Invalid('invalid controller checkpoint')
    integer(state['round'],1)
    for key,value in state['versions'].items():
        verify_version(value,journal.spec['config']['allowed_assets'])
        if key != value['version_id'] or value['parent'] is not None and value['parent'] not in state['versions']:
            raise Invalid('version lineage missing')
    if state['incumbent'] not in state['versions'] or state['initial'] not in state['versions']:raise Invalid('missing incumbent')
    spent = reserved = 0
    for key,op in state['operations'].items():
        if (key != op['id'] or key != hashed({'logical':op['logical'],'attempt':op['attempt']})
            or op['input_digest'] != hashed(op['request']) or op['request']['operation_id'] != key):
            raise Invalid('operation identity mismatch')
        if op['status'] not in {'prepared','started','captured','completed','unknown','invalid'}:raise Invalid('operation state')
        integer(op['bound'],1)
        if 'units_observed' in op:spent += integer(op['units_observed'])
        else:reserved += op['bound']
        if op['evidence']:
            envelope = json.loads(journal.store.read_payload(op['evidence']))
            raw = base64.b64decode(envelope['body'],validate=True)
            if digest(raw) != envelope['raw_digest']:raise Invalid('raw evidence digest mismatch')
            if op['status'] == 'completed':
                raw_value = json.loads(raw)
                if raw_value != op['result']:raise Invalid('result differs from captured evidence')
                req = dict(op['request'])
                if 'task' in req:req['task'] = SimpleNamespace(**req['task'])
                validate_result(op['kind'],raw_value,SimpleNamespace(**req))
        elif op['status'] in {'captured','completed','invalid'}:raise Invalid('result without evidence')
    if state['ledger'] != {'calls':len(state['operations']),'spent':spent,'reserved':reserved}:raise Invalid('budget ledger mismatch')
    for logical,opid in state['logical'].items():
        if opid not in state['operations'] or state['operations'][opid]['logical'] != logical:raise Invalid('invalid operation locator')
    expected_initial = (journal.spec['parent']['selected'] if journal.spec['purpose'] == 'final_test'
                        else version(journal.spec['inputs']['assets'])['version_id'])
    if state['initial'] != expected_initial:raise Invalid('initial version differs from frozen input')
    def batch(round_id, role, vid):
        rows = []
        for task in journal.spec['inputs']['tasks']:
            if task['split'] != 'validation':continue
            for seed in journal.spec['config']['seeds']:
                logical = f'round/{round_id}/{role}/{vid}/{task["id"]}/{seed}/evaluate'
                op = state['operations'].get(state['logical'].get(logical))
                if not op or op['status'] != 'completed':raise Invalid('decision lacks complete batch evidence')
                rows.append(op['result'])
        return rows
    selected = state['initial']
    for number,decision in enumerate(state['decisions'],1):
        if decision['round'] != number or decision['parent'] != selected:raise Invalid('decision lineage mismatch')
        if decision['candidate'] is not None:
            recomputed = decide(batch(number,'incumbent',selected),batch(number,'candidate',decision['candidate']))
            if any(decision.get(key) != value for key,value in recomputed.items()):raise Invalid('decision differs from evaluated evidence')
        elif decision['outcome'] != 'rejected':raise Invalid('only rejected invalid edits may lack a candidate')
        expected = decision['candidate'] if decision['outcome'] == 'accepted' else selected
        if decision['selected'] != expected or expected not in state['versions']:raise Invalid('invalid promotion')
        selected = expected
    if state['incumbent'] != selected:raise Invalid('incumbent not supported by decisions')
    if state['status'] == 'completed' and journal.spec['purpose'] == 'search' and len(state['decisions']) != journal.spec['config']['max_rounds']:raise Invalid('incomplete completed search')
    if journal.spec['purpose'] == 'final_test' and state['decisions']:raise Invalid('final evaluation cannot mutate search policy')
    return {'valid':True,'segments':len(journal.chain),'operations':len(state['operations'])}


def summary(journal):
    validation = verify(journal)
    state = journal.state
    versions = state['versions']
    decisions = []
    for item in state['decisions']:
        decisions.append({k:item[k] for k in ('round','outcome','reason','parent','candidate','selected','differences','parent_mean','candidate_mean','mean_gain','paired_cases') if k in item})
    semantic = {'mode':journal.spec['mode'],'purpose':journal.spec['purpose'],'status':state['status'],
                'protocol_digest':hashed({'dataset':{k:v for k,v in journal.spec['inputs'].items() if k != 'source_files'},
                                          'seeds':journal.spec['config']['seeds'],
                                          'policy':journal.spec['config']['policy'],'compiler':journal.spec['compiler'],
                                          'components':journal.spec['components']}),
                'selected_asset_digest':versions[state['incumbent']]['asset_digest'],
                'decisions':[{**{k:v for k,v in d.items() if k not in {'parent','candidate','selected'}},
                              **{k:versions[v]['asset_digest'] if v else None for k,v in d.items() if k in {'parent','candidate','selected'}}}
                             for d in decisions]}
    parent_ledger = journal.spec['parent']['ledger'] if journal.spec['parent'] else {'calls':0,'spent':0,'reserved':0}
    total = {k:state['ledger'][k]+parent_ledger[k] for k in state['ledger']}
    result = {**semantic,'synthetic_only':True,'validation':validation,'stop_reason':state['stop_reason'],
              'run_id':journal.ref.run_id,'selected_version':state['incumbent'],'decisions':decisions,
              'versions':[{k:v[k] for k in ('version_id','asset_digest','parent','candidate')} for v in versions.values()],
              'ledger':state['ledger'],'linked_total_ledger':total,'actual_model_tokens':None,'actual_dollars':None,
              'unknown_operations':[k for k,op in state['operations'].items() if op['status'] in {'started','unknown','captured'}],
              'semantic_digest':hashed(semantic),'record':journal.latest_record()}
    if journal.spec['purpose'] == 'search':
        keys = [(task['id'],seed) for task in journal.spec['inputs']['tasks'] if task['split'] == 'validation'
                for seed in journal.spec['config']['seeds']]
        evaluated = {}
        for op in state['operations'].values():
            if op['kind'] == 'evaluate' and op['status'] == 'completed' and op['result']['status'] == 'completed':
                row = op['result'];evaluated[(row['version_id'],row['task_id'],row['seed'])] = row['score']
        if keys and all((vid,task,seed) in evaluated for vid in (state['initial'],state['incumbent']) for task,seed in keys):
            result['observed_fixture_validation'] = {
                label:sum(evaluated[(vid,task,seed)] for task,seed in keys)/len(keys)
                for label,vid in [('initial_mean',state['initial']),('selected_mean',state['incumbent'])]}
    if journal.spec['parent']:result['parent_record'] = journal.spec['parent']['record']
    if 'final_evaluation' in state:
        result['final_evaluation'] = [{k:r[k] for k in ('task_id','seed','status','score','scale')} for r in state['final_evaluation']]
        # Final metrics are part of semantic identity too.
        result['semantic_digest'] = hashed({**semantic,'final_evaluation':result['final_evaluation']})
    result['record_digest'] = hashed({'segments':[ep.commits[-1]['hash'] if ep.commits else None for ep in journal.chain],
                                    'state':state,'spec':hashed(journal.spec)})
    return result


def markdown(report):
    lines = ['# Synthetic offline RSI report','',
             '**Fixture results only. No model or WaterTAP effectiveness claim.**','',
             f"Status: `{report['status']}`; reason: `{report['stop_reason']}`.",'',
             '| Round | Outcome | Reason |','|---|---|---|']
    for d in report['decisions']:
        reason = d['reason'].replace('|','\\|').replace('\n',' ')
        lines.append(f"| {d['round']} | {d['outcome']} | {reason} |")
    lines += ['',f"Selected asset digest: `{report['selected_asset_digest']}`.",
              f"Synthetic ledger: `{json.dumps(report['ledger'],sort_keys=True)}`.",
              f"Linked search + final-evaluation ledger: `{json.dumps(report['linked_total_ledger'],sort_keys=True)}`.",
              f"Unknown operations: {len(report['unknown_operations'])}.",
              f"Semantic digest: `{report['semantic_digest']}`.",f"Record digest: `{report['record_digest']}`."]
    return '\n'.join(lines)+'\n'


def write_reports(journal):
    result = summary(journal)
    # These are disposable projections; never use them for recovery decisions.
    for name,content in [('report.json',json.dumps(result,indent=2)+'\n'),('report.md',markdown(result))]:
        path = journal.directory/name
        if path.is_symlink():raise Invalid('symlink report projection')
        path.write_text(content,encoding='utf-8');path.chmod(0o600)
    return result
