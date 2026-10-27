import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from membraneclaw_runtime.rsi.contracts import *
from membraneclaw_runtime.rsi.versions import *
from membraneclaw_runtime.rsi.workspace import materialize,verify
from membraneclaw_runtime.rsi.fixtures import FixtureSolver


class Versions(unittest.TestCase):
    def setUp(self):
        self.assets = {'prompt.txt':'base','skills/units.md':'skill'}
        self.allowed = list(self.assets)
        self.parent = version(self.assets)

    def candidate(self,path='prompt.txt',after='changed'):
        return {'parent_version':self.parent['version_id'],'edits':[{'path':path,'before':hashed(self.assets.get(path,'')),'after':after}],
                'rationale':'test','evidence_digest':hashed('dev'),'operation_id':'op','units':1}

    def test_assets_and_lineage_have_different_identities(self):
        other = version(self.assets,'different-parent','candidate')
        self.assertEqual(self.parent['asset_digest'],other['asset_digest'])
        self.assertNotEqual(self.parent['version_id'],other['version_id'])

    def test_rejects_noop_duplicate_and_stale_preimage(self):
        for candidate in (self.candidate(after='base'),{**self.candidate(),'parent_version':'stale'},
                          {**self.candidate(),'edits':[{'path':'prompt.txt','before':'wrong','after':'new'}]}):
            with self.assertRaises(Invalid):apply_candidate(self.parent,candidate,self.allowed,{self.parent['asset_digest']})
        child = apply_candidate(self.parent,self.candidate(),self.allowed,set())
        with self.assertRaises(Invalid):apply_candidate(self.parent,self.candidate(),self.allowed,{child['asset_digest']})

    def test_rejects_paths_and_case_collisions(self):
        for path in ('../prompt.txt','/tmp/prompt.txt','skills\\bad.md','.git/config','evaluator.py','skills/../../x','skills//x.md'):
            with self.subTest(path=path),self.assertRaises(Invalid):safe_path(path)
        assets = {'prompt.txt':'p','skills/A.md':'a','skills/a.md':'b'}
        with self.assertRaises(Invalid):validate_assets(assets,list(assets))

    def test_workspace_detects_symlink_executable_extra_and_mutated_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            for mode in ('symlink','executable','extra','changed'):
                path = materialize(root/mode,self.parent,self.allowed)
                if mode == 'symlink':
                    (path/'prompt.txt').unlink();(path/'prompt.txt').symlink_to(root/'outside')
                elif mode == 'executable':(path/'prompt.txt').chmod(0o700)
                elif mode == 'extra':(path/'extra.txt').write_text('extra')
                else:(path/'prompt.txt').write_text('changed')
                with self.subTest(mode=mode),self.assertRaises(Invalid):verify(path,self.parent)

    def test_compile_is_sorted_and_skills_change_actual_solver_input(self):
        value = version({'prompt.txt':'base','skills/units.md':'[convert_mm]'})
        compiled = compile_agent(value)
        req = SolveRequest('op',value['version_id'],compiled,TaskInput('t','{"quantity":1000,"unit":"mm"}'),0)
        result = asyncio.run(FixtureSolver().solve(req))
        self.assertIn('1.0',result.answer)
        self.assertEqual(compiled.digest,compile_agent(value).digest)

    def test_crlf_asset_bytes_survive_materialization_and_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            value = version({'prompt.txt':'line1\r\nline2\r\n'})
            path = materialize(Path(temp).resolve()/'work',value,['prompt.txt'])
            self.assertEqual((path/'prompt.txt').read_bytes(),b'line1\r\nline2\r\n')
            verify(path,value)
