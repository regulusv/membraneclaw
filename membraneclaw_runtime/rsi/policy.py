"""Conservative demo policy, not a statistical claim about real models."""
from .contracts import hashed


def decide(parent_rows, candidate_rows):
    keys = lambda rows:[(r["task_id"],r["seed"]) for r in rows]
    if (not parent_rows or keys(parent_rows) != keys(candidate_rows) or len(set(keys(parent_rows))) != len(parent_rows)
        or any(r["status"] != "completed" or type(r["scale"]) is not int or r["scale"] != 1
               or type(r["score"]) is not int or not 0 <= r["score"] <= 1 for r in parent_rows+candidate_rows)):
        return {"outcome":"inconclusive", "reason":"incomplete_or_mismatched_evaluation"}
    differences = [c["score"]-p["score"] for p,c in zip(parent_rows,candidate_rows)]
    accepted = sum(differences) > 0 and min(differences) >= 0
    return {"outcome":"accepted" if accepted else "rejected",
            "reason":"positive_gain_without_regression" if accepted else "no_gain_or_case_regression",
            "differences":differences,"parent_mean":sum(r['score'] for r in parent_rows)/len(parent_rows),
            "candidate_mean":sum(r['score'] for r in candidate_rows)/len(candidate_rows),
            "mean_gain":sum(differences)/len(differences),
            "paired_cases":[{'task_id':p['task_id'],'seed':p['seed'],'parent_score':p['score'],'candidate_score':c['score']}
                            for p,c in zip(parent_rows,candidate_rows)],
            "parent_batch":hashed(parent_rows),"candidate_batch":hashed(candidate_rows)}
