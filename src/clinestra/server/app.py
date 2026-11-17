"""
The Clinical API: a FastAPI app generated from the concept registry.

    from clinestra.server import create_app
    app = create_app()          # reads .env, CLINESTRA_DATABASE_URL or CLINESTRA_DB, CLINESTRA_PACKS, auth from env

or run ``clinestra serve --dev``. Interactive docs are served at ``/docs``.

Every concept gets the same five routes::

    GET    /v1/{concept}                    list (filters: patient_id, status, encounter_id ...)
    POST   /v1/{concept}                    create (optionally from a definition_key)
    GET    /v1/{concept}/{id}               read
    PATCH  /v1/{concept}/{id}               update (refused in frozen states)
    POST   /v1/{concept}/{id}/transition    move state (202 + Approval when the state is gated)
    DELETE /v1/{concept}/{id}?reason=...    soft delete (where the concept allows it)

plus behaviour routes (proposals, approvals, journeys, profiles,
documents, conversations, routines) and the AI tool endpoints under ``/v1/tools``.
"""
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field

from .. import __version__
from ..base import Actor
from ..concepts.definitions import DEF_CONFIGS
from ..errors import ClinestraError
from ..harness import Harness
from ..spec import SPECS, ConceptSpec
from ..settings import database_url, load_env
from ..storage import store_from_url
from .accounts import UserSessionAuth, account_site, ensure_bootstrap, mount_accounts
from .agentic.tools import registry
from .auth import Authenticator, from_env

__all__ = ["create_app"]

GROUPS = {
    "People": "Patients (and family members), providers, care team members, care teams, clinical settings.",
    "How they are": "Observations, conditions, medications, clinical states, outcomes, health profiles.",
    "What can be done": "Cohort memberships, protocol assignments.",
    "What is being done": "Careplans, care actions, tasks, routines, regimens, journeys.",
    "Interactions": "Encounters, clinical records, note pages, assessments, documents, patient journals, conversations.",
    "Governance": "Consents, approvals, proposals and definitions (the configurable templates).",
}
TAGS = [{"name": "system", "description": "Health, identity, packs, calculators, timeline."}] + \
       [{"name": k, "description": v} for k, v in GROUPS.items()] + \
       [{"name": "AI harness", "description": "Context-scoped tools for agents over the Clinical API, plus harness tools."}]


class TransitionIn(BaseModel):
    to: str
    reason: Optional[str] = None


class Reason(BaseModel):
    reason: str = Field(..., min_length=3)


class DecisionIn(BaseModel):
    approve: bool
    note: Optional[str] = None


class AcceptIn(BaseModel):
    edits: Dict[str, Any] = Field(default_factory=dict, description="Amend the proposed data before accepting")


class MoveIn(BaseModel):
    to: str
    reason: Optional[str] = None
    force: bool = Field(False, description="Skip the allowed transitions and requirements (staff, reason required)")


class TurnIn(BaseModel):
    content: str


class AddendumIn(BaseModel):
    text: str = Field(..., min_length=2)


class CalcIn(BaseModel):
    inputs: Dict[str, Any]
    reference_rows: List[Dict[str, Any]] = Field(default_factory=list)
    config: Dict[str, Any] = Field(default_factory=dict)


class ToolCallIn(BaseModel):
    name: str
    args: Dict[str, Any] = Field(default_factory=dict)
    scope: str = "patient"
    patient_id: Optional[str] = None
    encounter_id: Optional[str] = None


class TickIn(BaseModel):
    duty_states: Optional[List[str]] = None


def _harness_from_env() -> Harness:
    load_env()
    packs = [p for p in os.environ.get("CLINESTRA_PACKS", "core").split(",") if p]
    return Harness(store=store_from_url(database_url()), packs=packs)


def _concept_routes(app: FastAPI, h: Harness, spec: ConceptSpec, A):
    k, path, tag = spec.kind, f"/v1/{spec.plural}", spec.group
    Model, Create, Update = spec.model, spec.api_create_model, spec.update_model
    Approval = SPECS["approval"].model
    label = k.replace("_", " ")

    @app.get(path, tags=[tag], operation_id=f"list_{k}", summary=f"List {label}s")
    def list_(actor: Actor = A, patient_id: Optional[str] = None, status: Optional[str] = None,
              encounter_id: Optional[str] = None, definition_key: Optional[str] = None,
              review_status: Optional[str] = None, include_deleted: bool = False,
              limit: Optional[int] = None, cursor: Optional[str] = None):
        where = {}
        if encounter_id and ("encounter_id" in Model.model_fields or k == "note_page"):
            where["encounter_id"] = encounter_id
        if definition_key:
            where["definition_key"] = definition_key
        rows = h.list(actor, k, patient_id, status, include_deleted, **where)
        if review_status and k == "definition":
            rows = [row for row in rows if getattr(row, "review_status", None) == review_status]
        if limit is None:
            return rows
        limit = min(max(limit, 1), 1000)
        start = 0
        if cursor:
            ids = [row.id for row in rows]
            start = ids.index(cursor) + 1 if cursor in ids else 0
        page = rows[start:start + limit]
        nxt = page[-1].id if len(rows) > start + limit else None
        return {"items": [row.model_dump(mode="json") for row in page], "next_cursor": nxt}

    if k not in ("approval",):
        @app.post(path, tags=[tag], operation_id=f"create_{k}", summary=f"Create a {label}", status_code=201,
                  description=spec.summary)
        def create(body: Create, actor: Actor = A) -> Model:
            return h.create(actor, k, body.model_dump(exclude_unset=True))

    @app.get(path + "/{id}", tags=[tag], operation_id=f"get_{k}", summary=f"Read a {label}")
    def get(id: str, actor: Actor = A) -> Model:
        return h.get(actor, k, id)

    if k not in ("approval", "proposal"):
        @app.patch(path + "/{id}", tags=[tag], operation_id=f"update_{k}", summary=f"Update a {label}")
        def update(id: str, body: Update, actor: Actor = A) -> Model:
            return h.update(actor, k, id, body)

    if spec.transitions:
        moves = "; ".join(f"{a} → {', '.join(b)}" for a, b in spec.transitions.items())
        gated = f" Gated (needs approval): {sorted(spec.approval_states)}." if spec.approval_states else ""

        @app.post(path + "/{id}/transition", tags=[tag], operation_id=f"transition_{k}",
                  summary=f"Move a {label} to another state", description=f"Allowed: {moves}.{gated}",
                  responses={202: {"model": Approval, "description": "The state is gated; an approval was requested"}})
        def transition(id: str, body: TransitionIn, response: Response, actor: Actor = A) -> Union[Model, Approval]:
            out = h.transition(actor, k, id, body.to, body.reason)
            if isinstance(out, Approval) and k != "approval":
                response.status_code = 202
            return out

    if spec.deletable:
        @app.delete(path + "/{id}", tags=[tag], operation_id=f"delete_{k}", summary=f"Soft-delete a {label}")
        def delete(id: str, reason: str = Query(..., min_length=3), actor: Actor = A) -> Model:
            return h.delete(actor, k, id, reason)


def create_app(harness: Optional[Harness] = None, auth: Optional[Authenticator] = None,
               cors_origins: Optional[List[str]] = None) -> FastAPI:
    """Build the app. ``cors_origins`` lists browser origins allowed to call the API; defaults to ``CLINESTRA_CORS_ORIGINS``."""
    h = harness or _harness_from_env()
    if cors_origins is None:
        cors_origins = [o.strip() for o in os.environ.get("CLINESTRA_CORS_ORIGINS", "").split(",") if o.strip()]
    auth = auth or from_env()
    if auth is None:
        raise RuntimeError("No authentication configured. Pass auth=..., set CLINESTRA_API_KEYS=/path/keys.json, "
                           "or for local development set CLINESTRA_DEV_AUTH=1 (or run `clinestra serve --dev`).")

    ensure_bootstrap(h.store, account_site(auth))
    auth = UserSessionAuth(auth, h.store)
    ai = _ai_extension(h, auth)
    if ai:
        auth = ai.PortalSessionAuth(auth, h.store)
    app = FastAPI(title="Clinestra Clinical API", version=__version__, openapi_tags=TAGS,
                  description="Every concept of the longitudinal-care structure with read, create, update, transition "
                              "and delete, and the rules that govern them. Writes are attributed to the authenticated "
                              "actor and recorded in a hash-chained timeline. Clinical effects of automatic reactions "
                              "and AI agents become proposals that a clinician accepts.")
    app.state.harness = h
    if cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_credentials=False,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                           allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Actor-Id", "X-Actor-Role",
                                          "X-Site", "X-Actor-Type", "X-Patient-Id", "X-Represents"])
    bearer = HTTPBearer(auto_error=False, description="API key from your keys file. Click Authorize in /docs and paste it.")

    @app.exception_handler(ClinestraError)
    async def _err(request: Request, exc: ClinestraError):
        body = {"error": type(exc).__name__, "detail": str(exc),
                "request_id": request.headers.get("x-request-id") or uuid.uuid4().hex[:12]}
        fields = getattr(exc, "fields", None)
        if fields:
            body["fields"] = fields
        return JSONResponse(status_code=exc.status_code, content=body)

    def me(request: Request, _key=Security(bearer)) -> Actor:
        return auth(request)

    A = Depends(me)
    mount_accounts(app, h.store, A)

    # ── system ──
    @app.get("/v1/health", tags=["system"])
    def health() -> dict:
        return {"status": "ok", "version": __version__, "packs": sorted(h.packs),
                "mode": os.environ.get("CLINESTRA_MODE", "pilot"),
                "unreviewed_content": os.environ.get("CLINESTRA_ALLOW_UNREVIEWED_PACKS") == "1", "llm": bool(h.llm), "evidence": bool(h.evidence_provider),
                "patient_graph": os.environ.get("CLINESTRA_PATIENT_GRAPH", "off")}

    @app.get("/v1/me", tags=["system"])
    def whoami(actor: Actor = A) -> Actor:
        return actor

    @app.get("/v1/packs", tags=["system"])
    def packs(actor: Actor = A) -> List[dict]:
        out = []
        for pack in h.packs.values():
            summary = pack.summary()
            counts: Dict[str, int] = {}
            for doc in h.store.list("definition", actor.site, None, pack_id=pack.pack_id):
                status = doc.get("review_status") or "draft"
                counts[status] = counts.get(status, 0) + 1
            summary["review_summary"] = counts
            out.append(summary)
        return out

    class ReviewIn(BaseModel):
        decision: str
        notes: Optional[str] = None
        review_ref: Optional[str] = None

    @app.post("/v1/definitions/{id}/review", tags=["Governance"], summary="Record a clinical review decision")
    def review_definition(id: str, body: ReviewIn, actor: Actor = A):
        return h.review_definition(actor, id, body.decision, body.notes, body.review_ref)

    @app.get("/v1/runs", tags=["system"], summary="Calculator, assessment and rule run records")
    def runs(actor: Actor = A, patient_id: Optional[str] = None, kind: Optional[str] = None,
             definition_key: Optional[str] = None) -> List[dict]:
        rows = h.store.list("run", actor.site, patient_id)
        if kind:
            rows = [row for row in rows if row.get("kind") == kind]
        if definition_key:
            rows = [row for row in rows if row.get("definition_key") == definition_key]
        return rows

    @app.get("/v1/runs/{usage_id}", tags=["system"])
    def run_one(usage_id: str, actor: Actor = A) -> dict:
        doc = h.store.get("run", usage_id, actor.site)
        if not doc:
            raise HTTPException(404, "No run with that id")
        return doc

    @app.get("/v1/concepts", tags=["system"], summary="The concept registry: states, transitions, rules")
    def concepts(actor: Actor = A) -> List[dict]:
        return [{"kind": s.kind, "path": f"/v1/{s.plural}", "group": s.group, "summary": s.summary, "scope": s.scope,
                 "states": list(s.states), "initial": s.initial, "transitions": {a: list(b) for a, b in s.transitions.items()},
                 "clinical": s.clinical if isinstance(s.clinical, bool) else "depends on record",
                 "clinical_to": sorted(s.clinical_to), "frozen_states": sorted(s.frozen_states),
                 "approval_states": sorted(s.approval_states), "definition_kind": s.definition_kind,
                 "write_roles": sorted(s.write_roles), "deletable": s.deletable} for s in SPECS.values()]

    @app.get("/v1/definition-kinds", tags=["Governance"], summary="Definition kinds and their config schemas")
    def def_kinds(actor: Actor = A) -> Dict[str, dict]:
        return {k: v.model_json_schema() for k, v in DEF_CONFIGS.items()}

    @app.get("/v1/calculators/{kernel}", tags=["system"], include_in_schema=False)
    def _calc_get(kernel: str):
        return JSONResponse(status_code=405, content={"detail": "POST inputs to this calculator"})

    @app.post("/v1/calculators/{kernel}", tags=["system"], summary="Run a calculator kernel")
    def calc(kernel: str, body: CalcIn, actor: Actor = A) -> dict:
        return h.run_calculator(actor, kernel, body.inputs, body.reference_rows, body.config)

    @app.get("/v1/patients/{patient_id}/timeline", tags=["system"], summary="Hash-chained audit trail")
    def timeline(patient_id: str, actor: Actor = A) -> List[dict]:
        return h.timeline(actor, patient_id)

    @app.get("/v1/patients/{patient_id}/timeline/verify", tags=["system"])
    def verify(patient_id: str, actor: Actor = A) -> dict:
        return h.verify_timeline(actor, patient_id)

    # ── generic concept routes ──
    for spec in SPECS.values():
        _concept_routes(app, h, spec, A)

    P, Ap = SPECS["proposal"].model, SPECS["approval"].model

    # ── behaviour routes ──
    @app.post("/v1/proposals/{id}/accept", tags=["Governance"], summary="Accept a proposal (clinician)")
    def accept(id: str, body: AcceptIn, actor: Actor = A) -> P:
        return h.accept_proposal(actor, id, body.edits)

    @app.post("/v1/proposals/{id}/reject", tags=["Governance"])
    def reject(id: str, body: Reason, actor: Actor = A) -> P:
        return h.reject_proposal(actor, id, body.reason)

    @app.post("/v1/approvals/{id}/decide", tags=["Governance"], summary="Approve or reject (not your own request)")
    def decide(id: str, body: DecisionIn, actor: Actor = A) -> Ap:
        return h.decide_approval(actor, id, body.approve, body.note)

    @app.get("/v1/patients/{patient_id}/summary", tags=["How they are"], summary="Profiles (present/absent) and pending actions")
    def summary(patient_id: str, actor: Actor = A) -> dict:
        return h.patient_summary(actor, patient_id)

    @app.get("/v1/patients/{patient_id}/facts", tags=["How they are"], summary="Facts used by cohort rules and criteria")
    def facts(patient_id: str, actor: Actor = A) -> dict:
        return dict(h.patient_facts(actor, patient_id))

    @app.get("/v1/patients/{patient_id}/pending-actions", tags=["What can be done"])
    def pending(patient_id: str, actor: Actor = A) -> List[dict]:
        return h.pending_actions(actor, patient_id)

    @app.post("/v1/patients/{patient_id}/suggest-cohorts", tags=["What can be done"],
              summary="Propose memberships for rule-based cohorts the patient matches")
    def suggest(patient_id: str, actor: Actor = A) -> List[P]:
        return h.suggest_cohorts(actor, patient_id)

    @app.get("/v1/health-profiles/{id}/view", tags=["How they are"], summary="What is present and absent")
    def profile_view(id: str, actor: Actor = A) -> dict:
        return h.profile_view(actor, id)

    @app.get("/v1/outcomes/{id}/evaluate", tags=["How they are"], summary="Trajectory of the outcome's measure")
    def evaluate(id: str, actor: Actor = A) -> dict:
        return h.evaluate_outcome(actor, id)

    @app.get("/v1/care-actions/{id}/schedule", tags=["What is being done"])
    def schedule(id: str, actor: Actor = A) -> dict:
        return h.schedule_status(actor, id)

    @app.post("/v1/journeys/{id}/move", tags=["What is being done"], summary="Move to another journey state")
    def move(id: str, body: MoveIn, actor: Actor = A) -> SPECS["journey"].model:
        return h.move_journey(actor, id, body.to, body.reason, force=body.force)

    # ── workflows: encounter journeys on a board (the 2.0 encounter workflow / Kanban) ──
    def _stage(cfg: dict, state: str) -> dict:
        st = (cfg.get("stages") or {}).get(state) or {}
        return {"id": state, "label": st.get("label") or state.replace("_", " ").capitalize(),
                "category": st.get("category") or ("done" if state in cfg.get("terminal", []) else "in_progress"),
                "color": st.get("color"), "description": st.get("description"), "sla_minutes": st.get("sla_minutes"),
                "gates": (cfg.get("gates") or {}).get(state), "terminal": state in cfg.get("terminal", []),
                "next": (cfg.get("transitions") or {}).get(state)}

    def _current(actor: Actor, def_kind: str) -> list:
        """The definition in effect per key: the site's own over the library's, then the highest version."""
        best: Dict[str, Any] = {}
        for d in h.list(actor, "definition", def_kind=def_kind, status="published"):
            cur = best.get(d.key)
            if cur is None or ((d.site != "*", d.version) > (cur.site != "*", cur.version)):
                best[d.key] = d
        return list(best.values())

    @app.get("/v1/workflows", tags=["What is being done"], summary="Encounter workflows (journeys tied to an encounter)")
    def workflows(actor: Actor = A) -> List[dict]:
        out = []
        for d in _current(actor, "journey"):
            cfg = d.config or {}
            if cfg.get("association_type") != "encounter":
                continue
            out.append({"key": d.key, "name": d.name, "description": d.description, "initial": cfg.get("initial"),
                        "stages": [_stage(cfg, s) for s in cfg.get("states", [])], "advance_on": cfg.get("advance_on") or [],
                        "requires": cfg.get("requires") or {},
                        "encounter_types": [e.key for e in _current(actor, "encounter") if (e.config or {}).get("workflow") == d.key]})
        return out

    @app.get("/v1/workflows/{key}/board", tags=["What is being done"], summary="Board: every encounter on this workflow, by stage")
    def workflow_board(key: str, actor: Actor = A, done_days: int = 7) -> dict:
        defs = [d for d in _current(actor, "journey") if d.key == key]
        if not defs:
            raise HTTPException(404, f"No workflow '{key}'")
        cfg = defs[0].config or {}
        now = datetime.now(timezone.utc)
        cards = []
        patients: Dict[str, Any] = {}
        for j in h.list(actor, "journey", definition_key=key):
            jd = j.model_dump(mode="json")
            if jd.get("association_type") != "encounter" or jd["status"] == "cancelled":
                continue
            entered = (jd.get("history") or [{}])[-1].get("at") or jd.get("created_at")
            at = datetime.fromisoformat(str(entered).replace("Z", "+00:00")) if entered else now
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            if jd["status"] == "completed" and (now - at).days > done_days:
                continue
            pid = jd["patient_id"]
            if pid not in patients:
                try:
                    patients[pid] = h.get(actor, "patient", pid).model_dump(mode="json")
                except Exception:  # noqa: BLE001 - a patient the actor cannot read is left off the board
                    patients[pid] = None
            pat = patients[pid]
            if not pat:
                continue
            enc = h.store.get("encounter", jd.get("association_id"), actor.site) or {}
            cards.append({"journey_id": jd["id"], "state": jd.get("state"), "status": jd["status"], "entered_at": entered,
                          "minutes_in_stage": int((now - at).total_seconds() // 60),
                          "patient": {"id": pid, "name": pat.get("name"), "age": pat.get("age"), "gender": pat.get("gender"),
                                      "employee_no": (pat.get("custom_fields") or {}).get("employee_no")},
                          "encounter": {"id": enc.get("id"), "type": enc.get("encounter_type"), "reason": enc.get("reason"),
                                        "status": enc.get("status"), "created_at": enc.get("created_at")},
                          "unmet": h.workflow_unmet(actor.site, jd, jd.get("state")) if jd["status"] == "active" else [],
                          "history": jd.get("history") or []})
        return {"key": key, "name": defs[0].name, "stages": [_stage(cfg, s) for s in cfg.get("states", [])], "cards": cards}

    @app.post("/v1/note-pages/{id}/addenda", tags=["Interactions"], summary="Add to a finalized page")
    def addendum(id: str, body: AddendumIn, actor: Actor = A) -> SPECS["note_page"].model:
        return h.add_addendum(actor, id, body.text)

    @app.post("/v1/clinical-documents/{id}/share", tags=["Interactions"], summary="Release (needs data_sharing consent)")
    def share(id: str, actor: Actor = A) -> dict:
        return h.share_document(actor, id)

    @app.post("/v1/conversations/{id}/turns", tags=["Interactions"])
    def turn(id: str, body: TurnIn, actor: Actor = A) -> SPECS["conversation"].model:
        return h.add_turn(actor, id, body.content)

    @app.post("/v1/routines/run", tags=["What is being done"], summary="Create tasks for due routine occurrences")
    def routines(body: TickIn, actor: Actor = A) -> List[SPECS["task"].model]:
        return h.run_routines(actor, duty_states=body.duty_states)

    @app.post("/v1/tick", tags=["system"], summary="Housekeeping: routines, expiries, overdue outcomes")
    def tick(actor: Actor = A) -> dict:
        return h.tick(actor)

    # ── AI harness ──
    @app.get("/v1/tools", tags=["AI harness"], summary="Tool definitions for a context (pin patient/encounter)")
    def tools(actor: Actor = A, scope: str = "patient", patient_id: Optional[str] = None,
              encounter_id: Optional[str] = None) -> List[dict]:
        return registry(h).definitions({"scope": scope, "patient_id": patient_id, "encounter_id": encounter_id}, actor)

    @app.post("/v1/tools/call", tags=["AI harness"], summary="Call a tool as the authenticated actor")
    def call(body: ToolCallIn, actor: Actor = A) -> Any:
        return registry(h).call(actor, body.name, body.args, {"scope": body.scope, "patient_id": body.patient_id,
                                                           "encounter_id": body.encounter_id})

    from .emr_routes import mount_emr
    mount_emr(app, h, auth)  # the Clinestra 2.0 EMR canvas API (/v1/emr)
    from .consents import mount_consents
    mount_consents(app, h, auth)  # consent forms: templates, signing links, signed documents, FHIR Consent
    if ai:
        _mount_ai(app, h, auth, ai)
    return app


def _ai_extension(h: Harness, auth):
    """The optional AI, admin and portal routes (``pip install 'clinestra[ai]'``); off with ``CLINESTRA_AI=0``."""
    if os.environ.get("CLINESTRA_AI", "1") == "0":
        return None
    try:
        import cryptography  # noqa: F401
        from . import ai_routes
    except ImportError:
        return None
    return ai_routes


def _mount_ai(app: FastAPI, h: Harness, auth, ai) -> None:
    from .agentic.runtime import TenantLLM, current_site
    if h.llm is None:
        h.llm = TenantLLM(h.store, default_site=ai.default_site(auth))  # the tenant's own model, per site; falsy until one is configured

    @app.middleware("http")
    async def _site_context(request: Request, call_next):
        site = None
        if request.headers.get("authorization") or request.headers.get("x-api-key") or request.headers.get("x-actor-id"):
            try:
                site = auth(request).site
            except Exception:  # noqa: BLE001 - the route's own auth dependency reports the error
                site = None
        token = current_site.set(site)
        try:
            return await call_next(request)
        finally:
            current_site.reset(token)

    ai.mount_ai(app, h, auth)
