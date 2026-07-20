你在识别趋势动物 App 的持仓详情截图。

目标品种代码：{{instrument_id}}

只判断截图中是否明确出现“波动率放大”止盈标签。不要根据涨跌幅、K线或其他信号推测。

输出严格 JSON，不要 Markdown：

{
  "rows": [
    {
      "instrument_id": "{{instrument_id}}",
      "name": "截图中的品种名称，无法确认则 null",
      "volatility_up": true,
      "evidence_text": "截图中支持判断的短文字"
    }
  ]
}

规则：

- 明确看到当日“波动率放大”标签，`volatility_up=true`。
- 明确看到该标签为未触发/否，`volatility_up=false`。
- 截图未覆盖该标签、文字不清或无法确认当日状态时，返回 `{"rows": []}`，禁止猜测。
