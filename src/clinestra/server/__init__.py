"""HTTP server for the Clinical API. Requires ``pip install clinestra[server]``."""
try:
    from .app import create_app
    from .auth import APIKeyAuth, DevHeaderAuth
except ImportError as exc:  # pragma: no cover
    raise ImportError("The Clinestra server needs FastAPI. Install it with: pip install 'clinestra[server]'") from exc

__all__ = ["create_app", "APIKeyAuth", "DevHeaderAuth"]
