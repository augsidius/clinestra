"""
Remote providers: the seam for a hosted, credit-metered provider API.

Everything a tenant needs runs inside one Clinestra install with the tenant's own model keys (see
``clinestra.ai``). Some capabilities are better served by a hosted service: evidence search,
managed models and managed voice. This module defines how the harness would consume such a service through the
interfaces it already has, configured by a single credit key:

    from clinestra.providers.remote import RemoteConfig, remote_providers
    cfg = RemoteConfig.from_env()            # CLINESTRA_REMOTE_URL + CLINESTRA_REMOTE_KEY
    h = Harness(store=..., **remote_providers(cfg))

Nothing here is implemented yet: every method raises :class:`RemoteNotConfigured` until a service exists. There is
deliberately no billing logic in the harness; metering and credits are the service's concern, the harness only sends
the key. The wire contract each method is expected to follow is documented on the method.

Contract (all JSON over HTTPS, ``Authorization: Bearer <credit key>``)::

    POST {url}/v1/evidence/search        {query, limit}                          -> [{title, source, grade, summary, url}]
    POST {url}/v1/llm/complete           {system, prompt, json_schema?}           -> {text} | {json}
    POST {url}/v1/voice/sessions         {instructions, tools, voice?}            -> {client_secret, expires_at, model}
    GET  {url}/v1/account                                                          -> {credits_remaining, plan}

A 402 from the service means the credit balance is exhausted; adapters should surface it as a clear error and let
the harness fall back to its deterministic tools.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

__all__ = ["RemoteConfig", "RemoteNotConfigured", "RemoteEvidenceProvider",
           "RemoteLLMProvider", "RemoteVoiceProvider", "remote_providers"]


class RemoteNotConfigured(RuntimeError):
    """The hosted provider API is not available in this install."""


@dataclass(frozen=True)
class RemoteConfig:
    url: str
    credit_key: str
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> Optional["RemoteConfig"]:
        url, key = os.environ.get("CLINESTRA_REMOTE_URL"), os.environ.get("CLINESTRA_REMOTE_KEY")
        return cls(url.rstrip("/"), key) if url and key else None


class _Remote:
    def __init__(self, cfg: RemoteConfig):
        self.cfg = cfg

    def _post(self, path: str, body: dict) -> Any:  # pragma: no cover - no service yet
        raise RemoteNotConfigured(f"The hosted provider API is not available yet ({path}).")


class RemoteEvidenceProvider(_Remote):
    """:class:`~clinestra.providers.EvidenceProvider` backed by a hosted evidence service."""

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        return self._post("/v1/evidence/search", {"query": query, "limit": limit})


class RemoteLLMProvider(_Remote):
    """:class:`~clinestra.providers.LLMProvider` on managed models (for tenants without their own keys)."""

    def complete(self, system: str, prompt: str, json_schema: Optional[Dict[str, Any]] = None) -> Any:
        return self._post("/v1/llm/complete", {"system": system, "prompt": prompt, "json_schema": json_schema})


class RemoteVoiceProvider(_Remote):
    """Managed realtime voice: mints a short-lived browser session, like the tenant's own OpenAI key does today."""

    def create_session(self, instructions: str, tools: List[dict], voice: Optional[str] = None) -> Dict[str, Any]:
        return self._post("/v1/voice/sessions", {"instructions": instructions, "tools": tools, "voice": voice})


def remote_providers(cfg: Optional[RemoteConfig] = None) -> Dict[str, Any]:
    """Keyword arguments for :class:`~clinestra.Harness`: empty when no remote is configured."""
    cfg = cfg or RemoteConfig.from_env()
    if not cfg:
        return {}
    return {"evidence_provider": RemoteEvidenceProvider(cfg)}
