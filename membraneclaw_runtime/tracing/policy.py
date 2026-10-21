"""Reject known authentication data before persistence; never silently redact evidence."""
import json

from .schema import TraceError, canonical_json


class CredentialRejected(TraceError):
    pass


class CapturePolicy:
    """Transport owners must register actual credential VALUES, excluding signatures.

    This is defense in depth, not a general sensitive-data classifier. Prompts and
    provider signatures remain private business data. Never pass auth headers to
    the recorder. Unknown credential strings cannot be reliably detected.
    """
    AUTH_KEYS = {"authorization", "proxyauthorization", "apikey", "accesstoken",
                 "refreshtoken", "clientsecret", "password", "cookie", "setcookie"}

    def __init__(self, authentication_values=()):
        self._values = tuple(v.encode() if isinstance(v, str) else v for v in authentication_values if v)
        if any(not isinstance(v, bytes) for v in self._values):
            raise ValueError("authentication values must be strings/bytes")

    def check(self, value):
        raw = value if isinstance(value, bytes) else canonical_json(value)
        if any(secret in raw for secret in self._values):
            raise CredentialRejected("registered authentication value rejected")
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeError):
            return
        def walk(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    normalized = "".join(c for c in key.lower() if c.isalnum())
                    if normalized in self.AUTH_KEYS and child is not None:
                        raise CredentialRejected("authentication field rejected")
                    walk(child)
            elif isinstance(item, list):
                for child in item:
                    walk(child)
        walk(obj)
