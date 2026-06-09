"""Unit tests for utils.safe_expr."""

from __future__ import annotations

import pytest

from utils.safe_expr import UnsafeExpressionError, compile_price_expression


class TestBasicExpressions:
    def test_constant_only(self):
        fn = compile_price_expression("4.20", allowed={"mid", "ask", "bid"})
        assert fn({"mid": 0, "ask": 0, "bid": 0}) == 4.20

    def test_single_variable(self):
        fn = compile_price_expression("mid", allowed={"mid", "ask", "bid"})
        assert fn({"mid": 4.60, "ask": 5.08, "bid": 4.12}) == 4.60

    def test_arithmetic_with_mid_and_spread(self):
        # The actual production formula used in the close-walk profiles.
        fn = compile_price_expression(
            "mid + 0.25*(ask-mid)", allowed={"mid", "ask", "bid"}
        )
        assert fn({"mid": 4.60, "ask": 5.08, "bid": 4.12}) == pytest.approx(4.72)

    def test_unary_minus(self):
        fn = compile_price_expression("-mid", allowed={"mid", "ask", "bid"})
        assert fn({"mid": 4.60, "ask": 5.08, "bid": 4.12}) == -4.60

    def test_division(self):
        fn = compile_price_expression(
            "(bid + ask) / 2", allowed={"mid", "ask", "bid"}
        )
        assert fn({"mid": 0, "ask": 5.08, "bid": 4.12}) == pytest.approx(4.60)

    def test_int_constant_coerced_to_float(self):
        fn = compile_price_expression("mid * 2", allowed={"mid"})
        result = fn({"mid": 4.60})
        assert result == 9.20
        assert isinstance(result, float)


class TestWhitelistEnforcement:
    def test_unknown_name_rejected(self):
        with pytest.raises(UnsafeExpressionError, match="Unknown name 'foo'"):
            compile_price_expression("foo + bar", allowed={"mid", "ask"})

    def test_builtin_name_rejected(self):
        # Even something Python recognizes as a builtin must be unreachable.
        # `abs(mid)` is a Call node — disallowed entirely, before name lookup.
        with pytest.raises(UnsafeExpressionError, match="Call"):
            compile_price_expression("abs(mid)", allowed={"mid"})

    def test_bare_builtin_name_rejected(self):
        # When the builtin appears as a bare name (not a call), the Name
        # whitelist rejects it explicitly.
        with pytest.raises(UnsafeExpressionError, match="Unknown name 'abs'"):
            compile_price_expression("abs + mid", allowed={"mid"})

    def test_partial_allowed_subset_used(self):
        # If the caller only allows {"mid"}, then "ask" must reject.
        with pytest.raises(UnsafeExpressionError, match="Unknown name 'ask'"):
            compile_price_expression("mid + ask", allowed={"mid"})


class TestDisallowedConstructs:
    def test_function_call_rejected(self):
        with pytest.raises(UnsafeExpressionError):
            compile_price_expression("max(mid, ask)", allowed={"mid", "ask"})

    def test_attribute_access_rejected(self):
        with pytest.raises(UnsafeExpressionError):
            compile_price_expression("mid.real", allowed={"mid"})

    def test_subscript_rejected(self):
        with pytest.raises(UnsafeExpressionError):
            compile_price_expression("mid[0]", allowed={"mid"})

    def test_comparison_rejected(self):
        with pytest.raises(UnsafeExpressionError):
            compile_price_expression("mid > ask", allowed={"mid", "ask"})

    def test_bitwise_rejected(self):
        with pytest.raises(UnsafeExpressionError):
            compile_price_expression("mid & ask", allowed={"mid", "ask"})

    def test_boolean_rejected(self):
        with pytest.raises(UnsafeExpressionError):
            compile_price_expression("mid and ask", allowed={"mid", "ask"})

    def test_lambda_rejected(self):
        with pytest.raises(UnsafeExpressionError):
            compile_price_expression("lambda x: x + 1", allowed={"mid"})

    def test_string_constant_rejected(self):
        # Only numeric constants are allowed — not strings, bytes, etc.
        with pytest.raises(UnsafeExpressionError, match="numeric constants"):
            compile_price_expression("'mid'", allowed={"mid"})

    def test_boolean_constant_rejected(self):
        # bool is a subclass of int in Python, but we intentionally reject it
        # so a config typo "True" doesn't silently become 1.0.
        with pytest.raises(UnsafeExpressionError, match="numeric constants"):
            compile_price_expression("True", allowed={"mid"})


class TestInputValidation:
    def test_empty_string_rejected(self):
        with pytest.raises(UnsafeExpressionError, match="non-empty"):
            compile_price_expression("", allowed={"mid"})

    def test_whitespace_only_rejected(self):
        with pytest.raises(UnsafeExpressionError, match="non-empty"):
            compile_price_expression("   ", allowed={"mid"})

    def test_syntax_error_in_expression(self):
        with pytest.raises(UnsafeExpressionError, match="Could not parse"):
            compile_price_expression("mid + + ", allowed={"mid"})

    def test_non_string_input_rejected(self):
        with pytest.raises(UnsafeExpressionError):
            compile_price_expression(42, allowed={"mid"})  # type: ignore[arg-type]


class TestRuntimeBehavior:
    def test_missing_binding_raises_keyerror(self):
        # Missing market data must not silently default to 0 — caller
        # gets a KeyError so they handle it explicitly.
        fn = compile_price_expression("mid + ask", allowed={"mid", "ask"})
        with pytest.raises(KeyError):
            fn({"mid": 4.60})  # ask missing

    def test_callable_is_pure(self):
        # The same bindings always produce the same output (no hidden state).
        fn = compile_price_expression(
            "mid + 0.5*(ask-mid)", allowed={"mid", "ask", "bid"}
        )
        bindings = {"mid": 4.60, "ask": 5.08, "bid": 4.12}
        first = fn(bindings)
        second = fn(bindings)
        assert first == second

    def test_negative_result_allowed(self):
        # Nothing inherent in the parser prevents a negative computed price.
        # Domain validation lives in the caller, not the parser.
        fn = compile_price_expression("mid - ask", allowed={"mid", "ask"})
        result = fn({"mid": 4.60, "ask": 5.08})
        assert result == pytest.approx(-0.48)


class TestProductionFormulas:
    """The actual formulas from settings.MLEG_CLOSE_PROFILES — must compile."""

    PRODUCTION_FORMULAS = [
        "mid",
        "mid + 0.25*(ask-mid)",
        "mid + 0.33*(ask-mid)",
        "mid + 0.50*(ask-mid)",
        "mid + 0.67*(ask-mid)",
        "mid + 0.75*(ask-mid)",
        "ask",
    ]

    @pytest.mark.parametrize("expr", PRODUCTION_FORMULAS)
    def test_each_production_formula_compiles(self, expr):
        fn = compile_price_expression(expr, allowed={"mid", "ask", "bid"})
        # Sanity-evaluate with realistic quote.
        result = fn({"mid": 4.60, "ask": 5.08, "bid": 4.12})
        # All production formulas should produce a price between bid and ask
        # (inclusive) on a normal quote.
        assert 4.12 <= result <= 5.08
