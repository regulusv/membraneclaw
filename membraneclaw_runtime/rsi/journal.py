"""One run lock, immutable segment genesis registration, atomic checkpoints."""
import copy
import fcntl
import json
import os
from pathlib import Path
from uuid import uuid4
from ..tracing import FileTraceStore, TraceRecorder
from ..tracing.schema import Busy, Corruption, EpisodeRef, Event, Payload, canonical_json
from ..tracing.store import genesis
from .contracts import hashed, Invalid
from .versions import version


def initial_state(spec):
    first = version(spec["inputs"]["assets"])
    versions = {first['version_id']:first}
    if spec['purpose'] == 'final_test':
        versions = copy.deepcopy(spec['parent']['versions'])
        first = versions[spec['parent']['selected']]
    return {"format":1,"status":"running","phase":"initialized","round":1,
            "incumbent":first["version_id"],"initial":first["version_id"],"versions":versions,
            "operations":{},"logical":{},"decisions":[],"ledger":{"calls":0,"spent":0,"reserved":0},
            "started_at":None,"elapsed_floor":0,"stop_reason":None}


class Journal:
    def __init__(self, directory, *, spec=None, fault=None, repair=False):
        supplied = Path(directory).absolute()
        if supplied.is_symlink():raise Invalid("symlink run directory")
        self.directory = supplied.resolve()
        self.fault = fault or (lambda stage:None)
        if spec is not None:self.directory.mkdir(parents=True,exist_ok=False,mode=0o700)
        if not self.directory.is_dir():raise Invalid("run directory missing")
        lock = self.directory/'.run.lock'
        if lock.is_symlink():raise Invalid("symlink run lock")
        fd = os.open(lock,os.O_CREAT|os.O_WRONLY|getattr(os,"O_NOFOLLOW",0),0o600)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd);raise Busy("RSI run busy") from None
        self.fd = fd
        try:
            traces = self.directory/'traces'
            if traces.is_symlink() or (spec is None and not traces.is_dir()):raise Invalid('missing or symlink trace store')
            self.store = FileTraceStore(traces)
            if spec is not None:
                ref = EpisodeRef('rsi',uuid4().hex,'root')
                metadata = {"kind":"rsi_root","spec":spec,"initial":initial_state(spec),"parent":None}
                self._register(ref,metadata)
            self._resolve(repair=spec is not None or repair)
            if spec is not None:self.bootstrap()
        except BaseException:
            self.close();raise

    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def close(self):
        if self.fd is not None:os.close(self.fd);self.fd=None

    def _register(self, ref, metadata):
        directory = self.store._directory(ref)
        if not directory.exists():
            self.store._mkdir(directory/'commits');self.store._mkdir(directory/'payloads')
        self.fault('registration_directories')
        path = directory/'genesis.json'
        if path.exists():
            if self.store.read_episode(ref).metadata != metadata:raise Corruption("conflicting segment registration")
        else:
            # The deterministic predecessor reconstructs only a successor that
            # crashed before its genesis; no external work was dispatched here.
            self.store._atomic(path,canonical_json(genesis(ref,metadata)),immutable=True)
        self.fault('registration_genesis')

    def _resolve(self, repair=False):
        roots = list(self.store.root.glob('rsi/*/root/genesis.json'))
        if len(roots) != 1:raise Corruption("exactly one registered RSI root required")
        relative = roots[0].relative_to(self.store.root)
        ref = EpisodeRef(*relative.parts[:3]);chain=[]
        root = self.store.read_episode(ref)
        if root.metadata.get("kind") != "rsi_root":raise Corruption("invalid RSI root")
        self.spec = root.metadata["spec"]
        while True:
            ep = self.store.read_episode(ref);chain.append(ep)
            if ep.status == 'running' or not ep.commits:break
            child = EpisodeRef(ref.tenant,ref.run_id,'s_'+ep.commits[-1]['hash'][:40])
            directory = self.store._directory(child)
            if not directory.exists():break
            state = json.loads(self.store.read_payload(ep.checkpoint_ref)) if ep.checkpoint_ref else ep.metadata['initial']
            metadata = {"kind":"rsi_segment","initial":state,
                        "parent":{"episode":ref.key,"head":ep.commits[-1]['hash'],"state_digest":hashed(state)}}
            if not (directory/'genesis.json').exists():
                if not repair:raise Corruption("incomplete successor registration; resume can repair")
                self._register(child,metadata)
            child_ep = self.store.read_episode(child)
            if child_ep.metadata != metadata:raise Corruption("successor predecessor mismatch")
            ref = child
        registered = set(self.store._episode_refs())
        if registered != {ep.ref for ep in chain}:raise Corruption("unregistered or branched RSI segment")
        self.chain = chain;self.ref = ref
        self.recorder = TraceRecorder(self.store,ref)
        self.state = json.loads(self.store.read_payload(ep.checkpoint_ref)) if ep.checkpoint_ref else copy.deepcopy(ep.metadata['initial'])

    def bootstrap(self):
        ep = self.store.read_episode(self.ref)
        if not ep.checkpoint_ref:self.save('rsi.segment_started',self.state)
        self.fault('registration_checkpoint')

    def prepare_resume(self):
        self._resolve(repair=True)
        ep = self.store.read_episode(self.ref)
        if not ep.checkpoint_ref:
            self.bootstrap()
            return  # Adopt a registered successor that never dispatched work.
        if len(ep.events) == 1 and ep.events[0]['type'] == 'rsi.segment_started':return
        if self.state['status'] == 'completed':return
        ep = self.store.read_episode(self.ref)
        if ep.status == 'running':
            self.store.recover(self.ref)
            ep = self.store.read_episode(self.ref)
        child = EpisodeRef(ep.ref.tenant,ep.ref.run_id,'s_'+ep.commits[-1]['hash'][:40])
        metadata = {"kind":"rsi_segment","initial":self.state,
                    "parent":{"episode":ep.ref.key,"head":ep.commits[-1]['hash'],"state_digest":hashed(self.state)}}
        self.fault('registration_before_create')
        self._register(child,metadata)
        self._resolve(repair=True);self.bootstrap()

    def save(self, kind, state, *, evidence=None, terminal=None):
        state = copy.deepcopy(state)
        checkpoint = Payload.json(state)
        payload = Payload.json(evidence) if evidence is not None else None
        event = Event("episode."+terminal if terminal else kind,payload_ref=payload.ref(self.ref) if payload else None,
                      data={"phase":state['phase'],"round":state['round'],"rsi_transition":kind})
        self.fault(kind+':before_commit')
        completeness = None
        if terminal:
            unknown = any(op['status'] in {'started','unknown','captured'} for op in state['operations'].values())
            completeness = {'capture':'complete','replay':'unsupported','external_outcome':'unknown' if unknown else 'known'}
        self.recorder.append_event(event,payload=payload,checkpoint=checkpoint,status=terminal,completeness=completeness)
        self.state = state
        self.fault(kind+':after_commit')

    def latest_record(self):
        ep = self.store.read_episode(self.ref)
        return {"episode":ep.ref.key,"head":ep.commits[-1]['hash'],"revision":ep.revision}
