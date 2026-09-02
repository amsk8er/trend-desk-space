from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlencode

import httpx


class OkxClientError(RuntimeError):
    pass


class ReadOnlyViolation(OkxClientError):
    pass


class OkxReadOnlyClient:
    """Minimal OKX REST client which cannot issue a mutating HTTP request."""

    _ALLOWED_METHOD = "GET"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        demo: bool = False,
        timeout: float = 15,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self.demo = demo
        self._client = http_client or httpx.Client(timeout=timeout)

    @property
    def private_configured(self) -> bool:
        return bool(self.api_key and self._api_secret and self._passphrase)

    @staticmethod
    def _timestamp() -> str:
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        return now.replace("+00:00", "Z")

    def _signature(self, timestamp: str, request_path: str) -> str:
        payload = f"{timestamp}GET{request_path}".encode()
        digest = hmac.new(self._api_secret.encode(), payload, hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _headers(self, request_path: str, *, private: bool) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "trend-desk-okx-monitor/v1"}
        if not private:
            return headers
        if not self.private_configured:
            raise OkxClientError("okx_private_credentials_not_configured")
        timestamp = self._timestamp()
        headers.update({
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self._signature(timestamp, request_path),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self._passphrase,
        })
        if self.demo:
            headers["x-simulated-trading"] = "1"
        return headers

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                private: bool = False) -> list[dict[str, Any]]:
        if method.upper() != self._ALLOWED_METHOD:
            raise ReadOnlyViolation("okx_monitor_rejects_non_get_request")
        clean_params = {key: value for key, value in (params or {}).items() if value not in (None, "")}
        query = urlencode(clean_params)
        request_path = path + (f"?{query}" if query else "")
        try:
            response = self._client.get(
                f"{self.base_url}{path}", params=clean_params,
                headers=self._headers(request_path, private=private),
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise OkxClientError(f"okx_transport_error:{type(exc).__name__}") from exc
        if str(payload.get("code")) != "0":
            code = str(payload.get("code") or "unknown")[:32]
            message = str(payload.get("msg") or "okx_api_error")[:180]
            raise OkxClientError(f"okx_api_error:{code}:{message}")
        data = payload.get("data")
        if not isinstance(data, list):
            raise OkxClientError("okx_invalid_data_shape")
        return data

    def instruments(self, inst_type: str) -> list[dict[str, Any]]:
        return self.request("GET", "/api/v5/public/instruments", params={"instType": inst_type})

    def ticker(self, inst_id: str) -> dict[str, Any]:
        rows = self.request("GET", "/api/v5/market/ticker", params={"instId": inst_id})
        return rows[0] if rows else {}

    def all_instruments(self, types: Iterable[str] = ("SPOT", "SWAP", "FUTURES")):
        """Load only the product families supported by the holdings monitor.

        OPTION is intentionally excluded: it is outside the monitor's current
        product scope and OKX can reject the public OPTION catalogue for
        accounts/regions where that product family is unavailable.
        """
        rows: list[dict[str, Any]] = []
        for inst_type in types:
            rows.extend(self.instruments(inst_type))
        return rows

    def account_balance(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/v5/account/balance", private=True)

    def funding_balances(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/v5/asset/balances", private=True)

    def positions(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/v5/account/positions", private=True)

    def pending_orders(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/v5/trade/orders-pending", private=True)

    def pending_algo_orders(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for order_type in ("conditional", "oco", "trigger", "move_order_stop"):
            rows.extend(self.request(
                "GET", "/api/v5/trade/orders-algo-pending",
                params={"ordType": order_type}, private=True,
            ))
        return rows

    def recent_orders(self, inst_type: str | None = None) -> list[dict[str, Any]]:
        return self.request(
            "GET", "/api/v5/trade/orders-history",
            params={"instType": inst_type}, private=True,
        )

    def candles(self, inst_id: str, *, bar: str = "5m", limit: int = 100) -> list[list[str]]:
        rows = self.request(
            "GET", "/api/v5/market/candles",
            params={"instId": inst_id, "bar": bar, "limit": limit}, private=False,
        )
        return rows  # OKX candle rows are arrays despite the generic endpoint annotation.
