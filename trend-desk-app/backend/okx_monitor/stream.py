from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from websockets.sync.client import connect

from backend.okx_monitor.contracts import ConfirmedBar, decimal_or_none


log = logging.getLogger("trend-desk.okx-monitor.stream")


class OkxPublicStream:
    """Reconnectable public ticker/candle stream; it never authenticates."""

    def __init__(
        self,
        public_url: str = "wss://ws.okx.com:8443/ws/v5/public",
        business_url: str = "wss://ws.okx.com:8443/ws/v5/business",
    ) -> None:
        self.public_url = public_url
        self.business_url = business_url
        self._lock = threading.Lock()
        self._desired: set[str] = set()
        self._generation = 0
        self._prices: dict[str, Decimal] = {}
        self._bars: dict[str, deque[ConfirmedBar]] = defaultdict(lambda: deque(maxlen=100))
        self._last_message_at: datetime | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._run, args=(self.public_url, "tickers"),
                             name="okx-ticker-stream", daemon=True),
            threading.Thread(target=self._run, args=(self.business_url, "candle5m"),
                             name="okx-candle-stream", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)

    def set_instruments(self, inst_ids: set[str]) -> None:
        clean = {item for item in inst_ids if item}
        with self._lock:
            if clean != self._desired:
                self._desired = clean
                self._generation += 1

    def price(self, inst_id: str) -> Decimal | None:
        with self._lock:
            return self._prices.get(inst_id)

    def completed_bars(self, inst_id: str, after: datetime | None) -> list[ConfirmedBar]:
        with self._lock:
            rows = list(self._bars.get(inst_id) or [])
        return [row for row in rows if after is None or row.closed_at.replace(tzinfo=None) > after]

    @property
    def last_message_at(self) -> datetime | None:
        with self._lock:
            return self._last_message_at

    def _snapshot(self) -> tuple[set[str], int]:
        with self._lock:
            return set(self._desired), self._generation

    def _run(self, url: str, channel: str) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            desired, generation = self._snapshot()
            if not desired:
                self._stop.wait(1)
                continue
            args = [{"channel": channel, "instId": inst_id} for inst_id in sorted(desired)]
            try:
                with connect(url, open_timeout=10, ping_interval=20, ping_timeout=20) as ws:
                    ws.send(json.dumps({"op": "subscribe", "args": args}, separators=(",", ":")))
                    backoff = 1.0
                    while not self._stop.is_set():
                        if self._snapshot()[1] != generation:
                            break
                        try:
                            raw = ws.recv(timeout=10)
                        except TimeoutError:
                            continue
                        self._handle(str(raw))
            except Exception as exc:
                log.warning("OKX %s stream reconnecting after %s", channel, type(exc).__name__)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)

    def _handle(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        arg = payload.get("arg") if isinstance(payload, dict) else None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(arg, dict) or not isinstance(data, list):
            return
        channel = str(arg.get("channel") or "")
        inst_id = str(arg.get("instId") or "")
        now = datetime.now(timezone.utc)
        with self._lock:
            self._last_message_at = now.replace(tzinfo=None)
            if channel == "tickers" and data and isinstance(data[0], dict):
                price = decimal_or_none(data[0].get("last"))
                if price is not None:
                    self._prices[inst_id] = price
            elif channel == "candle5m":
                for row in data:
                    if not isinstance(row, list) or len(row) < 9 or str(row[8]) != "1":
                        continue
                    close = decimal_or_none(row[4])
                    if close is None:
                        continue
                    opened = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc)
                    bar = ConfirmedBar(closed_at=opened + timedelta(minutes=5), close=close)
                    if not self._bars[inst_id] or self._bars[inst_id][-1].closed_at != bar.closed_at:
                        self._bars[inst_id].append(bar)
