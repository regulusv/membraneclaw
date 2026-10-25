"""Synthetic, deterministic adapters. No model/WaterTAP effectiveness claim."""
import json
from .contracts import Candidate, Edit, SolverResult, CaseEvaluation, hashed

IDENTITIES = {"solver":"arithmetic_fixture_v1", "proposer":"instruction_fixture_v1", "evaluator":"numeric_fixture_v1"}


class FixtureSolver:
    identity = IDENTITIES["solver"]
    offline_pure = True
    bound = 1

    async def solve(self, request):
        task = json.loads(request.task.text)
        value = task["quantity"]
        instructions = request.agent.prompt + '\n' + '\n'.join(text for _,text in request.agent.skills)
        if "[convert_mm]" in instructions and task["unit"] == "mm":value /= 1000
        if "[round_output]" in instructions:value = round(value,2)
        return SolverResult(request.operation_id,request.version_id,request.task.id,request.seed,
                            "completed", json.dumps({"value":value},allow_nan=False))


class FixtureProposer:
    identity = IDENTITIES["proposer"]
    offline_pure = True
    bound = 1

    async def propose(self, request):
        assets = dict(request.assets)
        instruction = "[convert_mm]" if request.round == 1 else "[round_output]"
        return Candidate(request.operation_id,request.parent_version,
            (Edit("prompt.txt",hashed(assets["prompt.txt"]),assets["prompt.txt"]+'\n'+instruction),),
            "Synthetic fixture instruction, not a learned improvement", hashed(request.development_evidence))


class FixtureEvaluator:
    identity = IDENTITIES["evaluator"]
    offline_pure = True
    bound = 1

    def __init__(self, references):self.references = references

    async def evaluate(self, request):
        ref = self.references[request.task.id]
        try:
            value = json.loads(request.answer)["value"]
            correct = type(value) in {float,int} and abs(value-ref["expected"]) <= ref["tolerance"]
        except (ValueError,KeyError,TypeError):correct = False
        status = request.solver_status
        return CaseEvaluation(request.operation_id,request.version_id,request.task.id,request.seed,
                              status,int(correct and status == "completed"))
