import json
import re

_FENCE = re.compile(r"```json\s*(.*?)\s*```", re.S)

def parse_ocr_json(text: str) -> dict:
    # Some CLI backends wrap the actual model text in an API response object.
    # Unwrap first so fenced JSON inside ``content`` can be parsed normally.
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        envelope = None
    if isinstance(envelope, dict) and isinstance(envelope.get("content"), str):
        text = envelope["content"]

    m = _FENCE.search(text)
    if not m:
        try:
            payload = json.loads(text)
            return payload if isinstance(payload, dict) else {
                "meta": {"market": None}, "rows": [],
            }
        except json.JSONDecodeError:
            return {"meta": {"market": None}, "rows": []}
    try:
        payload = json.loads(m.group(1))
        return payload if isinstance(payload, dict) else {
            "meta": {"market": None}, "rows": [],
        }
    except json.JSONDecodeError:
        return {"meta": {"market": None}, "rows": []}

def is_bad_image(payload: dict) -> tuple[bool, str | None]:
    meta = payload.get("meta") or {}
    rows = payload.get("rows") or []
    if not rows:
        return True, "rows_empty"
    if meta.get("market") != "A股":
        return True, "market_missing_or_wrong"
    conf = meta.get("confidence")
    if conf is not None and conf < 0.3:
        return True, f"confidence_low={conf}"
    return False, None
