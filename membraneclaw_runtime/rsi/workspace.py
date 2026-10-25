"""Disposable text workspaces, not an operating-system sandbox."""
import os
from pathlib import Path
from .contracts import Invalid
from .versions import validate_assets


def materialize(directory, value, allowed):
    validate_assets(value["assets"], allowed)
    directory = Path(directory).absolute()
    if any(p.is_symlink() for p in (directory, *directory.parents)):raise Invalid("symlink workspace")
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, text in value["assets"].items():
        path = directory/name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("xb") as f:
            os.chmod(path, 0o600)
            f.write(text.encode())
    verify(directory, value)
    return directory


def verify(directory, value):
    root = Path(directory)
    if root.is_symlink() or any(p.is_symlink() for p in root.parents):raise Invalid("symlink workspace")
    files = {}
    for p in root.rglob('*'):
        if p.is_symlink():raise Invalid("symlink asset")
        if p.is_file():
            if p.stat().st_mode & 0o111:raise Invalid("executable asset")
            files[p.relative_to(root).as_posix()] = p.read_bytes().decode("utf-8")
    if files != value["assets"]:raise Invalid("workspace differs from immutable version")
