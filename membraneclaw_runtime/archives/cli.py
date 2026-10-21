"""Publish/restore explicitly configured private snapshots; never rerun models."""
import argparse
import json
from dataclasses import asdict
from .git_archive import GitTraceArchive,load_snapshot
from ..tracing.file_store import FileTraceStore
from ..tracing.cli import resolve
from ..tracing.validation import seal,restore,validate


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=('publish','restore'),nargs='?',default='publish')
    p.add_argument('--trace-dir',default='./var/traces');p.add_argument('--episode')
    p.add_argument('--remote');p.add_argument('--branch');p.add_argument('--state-dir',default='./var/trace-archive')
    p.add_argument('--snapshot-dir');p.add_argument('--output')
    a=p.parse_args(argv)
    try:
        if a.command=='restore':
            if not a.snapshot_dir or not a.output:p.error('restore requires --snapshot-dir and --output')
            snapshot=load_snapshot(a.snapshot_dir);store=restore(snapshot,a.output)
            print(json.dumps(asdict(validate(store,snapshot.episode)),indent=2));return 0
        if not a.episode:p.error('publish requires --episode')
        store=FileTraceStore(a.trace_dir);episode=resolve(store,a.episode)
        receipt=GitTraceArchive(remote=a.remote,branch=a.branch,state_dir=a.state_dir).publish(seal(store,episode))
        print(json.dumps(asdict(receipt),indent=2));return 0 if receipt.status in {'published','not_configured'} else 1
    except Exception as error:
        print(json.dumps({'error':type(error).__name__}));return 1

if __name__=='__main__':raise SystemExit(main())
