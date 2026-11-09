import json
from datetime import date

from app.extensions import db
from app.models import RecurringRule, Transaction
from app.services import recurring as svc


def make_rule(ids, **over):
    data = {
        "household_id": 1,
        "name": "Huur",
        "type": "expense",
        "amount_cents": 85000,
        "account_id": ids["acc"]["Gezamenlijk"],
        "category_id": ids["cat"]["Huur/Hypotheek"],
        "frequency": "monthly",
        "interval": 1,
        "next_date": date(2025, 1, 31),
        "anchor_day": 31,
        "auto_create": True,
    }
    data.update(over)
    rule = RecurringRule(**data)
    db.session.add(rule)
    db.session.commit()
    return rule


def test_advance_month_end_keeps_anchor():
    d = date(2025, 1, 31)
    d = svc.advance(d, "monthly", 1, 31)
    assert d == date(2025, 2, 28)
    d = svc.advance(d, "monthly", 1, 31)
    assert d == date(2025, 3, 31)
    assert svc.advance(date(2024, 2, 29), "yearly", 1, 29) == date(2025, 2, 28)
    assert svc.advance(date(2025, 11, 30), "quarterly", 1, 30) == date(2026, 2, 28)
    assert svc.advance(date(2025, 3, 3), "weekly", 2) == date(2025, 3, 17)
    assert svc.advance(date(2025, 1, 15), "monthly", 3, 15) == date(2025, 4, 15)


def test_generate_due_creates_missed_terms_once(app, ids):
    with app.app_context():
        rule = make_rule(ids)
        assert svc.generate_due(date(2025, 4, 15)) == 3  # 31 jan, 28 feb, 31 mrt
        assert rule.next_date == date(2025, 4, 30)
        dates = [t.date for t in db.session.query(Transaction).order_by(Transaction.date)]
        assert dates == [date(2025, 1, 31), date(2025, 2, 28), date(2025, 3, 31)]
        tx = db.session.query(Transaction).first()
        assert (
            tx.recurring_rule_id == rule.id
            and tx.amount_cents == 85000
            and tx.description == "Huur"
        )
        assert svc.generate_due(date(2025, 4, 15)) == 0  # idempotent


def test_end_date_deactivates_rule(app, ids):
    with app.app_context():
        rule = make_rule(
            ids, next_date=date(2025, 1, 15), anchor_day=15, end_date=date(2025, 2, 20)
        )
        assert svc.generate_due(date(2025, 6, 1)) == 2
        assert rule.is_active is False


def test_manual_rules_wait_for_confirmation(app, ids):
    with app.app_context():
        rule = make_rule(ids, auto_create=False, next_date=date(2025, 3, 1), anchor_day=1)
        assert svc.generate_due(date(2025, 3, 5)) == 0
        from app.models import User

        admin = db.session.query(User).filter_by(username="admin").one()
        pending = svc.pending_confirmations(date(2025, 3, 5), user=admin)
        assert [(p.rule.id, p.due) for p in pending] == [(rule.id, date(2025, 3, 1))]
        tx = svc.confirm(rule, None, amount_cents=86000)
        assert tx.amount_cents == 86000 and tx.date == date(2025, 3, 1)
        assert rule.amount_cents == 85000 and rule.next_date == date(2025, 4, 1)
        assert svc.pending_confirmations(date(2025, 3, 5), user=admin) == []
        svc.skip(rule)
        assert rule.next_date == date(2025, 5, 1) and db.session.query(Transaction).count() == 1


def test_upcoming_30_days(app, configured, ids):
    from app.models import User

    with app.app_context():
        make_rule(
            ids,
            name="Wekelijks",
            frequency="weekly",
            next_date=date(2025, 3, 3),
            anchor_day=3,
            amount_cents=1000,
        )
        make_rule(ids, name="Maand", next_date=date(2025, 3, 20), anchor_day=20, amount_cents=5000)
        make_rule(
            ids,
            name="Salaris",
            type="income",
            next_date=date(2025, 3, 25),
            anchor_day=25,
            amount_cents=300000,
            account_id=ids["acc"]["Betaal Admin"],
            category_id=ids["cat"]["Salaris"],
        )
        admin = db.session.query(User).filter_by(username="admin").one()
        items = svc.upcoming(30, today=date(2025, 3, 1), user=admin)
        names = [(i.due, i.rule.name) for i in items]
        assert names == [
            (date(2025, 3, 3), "Wekelijks"),
            (date(2025, 3, 10), "Wekelijks"),
            (date(2025, 3, 17), "Wekelijks"),
            (date(2025, 3, 20), "Maand"),
            (date(2025, 3, 24), "Wekelijks"),
            (date(2025, 3, 25), "Salaris"),
            (date(2025, 3, 31), "Wekelijks"),
        ]


def test_rule_routes_and_page(app, configured, ids):
    form = {
        "type": "expense",
        "name": "Netflix",
        "amount": "13,99",
        "date": date.today().isoformat(),
        "account_id": ids["acc"]["Betaal Admin"],
        "category_id": ids["cat"]["Streaming"],
        "frequency": "monthly",
        "interval": "1",
        "tags": "abo",
    }
    assert configured.post("/vaste-lasten/", data=form).status_code == 204
    bad = configured.post(
        "/vaste-lasten/", data={**form, "name": "", "frequency": "dagelijks", "interval": "0"}
    )
    assert bad.status_code == 422
    html = configured.get("/vaste-lasten/").get_data(as_text=True)
    assert "Netflix" in html and "Te bevestigen" in html and "Komende 30 dagen" in html
    with app.app_context():
        rule = db.session.query(RecurringRule).one()
        assert rule.anchor_day == date.today().day and rule.auto_create is False
        rule_id = rule.id
    # vandaag vervalt → verschijnt als te bevestigen en kan worden bevestigd
    assert "Bevestig" in html
    resp = configured.post(f"/vaste-lasten/{rule_id}/bevestig")
    assert json.loads(resp.headers["HX-Trigger"])["toast"]["message"] == "Transactie aangemaakt."
    with app.app_context():
        assert db.session.query(Transaction).one().recurring_rule_id == rule_id
    again = configured.post(f"/vaste-lasten/{rule_id}/bevestig")
    assert json.loads(again.headers["HX-Trigger"])["toast"]["category"] == "error"
    assert configured.post(f"/vaste-lasten/{rule_id}/actief").status_code == 204
    assert configured.post(f"/vaste-lasten/{rule_id}/verwijder").status_code == 204


def test_rule_on_private_account_hidden_from_partner(app, configured, partner_client, ids):
    with app.app_context():
        rule = make_rule(
            ids, name="Geheim abonnement", account_id=ids["acc"]["Prive Admin"], auto_create=False
        )
        rule_id = rule.id
    assert "Geheim abonnement" not in partner_client.get("/vaste-lasten/").get_data(as_text=True)
    assert partner_client.get(f"/vaste-lasten/{rule_id}/bewerken").status_code == 404
    assert partner_client.post(f"/vaste-lasten/{rule_id}/verwijder").status_code == 404
    assert "Geheim abonnement" in configured.get("/vaste-lasten/").get_data(as_text=True)
    resp = partner_client.post(
        "/vaste-lasten/",
        data={
            "type": "expense",
            "name": "x",
            "amount": "1",
            "date": "2025-01-01",
            "account_id": ids["acc"]["Prive Admin"],
            "category_id": ids["cat"]["Streaming"],
            "frequency": "monthly",
            "interval": "1",
        },
    )
    assert resp.status_code == 422
