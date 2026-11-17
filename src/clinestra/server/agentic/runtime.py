"""
The tenant's own models: Claude clients built from each site's (decrypted) provider credentials, feature routing,
the two call shapes the console needs (one agent step with tools, a streamed text completion), and
:class:`TenantLLM`, the :class:`~clinestra.providers.LLMProvider` the harness tools use.

Platforms: any OpenAI-compatible endpoint (base URL + key; see :mod:`.openai_compat`), Anthropic API, Amazon Bedrock,
Google Vertex AI, Microsoft Foundry. The Claude platforms require ``pip install 'clinestra[ai]'``.
"""
from __future__ import annotations

import contextvars
import json
import re
from typing import Any, Dict, Iterator, Optional, Tuple

from ... import secrets as sec
from . import openai_compat as oai
from .state import SiteState

__all__ = ["current_site", "provider_doc", "client_for", "resolve", "agent_step", "stream_text", "test_provider", "TenantLLM"]

# Site of the request being served (set by the server per request; read by TenantLLM, which has no actor argument).
current_site: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("clinestra_current_site", default=None)

_clients: Dict[Tuple[str, str], Tuple[Any, Any]] = {}


def _anthropic():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise LookupError("Model providers need: pip install 'clinestra[ai]'") from exc
    return anthropic


def provider_doc(st: SiteState, pid: str) -> dict:
    p = st.get("_ai.provider", pid)
    if not p or p.get("_deleted"):
        raise LookupError(f"No provider {pid}")
    return p


def client_for(st: SiteState, p: dict):
    """Build (and cache until the provider is edited) the platform's dedicated Claude client."""
    key = (st.site, p["id"])
    cached = _clients.get(key)
    if cached and cached[0] == p["updated_at"]:
        return cached[1]
    cfg = p.get("config") or {}
    s = sec.decrypt(p["secret"]) if p.get("secret") else {}
    platform = p["platform"]
    if platform == "openai":
        c = oai.Client(cfg.get("base_url"), s.get("api_key") or None)
        _clients[key] = (p["updated_at"], c)
        return c
    a = _anthropic()
    if platform == "anthropic":
        c = a.Anthropic(api_key=s.get("api_key") or None)
    elif platform == "bedrock":
        c = a.AnthropicBedrockMantle(aws_region=cfg.get("aws_region"), aws_access_key=s.get("aws_access_key_id") or None,
                                     aws_secret_key=s.get("aws_secret_access_key") or None)
    elif platform == "vertex":
        creds = None
        if s.get("service_account_json"):
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(
                json.loads(s["service_account_json"]), scopes=["https://www.googleapis.com/auth/cloud-platform"])
        c = a.AnthropicVertex(project_id=cfg.get("project_id"), region=cfg.get("region") or "global", credentials=creds)
    elif platform == "foundry":
        c = a.AnthropicFoundry(resource=cfg.get("resource"), api_key=s.get("api_key"))
    else:
        raise ValueError(f"Unknown platform {platform}")
    _clients[key] = (p["updated_at"], c)
    return c


def model_id_for(platform: str, model_id: str) -> str:
    return f"anthropic.{model_id}" if platform == "bedrock" and not model_id.startswith("anthropic.") else model_id


def resolve(st: SiteState, feature: str, alias: Optional[str] = None) -> Tuple[dict, dict]:
    """Route a feature (or an explicit model alias, e.g. an agent's own model) to (model doc, provider doc)."""
    routes = st.kv_get("routes", {}) or {}
    alias = alias or routes.get(feature) or routes.get("agent")
    if not alias:
        raise LookupError("No model is routed for this feature. Configure Admin → Models & routing.")
    m = st.get("_ai.model", alias)
    if not m or m.get("_deleted") or not m.get("enabled"):
        raise LookupError(f"Model '{alias}' is missing or disabled.")
    p = provider_doc(st, m["provider_id"])
    if not p["enabled"]:
        raise LookupError(f"Provider '{p['name']}' is disabled.")
    return m, p


def _params(st: SiteState, m: dict, p: dict, system: str, messages: list, tools: Optional[list], stream_max: bool = False) -> dict:
    policy = st.kv_get("policy", {}) or {}
    params: Dict[str, Any] = {
        "model": model_id_for(p["platform"], m["model_id"]),
        "max_tokens": m.get("max_tokens") or (32000 if stream_max else 16000),
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": messages,
    }
    if m.get("effort") and not m["model_id"].startswith("claude-haiku"):
        params["output_config"] = {"effort": m["effort"]}
    if tools:
        params["tools"] = tools
    if policy.get("inference_geo") and p["platform"] == "anthropic":
        params["inference_geo"] = policy["inference_geo"]
    # Server-side refusal fallback: Claude API only, current Opus/Fable/Sonnet models.
    if policy.get("refusal_fallbacks", True) and p["platform"] == "anthropic" and m["model_id"] in (
            "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"):
        params["betas"] = ["server-side-fallback-2026-07-01"]
        params["fallbacks"] = "default"
    return params


def agent_step(st: SiteState, m: dict, p: dict, system: str, messages: list, tools: list):
    """One Messages API call. Returns the full response (its content is stored verbatim for the next turn)."""
    c = client_for(st, p)
    if p["platform"] == "openai":
        return c.create(m["model_id"], system, messages, tools, m.get("max_tokens") or 16000)
    params = _params(st, m, p, system, messages, tools)
    if "betas" in params:
        return c.beta.messages.create(**params)
    return c.messages.create(**params)


def stream_text(st: SiteState, m: dict, p: dict, system: str, prompt: str) -> Iterator[Tuple[str, Any]]:
    """Yields ("text", chunk) events and finally ("final", message)."""
    c = client_for(st, p)
    if p["platform"] == "openai":  # one-shot completion, delivered as a single chunk
        r = c.create(m["model_id"], system, [{"role": "user", "content": prompt}], None, m.get("max_tokens") or 32000)
        yield "text", "".join(b.text for b in r.content if b.type == "text")
        yield "final", r
        return
    params = _params(st, m, p, system, [{"role": "user", "content": prompt}], None, stream_max=True)
    cm = c.beta.messages.stream(**params) if "betas" in params else c.messages.stream(**params)
    with cm as stream:
        for text in stream.text_stream:
            yield "text", text
        yield "final", stream.get_final_message()


def test_provider(st: SiteState, p: dict, model_id: Optional[str] = None) -> dict:
    """Cheap connectivity check: a tiny completion on the given (or default) model."""
    if p["platform"] == "openai":
        try:
            return oai.test(client_for(st, p), model_id or (p.get("config") or {}).get("default_model") or "gpt-4o-mini")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:300]}
    a = _anthropic()
    try:
        c = client_for(st, p)
        r = c.messages.create(model=model_id_for(p["platform"], model_id or "claude-haiku-4-5"), max_tokens=16,
                              messages=[{"role": "user", "content": "Reply with the single word: ok"}])
        text = "".join(b.text for b in r.content if b.type == "text")
        return {"ok": True, "model": r.model, "reply": text.strip()[:40]}
    except a.AuthenticationError:
        return {"ok": False, "error": "Authentication failed: the key or credentials were rejected."}
    except a.PermissionDeniedError:
        return {"ok": False, "error": "The credentials do not have access to this model."}
    except a.NotFoundError:
        return {"ok": False, "error": "Model or endpoint not found on this platform/region."}
    except a.RateLimitError:
        return {"ok": False, "error": "Rate limited by the provider. The key works; try again shortly."}
    except a.APIStatusError as e:
        return {"ok": False, "error": f"Provider error {e.status_code}: {e.message}"}
    except a.APIConnectionError:
        return {"ok": False, "error": "Could not reach the provider (network)."}
    except Exception as e:  # noqa: BLE001 - surface credential/config problems (e.g. bad service account JSON) to the admin
        return {"ok": False, "error": str(e)[:300]}


class TenantLLM:
    """:class:`~clinestra.providers.LLMProvider` on the current site's own model (the one routed to ``agent``).

    Truthy only when that site has a routed, enabled model and AI is on, so ``/v1/health`` reports ``llm: true``
    and the harness tools use their deterministic fallbacks otherwise. ``default_site`` is used outside a request.
    """

    def __init__(self, store, default_site: Optional[str] = None, feature: str = "agent"):
        self.store, self.default_site, self.feature = store, default_site, feature

    def _state(self) -> Optional[SiteState]:
        site = current_site.get() or self.default_site
        return SiteState(self.store, site) if site else None

    def _resolved(self):
        st = self._state()
        if not st or not (st.kv_get("policy", {}) or {}).get("ai_enabled", True):
            return None
        try:
            return (st, *resolve(st, self.feature))
        except LookupError:
            return None

    def __bool__(self) -> bool:
        return self._resolved() is not None

    def complete(self, system: str, prompt: str, json_schema: Optional[Dict[str, Any]] = None) -> Any:
        r = self._resolved()
        if not r:
            raise LookupError("No model is configured for this site.")
        st, m, p = r
        if json_schema:
            prompt += ("\n\nRespond with only a JSON value (no prose, no code fence) matching this JSON schema:\n"
                       + json.dumps(json_schema)[:6000])
        resp = agent_step(st, m, p, system, [{"role": "user", "content": prompt}], [])
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()
        st.audit(actor="harness_tools", role="system", feature=f"llm:{self.feature}", model=m["model_id"], provider=p["name"],
                 status="ok", input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens)
        if not json_schema:
            return text
        found = re.search(r"[\[{].*[\]}]", text, re.S)
        try:
            return json.loads(found.group(0) if found else text)
        except ValueError:
            return {}
