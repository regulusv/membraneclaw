"""Pin inputs/source bytes; resume uses snapshots, not mutable source files."""
import json
from pathlib import Path
from . import VERSION
from .contracts import Invalid, fields, hashed, integer
from .datasets import audit, manifest
from .fixtures import IDENTITIES
from .versions import validate_assets


def source_snapshot():
    root = Path(__file__).resolve().parents[1]
    return {p.relative_to(root).as_posix():p.read_bytes().decode("utf-8")
            for folder in (root/'rsi',root/'tracing') for p in sorted(folder.glob('*.py'))}


def verify_spec(spec):
    fields(spec,"schema mode purpose source inputs config config_digest dataset_digest components compiler parent")
    if (spec["schema"] != 1 or type(spec["schema"]) is not int or spec["mode"] != "synthetic_offline"
        or spec["purpose"] not in {"search","final_test"} or spec["components"] != IDENTITIES
        or spec["compiler"] != "compile_agent_v1"):
        raise Invalid("unsupported run/component/compiler version")
    if spec["config_digest"] != hashed(spec["config"]) or spec["dataset_digest"] != hashed(spec["inputs"]):
        raise Invalid("input snapshot digest mismatch")
    if spec["source"]["format"] != VERSION or spec["source"]["digest"] != hashed(spec["source"]["files"]):
        raise Invalid("source snapshot mismatch")
    cfg = spec["config"]
    fields(cfg,"schema mode max_rounds seeds allowed_assets limits policy assets tasks references")
    if cfg["schema"] != 1 or type(cfg["schema"]) is not int or cfg["mode"] != "synthetic_offline":raise Invalid("unsupported configuration")
    integer(cfg["max_rounds"],1)
    if type(cfg["seeds"]) is not list or not cfg["seeds"]:raise Invalid("seeds required")
    for seed in cfg["seeds"]:integer(seed)
    if len(set(cfg["seeds"])) != len(cfg["seeds"]):raise Invalid("duplicate seeds")
    if type(cfg["allowed_assets"]) is not list or len(set(cfg["allowed_assets"])) != len(cfg["allowed_assets"]):raise Invalid("invalid allowlist")
    fields(cfg["limits"],"operations units wall_seconds")
    for value in cfg["limits"].values():integer(value,1)
    if cfg["policy"] != "strict_fixture_v1":raise Invalid("unsupported decision policy")
    fields(spec["inputs"],"assets tasks references manifest source_files")
    raw = fields(spec['inputs']['source_files'],'configuration assets tasks references')
    if (json.loads(raw['configuration']) != cfg or raw['assets'] != spec['inputs']['assets']
        or json.loads(raw['tasks']) != spec['inputs']['tasks'] or json.loads(raw['references']) != spec['inputs']['references']):
        raise Invalid('raw input bytes differ from parsed inputs')
    validate_assets(spec["inputs"]["assets"],cfg["allowed_assets"])
    audit(spec["inputs"]["tasks"],spec["inputs"]["references"])
    if spec["inputs"]["manifest"] != manifest(spec["inputs"]["tasks"]):raise Invalid("dataset manifest mismatch")


def load_config(path):
    path = Path(path)
    if path.is_symlink():raise Invalid("symlink configuration")
    root = path.resolve().parent
    config_text = path.read_bytes().decode("utf-8")
    cfg = json.loads(config_text)
    fields(cfg,"schema mode max_rounds seeds allowed_assets limits policy assets tasks references")
    def read(relative):
        if type(relative) is not str or Path(relative).is_absolute() or '..' in Path(relative).parts or '\\' in relative:
            raise Invalid("unsafe input reference")
        target = root/relative
        if target.is_symlink() or any(p.is_symlink() for p in target.parents if p != root.parent):raise Invalid("symlink input")
        if not target.resolve().is_relative_to(root):raise Invalid("input escapes configuration root")
        if target.stat().st_mode & 0o111:raise Invalid("executable input asset")
        return target.read_bytes().decode("utf-8")
    assets = {name:read(location) for name,location in cfg["assets"].items()}
    task_text, reference_text = read(cfg['tasks']), read(cfg['references'])
    tasks, refs = json.loads(task_text), json.loads(reference_text)
    inputs = {"assets":assets,"tasks":tasks,"references":refs,"manifest":manifest(tasks),
              "source_files":{"configuration":config_text,"assets":assets,"tasks":task_text,"references":reference_text}}
    source = source_snapshot()
    spec = {"schema":1,"mode":"synthetic_offline","purpose":"search",
            "source":{"format":VERSION,"digest":hashed(source),"files":source},"inputs":inputs,"config":cfg,
            "config_digest":hashed(cfg),"dataset_digest":hashed(inputs),"components":dict(IDENTITIES),
            "compiler":"compile_agent_v1","parent":None}
    verify_spec(spec)
    return spec


def check_current_source(spec):
    verify_spec(spec)
    if source_snapshot() != spec["source"]["files"]:raise Invalid("runtime source changed; use the pinned implementation")
