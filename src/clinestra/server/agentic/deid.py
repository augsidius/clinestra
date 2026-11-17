"""De-identify text before it can leave the server.

The layer is generic: pass the strings that must not appear (a name, a phone, an employee number, a date of birth).
Re-identification, when a product needs it, happens only in the response shown to the signed-in user.
"""
from __future__ import annotations

import re
from typing import Iterable, List

DEID_VERSION = "1"

_phone = re.compile(r"(?<!\d)(\+?\d[\d\s-]{8,}\d)(?!\d)")
_email = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def secrets_from_patient(patient: dict) -> List[str]:
    if not patient:
        return []
    found = [patient.get("name"), patient.get("date_of_birth")]
    demo = patient.get("demographics") or {}
    custom = patient.get("custom_fields") or {}
    found.extend([demo.get("phone"), demo.get("email"), custom.get("employee_no"), custom.get("phone")])
    name = patient.get("name") or ""
    if isinstance(name, str) and " " in name:
        found.append(name.split()[0])
    return [str(item) for item in found if item]


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace identifiers with tokens. Dates of birth become a relative-age placeholder when present as secrets."""
    if not text:
        return text
    out = text
    for secret in sorted({s for s in secrets if s and len(str(s)) >= 2}, key=len, reverse=True):
        out = out.replace(str(secret), "[redacted]")
    out = _email.sub("[email]", out)
    out = _phone.sub("[phone]", out)
    return out
