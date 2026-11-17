"""Defaults for a site that has not configured AI yet: features, platforms, model catalog, policy, prompts, agents.

Everything here is generic. A tenant's own wording (its organisation, its programs, its agents) is configuration,
saved through the admin API (``/v1/admin/...``), never code.
"""

FEATURES = {
    "agent": "Agents: personal, care team and patient (chat and tool use)",
    "synopsis": "Clinical synopsis, daily briefing and encounter summary cards",
    "case_sheet": "Case sheet drafting, autofill and review",
    "education": "Patient education drafts",
    "insights": "Insight and risk analysis",
}

PLATFORMS = {
    # The standard: any endpoint speaking the OpenAI Chat Completions API (OpenAI, Azure OpenAI, OpenRouter, Groq,
    # Together, DeepSeek, Mistral, vLLM, Ollama, LiteLLM ...). Base URL defaults to https://api.openai.com/v1.
    "openai": {"label": "OpenAI-compatible (endpoint + key)", "fields": ["base_url", "default_model"], "secret_fields": ["api_key"]},
    "anthropic": {"label": "Anthropic API", "fields": [], "secret_fields": ["api_key"]},
    "bedrock": {"label": "Amazon Bedrock", "fields": ["aws_region"], "secret_fields": ["aws_access_key_id", "aws_secret_access_key"]},
    "vertex": {"label": "Google Vertex AI", "fields": ["project_id", "region"], "secret_fields": ["service_account_json"]},
    "foundry": {"label": "Microsoft Foundry", "fields": ["resource"], "secret_fields": ["api_key"]},
}

# Models offered when adding a model (any other id can be typed). Bedrock ids take the "anthropic." prefix.
CATALOG = [
    {"model_id": "gpt-5", "label": "GPT-5 (OpenAI-compatible)", "efforts": [], "platforms": ["openai"]},
    {"model_id": "gpt-5-mini", "label": "GPT-5 mini (OpenAI-compatible)", "efforts": [], "platforms": ["openai"]},
    {"model_id": "gpt-4o-mini", "label": "GPT-4o mini (OpenAI-compatible)", "efforts": [], "platforms": ["openai"]},
    {"model_id": "claude-opus-5-5", "label": "Claude Opus 5.5 (default)", "efforts": ["low", "medium", "high", "xhigh", "max"]},
    {"model_id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5", "efforts": ["low", "medium", "high", "xhigh", "max"]},
    {"model_id": "claude-haiku-4-5", "label": "Claude Haiku 4.5", "efforts": []},
    {"model_id": "claude-fable-5-1", "label": "Claude Fable 5.1 (most capable)", "efforts": ["low", "medium", "high", "xhigh", "max"]},
]

DEFAULT_POLICY = {
    "ai_enabled": True,
    "refusal_fallbacks": True,          # Anthropic API only: retry a declined request on a fallback model
    "store_conversation_content": True, # needed for multi-step tool use; off = forgotten after each step
    "log_prompt_text": False,           # audit log keeps metadata only unless this is on
    "send_patient_identifiers": False,  # on = names, phones and ids may leave the server; default is off
    "daily_token_budget": 2_000_000,
    "per_user_daily_token_budget": 300_000,
    "max_agent_steps": 8,
    "inference_geo": "",                # e.g. "us"; empty = provider default
    "allowed_roles": ["clinician", "nurse", "chw", "coordinator"],
    "tool_access": {                    # role -> allowed tool kinds; write tools are always confirmed in the console
        "clinician": ["read", "write", "navigate"],
        "nurse": ["read", "write", "navigate"],
        "coordinator": ["read", "write", "navigate"],
        "chw": ["read", "navigate"],
    },
    "disabled_tools": [],
}

SYSTEM_PROMPTS = {
    "agent": (
        "You are the clinical assistant inside Clinestra, a longitudinal care harness. You help clinicians and "
        "staff by finding information and preparing actions through the tools provided.\n\n"
        "How you work:\n"
        "- Use read tools freely to ground every answer in the record. Do not state clinical facts about a patient that you "
        "have not read from a tool result in this conversation.\n"
        "- Write tools change the record. Propose them when they help; the clinician sees a confirmation card and decides. "
        "Never describe a write as done until its tool result confirms it.\n"
        "- You draft, summarise and suggest. Diagnoses, prescriptions, orders, stage moves and sign-offs are decisions a "
        "clinician makes; present options and evidence, not verdicts.\n"
        "- If a tool returns an error, explain it plainly (a safety rule refusing an action is expected behaviour, not a bug).\n"
        "- Be concise. Use short markdown: headings only for summaries, tables for lists of results."
    ),
    "synopsis": (
        "You write clinical synopses for clinicians from the structured record provided in the request context. Use only "
        "facts present in that context. Lead with what needs attention, then conditions, treatment, recent results and open "
        "items. Mark safety concerns with a '> 🔴' blockquote and cautions with '> ⚠️'. Markdown, under 350 words."
    ),
    "case_sheet": (
        "You help complete clinical case sheets. Propose values only from the record and the clinician's notes in the "
        "request; leave a field empty rather than guess. Every value you propose is a suggestion the clinician confirms."
    ),
    "education": (
        "You draft plain-language patient education (reading age ~12). Every factual claim must cite a source listed at "
        "the end. A clinician reviews and publishes it."
    ),
    "insights": (
        "You review the record in the request context and list clinical insights as short bullet points, each tagged "
        "[alert], [warning], [recommendation] or [info], each citing the record item it is based on."
    ),
}

# Agent types per level (see levels.py). "personal" and "team" types are templates: each team member gets their own
# personal agent and each care team its own Care Coordination Agent, copied from the template on first use.
DEFAULT_AGENTS = [
    {"id": "personal_assistant", "name": "Personal agent", "kind": "template", "level": "personal",
     "description": "Your own assistant: plans your day from your work, finds things in the library, drafts messages. It never opens a patient's record.",
     "audience": ["clinician", "nurse", "coordinator", "chw", "admin"], "color": "violet", "icon": "sparkles", "model_alias": None,
     "is_default": True, "ui_tools": True, "tools": ["ui_open"], "skills": ["plan_my_day", "library_search", "evidence_lookup", "draft_message"],
     "system_prompt": "You are {owner}'s personal agent inside Clinestra. You help with their own work: their tasks, the library, "
                      "evidence and drafts. You never open a patient's record and never change anything; for a patient, tell them to "
                      "open the patient agent from that patient's screen. Be brief."},
    {"id": "care_coordination", "name": "Care Coordination Agent", "kind": "template", "level": "team",
     "description": "Added to every care team: queues, due and overdue work, waiting decisions, workload and handovers. Not patient specific.",
     "audience": ["clinician", "nurse", "coordinator", "chw", "admin"], "color": "teal", "icon": "clipboard", "model_alias": None,
     "is_default": True, "ui_tools": True, "tools": ["ui_open", "render_blocks"],
     "skills": ["due_list", "handover_note", "workload_balance", "waiting_decisions"],
     "system_prompt": "You are the Care Coordination Agent for {team}. You help the team run the day: what is due, overdue and "
                      "waiting, who holds what, and handovers. You work on the team's queues, never on one patient's clinical record, "
                      "and you never change patient-specific records. Be brief and operational."},
    {"id": "clinical_copilot", "name": "Clinical copilot", "kind": "type", "level": "patient",
     "description": "Works on the patient on screen: brief, red flags, medication review, case-sheet draft and next steps. Every change is a proposal.",
     "audience": ["clinician", "nurse"], "color": "blue", "icon": "stethoscope", "model_alias": None, "is_default": True, "ui_tools": True,
     "tools": ["ui_open", "render_blocks", "*_list", "*_get", "summarize_patient", "fetch_clinical_data", "pending_actions"],
     "skills": ["pre_visit_brief", "red_flags", "medication_review", "draft_case_sheet", "next_steps"],
     "system_prompt": "You are the clinical copilot for one patient, opened by a clinician from that patient's screen. Ground every "
                      "statement in a tool result. Flag red flags first. When an action would help, call the matching _propose tool "
                      "with a clear rationale; the clinician accepts or rejects it. You never act on your own and never claim something "
                      "was done unless a tool result shows it."},
    {"id": "patient_coach", "name": "Health coach", "kind": "type", "level": "patient",
     "description": "For patients in the portal: check-ins, education, journal prompts.",
     "audience": ["patient"], "color": "emerald", "icon": "heart", "model_alias": None, "is_default": True, "ui_tools": False,
     "tools": ["fetch_clinical_data", "pending_actions", "care_action_list", "journal_entry_list"], "skills": [],
     "system_prompt": "You are a warm health coach for a patient using the patient app. Use plain language and reply in the language "
                      "they write in. Encourage today's care actions and journal logging. Never diagnose, never change or suggest "
                      "medicine doses; for anything worrying (chest pain, severe breathlessness, coughing blood, fainting) tell them "
                      "to contact their care team today."},
]

DEFAULT_NAV = [  # centred on patients: upcoming and recent patients sit under these in the sidebar
    {"id": "today", "label": "Today", "group": "Workspace", "roles": ["clinician", "nurse", "coordinator", "chw", "admin"], "enabled": True},
    {"id": "inbox", "label": "Inbox", "group": "Workspace", "roles": ["clinician", "nurse", "coordinator", "chw", "admin"], "enabled": True},
    {"id": "my_patients", "label": "My Patients", "group": "Workspace", "roles": ["clinician", "nurse", "coordinator", "chw", "admin"], "enabled": True},
    {"id": "agents", "label": "Agents", "group": "Configure", "roles": ["clinician", "coordinator", "admin"], "enabled": True},
    {"id": "library", "label": "Library", "group": "Configure", "roles": ["clinician", "coordinator", "admin"], "enabled": True},
    {"id": "consents", "label": "Consent forms", "group": "Configure", "roles": ["clinician", "coordinator", "admin"], "enabled": True},
]

DEFAULT_FEATURES = {
    "landing": "today",                # first screen after sign-in: Today, shaped by the person's role
    "ai_everywhere": True,             # AI buttons, synopsis cards and copilot on every screen
    "show_proposal_badges": True,
    "emr_dictation": True,
    "emr_autofill": True,
    "allow_ad_hoc_sections": True,     # clinicians may add sections not in the template
    "case_sheet_ui": "standard",       # "standard" (0.2 page editor) or "canvas" (the Clinestra 2.0 EMR canvas)
    "brand_name": "Clinestra",
    "brand_tagline": "AI Care Harness",
}

# Indicative list prices (USD per million input/output tokens) for the usage page's cost estimate.
PRICES = {"claude-opus-5-5": (4, 20), "claude-sonnet-5-5": (2, 10), "claude-haiku-4-5": (1, 5), "claude-fable-5-1": (10, 50)}
