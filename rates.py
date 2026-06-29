"""Scoro overcharge calculator — overcharge rate lookup.

Rates are fetched once per run from Scoro products via load_overcharge_rates().
calc.py never talks to Scoro — it only calls get_overcharge_rate().
"""

import json
import logging
import os

log = logging.getLogger("overcharge_calculator")

DEFAULT_SERVICE_LINES = ("BK", "BD", "EA", "SA")

# service_line -> AUD/h, populated by load_overcharge_rates()
_overcharge = None


class RateError(Exception):
    """Raised when no overcharge rate exists for a service line."""


def _product_code_map():
    """Return service_line -> Scoro product code."""
    raw = os.environ.get("OVERCHARGE_RATE_PRODUCT_CODES")
    if raw:
        return {k: str(v) for k, v in json.loads(raw).items()}
    return {sl: sl for sl in DEFAULT_SERVICE_LINES}


def load_overcharge_rates(client):
    """Fetch overcharge premium rates from Scoro products/list (once per run).

    Maps each service line to a product code (default: same as the line, e.g.
    ``BK`` -> product code ``BK``). Override with env
    ``OVERCHARGE_RATE_PRODUCT_CODES`` JSON, e.g.
    ``{"BK":"OC-BK","BD":"OC-BD","EA":"OC-EA","SA":"OC-SA"}``.

    Optional ``OVERCHARGE_PRICE_LIST`` selects a non-default price list id.
    """
    global _overcharge

    code_map = _product_code_map()
    request = {}
    price_list = os.environ.get("OVERCHARGE_PRICE_LIST")
    if price_list:
        request["price_list"] = int(price_list)

    products = client.list_all("products", request=request or None)
    by_code = {}
    for product in products:
        if str(product.get("is_deleted")) in ("1", "true", "True"):
            continue
        if str(product.get("is_active")) in ("0", "false", "False"):
            continue
        code = (product.get("code") or "").strip().upper()
        if code:
            by_code[code] = product

    rates = {}
    for service_line, product_code in code_map.items():
        code = product_code.strip().upper()
        product = by_code.get(code)
        if product is None:
            sample = ", ".join(sorted(by_code)[:15])
            extra = len(by_code) - 15
            hint = (
                f" Set OVERCHARGE_RATE_PRODUCT_CODES to map service lines "
                f"to Scoro product codes."
            )
            codes_hint = f" Known codes: {sample}." if sample else ""
            if extra > 0:
                codes_hint = codes_hint[:-1] + f" (+{extra} more)."
            raise RateError(
                f"No Scoro product with code {product_code!r} "
                f"for service line {service_line!r}.{hint}{codes_hint}"
            )
        price = product.get("price")
        if price is None:
            raise RateError(
                f"Scoro product {product_code!r} has no price "
                f"for service line {service_line!r}"
            )
        rate = float(price)
        if rate <= 0:
            raise RateError(
                f"Scoro product {product_code!r} has non-positive price "
                f"({rate}) for service line {service_line!r}"
            )
        rates[service_line] = rate

    _overcharge = rates
    log.info(
        "loaded overcharge rates from Scoro: %s",
        ", ".join(f"{sl}={rate}/h" for sl, rate in sorted(rates.items())),
    )
    return rates


def known_service_lines():
    """Service lines with a configured overcharge rate."""
    if _overcharge is not None:
        return set(_overcharge.keys())
    return set(_product_code_map().keys())


def get_overcharge_rate(service_line):
    """Return the overcharge premium rate for a service line (AUD/hour)."""
    if _overcharge is None:
        raise RateError(
            "Overcharge rates not loaded — call load_overcharge_rates() first"
        )
    rate = _overcharge.get(service_line)
    if rate is None:
        raise RateError(
            f"No overcharge rate configured for service line {service_line!r}"
        )
    return float(rate)


def get_all_overcharge_rates():
    """Return a copy of the loaded rate table (for reporting)."""
    if _overcharge is None:
        return {}
    return dict(_overcharge)
