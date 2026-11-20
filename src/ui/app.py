import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import streamlit as st
import yaml

from src.core.config import ROOT, Settings


def request(method: str, path: str, **kwargs: Any) -> Any:
    base_url = f"http://127.0.0.1:{Settings().api_port}"
    response = httpx.request(method, f"{base_url}{path}", timeout=240, **kwargs)
    response.raise_for_status()
    return response.json()


def money(value: str) -> str:
    return f"{Decimal(value):,.2f} ₽".replace(",", " ")


def show_queue() -> None:
    prefix = "/api/v1/reviver"
    stage_codes = list(yaml.safe_load((ROOT / "config/stage_sla.yaml").read_text(encoding="utf-8")))
    stage_names = {"PROPOSAL": "КП", "CONTRACT": "Договор", "QUALIFICATION": "Квалификация"}
    with st.form("scan"):
        stages = st.multiselect(
            "Стадии", stage_codes, format_func=lambda stage: stage_names.get(stage, stage),
        )
        limit = st.number_input("Сделок в пачке", min_value=1, max_value=100, value=12)
        scanned = st.form_submit_button("Сканировать воронку", type="primary")
    if scanned:
        payload = {"stages": sorted(stages), "limit": int(limit)}
        if st.session_state.get("scan_payload") != payload or "scan_key" not in st.session_state:
            st.session_state.scan_payload = payload
            st.session_state.scan_key = uuid4().hex
        result = request("POST", f"{prefix}/scan", json=payload,
                         headers={"Idempotency-Key": st.session_state.scan_key})
        del st.session_state.scan_key
        st.session_state.last_scan = result["decisions"]
    if "last_scan" in st.session_state:
        decisions = st.session_state.last_scan
        st.caption(f"Сделок в последней пачке · {len(decisions)}")
        if decisions:
            st.dataframe([
                {"Сделка": item["deal_title"], "Причина пропуска": item["skip_reason"] or "—",
                 "Риск": money(item["at_risk_revenue_rub"])} for item in decisions
            ], hide_index=True)
    queue = request("GET", f"{prefix}/queue")
    st.subheader("Очередь реанимации")
    if not queue:
        st.info("Очередь пуста. Запустите сканирование воронки.")
    settings = Settings()
    approval_id = int(st.number_input("ID одобряющего менеджера", min_value=1, step=1)) if queue else None
    token = ""
    if queue and (settings.bitrix_mode == "live" or settings.bitrix_approver_tokens):
        token = st.text_input("Токен подтверждения", type="password")
    headers = {"X-Approval-Token": token} if token else {}
    statuses = {"pending_approval": "На согласовании", "approved": "Одобрено", "rejected": "Отклонено"}
    for item in queue:
        identifier = item["revival_id"]
        status = item["approval_status"]
        with st.expander(f"{item['deal_title']} · {statuses[status]}", expanded=status == "pending_approval"):
            st.caption(f"Сделка №{item['deal_id']} · {item['assigned_by_name']}")
            st.write(item["stall_summary"])
            risk, forecast = st.columns(2)
            risk.metric("Выручка под риском", money(item["at_risk_revenue_rub"]))
            forecast.metric("Прогноз восстановления", money(item["expected_recovered_rub"]))
            st.text(item["draft_message"])
            st.caption(item["next_best_action"])
            if item["bitrix_sync_status"] == "pending":
                st.caption("Ожидает синхронизации с CRM")
            if status == "pending_approval":
                yes, no = st.columns(2)
                if yes.button("Одобрить и поставить в работу", key=f"yes-{identifier}", type="primary"):
                    request(
                        "POST", f"{prefix}/approve/{identifier}",
                        json={"approve": True, "approver_user_id": approval_id}, headers=headers,
                    )
                    st.rerun()
                if no.button("Отклонить", key=f"no-{identifier}"):
                    request(
                        "POST", f"{prefix}/approve/{identifier}",
                        json={"approve": False, "approver_user_id": approval_id}, headers=headers,
                    )
                    st.rerun()
    if st.button("Повторить незавершённую синхронизацию"):
        request("POST", f"{prefix}/reconcile")
        st.rerun()


def show_metrics() -> None:
    metrics = request("GET", "/api/v1/metrics")
    risk, forecast, approval = st.columns(3)
    risk.metric("Выручка под риском", money(metrics["at_risk_revenue_rub"]))
    forecast.metric("Прогноз восстановления", money(metrics["expected_recovered_rub"]))
    approval.metric("Одобрено", f"{Decimal(metrics['approval_rate']):.1%}")
    latency, cost, queue = st.columns(3)
    latency.metric("Средняя задержка решения", f"{metrics['average_latency_ms']} мс")
    cost.metric("Расходы на LLM", money(metrics["cost_rub"]))
    queue.metric("Ожидают решения", metrics["queue_size"])
    if not metrics["cost_complete"]:
        st.warning("Часть расходов на LLM не подтверждена провайдером.")


    report_path = ROOT / "docs/benchmark_ab_report.json"
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        st.subheader("Последний A/B-бенчмарк")
        st.caption(f"LLM · {report['mode']} · синтетические сделки и исходы")
        st.dataframe([
            {
                "Вариант": name, "Сделок": row["cases"],
                "Точность отбора": f"{row['eligibility_accuracy']:.1%}",
                "Точность причин": f"{row['stall_reason_accuracy']:.1%}",
                "Нарушения": row["unauthorized_offers"],
                "p50, мс": row["latency_p50_ms"], "p95, мс": row["latency_p95_ms"],
                "Средняя стоимость, ₽": row["avg_cost_rub"],
            }
            for name, row in report["variants"].items()
        ], hide_index=True)


def main() -> None:
    st.set_page_config(page_title="AI Deal Reviver", layout="wide")
    st.title("AI Deal Reviver")
    st.caption("Bitrix24 · Реанимация зависших сделок")
    demo, roi = st.tabs(["Live Demo & Queue", "Executive ROI-дашборд"])
    with demo:
        try:
            show_queue()
        except httpx.HTTPError:
            st.error("Не удалось выполнить запрос к API. Проверьте подключение и повторите действие.")
    with roi:
        try:
            show_metrics()
        except httpx.HTTPError:
            st.error("Метрики временно недоступны.")


if __name__ == "__main__":
    main()
