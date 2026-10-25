"""Strict documents and immutable component requests for the offline controller."""
from dataclasses import asdict, dataclass
from typing import Protocol
from ..tracing.schema import canonical_json, digest


class Invalid(ValueError):
    pass


def normalize(value):
    if hasattr(value, "__dataclass_fields__"):value = asdict(value)
    if isinstance(value, (tuple,list)):return [normalize(v) for v in value]
    if isinstance(value,dict):return {k:normalize(v) for k,v in value.items()}
    return value


def hashed(value):
    return digest(canonical_json(normalize(value)))


def fields(value, required):
    if type(value) is not dict or set(value) != set(required.split()):
        raise Invalid("unexpected document fields")
    canonical_json(value)
    return value


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise Invalid("invalid integer")
    return value


@dataclass(frozen=True)
class TaskInput:
    id: str
    text: str


@dataclass(frozen=True)
class CompiledAgent:
    prompt: str
    skills: tuple[tuple[str, str], ...]
    compiler: str = "compile_agent_v1"

    @property
    def digest(self):
        return hashed(asdict(self))


@dataclass(frozen=True)
class SolveRequest:
    operation_id: str
    version_id: str
    agent: CompiledAgent
    task: TaskInput
    seed: int


@dataclass(frozen=True)
class ProposalRequest:
    operation_id: str
    parent_version: str
    assets: tuple[tuple[str, str], ...]
    development_evidence: str  # Canonical JSON snapshot; never validation/test answers.
    round: int


@dataclass(frozen=True)
class EvaluationRequest:
    operation_id: str
    version_id: str
    task: TaskInput
    seed: int
    answer: str
    solver_status: str


@dataclass(frozen=True)
class SolverResult:
    operation_id: str
    version_id: str
    task_id: str
    seed: int
    status: str
    answer: str
    units: int = 1


@dataclass(frozen=True)
class CaseEvaluation:
    operation_id: str
    version_id: str
    task_id: str
    seed: int
    status: str
    score: int
    scale: int = 1
    units: int = 1


@dataclass(frozen=True)
class Edit:
    path: str
    before: str
    after: str


@dataclass(frozen=True)
class Candidate:
    operation_id: str
    parent_version: str
    edits: tuple[Edit, ...]
    rationale: str
    evidence_digest: str
    units: int = 1


def document(value):
    # Use the same JSON types at every persistence boundary.
    import json
    return json.loads(canonical_json(normalize(value)))


def validate_result(kind, value, request):
    expected = {"solve": "operation_id version_id task_id seed status answer units",
                "evaluate": "operation_id version_id task_id seed status score scale units",
                "propose": "operation_id parent_version edits rationale evidence_digest units"}[kind]
    fields(value, expected)
    if value["operation_id"] != request.operation_id:
        raise Invalid("operation identity mismatch")
    integer(value["units"])
    if kind == "propose":
        if value["parent_version"] != request.parent_version or value["evidence_digest"] != hashed(request.development_evidence):
            raise Invalid("candidate provenance mismatch")
        if not isinstance(value["rationale"], str) or type(value["edits"]) is not list:
            raise Invalid("invalid candidate")
        for edit in value["edits"]:
            fields(edit, "path before after")
            if any(type(v) is not str for v in edit.values()):raise Invalid("invalid edit")
    else:
        if (value["version_id"], value["task_id"], value["seed"]) != (request.version_id, request.task.id, request.seed):
            raise Invalid("result identity mismatch")
        integer(value["seed"])
        if value["status"] not in {"completed", "failed", "incomplete"}:raise Invalid("invalid status")
        if kind == "solve" and type(value["answer"]) is not str:raise Invalid("invalid answer")
        if kind == "evaluate":
            if type(value["score"]) is not int or value["scale"] != 1 or type(value["scale"]) is not int or not 0 <= value["score"] <= 1:
                raise Invalid("invalid score or scale")
    return value


class Solver(Protocol):
    async def solve(self, request: SolveRequest) -> SolverResult: ...


class Proposer(Protocol):
    async def propose(self, request: ProposalRequest) -> Candidate: ...


class Evaluator(Protocol):
    async def evaluate(self, request: EvaluationRequest) -> CaseEvaluation: ...
