"""Decimal-only, source-backed arithmetic. No generated Python or eval()."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP, localcontext

from .audit_models import Expression

CENT = Decimal("0.01")
# Dimensionless constants from the glossary. All business amounts/rates need fact references.
CONSTANTS = {Decimal(x) for x in ("0", "1", "2", "7", "12", "30", "60", "100", "365", "6000")}


def money(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("non-finite money")
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), ".2f")


def value_at(facts: dict[str, dict], fact_id: str, path: str):
    if fact_id not in facts:
        raise ValueError("calculation references an unavailable fact")
    value = facts[fact_id]
    if not path or any(part in ("evidence", "confidence", "references", "kind", "asserted_by") for part in path.split(".")):
        raise ValueError("calculation path is not a business field")
    try:
        for part in path.split("."):
            value = value[int(part)] if isinstance(value, list) else value[part]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError("calculation references a missing field") from exc
    return value


def evaluate(expression: Expression, facts: dict[str, dict], *, depth: int = 0) -> Decimal:
    if depth > 32:
        raise ValueError("calculation nesting limit exceeded")
    op = expression.op
    if op in ("fact", "constant"):
        if expression.args:
            raise ValueError("value expression cannot have arguments")
        raw = value_at(facts, expression.fact_id, expression.path) if op == "fact" else expression.constant
        if isinstance(raw, bool) or raw is None:
            raise ValueError("calculation requires an explicit numeric value")
        try:
            result = Decimal(str(raw))
        except InvalidOperation as exc:
            raise ValueError("calculation field is not numeric") from exc
        if not result.is_finite() or abs(result) > Decimal("1e20"):
            raise ValueError("calculation value out of bounds")
        if op == "constant" and result not in CONSTANTS:
            raise ValueError("business constants must be referenced from facts")
        return result
    if op == "days_between":
        if len(expression.args) != 2 or any(arg.op != "fact" for arg in expression.args):
            raise ValueError("date difference requires two source date fields")
        dates = [date.fromisoformat(value_at(facts, arg.fact_id, arg.path)) for arg in expression.args]
        return Decimal((dates[0] - dates[1]).days)
    required = {"subtract": 2, "divide": 2, "round_money": 1, "ceil": 1}
    if not expression.args or len(expression.args) > 10000 or (op in required and len(expression.args) != required[op]):
        raise ValueError("invalid number of calculation operands")
    values = [evaluate(arg, facts, depth=depth + 1) for arg in expression.args]
    with localcontext() as ctx:
        ctx.prec = 40
        if op == "add":
            result = sum(values, Decimal(0))
        elif op == "subtract":
            result = values[0] - values[1]
        elif op == "multiply":
            result = Decimal(1)
            for value in values:
                result *= value
        elif op == "divide":
            if values[1] == 0:
                raise ValueError("division by zero")
            result = values[0] / values[1]
        elif op == "round_money":
            result = values[0].quantize(CENT, rounding=ROUND_HALF_UP)
        elif op == "ceil":
            result = values[0].to_integral_value(rounding=ROUND_CEILING)
        elif op == "min":
            result = min(values)
        elif op == "max":
            result = max(values)
        else:
            raise ValueError("unknown calculation operation")
    if not result.is_finite() or abs(result) > Decimal("1e20"):
        raise ValueError("calculation result out of bounds")
    return result
