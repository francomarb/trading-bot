"""
Safe arithmetic expression evaluator for config-driven price formulas.

Purpose
-------
Configuration files describe MLEG close-walk steps as human-readable price
expressions like ``"mid + 0.25*(ask-mid)"``. We need to parse and evaluate
them safely (no ``eval()``, no attribute access, no function calls) and
compose them into a fast callable at config-load time so a typo blows up
the bot at startup rather than during a stop-loss close.

Why a real parser
-----------------
The price-expression DSL is small (variables + numbers + ``+ - * / ()``
+ unary minus), but it's exposed via settings. Using ``eval()`` even
with a restricted ``__builtins__`` is famously footgunny — ``__class__``
walks, ``__subclasses__``, etc. ``ast.parse(mode='eval')`` plus a strict
node whitelist closes that off completely.

Allowed grammar
---------------
- Variables: any identifier in the caller-provided whitelist (e.g.
  ``{"mid", "bid", "ask"}``). Anything else raises at parse time.
- Numeric constants: int or float literals.
- Operators: ``+`` ``-`` ``*`` ``/`` (binary) and unary ``-``.
- Parentheses.

Disallowed
----------
- Function calls, attribute access, subscripting, comprehensions.
- Names not in the whitelist (including builtins like ``len``).
- Comparison, boolean, bitwise, walrus, lambda, etc.

Usage
-----
    >>> compile_price_expression("mid + 0.25*(ask-mid)", allowed={"mid","ask","bid"})
    <function ...>
    >>> fn = compile_price_expression("mid + 0.25*(ask-mid)", allowed={"mid","ask","bid"})
    >>> fn({"mid": 4.60, "ask": 5.08, "bid": 4.12})
    4.72
"""

from __future__ import annotations

import ast
from typing import Callable, Mapping

__all__ = ["compile_price_expression", "UnsafeExpressionError"]


class UnsafeExpressionError(ValueError):
    """Raised when an expression contains a disallowed construct."""


# Whitelisted AST node types. Adding here is the only way to extend the DSL.
_ALLOWED_BINOPS: dict[type, Callable[[float, float], float]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}

_ALLOWED_UNARYOPS: dict[type, Callable[[float], float]] = {
    ast.UAdd: lambda x: +x,
    ast.USub: lambda x: -x,
}


def _walk(node: ast.AST, allowed_names: frozenset[str]) -> Callable[[Mapping[str, float]], float]:
    """Recursively compile an AST node into a closure over the bindings dict."""
    if isinstance(node, ast.Expression):
        return _walk(node.body, allowed_names)

    if isinstance(node, ast.Constant):
        # ast.Constant covers numeric literals on Python ≥3.8.
        if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
            raise UnsafeExpressionError(
                f"Only numeric constants are allowed, got {type(node.value).__name__}"
            )
        value = float(node.value)
        return lambda _bindings: value

    if isinstance(node, ast.Name):
        if node.id not in allowed_names:
            raise UnsafeExpressionError(
                f"Unknown name '{node.id}'; allowed: {sorted(allowed_names)}"
            )
        name = node.id
        return lambda bindings: float(bindings[name])

    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        op = _ALLOWED_BINOPS.get(op_type)
        if op is None:
            raise UnsafeExpressionError(
                f"Disallowed binary operator {op_type.__name__}"
            )
        left = _walk(node.left, allowed_names)
        right = _walk(node.right, allowed_names)
        return lambda bindings: op(left(bindings), right(bindings))

    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        op = _ALLOWED_UNARYOPS.get(op_type)
        if op is None:
            raise UnsafeExpressionError(
                f"Disallowed unary operator {op_type.__name__}"
            )
        operand = _walk(node.operand, allowed_names)
        return lambda bindings: op(operand(bindings))

    raise UnsafeExpressionError(
        f"Disallowed AST node type: {type(node).__name__}"
    )


def compile_price_expression(
    expression: str,
    *,
    allowed: frozenset[str] | set[str] | tuple[str, ...] | list[str],
) -> Callable[[Mapping[str, float]], float]:
    """
    Parse ``expression`` into a callable that takes a ``bindings`` mapping
    (e.g. ``{"mid": 4.60, "ask": 5.08, "bid": 4.12}``) and returns a float.

    Raises ``UnsafeExpressionError`` if the expression contains a node type
    or name outside the whitelist. The error fires at compile time — call
    this at config-load to fail fast.

    Args:
        expression: e.g. ``"mid + 0.25*(ask-mid)"``.
        allowed: whitelist of variable names that may appear in the
            expression. Typically ``frozenset({"mid", "bid", "ask"})`` for
            MLEG close walks.

    Returns:
        A pure function ``(bindings) -> float``. Calling it with bindings
        that omit a referenced name raises ``KeyError`` — that's
        intentional; missing market data must not silently fall back.
    """
    if not isinstance(expression, str) or not expression.strip():
        raise UnsafeExpressionError("Expression must be a non-empty string")

    allowed_names = frozenset(allowed)
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise UnsafeExpressionError(
            f"Could not parse expression {expression!r}: {exc}"
        ) from exc

    return _walk(tree, allowed_names)
