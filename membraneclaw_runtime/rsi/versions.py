"""Separate asset identity, lineage identity and deterministic compilation."""
from pathlib import PurePosixPath
import unicodedata
from .contracts import CompiledAgent, Invalid, fields, hashed


def safe_path(path):
    if type(path) is not str or not path or "\\" in path or ":" in path or "\x00" in path:
        raise Invalid("unsafe asset path")
    p = PurePosixPath(path)
    if p.is_absolute() or str(p) != path or any(x in {"..", "."} or x.startswith('.') for x in p.parts):
        raise Invalid("unsafe asset path")
    if not (path == "prompt.txt" or (len(p.parts) == 2 and p.parts[0] == "skills" and p.suffix == ".md")):
        raise Invalid("only prompt.txt and skills/*.md text assets are permitted")
    return path


def validate_assets(assets, allowed):
    if type(assets) is not dict or set(assets) != set(allowed) or "prompt.txt" not in assets:
        raise Invalid("asset allowlist mismatch")
    names = []
    for path, text in assets.items():
        safe_path(path)
        if type(text) is not str:raise Invalid("asset must be UTF-8 text")
        text.encode("utf-8")
        names.append(unicodedata.normalize("NFC", path).casefold())
    if len(set(names)) != len(names):raise Invalid("case/Unicode path collision")


def version(assets, parent=None, candidate=None):
    asset_digest = hashed(assets)
    body = {"asset_digest":asset_digest, "assets":dict(sorted(assets.items())), "parent":parent, "candidate":candidate}
    return {**body, "version_id":hashed(body)}


def verify_version(value, allowed):
    fields(value, "asset_digest assets parent candidate version_id")
    validate_assets(value["assets"], allowed)
    if version(value["assets"], value["parent"], value["candidate"]) != value:
        raise Invalid("version digest mismatch")


def apply_candidate(parent, candidate, allowed, seen):
    if candidate["parent_version"] != parent["version_id"]:raise Invalid("stale candidate parent")
    assets = dict(parent["assets"])
    edited = set()
    for edit in candidate["edits"]:
        path = safe_path(edit["path"])
        if path not in allowed or path in edited:raise Invalid("forbidden or duplicate edit")
        if hashed(assets[path]) != edit["before"]:raise Invalid("edit preimage mismatch")
        assets[path] = edit["after"]
        edited.add(path)
    validate_assets(assets, allowed)
    if not edited or hashed(assets) in seen:raise Invalid("no-op or duplicate asset version")
    # Runtime operation IDs are deliberately excluded from lineage identity.
    semantic = {k:candidate[k] for k in ("parent_version", "edits", "rationale", "evidence_digest")}
    return version(assets, parent["version_id"], hashed(semantic))


def compile_agent(value):
    """The only asset-to-solver mapping; used by fixtures and future live adapters."""
    assets = value["assets"]
    return CompiledAgent(assets["prompt.txt"], tuple((p, assets[p]) for p in sorted(assets) if p != "prompt.txt"))
