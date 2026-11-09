import json

from app.extensions import db
from app.models import Account, Category, Tag, Transaction
from app.services.balances import account_balance
from tests.conftest import tx_form


def seed(client, ids):
    a, p, j = ids["acc"]["Betaal Admin"], ids["acc"]["Spaar Partner"], ids["acc"]["Gezamenlijk"]
    for over in (
        dict(description="Admin boodschappen", amount="40"),
        dict(
            description="Joint huur",
            account_id=j,
            amount="800",
            category_id=ids["cat"]["Huur/Hypotheek"],
        ),
        dict(
            description="Naar partner spaar",
            type="transfer",
            account_id=a,
            to_account_id=p,
            category_id="",
            amount="100",
        ),
        dict(
            description="Naar gezamenlijk",
            type="transfer",
            account_id=a,
            to_account_id=j,
            category_id="",
            amount="200",
        ),
    ):
        assert client.post("/transacties/", data=tx_form(ids, **over)).status_code == 204


def listing(client, person):
    return client.get(f"/transacties/?person={person}").get_data(as_text=True)


def test_person_filter_and_transfer_labels(configured, ids):
    seed(configured, ids)
    admin, partner = f"member:{ids['admin_member']}", f"member:{ids['partner_member']}"

    household = listing(configured, "household")
    assert "Admin boodschappen" in household and "Joint huur" in household
    assert household.count("Overboeking: ") == 2  # neutraal label op huishoudniveau

    mine = listing(configured, admin)
    assert "Admin boodschappen" in mine and "Joint huur" not in mine
    assert mine.count("Uitgaande overboeking") == 2

    theirs = listing(configured, partner)
    assert "Admin boodschappen" not in theirs
    assert "Naar partner spaar" in theirs and "Inkomende overboeking" in theirs
    assert "Naar gezamenlijk" not in theirs

    joint = listing(configured, "joint")
    assert "Joint huur" in joint and "Inkomende overboeking" in joint
    assert "Admin boodschappen" not in joint


def test_transfers_never_count_as_income_or_expense(configured, ids):
    seed(configured, ids)
    html = configured.get("/transacties/?person=household").get_data(as_text=True)
    # alleen echte uitgaven (40 + 800) in de som; de overboekingen (300) niet
    assert "-€ 840,00" in html and "+€ 0,00" in html


def test_person_filter_is_remembered(configured, ids):
    seed(configured, ids)
    key = f"member:{ids['partner_member']}"
    resp = configured.post("/persoon", data={"person": key, "next": "/transacties/"})
    assert resp.status_code == 302
    html = configured.get("/transacties/").get_data(as_text=True)
    assert "Admin boodschappen" not in html and "Naar partner spaar" in html
    assert 'aria-pressed="true"' in html
    # onbekend lid valt terug op huishouden
    configured.post("/persoon", data={"person": "member:9999"})
    assert "Admin boodschappen" in configured.get("/transacties/").get_data(as_text=True)


def test_open_redirect_in_person_switch(configured):
    resp = configured.post("/persoon", data={"person": "household", "next": "//evil.example"})
    assert resp.headers["Location"] == "/"


def test_account_crud_and_archive(app, configured, ids):
    form = {
        "name": "Reserve",
        "bank": "ING",
        "owner": str(ids["partner_member"]),
        "type": "savings",
        "opening_balance": "1.500,00",
        "opening_date": "01-01-2025",
        "visibility": "private",
        "color": "#112233",
    }
    assert configured.post("/rekeningen/", data=form).status_code == 204
    with app.app_context():
        acc = db.session.query(Account).filter_by(name="Reserve").one()
        assert (acc.opening_balance_cents, acc.visibility, acc.owner_member_id) == (
            150000,
            "private",
            ids["partner_member"],
        )
        acc_id = acc.id
    assert (
        configured.post(
            f"/rekeningen/{acc_id}",
            data={**form, "name": "Reserve 2", "owner": "joint", "visibility": "shared"},
        ).status_code
        == 204
    )
    with app.app_context():
        acc = db.session.get(Account, acc_id)
        assert (acc.name, acc.owner_member_id, acc.visibility) == ("Reserve 2", None, "shared")
    configured.post(f"/rekeningen/{acc_id}/archiveer")
    html = configured.get("/rekeningen/").get_data(as_text=True)
    assert "Reserve 2" not in html
    assert "Reserve 2" in configured.get("/rekeningen/?archief=1").get_data(as_text=True)
    bad = configured.post("/rekeningen/", data={**form, "name": "", "opening_balance": "x"})
    assert bad.status_code == 422


def test_accounts_page_groups_and_person_filter(configured, ids):
    html = configured.get("/rekeningen/?person=household").get_data(as_text=True)
    assert "Betaal Admin" in html and "Spaar Partner" in html and "Gezamenlijk" in html
    only_partner = configured.get(f"/rekeningen/?person=member:{ids['partner_member']}").get_data(
        as_text=True
    )
    assert "Spaar Partner" in only_partner and "Betaal Admin" not in only_partner


def test_reconcile_creates_correction(app, configured, ids):
    acc = ids["acc"]["Betaal Admin"]  # boekhoudkundig € 1.000,50
    check = configured.post(
        f"/rekeningen/{acc}/controleren", data={"actual": "1.100,00", "date": "01-03-2025"}
    )
    assert check.status_code == 200 and "Correctie aanmaken" in check.get_data(as_text=True)
    resp = configured.post(
        f"/rekeningen/{acc}/controleren",
        data={"actual": "1.100,00", "date": "01-03-2025", "action": "correct"},
    )
    assert resp.status_code == 204
    with app.app_context():
        assert account_balance(acc) == 110000
        tx = db.session.query(Transaction).one()
        assert tx.type == "income" and tx.amount_cents == 9950
        assert tx.category.name == "Correctie" and tx.category.exclude_from_reports
    ok = configured.post(
        f"/rekeningen/{acc}/controleren", data={"actual": "1100", "date": "01-03-2025"}
    )
    assert "Het saldo klopt" in ok.get_data(as_text=True)


def test_category_delete_requires_target(app, configured, ids):
    configured.post("/transacties/", data=tx_form(ids))
    eten = ids["cat"]["Eten"]
    form = configured.get(f"/categorieen/{eten}/verwijderen").get_data(as_text=True)
    assert "Verplaats transacties naar" in form
    no_target = configured.post(f"/categorieen/{eten}/verwijderen", data={})
    assert "Kies een categorie waarnaartoe" in no_target.headers["HX-Trigger"]
    with app.app_context():
        assert db.session.get(Category, eten) is not None
    resp = configured.post(
        f"/categorieen/{eten}/verwijderen", data={"target": ids["cat"]["Kleding"]}
    )
    assert resp.status_code == 204
    with app.app_context():
        assert db.session.get(Category, eten) is None
        assert db.session.query(Transaction).one().category_id == ids["cat"]["Kleding"]


def test_system_category_cannot_be_deleted(configured, ids):
    corr = ids["cat"]["Correctie"]
    assert configured.get(f"/categorieen/{corr}/verwijderen").status_code == 403
    assert configured.post(f"/categorieen/{corr}", data={"name": "x"}).status_code == 403


def test_category_create_and_edit(app, configured, ids):
    resp = configured.post(
        "/categorieen/",
        data={
            "name": "Sport",
            "kind": "expense",
            "color": "#123456",
            "icon": "tag",
            "parent_id": ids["cat"]["Entertainment"],
        },
    )
    assert resp.status_code == 204
    with app.app_context():
        sport = db.session.query(Category).filter_by(name="Sport").one()
        assert sport.parent_id == ids["cat"]["Entertainment"] and sport.kind == "expense"
    bad = configured.post("/categorieen/", data={"name": "", "kind": "expense", "color": "zzz"})
    assert bad.status_code == 422


def test_tags_merge_and_delete(app, configured, ids):
    configured.post("/transacties/", data=tx_form(ids, tags="a, b"))
    configured.post("/transacties/", data=tx_form(ids, tags="a"))
    with app.app_context():
        a, b = (db.session.query(Tag).filter_by(name=n).one().id for n in ("a", "b"))
    resp = configured.post(f"/categorieen/tags/{a}/samenvoegen", data={"target": b})
    assert "samengevoegd" in json.loads(resp.headers["HX-Trigger"])["toast"]["message"]
    with app.app_context():
        assert db.session.query(Tag).filter_by(name="a").first() is None
        tagged = [sorted(t.name for t in tx.tags) for tx in db.session.query(Transaction)]
        assert tagged == [["b"], ["b"]]
    configured.post(f"/categorieen/tags/{b}/verwijder")
    with app.app_context():
        assert db.session.query(Tag).count() == 0
        assert db.session.query(Transaction).count() == 2
