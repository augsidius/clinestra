"""
Sandboxed expression evaluator.

Expressions are
parsed into a Python AST and only an allow-list of node types, operators,
functions and constants is evaluated. Python's ``eval`` is never used.

    >>> safe_eval("weight * 0.5 if age < 65 else weight * 0.25", {"weight": 70, "age": 70})
    17.5
"""
import ast
import math
import operator

__all__ = ["safe_eval", "UnsafeExpression"]

_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}
_CMP_OPS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
}
_MAX_POW_BASE = 1e6


def _bounded_pow(base, exponent):
    """Integer powers stay exact when small; everything else is a float, so size cannot explode."""
    if not all(isinstance(x, (int, float)) for x in (base, exponent)) or abs(exponent) > 8:
        raise UnsafeExpression("Exponent is out of bounds")
    if isinstance(base, int) and isinstance(exponent, int) and exponent >= 0 and abs(base) <= _MAX_POW_BASE:
        return base ** exponent
    return float(base) ** exponent


_FUNCS = {
    "abs": abs, "min": min, "max": max, "round": round, "pow": _bounded_pow,
    "sqrt": math.sqrt, "exp": math.exp, "log": math.log, "log10": math.log10,
    "floor": math.floor, "ceil": math.ceil,
}
_CONSTS = {"e": math.e, "pi": math.pi, "True": True, "False": False, "None": None}


class UnsafeExpression(ValueError):
    """Raised when an expression uses a construct outside the allowlist."""


_MAX_LEN = 2000
_MAX_DEPTH = 32


def safe_eval(expression, variables=None):
    """Evaluate an arithmetic / boolean expression against a variable map."""
    variables = variables or {}
    text = str(expression)
    if len(text) > _MAX_LEN:
        raise UnsafeExpression("Expression is too long")

    def _eval(node, depth=0):
        if depth > _MAX_DEPTH:
            raise UnsafeExpression("Expression is too deep")
        if isinstance(node, ast.Expression):
            return _eval(node.body, depth + 1)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id.startswith("__"):
                raise UnsafeExpression(f"Name '{node.id}' is not allowed")
            if node.id in variables:
                return variables[node.id]
            if node.id in _CONSTS:
                return _CONSTS[node.id]
            raise UnsafeExpression(f"Unknown name '{node.id}'")
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            left, right = _eval(node.left, depth + 1), _eval(node.right, depth + 1)
            if not all(isinstance(x, (int, float)) for x in (left, right)):
                raise UnsafeExpression("Arithmetic needs numbers")  # also stops 'a' * 10**9
            try:
                if isinstance(node.op, ast.Pow):
                    return _bounded_pow(left, right)
                return _BIN_OPS[type(node.op)](left, right)
            except (ZeroDivisionError, OverflowError) as exc:
                raise UnsafeExpression(f"Expression error: {exc}") from exc
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return _UNARY_OPS[type(node.op)](_eval(node.operand, depth + 1))
        if isinstance(node, ast.BoolOp):
            # Short-circuit, so "a or b" holds when a does even if b refers to data the patient lacks.
            if isinstance(node.op, ast.And):
                return all(_eval(v, depth + 1) for v in node.values)
            return any(_eval(v, depth + 1) for v in node.values)
        if isinstance(node, ast.Compare):
            left = _eval(node.left, depth + 1)
            for op, comparator in zip(node.ops, node.comparators):
                right = _eval(comparator, depth + 1)
                if type(op) not in _CMP_OPS:
                    return False
                # A missing value (None) never satisfies an ordering comparison; == / != still work.
                if (left is None or right is None) and type(op) not in (ast.Eq, ast.NotEq):
                    return False
                if not _CMP_OPS[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in _FUNCS:
                raise UnsafeExpression(f"Function '{node.func.id}' not allowed")
            if node.keywords:
                raise UnsafeExpression("Keyword arguments are not allowed")
            args = [_eval(a, depth + 1) for a in node.args]
            try:
                return _FUNCS[node.func.id](*args)
            except (ZeroDivisionError, OverflowError, ValueError) as exc:
                raise UnsafeExpression(f"Expression error: {exc}") from exc
        if isinstance(node, ast.IfExp):
            return _eval(node.body, depth + 1) if _eval(node.test, depth + 1) else _eval(node.orelse, depth + 1)
        raise UnsafeExpression(f"Unsupported expression node: {type(node).__name__}")

    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise UnsafeExpression(f"Cannot parse expression: {exc}") from exc
    return _eval(tree)
