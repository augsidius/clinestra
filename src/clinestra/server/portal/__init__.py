"""
The patient portal: phone OTP sign-in, patient sessions, the generated patient-app site config, and the realtime
voice interview.

Sign-in flow: phone -> OTP -> choose who is using the app (family members share phones) -> session. A session is a
short-lived bearer token (``pt-...``) that the server's authenticator accepts as a ``patient`` actor bound to that
patient (and to the people they are guardian for). The harness enforces patient permissions on every request: read
own record, write journal entries, patient-reported assessments, consents and conversations.

Sessions live in the harness store, per site, as hashes of the token; the token itself is never stored.
"""
from .sessions import (DEFAULT_PORTAL, active_sessions, end_session, public_directory, purge, request_otp,
                       session_actor, settings, site_config, site_id, start_session, verify_otp)

__all__ = ["DEFAULT_PORTAL", "settings", "public_directory", "request_otp", "verify_otp", "start_session", "end_session",
           "purge", "active_sessions", "session_actor", "site_config", "site_id"]
