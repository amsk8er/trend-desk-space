"""Gmail SMTP delivery with database-backed idempotency and audit."""

from __future__ import annotations

import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import make_msgid

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from backend import config
from backend.db import EmailDelivery


def email_config_status() -> dict:
    return {
        "configured": bool(config.EMAIL_FROM and config.EMAIL_TO and config.GMAIL_APP_PASSWORD),
        "sender": config.EMAIL_FROM,
        "recipient": config.EMAIL_TO,
        "provider": "gmail_smtp",
    }


def _send_smtp(subject: str, text_body: str, html_body: str | None = None) -> str:
    if not email_config_status()["configured"]:
        raise RuntimeError("gmail_not_configured")
    message = EmailMessage()
    message["From"] = config.EMAIL_FROM
    message["To"] = config.EMAIL_TO
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain="trend-desk.local")
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(config.EMAIL_FROM, config.GMAIL_APP_PASSWORD.replace(" ", ""))
        smtp.send_message(message)
    return str(message["Message-ID"])


def deliver_email(
    session: Session,
    *,
    trade_date: str,
    kind: str,
    idempotency_key: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
    plan_id: str | None = None,
) -> dict:
    existing = session.exec(select(EmailDelivery).where(
        EmailDelivery.idempotency_key == idempotency_key)).first()
    if existing and existing.status == "sent":
        return {**existing.model_dump(mode="json"), "reused": True}
    if existing is None:
        existing = EmailDelivery(
            trade_date=trade_date,
            plan_id=plan_id,
            recipient=config.EMAIL_TO,
            kind=kind,
            idempotency_key=idempotency_key,
            status="pending",
        )
        session.add(existing)
        try:
            session.commit()
            session.refresh(existing)
        except IntegrityError:
            session.rollback()
            existing = session.exec(select(EmailDelivery).where(
                EmailDelivery.idempotency_key == idempotency_key)).one()
            if existing.status == "sent":
                return {**existing.model_dump(mode="json"), "reused": True}
    existing.attempts += 1
    existing.error = None
    session.add(existing)
    session.commit()
    try:
        message_id = _send_smtp(subject, text_body, html_body)
    except Exception as exc:
        existing.status = "failed"
        existing.error = f"{type(exc).__name__}: {exc}"[:1000]
        session.add(existing)
        session.commit()
        raise
    existing.status = "sent"
    existing.message_id = message_id
    existing.sent_at = datetime.utcnow()
    session.add(existing)
    session.commit()
    session.refresh(existing)
    return existing.model_dump(mode="json")


def render_reminder(trade_date: str, public_url: str) -> tuple[str, str]:
    subject = f"[Trend Desk] {trade_date} 请确认今日成交"
    body = (
        f"{trade_date} 的成交台账尚未确认。\n\n"
        "请打开交易台，上传当日成交清单截图；如果今天没有成交，请点击“今日无成交”。\n"
        f"{public_url}\n\n"
        "19:30 前未确认时，系统不会猜测持仓或买卖数量。"
    )
    return subject, body


def render_blocked(trade_date: str, errors: list[str], public_url: str) -> tuple[str, str]:
    subject = f"[Trend Desk][待处理] {trade_date} 明日清单未生成"
    body = (
        f"{trade_date} 的明日清单未生成，系统没有输出任何猜测数量。\n\n"
        "需要处理：\n- " + "\n- ".join(errors) + "\n\n"
        f"处理入口：{public_url}"
    )
    return subject, body


def _action_label(side: str) -> str:
    return {
        "sell_all": "卖出",
        "reduce": "减仓",
        "buy": "买入",
        "hold": "继续持有",
    }.get(side, side)


def render_action_list(plan: dict, *, shadow: bool = False) -> tuple[str, str]:
    prefix = "[Trend Desk][影子]" if shadow else "[Trend Desk]"
    subject = f"{prefix} {plan['execute_date']} 明日行动清单"
    groups = {"sell_all": [], "reduce": [], "buy": [], "hold": []}
    for item in plan.get("items") or []:
        groups.setdefault(item["side"], []).append(item)
    lines = [
        f"信号日：{plan['signal_date']}",
        f"执行日：{plan['execute_date']}",
        f"纪律版本：{plan['discipline_version']}",
        f"计划 ID：{plan['plan_id']}",
    ]
    if shadow:
        lines += ["", "当前处于影子验证期：本邮件用于核对，不代表系统已自动锁定。"]
    action_count = 0
    total_reserved_fees = 0.0
    for side in ("sell_all", "reduce", "buy", "hold"):
        rows = groups.get(side) or []
        if not rows:
            continue
        lines += ["", f"【{_action_label(side)}】"]
        for item in rows:
            if side == "hold":
                lines.append(
                    f"- {item['instrument_id']} {item['name']}：继续持有"
                )
                continue
            shares = int(item.get("target_shares") or 0)
            lots, odd = divmod(shares, 100)
            quantity = f"{shares} 股（{lots} 手"
            if odd:
                quantity += f" + 零股 {odd} 股"
            quantity += "）"
            lines.append(
                f"- {item['instrument_id']} {item['name']}：{quantity}"
            )
            cash_evidence = (item.get("rule_evidence") or {}).get("cash") or {}
            if cash_evidence:
                gross = float(cash_evidence.get("estimated_gross") or 0)
                fee = float(cash_evidence.get("estimated_fee") or 0)
                total_reserved_fees += fee
                lines.append(f"  预计成交 ¥{gross:,.2f}；费用预留 ¥{fee:,.2f}")
            if side != "hold":
                action_count += 1
    if action_count == 0:
        lines += ["", "明日无交易。"]
    account = plan.get("account") or {}
    lines += [
        "",
        "【账户】",
        f"可用现金：¥{float(account.get('cash') or 0):,.2f}",
        f"账户净值：¥{float(account.get('nav') or 0):,.2f}",
        f"数据截止：{account.get('as_of_date') or '—'}",
        f"计划费用预留：¥{total_reserved_fees:,.2f}",
        "",
        f"数据健康：{'通过' if (plan.get('data_health') or {}).get('lockable') else '未通过'}",
    ]
    return subject, "\n".join(lines)
