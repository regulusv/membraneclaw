import asyncio
import copy
import json
import tempfile
from pathlib import Path
from membraneclaw_runtime.rsi.cli import demo_spec
from membraneclaw_runtime.rsi.contracts import hashed
from membraneclaw_runtime.rsi.controller import Controller
from membraneclaw_runtime.rsi.journal import Journal
from membraneclaw_runtime.rsi.report import summary


def spec(rounds=2, operations=100, units=100):
    value = demo_spec()
    value['config']['max_rounds'] = rounds
    value['config']['limits'].update(operations=operations,units=units)
    value['config_digest'] = hashed(value['config'])
    value['inputs']['source_files']['configuration'] = json.dumps(value['config'],sort_keys=True)
    value['dataset_digest'] = hashed(value['inputs'])
    return value


def run(root, value=None, **kwargs):
    with Journal(root,spec=value or spec()) as journal:
        asyncio.run(Controller(journal,**kwargs).run())
        return summary(journal)
