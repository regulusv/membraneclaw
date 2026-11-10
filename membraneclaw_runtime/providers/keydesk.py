"""Bounded Key Desk Gemini bridge. Credentials remain on the Key Desk server.

The injected async caller invokes the authenticated ``gemini_call`` MCP tool.
This profile deliberately rejects tools, streams and multi-turn requests.
"""
import asyncio
import json
from dataclasses import replace
from uuid import uuid4

from .gemini import GeminiAdapter
from .json_utils import strict_json
from .types import Capabilities, ConfigurationError, NativeResponse, TransportError


def wire_body(prompt, tokens):
    # Match Key Desk's JSON.stringify field order and UTF-8 serialization exactly.
    value = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
             "generationConfig": {"maxOutputTokens": tokens, "candidateCount": 1}}
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class KeyDeskGeminiAdapter(GeminiAdapter):
    provider = "gemini_keydesk"
    adapter_version = "gemini_keydesk_v1"
    MODELS = {"gemini-3.5-flash-lite"}

    def capabilities(self, model):
        if model not in self.MODELS:
            raise ConfigurationError("model has no reviewed Key Desk profile")
        return Capabilities(tool_calling=False, parallel_calls=False)

    def prepare(self, request, state):
        if (request.tools or request.system_instruction or request.temperature is not None
                or request.provider_options or state.native_history or len(request.content) != 1):
            raise ConfigurationError("Key Desk profile supports one text turn without tools or options")
        prompt = request.content[0].text
        # JavaScript length counts UTF-16 units, not Python code points.
        if (not isinstance(prompt, str) or not prompt.strip()
                or len(prompt.encode('utf-16-le')) // 2 > 8000):
            raise ConfigurationError("Key Desk prompt exceeds its text limit")
        if type(request.max_output_tokens) is not int or not 1 <= request.max_output_tokens <= 1024:
            raise ConfigurationError("Key Desk requires explicit output limit in 1..1024")
        native = super().prepare(request, state)
        return replace(native, body=wire_body(prompt, request.max_output_tokens))


class KeyDeskTransport:
    authentication_values = ()  # Secret exists only in the remote vault.

    def __init__(self, credential_id, invoke, record_link):
        if not isinstance(credential_id, str) or not credential_id or not callable(invoke) or not callable(record_link):
            raise ConfigurationError("credential ID and authenticated async caller required")
        self.credential_id, self.invoke, self.record_link = credential_id, invoke, record_link
        self.request_ids = []

    async def exchange(self, native, *, before_send, after_receive, timeout):
        if (native.route.provider != 'gemini_keydesk' or native.method != 'POST'
                or native.path != f'/v1beta/models/{native.route.model}:generateContent'):
            raise ConfigurationError('unsupported Key Desk native request')
        body = strict_json(native.body)
        try:
            prompt = body['contents'][0]['parts'][0]['text']
            tokens = body['generationConfig']['maxOutputTokens']
            if native.body != wire_body(prompt, tokens):
                raise ValueError()
        except (KeyError, IndexError, TypeError, ValueError):
            raise ConfigurationError('Key Desk cannot represent this request exactly') from None
        request_id = uuid4().hex
        self.request_ids.append(request_id)
        await before_send(native)  # Durable before the caller can issue any network request.
        await self.record_link(request_id)  # Durable correlation even if the caller times out.
        try:
            record = await asyncio.wait_for(self.invoke({
                'credential_id': self.credential_id, 'request_id': request_id,
                'operation': 'generate', 'model': native.route.model,
                'prompt': prompt, 'max_output_tokens': tokens,
            }), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise TransportError('keydesk_call_outcome_unknown') from None
        if not isinstance(record, dict):
            raise TransportError('keydesk_invalid_record')
        if record.get('request_id') != request_id or record.get('credential_id') != self.credential_id:
            raise TransportError('keydesk_record_identity_mismatch')
        expected = {'url': 'https://generativelanguage.googleapis.com' + native.path,
                    'method': 'POST', 'body': body}
        if record.get('request') != expected:
            raise TransportError('keydesk_request_mismatch')
        result = record.get('result')
        if record.get('state') not in {'completed', 'failed'} or not isinstance(result, dict):
            raise TransportError('keydesk_remote_outcome_unknown')
        status, raw = result.get('http_status'), result.get('raw_response')
        if type(status) is not int or not 100 <= status <= 599 or not isinstance(raw, str):
            raise TransportError('keydesk_missing_raw_response')
        # Correlate without exporting a credential. Google HTTP headers are unavailable.
        response = NativeResponse(status, {'x-keydesk-request-id': request_id}, raw.encode('utf-8'))
        await after_receive(response)  # Raw body durable before Gemini normalization.
        return response


async def start_keydesk_gateway(store, credential_id, invoke, *, model="gemini-3.5-flash-lite", budget=None):
    """Start with retries disabled; an unknown remote request must be reconciled."""
    from ..gateway import ModelGateway, RetryPolicy
    adapter = KeyDeskGeminiAdapter()
    async def record_link(request_id):
        await gateway.record_event('keydesk.requested', data={
            'request_id': request_id, 'credential_id': credential_id,
            'capture_boundary': 'keydesk_utf8_response_body', 'upstream_headers_captured': False})
    transport = KeyDeskTransport(credential_id, invoke, record_link)
    gateway = await ModelGateway.start(store, adapter, transport, adapter.initial_state(model).route,
                                       retry=RetryPolicy(max_attempts=1), budget=budget)
    return gateway
