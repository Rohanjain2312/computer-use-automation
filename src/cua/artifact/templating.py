"""Value binding and typed coercion.

Substitution is deliberately not a template engine. It recognises exactly two
namespaces — ``{{ inputs.x }}`` and ``{{ config.x }}`` — and nothing else. A
capability artifact is a reviewed, replayed document; it must not be able to
evaluate expressions.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .schema import InputSpec, OutputSpec, ValueRef, ValueType

_PLACEHOLDER = re.compile(r"\{\{\s*(inputs|config)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class BindingError(ValueError):
    pass


class InputValidationError(ValueError):
    """A supplied input failed its declared contract. Never a system failure."""


def render_template(text: str, inputs: dict[str, Any], config: dict[str, Any]) -> str:
    def repl(m: re.Match[str]) -> str:
        namespace, key = m.group(1), m.group(2)
        source = inputs if namespace == "inputs" else config
        if key not in source:
            raise BindingError(f"{{{{ {namespace}.{key} }}}} is not bound")
        return str(source[key])

    return _PLACEHOLDER.sub(repl, text)


def render_value(ref: ValueRef, inputs: dict[str, Any], config: dict[str, Any]) -> str:
    if ref.literal is not None:
        return render_template(ref.literal, inputs, config)
    if ref.input is not None:
        if ref.input not in inputs:
            raise BindingError(f"input {ref.input!r} was not supplied")
        return str(inputs[ref.input])
    if ref.config is not None:
        if ref.config not in config:
            raise BindingError(f"config key {ref.config!r} was not supplied")
        return str(config[ref.config])
    raise BindingError("empty ValueRef")  # pragma: no cover - schema prevents this


# -- typed coercion -------------------------------------------------------

_MONEY = re.compile(r"-?\$?\s*[\d,]+(?:\.\d+)?")


def apply_transforms(text: str, transforms: list[str]) -> str:
    out = text
    for t in transforms:
        if t == "strip":
            out = out.strip()
        elif t == "collapse_ws":
            out = re.sub(r"\s+", " ", out).strip()
        elif t == "upper":
            out = out.upper()
        elif t == "digits_only":
            out = re.sub(r"\D", "", out)
        elif t == "money_to_decimal":
            m = _MONEY.search(out)
            if not m:
                raise ValueError(f"no monetary amount found in {out!r}")
            out = m.group(0).replace("$", "").replace(",", "").strip()
    return out


def coerce(value: str, vtype: ValueType, *, field: str = "value", enum_values: list[str] | None = None) -> Any:
    raw = value.strip() if isinstance(value, str) else value
    try:
        if vtype is ValueType.string:
            return str(raw)
        if vtype is ValueType.integer:
            return int(str(raw).replace(",", ""))
        if vtype in (ValueType.decimal, ValueType.money):
            return Decimal(str(raw).replace(",", "").replace("$", "").strip())
        if vtype is ValueType.boolean:
            low = str(raw).strip().lower()
            if low in {"true", "yes", "y", "1", "open", "active"}:
                return True
            if low in {"false", "no", "n", "0", "closed", "inactive"}:
                return False
            raise ValueError(f"{raw!r} is not a boolean")
        if vtype is ValueType.date:
            return date.fromisoformat(str(raw)[:10]).isoformat()
        if vtype is ValueType.enum:
            if enum_values and str(raw) not in enum_values:
                raise ValueError(f"{raw!r} is not one of {enum_values}")
            return str(raw)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field}: cannot read {raw!r} as {vtype.value} ({exc})") from exc
    raise ValueError(f"{field}: unsupported type {vtype}")  # pragma: no cover


def bind_inputs(
    specs: list[InputSpec], supplied: dict[str, Any], env: dict[str, str]
) -> tuple[dict[str, Any], list[str]]:
    """Validate and bind declared inputs. Returns (bound values, secret values).

    Environment-sourced inputs (credentials) are read from the process
    environment and returned separately so the caller can register them with the
    redactor before anything is written.
    """
    bound: dict[str, Any] = {}
    secrets: list[str] = []
    for spec in specs:
        if spec.source == "environment":
            raw = env.get(spec.env_var or "", "")
            if not raw and spec.required:
                raise InputValidationError(
                    f"input {spec.name!r} must be supplied via environment variable "
                    f"{spec.env_var!r} (it is {spec.sensitivity.value} and is never stored)"
                )
            if raw:
                bound[spec.name] = raw
                secrets.append(raw)
            continue

        if spec.name not in supplied or supplied[spec.name] in (None, ""):
            if spec.required:
                raise InputValidationError(f"required input {spec.name!r} was not supplied")
            continue

        raw = str(supplied[spec.name])
        if spec.pattern and not re.fullmatch(spec.pattern, raw):
            raise InputValidationError(
                f"input {spec.name!r} value does not match the declared pattern {spec.pattern!r}"
            )
        try:
            value = coerce(raw, spec.type, field=f"input {spec.name!r}", enum_values=spec.enum_values)
        except ValueError as exc:
            raise InputValidationError(str(exc)) from exc
        # Keep the caller's textual form for typing into the UI; the coerced
        # value only proves the contract held.
        bound[spec.name] = raw if spec.type in (ValueType.string, ValueType.enum) else raw
        _ = value
        if spec.sensitivity.value in {"pii", "secret"}:
            secrets.append(raw)
    unknown = set(supplied) - {s.name for s in specs}
    if unknown:
        raise InputValidationError(f"unknown input(s) supplied: {sorted(unknown)}")
    return bound, secrets


def coerce_output(spec: OutputSpec, text: str) -> Any:
    transformed = apply_transforms(text, spec.extract.transforms)
    return coerce(transformed, spec.type, field=f"output {spec.name!r}")
