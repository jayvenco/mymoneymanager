"""Privé-rekeningen mogen nooit zichtbaar of bewerkbaar zijn voor niet-eigenaren (niet-beheerders)."""

import pytest

from app.extensions import db
from app.models import Transaction, User
from app.services import access
from tests.conftest import tx_form


@pytest.fixture()
def private_tx(app, configured, ids):
    """Een uitgave op de privérekening van de admin + een overboeking van de gezamenlijke naar die rekening."""
    priv = ids["acc"]["Prive Admin"]
    configured.post(
        "/transacties/",
        data=tx_form(ids, account_id=priv, description="Geheim cadeau", amount="77,77"),
    )
    configured.post(
        "/transacties/",
        data=tx_form(
            ids,
            type="transfer",
            account_id=ids["acc"]["Gezamenlijk"],
            to_account_id=priv,
            category_id="",
            description="Naar privé",
            amount="10",
        ),
    )
    configured.post("/transacties/", data=tx_form(ids, description="Gewone uitgave"))
    with app.app_context():
        rows = {t.description: t.id for t in db.session.query(Transaction)}
    return rows


def test_service_level_filtering(app, ids, partner_client):
    with app.app_context():
        partner = db.session.query(User).filter_by(username="partner").one()
        admin = db.session.query(User).filter_by(username="admin").one()
        seen = {a.name for a in db.session.execute(access.visible_accounts(partner)).scalars()}
        assert seen == {"Betaal Admin", "Spaar Partner", "Gezamenlijk"}  # eigen privé + gedeeld
        assert "Prive Admin" not in seen
        assert {
            a.name for a in db.session.execute(access.visible_accounts(admin)).scalars()
        } == set(ids["acc"])
        assert not access.can_see_account(ids["acc"]["Prive Admin"], partner)
        assert access.can_see_account(ids["acc"]["Spaar Partner"], partner)


def test_partner_cannot_see_private_in_list(partner_client, private_tx):
    html = partner_client.get("/transacties/").get_data(as_text=True)
    assert "Gewone uitgave" in html
    assert "Geheim cadeau" not in html
    assert "77,77" not in html
    # ook niet de overboeking die naar de privérekening gaat (zou het bestaan ervan verraden)
    assert "Naar privé" not in html


def test_partner_cannot_open_or_edit_private_tx(partner_client, private_tx, ids):
    for desc in ("Geheim cadeau", "Naar privé"):
        tx_id = private_tx[desc]
        assert partner_client.get(f"/transacties/{tx_id}").status_code == 404
        assert partner_client.get(f"/transacties/{tx_id}/bewerken").status_code == 404
        assert partner_client.get(f"/transacties/{tx_id}/inline").status_code == 404
        assert (
            partner_client.post(
                f"/transacties/{tx_id}", data=tx_form(ids, description="gehackt")
            ).status_code
            == 404
        )
        assert partner_client.post(f"/transacties/{tx_id}/verwijder").status_code == 404
        assert partner_client.post(f"/transacties/{tx_id}/dupliceer").status_code == 404


def test_bulk_ignores_private(app, partner_client, private_tx, ids):
    ids_list = list(private_tx.values())
    partner_client.post("/transacties/bulk", data={"ids": ids_list, "action": "delete"})
    with app.app_context():
        remaining = {t.description for t in db.session.query(Transaction)}
    assert {"Geheim cadeau", "Naar privé"} <= remaining
    assert "Gewone uitgave" not in remaining  # de zichtbare werd wél verwijderd


def test_cannot_create_on_private_account(partner_client, ids):
    resp = partner_client.post(
        "/transacties/", data=tx_form(ids, account_id=ids["acc"]["Prive Admin"])
    )
    assert resp.status_code == 422
    resp = partner_client.post(
        "/transacties/",
        data=tx_form(ids, type="transfer", to_account_id=ids["acc"]["Prive Admin"], category_id=""),
    )
    assert resp.status_code == 422


def test_accounts_pages_hide_private(partner_client, private_tx, ids):
    html = partner_client.get("/rekeningen/").get_data(as_text=True)
    assert "Prive Admin" not in html and "Spaar Partner" in html
    priv = ids["acc"]["Prive Admin"]
    assert partner_client.get(f"/rekeningen/{priv}").status_code == 404
    assert partner_client.get(f"/rekeningen/{priv}/bewerken").status_code == 404
    assert partner_client.get(f"/rekeningen/{priv}/controleren").status_code == 404
    assert partner_client.post(f"/rekeningen/{priv}/archiveer").status_code == 404
    # filteren op de privérekening levert niets op
    html = partner_client.get(f"/transacties/?account={priv}").get_data(as_text=True)
    assert "Geheim cadeau" not in html


def test_admin_sees_everything(configured, private_tx):
    html = configured.get("/transacties/").get_data(as_text=True)
    assert "Geheim cadeau" in html and "Naar privé" in html


def test_non_owner_cannot_manage_foreign_account(partner_client, ids):
    shared_admin = ids["acc"]["Betaal Admin"]
    assert partner_client.get(f"/rekeningen/{shared_admin}/bewerken").status_code == 403
    assert partner_client.post(f"/rekeningen/{shared_admin}", data={"name": "x"}).status_code == 403


def test_tags_from_private_transactions_do_not_leak(app, configured, partner_client, ids):
    configured.post(
        "/transacties/",
        data=tx_form(
            ids, account_id=ids["acc"]["Prive Admin"], tags="verrassingsfeest", description="Prive"
        ),
    )
    configured.post("/transacties/", data=tx_form(ids, tags="gewoon"))
    for url in (
        "/categorieen/tags",
        "/transacties/",
        "/transacties/nieuw",
        "/transacties/tags/zoek?q=verr",
    ):
        html = partner_client.get(url).get_data(as_text=True)
        assert "verrassingsfeest" not in html, url
    assert "gewoon" in partner_client.get("/categorieen/tags").get_data(as_text=True)
    assert "verrassingsfeest" in configured.get("/categorieen/tags").get_data(as_text=True)
    from app.models import Tag

    with app.app_context():
        hidden = db.session.query(Tag).filter_by(name="verrassingsfeest").one().id
    assert partner_client.get(f"/categorieen/tags/{hidden}/bewerken").status_code == 404
    assert partner_client.post(f"/categorieen/tags/{hidden}/verwijder").status_code == 404
