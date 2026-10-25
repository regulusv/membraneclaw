import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from membraneclaw_runtime.rsi.controller import Controller
from membraneclaw_runtime.rsi.journal import Journal
from membraneclaw_runtime.rsi.fixtures import FixtureSolver
from membraneclaw_runtime.rsi.report import summary,verify
from membraneclaw_runtime.tracing.schema import Busy,Corruption
from helpers import spec,run

import membraneclaw_runtime
PACKAGE_ROOT = str(Path(membraneclaw_runtime.__file__).resolve().parent.parent)


def child(code,*args):
    env = {**os.environ,'PYTHONPATH':PACKAGE_ROOT}
    return subprocess.run([sys.executable,'-c',code,*map(str,args)],env=env,capture_output=True,text=True,timeout=30)


class Recovery(unittest.TestCase):
    def test_process_death_at_operation_and_promotion_boundaries(self):
        code = '''import asyncio,os,signal,sys
from membraneclaw_runtime.rsi.cli import demo_spec
from membraneclaw_runtime.rsi.journal import Journal
from membraneclaw_runtime.rsi.controller import Controller
def fault(stage):
 if stage == sys.argv[2]:os.kill(os.getpid(),signal.SIGKILL)
with Journal(sys.argv[1],spec=demo_spec(),fault=fault) as j:asyncio.run(Controller(j).run())
'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = run(root/'baseline')['semantic_digest']
            for point in ('operation.prepared:after_commit','operation.started:after_commit',
                          'operation.returned_before_capture','operation.captured:after_commit',
                          'operation.completed:after_commit','candidate.checked:after_commit',
                          'candidate.accepted:after_commit','rsi.completed:after_commit'):
                with self.subTest(point=point):
                    path = root/point.replace(':','-')
                    killed = child(code,path,point)
                    self.assertEqual(killed.returncode,-signal.SIGKILL,killed.stderr)
                    with Journal(path,repair=True) as j:
                        if j.state['status'] != 'completed':
                            verify(j);j.prepare_resume()
                            asyncio.run(Controller(j,recompute=True).run())
                        result = summary(j)
                        self.assertEqual(result['status'],'completed')
                        self.assertEqual(result['semantic_digest'],expected)
                        self.assertEqual(len(result['decisions']),2)

    def test_started_without_outcome_does_not_auto_retry(self):
        def fail(stage):
            if stage == 'operation.started:after_commit':raise OSError('crash')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'run'
            with Journal(path,spec=spec(1),fault=fail) as j:
                with self.assertRaises(OSError):asyncio.run(Controller(j).run())
            with Journal(path) as j:
                j.prepare_resume();asyncio.run(Controller(j).run())
                self.assertEqual(len(j.state['operations']),1)
                self.assertEqual(j.state['ledger']['reserved'],1)
                self.assertIn('requires_explicit',j.state['stop_reason'])
                self.assertEqual(len(j.chain),2)

    def test_successor_genesis_registration_is_repairable_and_unique(self):
        code = '''import os,signal,sys
from membraneclaw_runtime.rsi.journal import Journal
def fault(stage):
 if stage == sys.argv[2]:os.kill(os.getpid(),signal.SIGKILL)
with Journal(sys.argv[1],repair=True,fault=fault) as j:j.prepare_resume()
'''
        for point in ('registration_before_create','registration_directories','registration_genesis','registration_checkpoint'):
            with self.subTest(point=point),tempfile.TemporaryDirectory() as temp:
                path = Path(temp)/'run'
                with Journal(path,spec=spec(1)) as j:
                    state = dict(j.state);state['started_at'] = 1
                    j.save('test.initialized',state)
                    j.store.recover(j.ref)
                    # Set a realistic start without mutating terminal history: fixture below uses a generous clock.
                killed = child(code,path,point)
                self.assertEqual(killed.returncode,-signal.SIGKILL,killed.stderr)
                with Journal(path,repair=True) as j:
                    j.prepare_resume()
                    asyncio.run(Controller(j,clock=lambda:1).run())
                    self.assertEqual(summary(j)['status'],'completed')
                    self.assertEqual(len(j.chain),2)
                    self.assertEqual(j.chain[0].status,'interrupted')

    def test_lost_commit_ack_does_not_repeat_solver(self):
        calls=[]
        class Counting(FixtureSolver):
            async def solve(self,request):calls.append(request.task.id);return await super().solve(request)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'run'
            with Journal(path,spec=spec(1)) as j:
                def arm(stage):
                    if stage == 'operation.completed:before_commit':
                        def fail(stage):
                            if stage == 'commit_committed':raise OSError('lost acknowledgement')
                        j.store._fault_hook = fail
                j.fault = arm
                with self.assertRaises(OSError):asyncio.run(Controller(j,solver=Counting()).run())
            with Journal(path) as j:
                j.prepare_resume();asyncio.run(Controller(j,solver=Counting()).run())
                self.assertEqual(summary(j)['status'],'completed')
                self.assertEqual(calls.count('dev-mm'),1)
                self.assertEqual(j.state['ledger']['calls'],13)

    def test_second_process_cannot_own_or_resume_active_run(self):
        code = '''import sys
from membraneclaw_runtime.rsi.journal import Journal
with Journal(sys.argv[1]) as j:
 print("READY",flush=True)
 sys.stdin.readline()
'''
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'run'
            with Journal(path,spec=spec(1)):pass
            proc = subprocess.Popen([sys.executable,'-c',code,str(path)],env={**os.environ,'PYTHONPATH':PACKAGE_ROOT},stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
            try:
                self.assertEqual(proc.stdout.readline().strip(),'READY')
                with self.assertRaises(Busy):Journal(path)
            finally:
                proc.communicate('\n',timeout=5)
            self.assertEqual(proc.returncode,0)

    def test_corrupt_or_missing_payload_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'run'
            with Journal(path,spec=spec(1)) as j:
                ep = j.store.read_episode(j.ref)
                payload = j.store._safe(j.ref.key+'/payloads/'+ep.checkpoint_ref.rsplit(':',1)[1]+'.bin')
                payload.unlink()
            with self.assertRaises(Corruption):Journal(path)
