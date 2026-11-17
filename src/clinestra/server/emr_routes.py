"""The Clinestra 2.0 EMR canvas API, on Clinestra 0.2 concepts.

The paths and response shapes are (``/clinical_notes/...``, ``/encounter_base/...`` and the
``emr_canvas`` socket), under ``/v1/emr``, so the 2.0 case-sheet UI runs unchanged. Underneath, a canvas page is a
``note_page`` of the encounter's clinical record and a canvas template is a ``note_page`` definition (see
``clinestra.emr.canvas``). Admin → Navigation & features → "Case sheet UI" picks which UI the console shows.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import secrets as _secrets
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from ..base import Actor, system_actor
from ..emr import canvas as cv
from ..emr.markdown import process_section_to_markdown
from ..errors import ClinestraError

__all__ = ["mount_emr"]
P = "/v1/emr"
TAG = ["EMR canvas"]


MAX_AUDIO_BYTES = 25 * 1024 * 1024


async def _read_limited(file: UploadFile) -> bytes:
    data = await file.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Audio is larger than 25 MB")
    return data


def mount_emr(app: FastAPI, h, auth) -> None:
    store = h.store
    actors: Dict[str, Actor] = {}            # page id -> last editor (writes are attributed to them)
    rooms: Dict[str, set] = {}               # page id -> open sockets
    tickets: Dict[str, tuple] = {}           # socket ticket -> (actor, expires)
    transcripts: Dict[str, List[tuple]] = {}  # recording id -> [(sequence, text)]
    pending: Dict[str, int] = {}             # recording id -> fragments still transcribing
    failures: Dict[str, str] = {}            # recording id -> why transcription failed

    def me(request: Request) -> Actor:
        return auth(request)

    def ok(**kw) -> dict:
        return {"status": "success", **kw}

    # ───────────────────────── storage ─────────────────────────
    def _definition(site: str, page: dict) -> Optional[dict]:
        return store.get("definition", page["definition_id"], site) if page.get("definition_id") else None

    def _known(defn: Optional[dict]) -> Optional[set]:
        keys = {b["key"] for b in ((defn or {}).get("config") or {}).get("blocks", [])}
        return keys or None

    def _sections(site: str, page: dict) -> List[dict]:
        """The page's canvas sections, seeded from its template on first open and refreshed from its blocks."""
        secs = page.get("page_clinical_data") or cv.sections_from_definition(_definition(site, page))
        return cv.overlay_blocks(secs, page.get("blocks") or {})

    def _save(actor: Actor, page_id: str, sections: List[dict]) -> None:
        page = store.get("note_page", page_id, actor.site)
        if not page or page.get("status") in ("approved", "finalized"):
            return
        blocks = cv.blocks_from_sections(sections, _known(_definition(actor.site, page)))
        try:
            h.update(actor, "note_page", page_id, {"page_clinical_data": sections, "blocks": blocks})
        except ClinestraError:
            h.update(actor, "note_page", page_id, {"page_clinical_data": sections})  # block validation is the template's

    def _flush(site: str, page_id: str, ops: List[dict]) -> None:
        actor = actors.get(page_id) or system_actor(site, "emr_canvas")
        page = store.get("note_page", page_id, site)
        if page:
            _save(actor, page_id, cv.apply_ops(_sections(site, page), ops))

    buf = cv.CanvasBuffer(_flush)

    def _page(actor: Actor, page_id: str) -> dict:
        buf.flush(page_id)
        h.get(actor, "note_page", page_id)  # authorizes
        return store.get("note_page", page_id, actor.site)

    def _encounter_pages(actor: Actor, encounter_id: str) -> List[dict]:
        enc = h.get(actor, "encounter", encounter_id)
        recs = [r.id for r in h.list(actor, "clinical_record", enc.patient_id) if r.encounter_id == encounter_id]
        pages = [p for p in h.list(actor, "note_page", enc.patient_id) if p.record_id in recs and p.page_type == "case_sheet"]
        return sorted(pages, key=lambda p: str(p.created_at))

    def _record_for(actor: Actor, encounter_id: str) -> str:
        enc = h.get(actor, "encounter", encounter_id)
        for r in h.list(actor, "clinical_record", enc.patient_id):
            if r.encounter_id == encounter_id and r.status != "finalized":
                return r.id
        return h.create(actor, "clinical_record", {"patient_id": enc.patient_id, "encounter_id": encounter_id,
                                                   "title": f"{enc.encounter_type} record"}).id

    def _templates(actor: Actor) -> List[dict]:
        return [d.model_dump(mode="json") for d in h.list(actor, "definition", def_kind="note_page", status="published")
                if (d.config or {}).get("page_type", "case_sheet") == "case_sheet"]

    def _new_page(actor: Actor, encounter_id: str, defn: Optional[dict], sections: Optional[List[dict]] = None, name: str = "Case sheet") -> dict:
        enc = h.get(actor, "encounter", encounter_id)
        rec = _record_for(actor, encounter_id)
        secs = sections if sections is not None else cv.sections_from_definition(defn)
        body = {"patient_id": enc.patient_id, "record_id": rec, "page_clinical_data": secs,
                "template_metadata": cv.template_metadata(defn, name), "title": (defn or {}).get("name") or name}
        if defn:
            body["definition_key"] = defn["key"]
        p = h.create(actor, "note_page", body)
        return {"page_id": p.id, "template_config": secs, "page_clinical_data": secs}

    def _status(page: dict) -> str:
        return "finalized" if page.get("status") in ("approved", "finalized") else "draft"

    def _page_meta(p) -> dict:
        d = p if isinstance(p, dict) else p.model_dump(mode="json")
        return {"page_id": d["id"], "template_metadata": {"name": (d.get("template_metadata") or {}).get("name") or d.get("title")},
                "status": _status(d), "meta_info": {"created_at": d.get("created_at"), "created_by": d.get("created_by"),
                                                    "updated_by": d.get("updated_by"), "updated_at": d.get("updated_at")}}

    def _patient_info(actor: Actor, pid: str) -> dict:
        p = h.get(actor, "patient", pid).model_dump(mode="json")
        return {"beneficiary_id": pid, "name": p.get("name", ""), "age": p.get("age", ""), "gender": p.get("gender", ""),
                "mobile": (p.get("demographics") or {}).get("phone", ""), "medical_record_number": p.get("mrn") or pid}

    def _provider_info(actor: Actor) -> dict:
        prov = next((x.model_dump(mode="json") for x in h.list(actor, "provider") if getattr(x, "user_id", None) == actor.id), {})
        return {"service_provider_id": actor.id, "name": prov.get("name") or actor.id, "specialization": prov.get("specialty"),
                "medicine_reg_id": prov.get("registration_no")}

    # ───────────────────────── encounter_base ─────────────────────────
    @app.get(f"{P}/encounter_base/get_encounter_by_id", tags=TAG)
    def get_encounter_by_id(request: Request, encounter_id: str, page_id: Optional[str] = None):
        actor = me(request)
        enc = h.get(actor, "encounter", encounter_id).model_dump(mode="json")
        pages = _encounter_pages(actor, encounter_id)
        page = _page(actor, page_id) if page_id else (_page(actor, pages[-1].id) if pages else None)
        data = {"encounter_id": encounter_id, "encounter_status": enc.get("status"), "encounter_type": enc.get("encounter_type"),
                "beneficiary_data": _patient_info(actor, enc["patient_id"]), "reason": enc.get("reason")}
        return ok(message="Encounter retrieved", data=data, page_id=page["id"] if page else None,
                  page_clinical_data=[s for s in _sections(actor.site, page) if not s.get("is_deleted")] if page else [],
                  template_metadata=(page or {}).get("template_metadata") or {}, page_status=_status(page) if page else None,
                  is_pages_exists=bool(pages), v4_emr_enabled=True, chart_options=[], tasks_data=[])

    @app.get(f"{P}/encounter_base/get_encounter_pages", tags=TAG)
    def get_encounter_pages(request: Request, encounter_id: str, beneficiary_id: Optional[str] = None, page_id: Optional[str] = None):
        actor = me(request)
        enc = h.get(actor, "encounter", encounter_id)
        if page_id:
            return {"status": "success", "pages": [_page_meta(_page(actor, page_id))]}
        return {"status": "success", "pages": [_page_meta(p) for p in _encounter_pages(actor, encounter_id)],
                "encounter_status": "completed" if enc.status in ("completed", "finalized") else "pending"}

    @app.get(f"{P}/encounter_base/get_encounter_charts", tags=TAG)
    def get_encounter_charts(request: Request, encounter_id: str, beneficiary_id: Optional[str] = None):
        me(request)
        return {"status": "success", "charts": []}

    @app.get(f"{P}/encounter_base/search_advice", tags=TAG)
    def search_advice(request: Request, search_type: str = "medicine", search_term: str = ""):
        """Medicine / investigation names for table cells: the tenant's regimens and the site's medication records."""
        actor = me(request)
        term = search_term.lower().replace("_", " ").strip()
        names = set()
        if search_type == "medicine":
            for d in h.list(actor, "definition", def_kind="regimen"):
                for s in re.findall(r'"(?:drug|drug_name|medication|medication_name|name)"\s*:\s*"([^"]{2,60})"', json.dumps(d.config)):
                    names.add(s)
            for doc in store.list("medication", actor.site, None):
                if doc.get("name"):
                    names.add(doc["name"])
        else:
            for doc in store.list("observation", actor.site, None):
                if doc.get("display"):
                    names.add(doc["display"])
        hits = sorted(n for n in names if term and term in n.lower())[:20]
        return {"status": "success", "data": [{"name": n, "label": n, "value": n} for n in hits]}

    # ───────────────────────── clinical_notes ─────────────────────────
    @app.get(f"{P}/clinical_notes/get_clinical_notes_by_page_id/", tags=TAG)
    def get_clinical_notes_by_page_id(request: Request, page_id: str):
        actor = me(request)
        page = _page(actor, page_id)
        secs = _sections(actor.site, page)
        md = []
        for s in secs:
            if s.get("is_deleted"):
                continue
            try:
                md.append({"section_id": s.get("section_id"), "section_name": s.get("section_name"), "section_type": s.get("section_type"),
                           "markdown": process_section_to_markdown(s)})
            except Exception:  # noqa: BLE001 - one bad section must not hide the page
                continue
        return ok(message="Clinical notes retrieved successfully", page_id=page_id, page_clinical_data=secs, summary="draft",
                  extracted_terms=[], sections_markdown=md, formatted_markdown="\n\n".join(x["markdown"] for x in md if x["markdown"]),
                  prescription_constants={}, beneficiary_info=_patient_info(actor, page["patient_id"]),
                  service_provider_info=_provider_info(actor), page_status=_status(page),
                  template_metadata=page.get("template_metadata") or cv.template_metadata(_definition(actor.site, page), page.get("title")),
                  finalized_at=page.get("updated_at") if _status(page) == "finalized" else None)

    @app.post(f"{P}/clinical_notes/save_draft_clinical_note_pages/", tags=TAG)
    def save_draft(request: Request, body: Dict[str, Any]):
        actor = me(request)
        actors[body["page_id"]] = actor
        _page(actor, body["page_id"])
        return ok(message="Draft saved successfully")

    @app.post(f"{P}/clinical_notes/update_clinical_note_pages/", tags=TAG)
    def update_pages(request: Request, body: Dict[str, Any]):
        actor = me(request)
        _page(actor, body["page_id"])
        _save(actor, body["page_id"], body.get("page_clinical_data") or [])
        return ok(message="Page updated")

    @app.post(f"{P}/clinical_notes/finalize_clinical_note/", tags=TAG)
    def finalize(request: Request, body: Dict[str, Any]):
        actor = me(request)
        page = _page(actor, body["page_id"])
        if not [s for s in _sections(actor.site, page) if not s.get("is_deleted")]:
            return {"status": "error", "message": "No valid (non-deleted) clinical data found in the page"}
        try:
            h.transition(actor, "note_page", page["id"], "finalized", "Locked from the case sheet")
        except ClinestraError as e:
            return {"status": "error", "message": str(e)}
        return ok(message="Page locked", data={"page_id": page["id"], "status": "finalized"})

    @app.get(f"{P}/clinical_notes/get_clinical_notes_templates/", tags=TAG)
    def get_templates(request: Request):
        actor = me(request)
        return ok(message="Templates retrieved successfully.", data=[
            {"template_id": d["key"], "unique_id": d["key"], "search_term": d["name"], "template_name": d["name"], "name": d["name"],
             "template_description": d.get("description") or "", "template_type": "clinical_note", "is_careplan_enabled": False}
            for d in _templates(actor)])

    @app.post(f"{P}/clinical_notes/get_clinical_notes_templates/", tags=TAG)
    def create_from_template(request: Request, body: Dict[str, Any]):
        actor = me(request)
        defn = next((d for d in _templates(actor) if d["key"] == body.get("template_id")), None)
        if body.get("template_id") and not defn:
            raise HTTPException(404, "Template not found")
        return ok(**_new_page(actor, body["encounter_id"], defn), is_careplan_enabled=False)

    @app.post(f"{P}/clinical_notes/add_page", tags=TAG)
    @app.post(f"{P}/clinical_notes/add_page/", tags=TAG, include_in_schema=False)
    def add_page(request: Request, body: Dict[str, Any]):
        actor = me(request)
        key = body.get("template_id") or body.get("page_template_id")
        defn = next((d for d in _templates(actor) if d["key"] == key), None) if key else None
        r = _new_page(actor, body["encounter_id"], defn, None if defn else body.get("page_clinical_data") or [], body.get("name") or "Case sheet")
        return ok(page_kind="case_sheet", **r)

    @app.post(f"{P}/clinical_notes/create_page_from_template/", tags=TAG)
    def create_page_from_template(request: Request, body: Dict[str, Any]):
        actor = me(request)
        src = _page(actor, body["template_page_id"]) if body.get("template_page_id") else None
        if not src:
            src = next((p for t in _protocol_templates(actor.site) for p in t["pages"] if p["page_id"] == body.get("template_page_id")), None)
        if not src:
            raise HTTPException(404, "Template page not found")
        secs = [{**s, "data": cv.section_from_definition(cv._section_def(s))["data"]} for s in (src.get("page_clinical_data") or []) if not s.get("is_deleted")]
        return ok(**_new_page(actor, body["encounter_id"], None, secs, (src.get("template_metadata") or {}).get("name") or "Case sheet"))

    # protocol templates (Save as template): a set of page layouts, kept per site
    def _protocol_templates(site: str) -> List[dict]:
        from .agentic.state import SiteState
        return SiteState(store, site).kv_get("emr_protocol_templates") or []

    @app.get(f"{P}/clinical_notes/emr_templating/", tags=TAG)
    def emr_templating(request: Request, encounter_id: Optional[str] = None, template_id: Optional[str] = None):
        actor = me(request)
        if encounter_id:
            pages = [_page_meta(p) for p in _encounter_pages(actor, encounter_id)]
            return ok(message="Retrieved EMR components for encounter", data={"pages": pages, "charts": [], "tasks": [],
                                                                               "counts": {"pages": len(pages), "charts": 0, "tasks": 0}})
        tpls = _protocol_templates(actor.site)
        if template_id:
            t = next((t for t in tpls if t["protocol_template_id"] == template_id), None)
            if not t:
                raise HTTPException(404, "Template not found")
            return {**t, "pages": [{**p, "include": True} for p in t["pages"]]}
        return ok(count=len(tpls), data=[{"protocol_template_id": t["protocol_template_id"], "template_id": t["protocol_template_id"],
                                          "unique_id": t["protocol_template_id"], "template_name": t["template_name"], "name": t["template_name"],
                                          "search_term": t["template_name"], "template_description": t.get("template_description", ""),
                                          "template_type": "protocol_template", "template_metadata": t["template_metadata"],
                                          "is_careplan_enabled": False, "components_count": {"pages": len(t["pages"]), "charts": 0, "tasks": 0},
                                          "meta_info": t.get("meta_info", {})} for t in tpls])

    @app.post(f"{P}/clinical_notes/emr_templating/", tags=TAG)
    def save_template(request: Request, body: Dict[str, Any]):
        from .agentic.state import SiteState
        actor = me(request)
        if not body.get("template_name"):
            return {"status": "error", "message": "Missing required field: template_name"}
        pages = []
        for item in body.get("pages") or []:
            if item.get("include", True) and item.get("page_id"):
                p = _page(actor, item["page_id"])
                pages.append({"page_id": p["id"], "template_metadata": p.get("template_metadata") or {"name": p.get("title")},
                              "page_clinical_data": [s for s in _sections(actor.site, p) if not s.get("is_deleted")]})
        tid = f"protocol_template|{_secrets.token_hex(8)}"
        md = body.get("template_metadata") or {}
        t = {"protocol_template_id": tid, "template_name": body["template_name"], "template_description": body.get("template_description", ""),
             "template_metadata": {**md, "name": body["template_name"], "type": md.get("type", "protocol_template"),
                                   "key": md.get("key") or re.sub(r"[^a-z0-9]+", "_", body["template_name"].lower())},
             "pages": pages, "charts": [], "tasks": [], "meta_info": {"created_by": actor.id, "created_at": time.time()}}
        st = SiteState(store, actor.site)
        st.kv_set("emr_protocol_templates", (st.kv_get("emr_protocol_templates") or []) + [t])
        return ok(message="Protocol template created successfully", data={"protocol_template_id": tid, "template_name": t["template_name"],
                                                                         "template_key": t["template_metadata"]["key"],
                                                                         "components_count": {"pages": len(pages), "charts": 0, "tasks": 0}})

    @app.post(f"{P}/clinical_notes/emr_templating/load_template/", tags=TAG, status_code=201)
    def load_template(request: Request, body: Dict[str, Any]):
        actor = me(request)
        t = next((t for t in _protocol_templates(actor.site) if t["protocol_template_id"] == body.get("template_id")), None)
        if not t:
            raise HTTPException(404, "Template not found")
        made = []
        for p in t["pages"]:
            secs = [{**s, "data": cv.section_from_definition(cv._section_def(s))["data"]} for s in p["page_clinical_data"]]
            made.append(_new_page(actor, body["encounter_id"], None, secs, (p.get("template_metadata") or {}).get("name") or "Case sheet")["page_id"])
        return ok(message="Template loaded", data={"pages": made, "charts": [], "tasks": []})

    # ───────────────────────── dictation (voice → sections) ─────────────────────────
    def _openai_key(site: str) -> str:
        from .agentic.state import SiteState
        from .portal import interview as iv
        return iv._api_key(SiteState(store, site))

    def _why(e: Exception) -> str:
        import httpx
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
            return "OpenAI refused the key. An administrator updates it in Admin → Realtime voice."
        if isinstance(e, LookupError):
            return str(e)
        return f"transcription failed ({type(e).__name__})"

    def _transcribe(site: str, data: bytes, name: str) -> str:
        import httpx
        ext = (name.rsplit(".", 1)[-1] if "." in name else "webm").lower()
        r = httpx.post("https://api.openai.com/v1/audio/transcriptions", headers={"Authorization": f"Bearer {_openai_key(site)}"},
                       data={"model": "gpt-4o-mini-transcribe", "response_format": "text", "temperature": "0"},
                       files={"file": (f"audio.{ext}", data, f"audio/{ext}")}, timeout=60)
        r.raise_for_status()
        return r.text.strip()

    @app.post(f"{P}/clinical_notes/process_audio_in_fragments/", tags=TAG)
    async def process_fragment(request: Request, file: UploadFile = File(...), page_id: str = Form(...), recording_id: str = Form(...),
                               sequence_id: str = Form("0")):
        actor = me(request)
        h.get(actor, "note_page", page_id)
        data = await _read_limited(file)
        pending[recording_id] = pending.get(recording_id, 0) + 1
        try:
            text = await asyncio.to_thread(_transcribe, actor.site, data, file.filename or "audio.webm")
        except Exception as e:  # noqa: BLE001 - a failed chunk is skipped, as in 2.0; the reason is reported at the end
            text = ""
            failures[recording_id] = _why(e)
        finally:
            pending[recording_id] -= 1
        transcripts.setdefault(recording_id, []).append((int(sequence_id or 0), text))
        return {"status": "success", "recording_id": recording_id, "response": {"transcript": text, "status": "completed"}}

    @app.post(f"{P}/clinical_notes/voice_to_clinical_notes_sse/", tags=TAG)
    async def voice_sse(request: Request, page_id: str = Form(...), recording_id: str = Form(...), file: Optional[UploadFile] = File(None)):
        actor = me(request)
        page = _page(actor, page_id)
        last = await _read_limited(file) if file else b""

        def send(d: dict) -> str:
            return f"data: {json.dumps(d)}\n\n"

        async def gen():
            yield send({"type": "streaming_start", "message": "Starting basic decision model analysis..."})
            for _ in range(120):  # wait for chunks still being transcribed
                if not pending.get(recording_id):
                    break
                await asyncio.sleep(0.5)
            text = " ".join(t for _, t in sorted(transcripts.pop(recording_id, []), key=lambda x: x[0]) if t)
            if last:
                try:
                    text += " " + await asyncio.to_thread(_transcribe, actor.site, last, file.filename or "audio.webm")
                except Exception:  # noqa: BLE001
                    pass
            if not text.strip():
                why = failures.pop(recording_id, None)
                yield send({"type": "error", "message": f"Error in Transcribing Audio: {why}" if why else "Error in Transcribing Audio"})
                return
            failures.pop(recording_id, None)
            yield send({"type": "event", "message": "Audio Successfully Transcribed", "transcription": text, "file_url": ""})
            try:
                cmds = await asyncio.to_thread(_emru, actor.site, page, text)
            except LookupError as e:
                yield send({"type": "error", "message": str(e)})
                return
            except Exception as e:  # noqa: BLE001 - a provider failure must reach the clinician, not end the stream silently
                why = _why(e) if "httpx" in type(e).__module__ else f"{type(e).__name__}: {e}"[:300]
                yield send({"type": "error", "message": f"The case-sheet model could not structure the dictation ({why}). "
                                                        "Nothing was changed; the transcript is kept below.", "transcription": text})
                return
            # Apply on the server too, so any case-sheet UI (and the page's blocks) gets the dictated values;
            # the canvas also applies them live and its socket writes the same result.
            buf.flush(page_id)
            fresh = store.get("note_page", page_id, actor.site)
            secs = _sections(actor.site, fresh)
            touched = [c.get("section_id") for c in cmds if cv.apply_emru(secs, c)]
            changed = len(touched)
            sections = sorted({s for s in touched if s})
            err = None
            if changed:
                try:
                    await asyncio.to_thread(_save, actor, page_id, secs)
                except ClinestraError as e:
                    err = str(e)
            for cmd in cmds:
                yield send(cmd)
            yield send({"type": "error", "message": err} if err else
                       {"type": "event", "message": "Sections updated", "applied": changed, "commands": len(cmds), "sections": sections})

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    def _emru(site: str, page: dict, transcription: str) -> List[dict]:
        from .agentic import runtime
        from .agentic.state import SiteState
        from ..emr.prompts import EMR_TRANSCRIPTION_PROMPT
        from ..emr.emru import emru_syntax_parser, template_to_text
        st = SiteState(store, site)
        m, p = runtime.resolve(st, "case_sheet")
        tpl = {"template_metadata": page.get("template_metadata") or {"name": page.get("title")},
               "sections": [s for s in _sections(site, page) if not s.get("is_deleted")]}
        prompt = EMR_TRANSCRIPTION_PROMPT.safe_substitute(template_reference=template_to_text(tpl))
        out = "".join(chunk for kind, chunk in runtime.stream_text(st, m, p, prompt, transcription) if kind == "text")
        return [c for c in (emru_syntax_parser(x) for x in out.split("~") if x.strip() and "NO_UPDATE_REQUIRED" not in x) if c]

    # ───────────────────────── live canvas socket ─────────────────────────
    @app.post(f"{P}/socket-ticket", tags=TAG, summary="Short-lived ticket for the canvas socket (browsers cannot send headers on a WebSocket)")
    def socket_ticket(request: Request):
        actor = me(request)
        now = time.time()
        for k in [k for k, (_, exp) in tickets.items() if exp < now]:
            tickets.pop(k, None)
        t = _secrets.token_urlsafe(24)
        tickets[t] = (actor, now + 600)
        return {"ticket": t, "expires_in": 600}

    @app.websocket(P + "/t/{ticket}/sockets/ws/emr_canvas/{site}/{user_id}/{encounter_id}/{page_id}")
    async def canvas_socket(ws: WebSocket, ticket: str, site: str, user_id: str, encounter_id: str, page_id: str):
        actor, exp = tickets.get(ticket, (None, 0))
        if not actor or exp < time.time():
            await ws.close(code=4401)
            return
        try:
            h.get(actor, "note_page", page_id)
        except ClinestraError:
            await ws.close(code=4403)
            return
        await ws.accept()
        room = rooms.setdefault(page_id, set())
        room.add(ws)
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("page_id") != page_id:
                    continue
                actors[page_id] = actor
                if msg.get("is_sequence_update"):
                    buf.flush(page_id)
                    page = store.get("note_page", page_id, actor.site)
                    secs = cv.reorder(_sections(actor.site, page), msg.get("sequence_content") or [])
                    await asyncio.to_thread(_save, actor, page_id, secs)
                    msg["reordered_data"] = [s for s in secs if not s.get("is_deleted")]
                    raw = json.dumps(msg)
                else:
                    buf.put(actor.site, page_id, msg)
                for peer in list(room):
                    try:
                        await peer.send_text(raw)
                    except Exception:  # noqa: BLE001 - a closed peer is dropped
                        room.discard(peer)
        except WebSocketDisconnect:
            pass
        finally:
            room.discard(ws)
            if not room:
                rooms.pop(page_id, None)
                await asyncio.to_thread(buf.flush, page_id)

    # stubs for 2.0 panels that have no 0.2 counterpart yet (uploads, protocol assets)
    @app.get(f"{P}/clinical_extraction/get_beneficiary_uploads/", tags=TAG, include_in_schema=False)
    def uploads(request: Request):
        me(request)
        return {"status": "success", "data": []}

    @app.get(P + "/protocols/{protocol_id}/addable_items", tags=TAG, include_in_schema=False)
    def addable(request: Request, protocol_id: str):
        me(request)
        return {"status": "success", "data": {"pages": [], "forms": []}}
