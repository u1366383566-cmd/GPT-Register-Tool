"""5sim.net handler API client.

5sim exposes a REST API under https://5sim.net/v1 authenticated with a
Bearer token (JWT issued from the user centre). Purchase is keyed by
country/operator/product instead of the sms-activate style service code,
and codes are delivered via order status polling rather than push events.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Optional

import requests as _requests


DEFAULT_ENDPOINT = "https://5sim.net/v1"
OPENAI_PRODUCT = "openai"
DEFAULT_COUNTRY = "ghana"
DEFAULT_OPERATOR = "any"

PLACEHOLDER_KEYS = {"", "YOUR_5SIM_API_KEY", "$5SIM_API_KEY"}

# 5sim order statuses (checked against the `status` field of buy/check):
# RECEIVED / SMS_RECEIVED means a code is present; TIMEOUT / CANCELED are
# terminal; PENDING means we keep polling.
STATUS_OK = {"RECEIVED", "SMS_RECEIVED"}
STATUS_CANCELED = {"TIMEOUT", "CANCELED", "CANCEL"}


def normalize_phone(phone: str) -> str:
    value = str(phone or "").strip()
    if not value:
        return ""
    if value.startswith("+"):
        return "+" + "".join(ch for ch in value[1:] if ch.isdigit())
    if value.startswith("00"):
        return "+" + "".join(ch for ch in value[2:] if ch.isdigit())
    digits = "".join(ch for ch in value if ch.isdigit())
    return f"+{digits}" if digits else ""


def normalize_product(value: str) -> str:
    return str(value or OPENAI_PRODUCT).strip() or OPENAI_PRODUCT


def normalize_country(value: str) -> str:
    return str(value or DEFAULT_COUNTRY).strip() or DEFAULT_COUNTRY


def normalize_operator(value: str) -> str:
    return str(value or DEFAULT_OPERATOR).strip() or DEFAULT_OPERATOR


@dataclass
class FiveSimActivation:
    activation_id: str
    phone: str
    operator: str
    country: str
    product: str
    price: str = ""
    acquired_at: float = field(default_factory=time.time)


class FiveSimClient:
    def __init__(self, api_key: str = "", endpoint: str = DEFAULT_ENDPOINT, timeout: int = 15):
        self.api_key = str(api_key or "").strip()
        self.endpoint = str(endpoint or DEFAULT_ENDPOINT).strip().rstrip("/") or DEFAULT_ENDPOINT
        self.timeout = timeout

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict | None = None, timeout: int | None = None) -> str:
        url = f"{self.endpoint}/{str(path or '').lstrip('/')}"
        response = _requests.get(url, headers=self._headers(), params=params, timeout=timeout or self.timeout)
        body = response.text.strip()
        if response.status_code < 200 or response.status_code >= 300:
            detail = body[:300] or f"HTTP {response.status_code}"
            raise RuntimeError(f"5sim HTTP {response.status_code}: {detail}")
        return body

    def get_balance(self) -> str:
        data = json.loads(self._get("/user/profile"))
        return str(data.get("balance") or "")

    def get_prices(self, country: str = "", product: str = "") -> list[dict]:
        """Flatten /guest/prices into offers: country/product/operator with cost/count/rate."""
        params = {}
        country = normalize_country(country)
        product = normalize_product(product)
        if country:
            params["country"] = country
        if product:
            params["product"] = product
        data = json.loads(self._get("/guest/prices", params))
        offers: list[dict] = []
        if not isinstance(data, dict):
            return offers
        for country_id, by_product in data.items():
            if not isinstance(by_product, dict):
                continue
            for product_name, by_operator in by_product.items():
                if not isinstance(by_operator, dict):
                    continue
                for operator, offer in by_operator.items():
                    if not isinstance(offer, dict):
                        continue
                    offers.append({
                        "country": str(country_id),
                        "product": str(product_name),
                        "operator": str(operator),
                        "cost": str(offer.get("cost") or ""),
                        "count": offer.get("count", 0),
                        "rate": str(offer.get("rate") or ""),
                    })
        return sorted(offers, key=lambda item: _as_float(item.get("cost")))

    def get_products(self, country: str = "", operator: str = "") -> dict:
        """Return product -> {Category, Qty, Price} for the given country/operator."""
        country = normalize_country(country)
        operator = normalize_operator(operator)
        data = json.loads(self._get(f"/guest/products/{country}/{operator}"))
        return data if isinstance(data, dict) else {}

    def get_number(
        self,
        country: str = "",
        operator: str = "",
        product: str = "",
        max_price: str = "",
    ) -> FiveSimActivation:
        country = normalize_country(country)
        operator = normalize_operator(operator)
        product = normalize_product(product)
        params = {}
        # maxPrice only applies when operator is "any" (per 5sim docs).
        if operator == "any":
            max_price = str(max_price or "").strip()
            if max_price:
                params["maxPrice"] = max_price
        body = self._get(f"/user/buy/activation/{country}/{operator}/{product}", params)
        data = json.loads(body)
        activation_id = str(data.get("id") or "").strip()
        phone = normalize_phone(data.get("phone") or "")
        if not activation_id or not phone:
            raise RuntimeError(f"5sim buy error: {body[:300]}")
        return FiveSimActivation(
            activation_id=activation_id,
            phone=phone,
            operator=str(data.get("operator") or operator),
            country=str(data.get("country") or country),
            product=str(data.get("product") or product),
            price=str(data.get("price") or ""),
        )

    def get_status(self, activation_id: str) -> dict:
        data = json.loads(self._get(f"/user/check/{activation_id}"))
        status = str(data.get("status") or "").strip().upper()
        if status in STATUS_OK:
            code = _extract_code(data)
            if code:
                return {"status": "OK", "code": code}
            return {"status": "WAIT_CODE"}
        if status in STATUS_CANCELED:
            return {"status": "CANCEL"}
        if status in {"PENDING", "RECEIVED"}:
            return {"status": "WAIT_CODE"}
        return {"status": "WAIT_CODE"}

    def wait_for_code(
        self,
        activation_id: str,
        timeout: int = 120,
        poll_interval: int = 5,
        previous_code: str = "",
        accept_wait_retry: bool = False,
    ) -> Optional[str]:
        deadline = time.time() + timeout
        attempt = 0
        previous_code = str(previous_code or "").strip()
        while time.time() < deadline:
            attempt += 1
            try:
                status = self.get_status(activation_id)
                if status["status"] == "OK":
                    code = str(status.get("code") or "").strip()
                    if code and code != previous_code:
                        return code
                if status["status"] == "CANCEL":
                    print(f"  [5sim] order {activation_id} was cancelled")
                    return None
            except Exception as exc:
                print(f"  [5sim] poll attempt {attempt} error: {exc}")
            wait = min(poll_interval, max(1, deadline - time.time()))
            if wait > 0:
                time.sleep(wait)
        return None

    def complete(self, activation_id: str) -> bool:
        try:
            self._get(f"/user/finish/{activation_id}")
            return True
        except Exception:
            return False

    def cancel(self, activation_id: str) -> bool:
        try:
            self._get(f"/user/cancel/{activation_id}")
            return True
        except Exception:
            return False

    def request_additional(self, activation_id: str) -> bool:
        # 5sim has no "request another SMS" call; callers fall back to buying
        # a fresh number when they need a new code.
        return False


def acquire_and_wait_code(
    api_key: str,
    country: str = DEFAULT_COUNTRY,
    operator: str = DEFAULT_OPERATOR,
    product: str = OPENAI_PRODUCT,
    max_price: str = "",
    timeout: int = 120,
    poll_interval: int = 5,
    endpoint: str = DEFAULT_ENDPOINT,
) -> dict:
    client = FiveSimClient(api_key=api_key, endpoint=endpoint)
    try:
        activation = client.get_number(country=country, operator=operator, product=product, max_price=max_price)
        print(
            "  [5sim] acquired "
            f"{activation.phone} (id={activation.activation_id}, operator={activation.operator}, price={activation.price})"
        )
    except Exception as exc:
        return {"ok": False, "error": f"acquire_failed:{exc}"}

    code = client.wait_for_code(activation.activation_id, timeout=timeout, poll_interval=poll_interval)
    if not code:
        client.cancel(activation.activation_id)
        return {
            "ok": False,
            "error": "sms_timeout",
            "activation_id": activation.activation_id,
            "phone": activation.phone,
        }
    return {
        "ok": True,
        "activation_id": activation.activation_id,
        "phone": activation.phone,
        "code": code,
        "price": activation.price,
    }


def _extract_code(data: dict) -> str:
    sms = data.get("sms")
    if isinstance(sms, list):
        for item in sms:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if code:
                return code
    if isinstance(sms, dict):
        code = str(sms.get("code") or "").strip()
        if code:
            return code
    return ""


def _as_float(value) -> float:
    try:
        return float(str(value or "").strip())
    except Exception:
        return 0.0