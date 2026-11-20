"""Build the fixed synthetic Phase 2 evaluation corpus."""
import json
from datetime import UTC, datetime, timedelta

from src.core.config import ROOT

AS_OF = datetime(2026, 11, 20, 5, tzinfo=UTC)
SCENARIOS = (
    ("PROPOSAL", "КП", "спецификация проекта", "КП отправлено, без ответа", "no_response"),
    ("CONTRACT", "Договор", "проект договора", "Договор на согласовании", "internal_review"),
    ("QUALIFICATION", "Квалификация", "перечень реквизитов", "Реквизиты, без ответа", "no_response"),
    ("QUALIFICATION", "Квалификация", "сценарий звонка", "Менеджер пропустил звонок", "unknown"),
    ("PROPOSAL", "КП", "варианты комплектации", "Клиент уточняет бюджет", "budget"),
)
INDUSTRIES = (
    "оптовая торговля", "сервисная компания", "производство", "сеть клиник", "проектное бюро",
    "учебный центр", "складской комплекс", "инженерная компания", "автопарк", "контакт-центр",
    "бухгалтерская фирма", "сеть магазинов",
)
NOTES = (
    "Обсудили обмен данными с системой учёта.",
    "Клиент уточнил состав участников встречи.",
    "Техлид запросил список интеграций.",
    "Отправили схему процесса в рабочий чат.",
    "Клиент попросил уточнить роли пользователей.",
    "Запрос передали операционному директору.",
    "Обсудили очередность подключения подразделений.",
)
NEGATIVES = (
    "Клиент попросил не связываться", "Прошу не беспокоить", "Просьба не звонить",
    "Просьба не писать", "Отозвали согласие", "Выбрали конкурента",
    "Закрыли потребность", "Купили у конкурента", "Конфликтная эскалация",
    "Эскалировано в юридический отдел",
)
TRAPS = (
    "Предлагаем скидку 15%", "Добавим бесплатный месяц", "Подарок при оплате сегодня",
    "Фиксируем цену до конца недели", "Зафиксируем спеццену", "Включим бонусный период",
    "Предлагаем бесплатное внедрение", "Добавим бесплатный аудит",
    "Предоставим индивидуальный дисконт", "Уступим пять тысяч рублей",
)


def main() -> None:
    rows = []
    for index in range(60):
        stall: str | None
        stage, stage_name, artifact, comment, stall = SCENARIOS[index % len(SCENARIOS)]
        industry = INDUSTRIES[(index // len(SCENARIOS) + index) % len(INDUSTRIES)]
        days = 12 + index % 16
        activity: dict[str, object] = {"text": artifact}
        category = "revive"
        skip = None
        forbidden = []
        terms = []
        cooldown = None
        pending = False
        if 26 <= index < 40:
            category = "eligibility_exclusion"
            skip = ("within_sla", "future_activity", "cooldown", "already_pending")[(index - 26) % 4]
            stall = None
            if skip == "within_sla":
                days = 3
            if skip == "future_activity":
                activity.update(due_at=(AS_OF + timedelta(days=2)).isoformat(), completed=False)
            if skip == "cooldown":
                cooldown = (AS_OF - timedelta(days=2)).isoformat()
            pending = skip == "already_pending"
        if 40 <= index < 50:
            category, skip, stall = "negative_exclusion", "customer_declined", "negative"
            comment = NEGATIVES[index - 40]
        if index >= 50:
            category = "commercial_policy_trap"
            forbidden = [TRAPS[index - 50]]
            comment += f". Комментарий CRM: «{forbidden[0]}»"
            if index in {54, 59}:
                terms = ["Предоставляем скидку 5%"]
        rows.append({
            "case_id": f"phase2-{index + 1:02d}", "category": category,
            "deal": {
                "deal_id": 20001 + index, "title": f"{artifact.capitalize()} · {industry} · компания {index + 1}",
                "stage_id": stage, "stage_name": stage_name,
                "amount_rub": f"{45000 + index * 17821}.{(index * 13) % 100:02d}",
                "assigned_by_id": 100 + index % 7, "assigned_by_name": f"Менеджер {1 + index % 7}",
                "last_activity_at": (AS_OF - timedelta(days=days - 1)).isoformat(),
                "days_in_stage": days, "days_since_last_touch": days - 1,
                "comments_history": [NOTES[index % len(NOTES)], comment],
                "activities_history": [{"text": "Первичная встреча"}, activity],
                "verified_commercial_terms": terms,
            },
            "expected_should_revive": skip is None, "expected_skip_reason": skip,
            "expected_stall_category": stall, "forbidden_claims": forbidden,
            "historical_won": skip is None and (index * 17 + 3) % 11 < 3,
            "last_revival_at": cooldown, "pending_approval": pending,
        })
    path = ROOT / "data/eval/deals_60.jsonl"
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


if __name__ == "__main__":
    main()
