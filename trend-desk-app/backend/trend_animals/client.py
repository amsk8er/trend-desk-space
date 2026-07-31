"""趋势动物官方 HTTP API 客户端。

所有接口名、参数和业务成功条件来自实时 getApiDocIntro；本客户端只封装
当前项目实际使用的端点。禁止在异常或日志中携带完整请求 URL。
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable

import httpx

from backend import config
from backend.trend_animals.errors import TrendAnimalsError, redact_secret


class TrendAnimalsClient:
    def __init__(self, api_key: str | None = None, *, base_url: str | None = None,
                 timeout_s: float | None = None, retries: int = 1,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.api_key = api_key or os.getenv("TREND_ANIMALS_API_KEY", "")
        self.base_url = (base_url or config.TREND_ANIMALS_BASE_URL).rstrip("/")
        self.retries = max(0, retries)
        self.sleep = sleep
        self._http = httpx.Client(
            timeout=timeout_s or config.TREND_ANIMALS_TIMEOUT_S,
            transport=transport,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _get(self, endpoint: str, **params) -> Any:
        if not self.api_key:
            raise TrendAnimalsError("not_configured", "未配置 TREND_ANIMALS_API_KEY")
        query = {**params, "apiKey": self.api_key}
        last_error: TrendAnimalsError | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self._http.get(f"{self.base_url}/{endpoint}", params=query)
            except httpx.TransportError as exc:
                last_error = TrendAnimalsError(
                    "upstream_error", f"趋势动物网络调用失败：{type(exc).__name__}", retriable=True)
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = TrendAnimalsError(
                        "rate_limited" if response.status_code == 429 else "upstream_error",
                        f"趋势动物 HTTP {response.status_code}", retriable=True,
                        status_code=response.status_code,
                    )
                elif response.status_code >= 400:
                    raise TrendAnimalsError(
                        "upstream_error", f"趋势动物 HTTP {response.status_code}",
                        status_code=response.status_code)
                else:
                    try:
                        payload = response.json()
                    except ValueError:
                        raise TrendAnimalsError("api_contract_error", "趋势动物返回非 JSON")
                    if not isinstance(payload, dict):
                        raise TrendAnimalsError("api_contract_error", "趋势动物返回结构不是对象")
                    code = str(payload.get("code", ""))
                    success = payload.get("success")
                    if code != "00000" or success is False:
                        msg = redact_secret(payload.get("msg") or "接口业务失败")
                        raise TrendAnimalsError("api_contract_error", f"{endpoint}: {code} {msg}")
                    if "data" not in payload:
                        raise TrendAnimalsError("api_contract_error", f"{endpoint}: 缺少 data")
                    return payload["data"]
            if last_error is not None and attempt < self.retries:
                self.sleep(0.25 * (2 ** attempt))
        assert last_error is not None
        raise last_error

    def get_api_doc_intro(self):
        return self._get("getApiDocIntro")

    def get_change_log(self):
        return self._get("getChangeLog")

    def get_update_status(self):
        return self._get("getUpdateStatus")

    def get_favorites_ticker(self, category: str):
        return self._get("getFavoritesTicker", favCategory=category)

    def get_snapshot_billing(self):
        return self._get("getSnapshotColumnBilling")

    def search_ticker(self, keyword: str):
        return self._get("searchTicker", keyword=keyword)

    def get_components(self, tm_id: int, *, all_basic: bool = False):
        return self._get(
            "getComponentTicker", tmId=tm_id,
            getAllBasicComponentsFlag=1 if all_basic else 0,
        )

    def get_snapshot(self, tm_ids: list[int], fields: list[str]):
        if not tm_ids:
            return []
        return self._get(
            "getTickerSnapshot",
            tmIds=",".join(str(v) for v in tm_ids),
            fields=",".join(fields),
        )

    def get_account_ledger(self):
        return self._get("getAccountBalance", viewLevel="ledger")
