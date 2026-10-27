import asyncio
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from membraneclaw_runtime.rsi.cli import final_spec,main,demo_spec
from membraneclaw_runtime.rsi.controller import Controller
from membraneclaw_runtime.rsi.contracts import Invalid
from membraneclaw_runtime.rsi.journal import Journal
from membraneclaw_runtime.rsi.report import summary,write_reports
from helpers import run,spec


class Reports(unittest.TestCase):
    def test_semantic_digest_is_stable_but_record_digest_is_not(self):
        with tempfile.TemporaryDirectory() as temp:
            first = run(Path(temp)/'a',spec(1));second = run(Path(temp)/'b',spec(1))
            self.assertEqual(first['semantic_digest'],second['semantic_digest'])
            self.assertNotEqual(first['record_digest'],second['record_digest'])

    def test_report_rebuild_and_completed_resume_are_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'run';first = run(path,spec(1))
            with Journal(path) as j:
                write_reports(j)
                (path/'report.json').unlink();(path/'report.md').unlink()
                second = write_reports(j)
                self.assertEqual(first,second)
                record = j.latest_record()
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(main(['resume','--run-dir',str(path)]),0)
            with Journal(path) as j:self.assertEqual(j.latest_record(),record)
            self.assertNotIn('PRIVATE_EVALUATOR_CANARY',(path/'report.json').read_text())

    def test_final_evaluation_is_independent_linked_and_charged(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'search';run(path,spec(1))
            with Journal(path) as parent:
                before = parent.latest_record()
                final = final_spec(parent,{'operations':2,'units':2,'wall_seconds':100})
            with Journal(Path(temp)/'final',spec=final) as j:
                asyncio.run(Controller(j).run());report = summary(j)
                self.assertEqual(report['status'],'completed')
                self.assertEqual(report['decisions'],[])
                self.assertEqual(report['final_evaluation'][0]['score'],1)
                self.assertEqual(report['ledger']['calls'],2)
                self.assertEqual(report['linked_total_ledger']['calls'],15)
            with Journal(path) as parent:self.assertEqual(parent.latest_record(),before)

    def test_final_evaluation_requires_completed_search(self):
        with tempfile.TemporaryDirectory() as temp,Journal(Path(temp)/'search',spec=spec()) as j:
            with self.assertRaises(Invalid):final_spec(j,{'operations':2,'units':2,'wall_seconds':100})

    def test_cli_demo_verify_inspect_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()):
            path = str(Path(temp)/'run')
            self.assertEqual(main(['demo','--output',path]),0)
            self.assertEqual(main(['verify','--run-dir',path]),0)
            self.assertEqual(main(['inspect','--run-dir',path]),0)
            self.assertEqual(main(['demo','--output',path]),1)

    def test_packaged_fixture_bytes_match_editable_examples(self):
        examples = Path(__file__).resolve().parents[2]/'examples/rsi'
        from importlib.resources import files
        packaged = Path(str(files('membraneclaw_runtime.rsi').joinpath('data')))
        for path in examples.rglob('*'):
            if path.is_file():self.assertEqual(path.read_bytes(),(packaged/path.relative_to(examples)).read_bytes())

    def test_budget_stop_resume_cannot_override_the_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'run';run(path,spec(1,operations=1))
            with Journal(path) as j:before = j.latest_record()
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(main(['resume','--run-dir',str(path)]),2)
            with Journal(path) as j:self.assertEqual(j.latest_record(),before)

    def test_decision_verification_recomputes_from_evaluation_evidence(self):
        from membraneclaw_runtime.rsi.report import verify
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'run';run(path,spec(1))
            with Journal(path) as j:
                j.store.read_episode(j.ref)  # Terminal history must remain untouched.
                saved = copy.deepcopy(j.state)
                j._resolve = lambda:None
                j.state['decisions'][0]['mean_gain'] = 999
                with self.assertRaises(Invalid):verify(j)
                j.state = saved
