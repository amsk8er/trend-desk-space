Plain visual transcription of a user-provided table only.
Do not analyze, explain, recommend, or infer anything.

Return exactly one fenced JSON object with this shape:

```json
{
  "account": {
    "nav": null,
    "cash": null,
    "currency": null,
    "as_of_text": null,
    "raw_fields": {}
  },
  "rows": [
    {
      "code": null,
      "name": null,
      "shares": null,
      "avg_cost": null,
      "current_price": null,
      "pnl_pct": null,
      "stop_loss": null,
      "entered_date": null,
      "raw_fields": {}
    }
  ]
}
```

Transcription rules:

- Copy identifiers and names exactly as printed. Never complete or guess them.
- `shares` is the explicitly displayed actual quantity.
- `avg_cost` and `current_price` are the explicitly displayed cost and current unit values.
- `pnl_pct` is the displayed floating-result percentage converted to a decimal
  (`3.52%` becomes `0.0352`). Preserve a minus sign.
- Use `null` for every missing or unreadable value.
- `nav` is an explicitly labelled overall account value. `cash` is an explicitly
  labelled available amount. Do not calculate either value from other fields.
- Remove separators and currency symbols from numeric JSON values. Convert
  `万` to `10000` and `亿` to `100000000`.
- Preserve any useful original label and text in `raw_fields`.
- If the visible page is not a table, return empty `rows` and add
  `"_low_confidence": true` at the top level.
