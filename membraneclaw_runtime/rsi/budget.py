"""Integer synthetic units are never represented as actual API dollars/tokens."""
from .contracts import Invalid, integer


class Exhausted(Exception):
    pass


def reserve(ledger, limits, bound, elapsed):
    integer(bound, 1)
    if elapsed >= limits["wall_seconds"]:raise Exhausted("wall_time")
    if ledger["calls"] >= limits["operations"]:raise Exhausted("operation_count")
    if ledger["spent"] + ledger["reserved"] + bound > limits["units"]:raise Exhausted("synthetic_units")
    ledger["calls"] += 1
    ledger["reserved"] += bound


def settle(ledger, bound, observed):
    # No release until observed usage has been validated. Unknown stays reserved.
    integer(observed)
    if ledger["reserved"] < bound:raise Invalid("reservation underflow")
    ledger["reserved"] -= bound
    ledger["spent"] += observed
    return observed > bound
