"""趋势动物 API 的脱敏异常。"""
import re


_KEY_PARAM = re.compile(r"([?&]apiKey=)[^&\s]+", re.IGNORECASE)
_SK_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]+\b")


def redact_secret(value: object) -> str:
    """删除 URL 查询参数和常见 sk-* token，避免异常/审计泄露密钥。"""
    text = str(value or "")
    text = _KEY_PARAM.sub(r"\1[REDACTED]", text)
    return _SK_TOKEN.sub("[REDACTED]", text)


class TrendAnimalsError(RuntimeError):
    def __init__(self, code: str, message: str, *, retriable: bool = False,
                 status_code: int | None = None):
        self.code = code
        self.message = redact_secret(message)
        self.retriable = retriable
        self.status_code = status_code
        super().__init__(self.message)


class BudgetConfirmationRequired(TrendAnimalsError):
    def __init__(self, estimated_cost: float, approved_budget: float | None):
        self.estimated_cost = estimated_cost
        self.approved_budget = approved_budget
        super().__init__(
            "confirmation_required",
            f"预计费用 {estimated_cost:.3f} 元，超过已批准预算 "
            f"{approved_budget if approved_budget is not None else '未设置'} 元",
        )
