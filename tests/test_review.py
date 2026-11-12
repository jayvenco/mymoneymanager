import json
from datetime import date, timedelta

from app.extensions import db
from app.models import Transaction
from app.services import transactions as svc
from tests.conftest import tx_form


def create(client, ids, **over):
    resp = client.post("/transacties/", data=tx_form(ids, **over))
    assert resp.status_code == 204, resp.get_data(as_text=True)
    with client.application.app_context():
        return db.session.query(Transaction).order_by(Transaction.id.desc()).first().id


def old(days):
    return (date.today() - timedelta(days=days)).strftime("%d-%m-%Y")


def test_checkbox_saves_6_or_12_months(app, configured, ids):
    a = create(configured, ids, recurring_review="1", review_months="6", description="Netflix")
    b = create(configured, ids, recurring_review="1", review_months="12", description="Verzekering")
    c = create(configured, ids, description="Gewoon")  # geen vinkje
    with app.app_context():
        assert [db.session.get(Transaction, i).review_months for i in (a, b, c)] == [6, 12, None]


def test_validation_and_only_for_expenses(app, configured, ids):
    bad = configured.post(
        "/transacties/", data=tx_form(ids, recurring_review="1", review_months="3")
    )
    assert bad.status_code == 422 and "6 of 12 maanden" in bad.get_data(as_text=True)
    assert (
        configured.post("/transacties/", data=tx_form(ids, recurring_review="1")).status_code == 422
    )
    # bij inkomsten wordt het vinkje genegeerd
    inc = create(
        configured,
        ids,
        type="income",
        category_id=ids["cat"]["Salaris"],
        recurring_review="1",
        review_months="6",
    )
    with app.app_context():
        assert db.session.get(Transaction, inc).review_months is None


def test_form_shows_checkbox_and_keeps_state_on_edit(app, configured, ids):
    form = configured.get("/transacties/nieuw").get_data(as_text=True)
    assert 'name="recurring_review"' in form and "6 maanden" in form and "12 maanden" in form
    tx_id = create(configured, ids, recurring_review="1", review_months="6")
    edit = configured.get(f"/transacties/{tx_id}/bewerken").get_data(as_text=True)
    assert (
        "checked" in edit.split('name="recurring_review"')[1][:220]
        and '<option value="6" selected>' in edit
    )
    # inline bewerken mag de instelling niet kwijtraken
    configured.post(
        f"/transacties/{tx_id}/inline",
        data={
            "amount": "20",
            "description": "Nieuw",
            "date": "2025-03-16",
            "category_id": ids["cat"]["Boodschappen"],
        },
    )
    with app.app_context():
        assert db.session.get(Transaction, tx_id).review_months == 6
    # uitzetten via het formulier
    configured.post(f"/transacties/{tx_id}", data=tx_form(ids))
    with app.app_context():
        assert db.session.get(Transaction, tx_id).review_months is None


def test_due_dates_and_marking_reviewed(app, configured, ids):
    from app.models import User

    create(
        configured,
        ids,
        date=old(200),
        recurring_review="1",
        review_months="6",
        description="Te laat",
    )
    create(
        configured,
        ids,
        date=old(10),
        recurring_review="1",
        review_months="12",
        description="Nog ver",
    )
    soon = create(
        configured,
        ids,
        date=old(160),
        recurring_review="1",
        review_months="6",
        description="Binnenkort",
    )
    with app.app_context():
        admin = db.session.query(User).filter_by(username="admin").one()
        items = svc.reviews(admin)
        assert [i.tx.description for i in items] == ["Te laat", "Binnenkort", "Nog ver"]
        assert items[0].overdue and not items[1].overdue
        within = svc.reviews(admin, within_days=45)
        assert [i.tx.description for i in within] == ["Te laat", "Binnenkort"]
    html = configured.get("/").get_data(as_text=True)
    widget = html.split('aria-labelledby="w-review"')[1].split("</section>")[0]
    assert "Te laat" in widget and "Binnenkort" in widget and "Nog ver" not in widget
    resp = configured.post(f"/transacties/{soon}/herzien")
    assert "is herzien" in json.loads(resp.headers["HX-Trigger"])["toast"]["message"]
    with app.app_context():
        tx = db.session.get(Transaction, soon)
        assert tx.review_checked_on == date.today()
        assert svc.review_due_date(tx) > date.today() + timedelta(days=170)
        admin = db.session.query(User).filter_by(username="admin").one()
        assert "Binnenkort" not in [i.tx.description for i in svc.reviews(admin, within_days=45)]
    # herziene datum staat in de audit-log
    assert "Gewijzigd" in configured.get(f"/transacties/{soon}").get_data(as_text=True)


def test_stop_review_and_changing_period_resets(app, configured, ids):
    tx_id = create(configured, ids, date=old(100), recurring_review="1", review_months="6")
    configured.post(f"/transacties/{tx_id}/herzien")
    configured.post(
        f"/transacties/{tx_id}", data=tx_form(ids, recurring_review="1", review_months="12")
    )
    with app.app_context():
        tx = db.session.get(Transaction, tx_id)
        assert tx.review_months == 12 and tx.review_checked_on is None  # nieuwe termijn
    configured.post(f"/transacties/{tx_id}/herziening-stoppen")
    with app.app_context():
        assert db.session.get(Transaction, tx_id).review_months is None
    assert configured.post(f"/transacties/{tx_id}/herzien").status_code == 400


def test_duplicate_undo_and_badge(app, configured, ids):
    tx_id = create(configured, ids, recurring_review="1", review_months="12")
    assert "12 mnd" in configured.get("/transacties/").get_data(as_text=True)
    configured.post(f"/transacties/{tx_id}/dupliceer")
    toast = json.loads(configured.post(f"/transacties/{tx_id}/verwijder").headers["HX-Trigger"])[
        "toast"
    ]
    configured.post(toast["undoUrl"])
    with app.app_context():
        assert [t.review_months for t in db.session.query(Transaction)] == [12, 12]


def test_private_account_reviews_hidden_from_partner(app, configured, partner_client, ids):
    create(
        configured,
        ids,
        account_id=ids["acc"]["Prive Admin"],
        date=old(400),
        recurring_review="1",
        review_months="12",
        description="Geheim abonnement",
    )
    assert "Geheim abonnement" in configured.get("/").get_data(as_text=True)
    for url in ("/", "/vaste-lasten/"):
        assert "Geheim abonnement" not in partner_client.get(url).get_data(as_text=True)
    with app.app_context():
        tx_id = db.session.query(Transaction).one().id
    assert partner_client.post(f"/transacties/{tx_id}/herzien").status_code == 404
    assert partner_client.post(f"/transacties/{tx_id}/herziening-stoppen").status_code == 404


def test_migration_adds_columns(tmp_path):
    from flask_migrate import upgrade
    from sqlalchemy import inspect

    from app import create_app

    app = create_app(
        {
            "SECRET_KEY": "t",
            "DATA_DIR": tmp_path / "d",
            "BACKUP_DIR": tmp_path / "b",
            "TESTING": True,
        }
    )
    with app.app_context():
        upgrade()
        cols = {c["name"] for c in inspect(db.engine).get_columns("transaction")}
        assert {"review_months", "review_checked_on"} <= cols
        db.engine.dispose()
