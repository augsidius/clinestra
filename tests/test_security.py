import pytest

from clinestra.engine import UnsafeExpression, safe_eval
from clinestra.server.auth import APIKeyAuth, HASHED_FORMAT
from clinestra.server.portal.sessions import returns_code_on_screen_allowed


@pytest.mark.parametrize("expr", ["pow(2, 100000000)", "'a' * 1000000000", "((9**8)**8)**8 ** 8 ** 8"])
def test_expressions_cannot_exhaust_resources(expr):
    try:
        value = safe_eval(expr)
    except UnsafeExpression:
        return
    assert isinstance(value, float)


def test_ordinary_arithmetic_is_unchanged():
    assert safe_eval("2 ** 3") == 8
    assert safe_eval("pow(2, 3) + 1") == 9
    assert safe_eval("70 / (1.75 ** 2)") == pytest.approx(22.857, abs=0.001)


def test_production_never_returns_otp(monkeypatch):
    monkeypatch.setenv("CLINESTRA_MODE", "production")
    assert returns_code_on_screen_allowed() is False


def test_production_needs_a_pepper(monkeypatch):
    monkeypatch.setenv("CLINESTRA_MODE", "production")
    monkeypatch.delenv("CLINESTRA_KEY_PEPPER", raising=False)
    with pytest.raises(RuntimeError, match="PEPPER"):
        APIKeyAuth({"format": HASHED_FORMAT, "keys": []})
