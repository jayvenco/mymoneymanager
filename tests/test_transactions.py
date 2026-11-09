import json

from app.extensions import db
from app.models import AuditLog, Transaction
from app.services.balances import account_balance
from tests.conftest import tx_form


def trig(resp):
    return json.loads(resp.headers["HX-Trigger"])


def create(client, ids, **over):
    resp = client.post("/transacties/", data=tx_form(ids, **over))
    assert resp.status_code == 204, resp.get_data(as_text=True)
    with client.application.app_context():
        return db.session.query(Transaction).order_by(Transaction.id.desc()).first().id


def test_create_and_list(configured, ids):
    tx_id = create(configured, ids, tags="vakantie-2026, eten")
    html = configured.get("/transacties/").get_data(as_text=True)
    assert "Albert Heijn" in html
    assert "#vakantie-2026" in html and "#eten" in html
    detail = configured.get(f"/transacties/{tx_id}").get_data(as_text=True)
    assert "Aangemaakt" in detail


def test_validation_errors(configured, ids):
    resp = configured.post(
        "/transacties/", data=tx_form(ids, amount="abc", date="31-02-2025", category_id="")
    )
    assert resp.status_code == 422
    html = resp.get_data(as_text=True)
    assert "Ongeldig bedrag" in html and "Ongeldige datum" in html and "Kies een categorie" in html
    # categorie van verkeerd type
    resp = configured.post("/transacties/", data=tx_form(ids, category_id=ids["cat"]["Salaris"]))
    assert resp.status_code == 422
    # overboeking naar dezelfde rekening
    a = ids["acc"]["Betaal Admin"]
    resp = configured.post(
        "/transacties/", data=tx_form(ids, type="transfer", to_account_id=a, category_id="")
    )
    assert resp.status_code == 422


def test_edit_all_fields_updates_balances_and_audit(app, configured, ids):
    tx_id = create(configured, ids)
    a, j = ids["acc"]["Betaal Admin"], ids["acc"]["Gezamenlijk"]
    resp = configured.post(
        f"/transacties/{tx_id}",
        data=tx_form(
            ids,
            amount="20",
            account_id=j,
            category_id=ids["cat"]["Uit eten"],
            description="Restaurant",
            date="01-04-2025",
            tags="uit",
        ),
    )
    assert resp.status_code == 204
    with app.app_context():
        assert account_balance(a) == 100050
        assert account_balance(j) == -2000
        tx = db.session.get(Transaction, tx_id)
        assert (tx.description, tx.amount_cents, tx.account_id) == ("Restaurant", 2000, j)
        assert [t.name for t in tx.tags] == ["uit"]
        actions = [
            e.action
            for e in db.session.query(AuditLog).filter_by(entity="transaction", entity_id=tx_id)
        ]
        assert actions == ["create", "update"]
        assert tx.updated_by_user_id is not None
    detail = configured.get(f"/transacties/{tx_id}").get_data(as_text=True)
    assert "Gewijzigd" in detail and "Restaurant" in detail


def test_inline_edit(app, configured, ids):
    tx_id = create(configured, ids)
    resp = configured.post(
        f"/transacties/{tx_id}/inline",
        data={
            "amount": "99,99",
            "description": "Inline",
            "date": "2025-03-16",
            "category_id": ids["cat"]["Afhalen"],
        },
    )
    assert resp.status_code == 200
    assert "Inline" in resp.get_data(as_text=True)
    with app.app_context():
        tx = db.session.get(Transaction, tx_id)
        assert tx.amount_cents == 9999 and tx.date.isoformat() == "2025-03-16"
        assert tx.payee == "AH"  # niet-getoonde velden blijven behouden
    bad = configured.post(f"/transacties/{tx_id}/inline", data={"amount": "x"})
    assert bad.status_code == 422


def test_delete_and_undo(app, configured, ids):
    tx_id = create(configured, ids, tags="a")
    resp = configured.post(f"/transacties/{tx_id}/verwijder")
    toast = trig(resp)["toast"]
    assert toast["message"] == "Transactie verwijderd." and toast["undoUrl"]
    with app.app_context():
        assert db.session.get(Transaction, tx_id) is None
    resp = configured.post(toast["undoUrl"])
    assert trig(resp)["toast"]["message"] == "Transactie hersteld."
    with app.app_context():
        restored = db.session.query(Transaction).one()
        assert restored.amount_cents == 1250 and [t.name for t in restored.tags] == ["a"]
    # tweede keer ongedaan maken kan niet
    assert trig(configured.post(toast["undoUrl"]))["toast"]["category"] == "error"


def test_duplicate(app, configured, ids):
    tx_id = create(configured, ids)
    configured.post(f"/transacties/{tx_id}/dupliceer")
    with app.app_context():
        assert db.session.query(Transaction).count() == 2


def test_bulk_actions(app, configured, ids):
    t1, t2 = create(configured, ids), create(configured, ids, description="Tweede")
    cat = ids["cat"]["Uit eten"]
    configured.post("/transacties/bulk", data={"ids": [t1, t2], "action": "category", "value": cat})
    configured.post(
        "/transacties/bulk", data={"ids": [t1, t2], "action": "add_tag", "value": "x, y"}
    )
    configured.post("/transacties/bulk", data={"ids": [t1], "action": "remove_tag", "value": "x"})
    configured.post(
        "/transacties/bulk",
        data={"ids": [t2], "action": "account", "value": ids["acc"]["Gezamenlijk"]},
    )
    with app.app_context():
        a, b = db.session.get(Transaction, t1), db.session.get(Transaction, t2)
        assert a.category_id == b.category_id == cat
        assert sorted(t.name for t in a.tags) == ["y"]
        assert sorted(t.name for t in b.tags) == ["x", "y"]
        assert b.account_id == ids["acc"]["Gezamenlijk"]
    configured.post("/transacties/bulk", data={"ids": [t1, t2], "action": "delete"})
    with app.app_context():
        assert db.session.query(Transaction).count() == 0


def test_filters_search_sort_and_pagination(app, configured, ids):
    create(
        configured,
        ids,
        description="Pizza",
        amount="25",
        date="01-02-2025",
        category_id=ids["cat"]["Uit eten"],
    )
    create(configured, ids, description="Brood", amount="3,50", date="02-02-2025")
    create(
        configured,
        ids,
        type="income",
        amount="1000",
        category_id=ids["cat"]["Salaris"],
        description="Loon",
        date="03-02-2025",
    )

    def descriptions(query):
        html = configured.get(f"/transacties/?{query}").get_data(as_text=True)
        return [d for d in ("Pizza", "Brood", "Loon") if d in html]

    assert descriptions("q=pizza") == ["Pizza"]
    assert descriptions("type=income") == ["Loon"]
    assert descriptions("min=20&max=30") == ["Pizza"]
    assert descriptions("from=02-02-2025&to=02-02-2025") == ["Brood"]
    assert descriptions(f"category={ids['cat']['Eten']}") == [
        "Pizza",
        "Brood",
    ]  # incl. subcategorieën
    assert descriptions(f"category={ids['cat']['Uit eten']}") == ["Pizza"]
    html = configured.get("/transacties/?sort=amount&dir=asc").get_data(as_text=True)
    assert html.index("Brood") < html.index("Pizza") < html.index("Loon")
    # onbekende sorteerwaarde valt veilig terug
    assert configured.get("/transacties/?sort=password;drop&page=zzz").status_code == 200


def test_pagination(app, configured, ids):
    with app.app_context():
        for i in range(55):
            db.session.add(
                Transaction(
                    household_id=1,
                    date=__import__("datetime").date(2025, 3, 1),
                    amount_cents=100 + i,
                    type="expense",
                    account_id=ids["acc"]["Betaal Admin"],
                    category_id=ids["cat"]["Boodschappen"],
                    description=f"T{i}",
                )
            )
        db.session.commit()
    p1 = configured.get("/transacties/").get_data(as_text=True)
    p2 = configured.get("/transacties/?page=2").get_data(as_text=True)
    assert p1.count("data-row-check") == 50 and p2.count("data-row-check") == 5
    assert "55" in p1


def test_htmx_returns_partial(configured, ids):
    create(configured, ids)
    partial = configured.get("/transacties/", headers={"HX-Request": "true"}).get_data(as_text=True)
    assert "<html" not in partial and "Albert Heijn" in partial
