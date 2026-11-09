import io
from datetime import date

import pytest
from openpyxl import load_workbook

from app.extensions import db
from app.models import Tag, Transaction, User
from app.services import report_core as core
from app.services.people import HOUSEHOLD, JOINT, PersonFilter

SLUGS = [
    "overzicht",
    "cashflow",
    "categorieen",
    "per-persoon",
    "trends",
    "tags",
    "budget",
    "jaar",
    "vermogen",
    "toplijsten",
    "losse-uitgaven",
]


def add(
    ids,
    d,
    cents,
    *,
    acc="Betaal Admin",
    cat="Boodschappen",
    type="expense",
    to=None,
    payee="",
    tags=(),
):
    tx = Transaction(
        household_id=1,
        date=d,
        amount_cents=cents,
        type=type,
        account_id=ids["acc"][acc],
        to_account_id=ids["acc"][to] if to else None,
        category_id=ids["cat"][cat] if cat else None,
        description=f"{type} {cents}",
        payee=payee,
    )
    for name in tags:
        tag = db.session.query(Tag).filter_by(name=name).first() or Tag(household_id=1, name=name)
        tx.tags.append(tag)
    db.session.add(tx)


@pytest.fixture()
def data(app, ids):
    """Vaste dataset in maart 2026 (startdag 25 => financiële maand 2026-03 = 25 feb t/m 24 mrt)."""
    with app.app_context():
        # Inkomsten
        add(ids, date(2026, 3, 1), 300000, cat="Salaris", type="income")
        add(ids, date(2026, 3, 1), 200000, acc="Spaar Partner", cat="Salaris", type="income")
        # Uitgaven admin / partner / joint / privé
        add(ids, date(2026, 3, 2), 8000, cat="Boodschappen", payee="AH", tags=["weekend"])
        add(ids, date(2026, 3, 3), 4500, cat="Uit eten", payee="Pizza")
        add(ids, date(2026, 3, 4), 15000, acc="Spaar Partner", cat="Boodschappen", payee="AH")
        add(
            ids,
            date(2026, 3, 5),
            90000,
            acc="Gezamenlijk",
            cat="Huur/Hypotheek",
            payee="Woonbedrijf",
        )
        add(
            ids,
            date(2026, 3, 6),
            12345,
            acc="Prive Admin",
            cat="Kleding",
            payee="Geheime winkel",
            tags=["geheim"],
        )
        add(ids, date(2026, 3, 7), 60000, cat="Losse grote uitgaven", payee="Keukenman")
        # Overboekingen (mogen NOOIT meetellen)
        add(
            ids,
            date(2026, 3, 8),
            50000,
            cat=None,
            type="transfer",
            acc="Betaal Admin",
            to="Gezamenlijk",
        )
        add(
            ids,
            date(2026, 3, 8),
            20000,
            cat=None,
            type="transfer",
            acc="Spaar Partner",
            to="Betaal Admin",
        )
        # Correctie (uitgesloten van rapportages)
        add(ids, date(2026, 3, 9), 99999, cat="Correctie", type="expense")
        db.session.commit()


def params_for(person=HOUSEHOLD, **kw):
    start_day = 25
    today = date(2026, 3, 15)
    period = core.resolve_period("this_month", start_day, today)
    return core.ReportParams("this_month", period, "none", None, person, today=today, **kw)


def admin_user():
    return db.session.query(User).filter_by(username="admin").one()


def test_period_resolution_follows_start_day():
    today = date(2026, 3, 15)
    assert core.resolve_period("this_month", 25, today).start == date(2026, 2, 25)
    assert core.resolve_period("this_month", 25, today).end == date(2026, 3, 24)
    assert core.resolve_period("last_month", 25, today).end == date(2026, 2, 24)
    assert core.resolve_period("this_year", 25, today).start == date(2025, 12, 25)
    assert core.resolve_period("this_year", 25, today).end == date(2026, 12, 24)
    assert core.resolve_period("last_year", 1, today).start == date(2025, 1, 1)
    assert core.resolve_period("this_quarter", 1, today) == core.Period(
        date(2026, 1, 1), date(2026, 3, 31), "Dit kwartaal"
    )
    p12 = core.resolve_period("last_12m", 1, today)
    assert (p12.start, p12.end) == (date(2025, 4, 1), date(2026, 3, 31))
    custom = core.resolve_period("custom", 1, today, date(2026, 1, 5), date(2026, 1, 9))
    assert (custom.start, custom.end) == (date(2026, 1, 5), date(2026, 1, 9))
    assert (
        core.resolve_period("custom", 1, today, date(2026, 2, 1), date(2026, 1, 1)).label
        == "Deze maand"
    )


def test_comparison_periods():
    month = core.resolve_period("this_month", 25, date(2026, 3, 15))
    prev = core.comparison_period(month, "prev", 25)
    assert (prev.start, prev.end) == (date(2026, 1, 25), date(2026, 2, 24))
    yoy = core.comparison_period(month, "yoy", 25)
    assert (yoy.start, yoy.end) == (date(2025, 2, 25), date(2025, 3, 24))
    quarter = core.resolve_period("this_quarter", 1, date(2026, 5, 15))
    assert core.comparison_period(quarter, "prev", 1).start == date(2026, 1, 1)
    custom = core.Period(date(2026, 3, 10), date(2026, 3, 19))
    assert core.comparison_period(custom, "prev", 1) == core.Period(
        date(2026, 2, 28), date(2026, 3, 9)
    )
    assert core.comparison_period(month, "none", 25) is None


def test_totals_exclude_transfers_corrections(app, ids, data):
    with app.app_context():
        t = core.totals(params_for(), params_for().period, user=admin_user())
        # inkomsten 5000; uitgaven 80+45+150+900+123,45+600 (excl. correctie en overboekingen)
        assert t.income == 500000
        assert t.expense == 8000 + 4500 + 15000 + 90000 + 12345 + 60000
        assert t.net == t.income - t.expense


def test_household_equals_members_plus_joint(app, ids, data):
    with app.app_context():
        user = admin_user()
        params = params_for()
        total = core.totals(params, params.period, user=user)
        parts = [
            core.totals(params, params.period, person=p, user=user)
            for p in (
                PersonFilter(f"member:{ids['admin_member']}", "member", ids["admin_member"]),
                PersonFilter(f"member:{ids['partner_member']}", "member", ids["partner_member"]),
                JOINT,
            )
        ]
        assert sum(p.income for p in parts) == total.income
        assert sum(p.expense for p in parts) == total.expense
        owners = core.owner_totals(params, params.period, user)
        assert sum(t.expense for t in owners.values()) == total.expense


def test_one_off_toggle(app, ids, data):
    with app.app_context():
        user = admin_user()
        normal = core.totals(params_for(hide_one_off=True), params_for().period, user=user)
        assert (
            normal.expense
            == core.totals(params_for(), params_for().period, user=user).expense - 60000
        )


def test_category_breakdown_matches_raw_data(app, ids, data):
    with app.app_context():
        user = admin_user()
        by_cat = core.category_totals(params_for(), params_for().period, "expense", user=user)
        assert by_cat[ids["cat"]["Boodschappen"]] == 8000 + 15000
        tree = {n.category.name: n for n in core.category_tree(by_cat, "expense")}
        assert tree["Eten"].total == 8000 + 4500 + 15000
        assert {c.category.name: c.total for c in tree["Eten"].children} == {
            "Boodschappen": 23000,
            "Uit eten": 4500,
        }
        assert "Correctie" not in tree
        raw = (
            db.session.query(db.func.sum(Transaction.amount_cents))
            .filter(
                Transaction.type == "expense",
                Transaction.category_id.in_([ids["cat"][n] for n in ("Boodschappen", "Uit eten")]),
            )
            .scalar()
        )
        assert tree["Eten"].total == raw


def test_savings_deposits_and_contributions(app, ids, data):
    with app.app_context():
        params = params_for()
        assert core.joint_contributions(params, params.period, admin_user()) == {
            ids["admin_member"]: 50000
        }
        # Overboeking Spaar Partner -> Betaal Admin: van spaar- naar betaalrekening is geen storting
        assert core.savings_deposits(params, params.period, user=admin_user()) == 0


def test_every_report_renders_for_every_person(configured, ids, data):
    for person in (
        "household",
        "joint",
        f"member:{ids['admin_member']}",
        f"member:{ids['partner_member']}",
    ):
        configured.post("/persoon", data={"person": person})
        for slug in SLUGS:
            url = "/" if slug == "overzicht" else f"/rapportages/{slug}"
            resp = configured.get(url + "?periode=this_year&vergelijk=prev")
            assert resp.status_code == 200, (person, slug)
            assert "Traceback" not in resp.get_data(as_text=True)


def test_overview_page_contents(configured, ids, data):
    html = configured.get("/?periode=custom&van=2026-03-01&tot=2026-03-31").get_data(as_text=True)
    for needle in (
        "Spaarquote",
        "Wij samen",
        "Budgetstatus",
        "Laatste 10 transacties",
        "Te bevestigen",
        "Netto resultaat",
    ):
        assert needle in html
    assert "€ 5.000,00" in html  # inkomsten


def test_person_per_person_difference_matches_cents(app, configured, ids, data):
    html = configured.get(
        "/rapportages/per-persoon?periode=custom&van=2026-03-01&tot=2026-03-31"
    ).get_data(as_text=True)
    # admin uitgaven (excl. privé? admin ziet alles): 80+45+123,45+600 = 848,45 ; partner: 150
    assert "€ 848,45" in html and "€ 150,00" in html
    assert "€ 698,45" in html  # verschil admin − partner = 848,45 − 150,00
    assert "Bijdragen aan gezamenlijke rekeningen" in html and "€ 500,00" in html


def test_csv_export_matches_data(configured, ids, data):
    resp = configured.get(
        "/rapportages/categorieen/export/csv?periode=custom&van=2026-03-01&tot=2026-03-31"
    )
    assert resp.status_code == 200 and resp.mimetype == "text/csv"
    assert "attachment" in resp.headers["Content-Disposition"]
    text = resp.get_data(as_text=True)
    assert "Periode;01-03-2026 t/m 31-03-2026" in text
    assert "Boodschappen;230,00" in text and "Uit eten;45,00" in text
    assert "Correctie" not in text


def test_xlsx_export_is_formatted(configured, ids, data):
    resp = configured.get(
        "/rapportages/cashflow/export/xlsx?periode=custom&van=2026-03-01&tot=2026-03-31"
    )
    wb = load_workbook(io.BytesIO(resp.data))
    ws = wb.worksheets[0]
    values = [[c.value for c in row] for row in ws.iter_rows()]
    flat = [v for row in values for v in row if v is not None]
    assert any("MyMoneyManager" in str(v) for v in flat)
    assert 5000.0 in flat  # inkomsten als echt getal
    assert any(
        cell.number_format.startswith("€")
        for row in ws.iter_rows()
        for cell in row
        if cell.value == 5000.0
    )
    wb2 = load_workbook(
        io.BytesIO(configured.get("/rapportages/per-persoon/export/xlsx?periode=this_year").data)
    )
    assert len(wb2.worksheets) >= 4  # één blad per sectie


def test_pdf_export(configured, ids, data):
    for slug in ("overzicht", "per-persoon", "cashflow", "categorieen"):
        resp = configured.get(
            f"/rapportages/{slug}/export/pdf?periode=custom&van=2026-03-01&tot=2026-03-31"
        )
        assert resp.status_code == 200 and resp.data.startswith(b"%PDF"), slug
        assert len(resp.data) > 1500
    assert configured.get("/rapportages/cashflow/export/exe").status_code == 404
    assert configured.get("/rapportages/bestaat-niet").status_code == 404


def test_private_account_hidden_in_every_report_and_export(configured, partner_client, ids, data):
    """Privé-transacties mogen nergens zichtbaar zijn voor de partner; wel voor de beheerder."""
    q = "?periode=custom&van=2026-03-01&tot=2026-03-31"
    for slug in SLUGS:
        url = "/" if slug == "overzicht" else f"/rapportages/{slug}"
        partner_html = partner_client.get(url + q).get_data(as_text=True)
        assert (
            "Geheime winkel" not in partner_html
            and "123,45" not in partner_html
            and "#geheim" not in partner_html
        ), slug
        for fmt in ("csv", "xlsx", "pdf"):
            resp = partner_client.get(f"/rapportages/{slug}/export/{fmt}{q}")
            assert resp.status_code == 200
            assert b"Geheime winkel" not in resp.data and b"123,45" not in resp.data, (slug, fmt)
    admin_csv = configured.get("/rapportages/toplijsten/export/csv" + q).get_data(as_text=True)
    assert "Geheime winkel" in admin_csv
    partner_totals = partner_client.get("/rapportages/cashflow/export/csv" + q).get_data(
        as_text=True
    )
    admin_totals = configured.get("/rapportages/cashflow/export/csv" + q).get_data(as_text=True)
    assert "Totaal;5000,00;1" in partner_totals and "Totaal;5000,00;1" in admin_totals
    assert partner_totals != admin_totals  # uitgaven verschillen met precies de privé-uitgave


def test_preferences_are_remembered(app, configured):
    configured.get("/?periode=last_year&vergelijk=yoy")
    html = configured.get("/").get_data(as_text=True)
    assert '<option value="last_year" selected>' in html or 'value="last_year" selected' in html
    assert 'value="yoy" selected' in html
    with app.app_context():
        assert admin_user().preferences["report_period"] == "last_year"


def test_comparison_deltas_in_overview(app, configured, ids, data):
    with app.app_context():
        add(
            ids, date(2026, 2, 1), 10000, cat="Boodschappen"
        )  # in februari (vorige maand bij 1-3 t/m 31-3)
        db.session.commit()
    html = configured.get("/?periode=custom&van=2026-03-01&tot=2026-03-31&vergelijk=prev").get_data(
        as_text=True
    )
    assert "vs vorige periode" in html


def test_tags_report_and_combination(configured, ids, data):
    html = configured.get(
        "/rapportages/tags?periode=custom&van=2026-03-01&tot=2026-03-31"
    ).get_data(as_text=True)
    assert "#weekend" in html and "#geheim" in html  # admin ziet beide
    with configured.application.app_context():
        weekend, geheim = (
            db.session.query(Tag).filter_by(name=n).one().id for n in ("weekend", "geheim")
        )
    combo = configured.get(
        f"/rapportages/tags?periode=custom&van=2026-03-01&tot=2026-03-31&tags={weekend}&tags={geheim}"
    ).get_data(as_text=True)
    assert "Gecombineerd" in combo and "€ 203,45" in combo  # 80,00 + 123,45


def test_budget_vs_actual_heatmap(configured, ids, data):
    configured.post(
        "/budgetten/?maand=2026-03",
        data={"category_id": ids["cat"]["Boodschappen"], "amount": "250", "scope": "default"},
    )
    html = configured.get(
        "/rapportages/budget?periode=custom&van=2026-03-01&tot=2026-03-31"
    ).get_data(as_text=True)
    assert "Boodschappen" in html and "van € 250,00" in html


def test_year_matrix_totals(configured, ids, data):
    html = configured.get("/rapportages/jaar?jaar=2026").get_data(as_text=True)
    assert "Jaaroverzicht 2026" in html and "Salaris" in html


def test_one_off_report_and_toggle(configured, ids, data):
    html = configured.get(
        "/rapportages/losse-uitgaven?periode=custom&van=2026-03-01&tot=2026-03-31"
    ).get_data(as_text=True)
    assert "Keukenman" not in html and "€ 600,00" in html  # omschrijving, niet begunstigde
    off = configured.get("/?periode=custom&van=2026-03-01&tot=2026-03-31&losse=uit").get_data(
        as_text=True
    )
    on = configured.get("/?periode=custom&van=2026-03-01&tot=2026-03-31&losse=").get_data(
        as_text=True
    )
    assert off != on
