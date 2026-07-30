Transcribe the user-provided broker execution table. Do not recommend, analyze,
complete, or guess any value.

Return exactly one fenced JSON object:

```json
{
  "rows": [
    {
      "trade_date": null,
      "executed_at": null,
      "code": null,
      "name": null,
      "side": null,
      "price": null,
      "shares": null,
      "gross_amount": null,
      "net_amount": null,
      "fees": null,
      "raw_fields": {}
    }
  ]
}
```

Rules:
- Copy only visible executions. Never infer a missing code, number, fee, or date.
- `side` must be `buy`, `sell`, or null.
- Preserve a six-digit security code as printed; an exchange suffix is optional.
- Convert dates to YYYY-MM-DD only when the displayed date is unambiguous.
- `shares` is the actual executed quantity, not the ordered quantity.
- `gross_amount` is the labelled trade amount.
- `net_amount` is the labelled settlement/net cash amount and may be signed.
- `fees` is the displayed total fee/tax amount. Do not calculate it.
- Remove thousands separators and currency symbols from numeric JSON values.
- Merge repeated table headers across screenshots, but keep every distinct fill.
- Use null for missing or unreadable values.
- If no execution table is visible, return an empty rows array.
