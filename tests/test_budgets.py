from datetime import date

import pytest

from app.extensions import db
from app.models import Budget, Transaction, User
from app.services import budgets as svc
from app.services.people import HOUSEHOLD, JOINT, PersonFilter
from tests.conftest import tx_form


def lines_for_admin(ym, person, today, user=None):
    """Buiten een request is er geen ingelogde gebruiker: gebruik standaard de admin."""
    admin = user or db.session.query(User).filter_by(username="admin").one()
    return svc.budget_lines(ym, person, user=admin, today=today)


def add_tx(ids, d, cents, *, account="Betaal Admin", category="Boodschappen", type="expense"):
    db.session.add(
        Transaction(
            household_id=1,
            date=d,
            amount_cents=cents,
            type=type,
            account_id=ids["acc"][account],
            category_id=ids["cat"][category],
            description="t",
        )
    )


def add_budget(ids, category, cents, *, month=None, member_id=None, rollover=False):
    db.session.add(
        Budget(
            household_id=1,
            category_id=ids["cat"][category],
            member_id=member_id,
            month=month,
            amount_cents=cents,
            rollover=rollover,
        )
    )


def line_for(lines, ids, category):
    return next(line for line in lines if line.category.id == ids["cat"][category])


def test_financial_month_boundaries(app, ids):
    """Startdag 25: 24 maart hoort bij maart, 25 maart bij april."""
    with app.app_context():
        add_budget(ids, "Boodschappen", 50000)
        add_tx(ids, date(2025, 3, 24), 1000)
        add_tx(ids, date(2025, 3, 25), 2500)
        db.session.commit()
        march = line_for(
            lines_for_admin("2025-03", HOUSEHOLD, date(2025, 3, 10)), ids, "Boodschappen"
        )
        april = line_for(
            lines_for_admin("2025-04", HOUSEHOLD, date(2025, 3, 30)), ids, "Boodschappen"
        )
        assert march.spent_cents == 1000
        assert april.spent_cents == 2500


@pytest.mark.parametrize(
    ("spent", "state", "pct"),
    [(7900, "ok", 79), (8000, "warn", 80), (10000, "warn", 100), (10100, "over", 101)],
)
def test_color_states(app, ids, spent, state, pct):
    with app.app_context():
        add_budget(ids, "Boodschappen", 10000)
        add_tx(ids, date(2025, 3, 10), spent)
        db.session.commit()
        line = line_for(
            lines_for_admin("2025-03", HOUSEHOLD, date(2025, 3, 10)), ids, "Boodschappen"
        )
        assert (line.state, line.pct) == (state, pct)


def test_remaining_and_per_day(app, ids):
    with app.app_context():
        add_budget(ids, "Boodschappen", 30000)
        add_tx(ids, date(2025, 3, 10), 10000)
        db.session.commit()
        # periode 25 feb t/m 24 mrt; op 15 mrt resten 10 dagen (15..24)
        line = line_for(
            lines_for_admin("2025-03", HOUSEHOLD, date(2025, 3, 15)), ids, "Boodschappen"
        )
        assert line.remaining_cents == 20000
        assert line.per_day_cents == 2000
        past = line_for(
            lines_for_admin("2025-03", HOUSEHOLD, date(2025, 5, 1)), ids, "Boodschappen"
        )
        assert past.per_day_cents is None


def test_parent_budget_includes_subcategories(app, ids):
    with app.app_context():
        add_budget(ids, "Eten", 20000)
        add_tx(ids, date(2025, 3, 5), 3000, category="Boodschappen")
        add_tx(ids, date(2025, 3, 6), 4000, category="Uit eten")
        db.session.commit()
        assert (
            line_for(
                lines_for_admin("2025-03", HOUSEHOLD, date(2025, 3, 10)), ids, "Eten"
            ).spent_cents
            == 7000
        )


def test_month_override_beats_default(app, ids):
    with app.app_context():
        add_budget(ids, "Boodschappen", 10000)
        add_budget(ids, "Boodschappen", 25000, month="2025-04")
        db.session.commit()
        t = date(2025, 3, 1)
        assert (
            line_for(lines_for_admin("2025-03", HOUSEHOLD, t), ids, "Boodschappen").base_cents
            == 10000
        )
        assert (
            line_for(lines_for_admin("2025-04", HOUSEHOLD, t), ids, "Boodschappen").base_cents
            == 25000
        )


@pytest.mark.parametrize(
    ("march_spent", "expected_april"), [(6000, 14000), (13000, 7000), (10000, 10000)]
)
def test_rollover(app, ids, march_spent, expected_april):
    with app.app_context():
        add_budget(ids, "Boodschappen", 10000, rollover=True)
        add_tx(ids, date(2025, 3, 10), march_spent)
        db.session.commit()
        april = line_for(
            lines_for_admin("2025-04", HOUSEHOLD, date(2025, 4, 1)), ids, "Boodschappen"
        )
        assert april.available_cents == expected_april


def test_rollover_chains_over_months_and_is_off_by_default(app, ids):
    with app.app_context():
        add_budget(ids, "Boodschappen", 10000, rollover=True)
        add_budget(ids, "Kleding", 10000, rollover=False)
        add_tx(ids, date(2025, 2, 1), 2000)  # februari (24 jan–24 feb): resten 8000
        add_tx(ids, date(2025, 3, 1), 12000)  # maart: beschikbaar 18000, besteed 12000 => 6000 mee
        add_tx(ids, date(2025, 3, 1), 2000, category="Kleding")
        db.session.commit()
        lines = lines_for_admin("2025-04", HOUSEHOLD, date(2025, 4, 1))
        assert line_for(lines, ids, "Boodschappen").available_cents == 16000
        assert line_for(lines, ids, "Kleding").available_cents == 10000


def test_member_scoped_budget_and_person_filter(app, ids):
    with app.app_context():
        add_budget(ids, "Boodschappen", 10000, member_id=ids["partner_member"])
        add_budget(ids, "Boodschappen", 50000)  # huishouden
        add_tx(ids, date(2025, 3, 5), 1500, account="Betaal Admin")
        add_tx(ids, date(2025, 3, 6), 2500, account="Spaar Partner")
        add_tx(ids, date(2025, 3, 7), 4000, account="Gezamenlijk")
        db.session.commit()
        today = date(2025, 3, 10)
        household = lines_for_admin("2025-03", HOUSEHOLD, today)
        assert [(line.budget.member_id, line.spent_cents) for line in household] == [(None, 8000)]
        partner = lines_for_admin(
            "2025-03",
            PersonFilter(f"member:{ids['partner_member']}", "member", ids["partner_member"]),
            today,
        )
        assert [(line.budget.member_id, line.spent_cents) for line in partner] == [
            (ids["partner_member"], 2500)
        ]
        joint = lines_for_admin("2025-03", JOINT, today)
        assert [line.spent_cents for line in joint] == [4000]


def test_transfers_and_corrections_never_count(app, ids):
    with app.app_context():
        add_budget(ids, "Boodschappen", 10000)
        db.session.add(
            Transaction(
                household_id=1,
                date=date(2025, 3, 5),
                amount_cents=5000,
                type="transfer",
                account_id=ids["acc"]["Betaal Admin"],
                to_account_id=ids["acc"]["Gezamenlijk"],
                description="x",
            )
        )
        db.session.commit()
        assert (
            line_for(
                lines_for_admin("2025-03", HOUSEHOLD, date(2025, 3, 10)), ids, "Boodschappen"
            ).spent_cents
            == 0
        )


def test_private_spending_hidden_from_other_member(app, ids, partner_client):
    with app.app_context():
        add_budget(ids, "Boodschappen", 10000)
        add_tx(ids, date(2025, 3, 5), 3000, account="Betaal Admin")
        add_tx(ids, date(2025, 3, 6), 7000, account="Prive Admin")
        db.session.commit()
        partner = db.session.query(User).filter_by(username="partner").one()
        admin = db.session.query(User).filter_by(username="admin").one()
        today = date(2025, 3, 10)
        assert (
            line_for(
                svc.budget_lines("2025-03", HOUSEHOLD, user=admin, today=today), ids, "Boodschappen"
            ).spent_cents
            == 10000
        )
        assert (
            line_for(
                svc.budget_lines("2025-03", HOUSEHOLD, user=partner, today=today),
                ids,
                "Boodschappen",
            ).spent_cents
            == 3000
        )


def test_copy_previous_month(app, ids):
    with app.app_context():
        add_budget(ids, "Boodschappen", 10000, month="2025-03", rollover=True)
        add_budget(ids, "Kleding", 5000)  # standaard
        add_budget(ids, "Kleding", 9999, month="2025-04")  # bestaande override blijft
        db.session.commit()
        created = svc.copy_previous_month("2025-04")
        assert created == 1
        copied = (
            db.session.query(Budget)
            .filter_by(category_id=ids["cat"]["Boodschappen"], month="2025-04")
            .one()
        )
        assert (copied.amount_cents, copied.rollover) == (10000, True)
        assert (
            db.session.query(Budget)
            .filter_by(category_id=ids["cat"]["Kleding"], month="2025-04")
            .one()
            .amount_cents
            == 9999
        )


def test_budget_routes(app, configured, ids):
    form = {
        "category_id": ids["cat"]["Boodschappen"],
        "amount": "250,00",
        "scope": "default",
        "rollover": "1",
    }
    assert configured.post("/budgetten/?maand=2025-03", data=form).status_code == 204
    dup = configured.post("/budgetten/?maand=2025-03", data=form)
    assert dup.status_code == 422 and "bestaat al" in dup.get_data(as_text=True)
    assert (
        configured.post("/budgetten/", data={**form, "amount": "x", "category_id": ""}).status_code
        == 422
    )
    configured.post("/transacties/", data=tx_form(ids, date="10-03-2025", amount="210"))
    html = configured.get("/budgetten/?maand=2025-03").get_data(as_text=True)
    assert (
        "Boodschappen" in html
        and "€ 210,00" in html
        and "€ 250,00" in html
        and "bg-amber-500" in html  # 84%: oranje
    )
    with app.app_context():
        budget_id = db.session.query(Budget).one().id
    assert (
        configured.post(f"/budgetten/{budget_id}", data={**form, "amount": "200"}).status_code
        == 204
    )
    assert "bg-red-500" in configured.get("/budgetten/?maand=2025-03").get_data(as_text=True)
    assert configured.post(f"/budgetten/{budget_id}/verwijder").status_code == 204
    assert "Nog geen budgetten" in configured.get("/budgetten/?maand=2025-03").get_data(
        as_text=True
    )
    assert configured.post("/budgetten/kopieer", data={"maand": "kapot"}).status_code == 400
