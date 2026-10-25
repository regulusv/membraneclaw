"""Public projections and group/exact-duplicate separation checks."""
import json
from .contracts import Invalid, TaskInput, fields, hashed


def audit(tasks, references):
    if type(tasks) is not list or type(references) is not dict:raise Invalid("invalid dataset")
    ids, groups, texts = set(), {}, {}
    for task in tasks:
        fields(task, "id text split group source")
        if any(type(v) is not str or not v for v in task.values()):raise Invalid("invalid task fields")
        if task["split"] not in {"development", "validation", "test"}:raise Invalid("invalid split")
        if task["id"] in ids:raise Invalid("duplicate task ID")
        ids.add(task["id"])
        for table, key in [(groups,task["group"]),(texts,hashed(task["text"]))]:
            if key in table and table[key] != task["split"]:raise Invalid("cross-split task/group leakage")
            table[key] = task["split"]
        if task["id"] not in references:raise Invalid("missing private evaluator reference")
        # Fixtures use numeric comparisons; values must be finite JSON numbers.
        ref = references[task["id"]]
        fields(ref, "expected tolerance private_note")
        if type(ref["expected"]) not in {int,float} or type(ref["tolerance"]) not in {int,float} or ref["tolerance"] < 0:
            raise Invalid("invalid evaluator reference")
        json.loads(task["text"])
    if ids != set(references):raise Invalid("unmatched reference roster")
    for split in ("development", "validation", "test"):
        if not any(t["split"] == split for t in tasks):raise Invalid("missing split")


def public(task):
    return TaskInput(task["id"], task["text"])


def manifest(tasks):
    return [{"id":t["id"], "group":t["group"], "source":t["source"], "split":t["split"],
             "task_digest":hashed(t["text"])} for t in tasks]
