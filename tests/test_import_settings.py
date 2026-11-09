import io
from datetime import date

import pyotp

from app.extensions import db
from app.models import Account, AuditLog, Category, Member, Transaction, User
from app.services import importer
from app.services.balances import account_balance
from tests.conftest import PARTNER_PASSWORD, SETUP_FORM, tx_form

ING = (
    '"Datum","Naam / Omschrijving","Rekening","Tegenrekening","Code","Af Bij","Bedrag (EUR)","Mutatiesoort","Mededelingen"\n'
    '"20260105","Albert Heijn 1234","NL01INGB0001234567","","BA","Af","23,45","Betaalautomaat","Pasvolgnr 001"\n'
    '"20260106","Werkgever BV","NL01INGB0001234567","NL02ABNA0123456789","OV","Bij","2.500,00","Overschrijving","Salaris"\n'
    '"20260107","Koffietent","NL01INGB0001234567","","BA","Af","3,50","Betaalautomaat",""\n'
    '"20260107","Koffietent","NL01INGB0001234567","","BA","Af","3,50","Betaalautomaat",""\n'
    '"kapot","Foute rij","NL01","","BA","Af","1,00","x",""\n'
)
RABO = (
    '"IBAN/BBAN","Munt","BIC","Volgnr","Datum","Rentedatum","Bedrag","Saldo na trn","Tegenrekening IBAN/BBAN","Naam tegenpartij","Naam uiteindelijke partij","Naam initiërende partij","BIC tegenpartij","Code","Batch ID","Transactiereferentie","Machtigingskenmerk","Incassant ID","Betalingskenmerk","Omschrijving-1","Omschrijving-2","Omschrijving-3","Reden retour","Oorspr bedrag","Oorspr munt","Koers"\n'
    '"NL01RABO0123456789","EUR","RABONL2U","1","2026-01-07","2026-01-07","-12,50","987,50","NL02INGB0001","Bakker Jansen","","","","bg","","REF1","","","","Brood","pasvolgnr","","","","",""\n'
    '"NL01RABO0123456789","EUR","RABONL2U","2","2026-01-08","2026-01-08","+1.000,00","1987,50","NL03INGB0002","Opdrachtgever","","","","bg","","REF2","","","","Factuur 42","","","","","",""\n'
)
ABN = "123456789\tEUR\t20260108\t1000,00\t980,50\t20260108\t-19,50\tBEA NR:XX01 08.01.26 Café Tien,PAS123\n"


def upload(client, content, account_id, preset="auto"):
    return client.post(
        "/gegevens/import/koppelen",
        data={
            "account_id": account_id,
            "preset": preset,
            "file": (io.BytesIO(content.encode()), "bank.csv"),
        },
        content_type="multipart/form-data",
    )


def token_of(html):
    return html.split('name="token" value="')[1].split('"')[0]


def category_ids(app):
    with app.app_context():
        return (
            db.session.query(Category).filter_by(name="Boodschappen").one().id,
            db.session.query(Category).filter_by(name="Overige inkomsten").one().id,
        )


def preview_and_commit(client, app, content, account_id, *, commit=True, extra=None):
    expense, income = category_ids(app)
    html = upload(client, content, account_id).get_data(as_text=True)
    token = token_of(html)
    fields = {
        "token": token,
        "account_id": account_id,
        "date_format": "auto",
        "default_expense": expense,
        "default_income": income,
    }
    from app.services.importer import PRESETS

    preset = PRESETS[importer.detect_preset(importer.parse_file(content.encode()).headers)]
    for key, col in preset["map"].items():
        fields[f"map_{key}"] = col
    fields["date_format"] = preset["date_format"]
    fields.update(extra or {})
    prev = client.post("/gegevens/import/voorbeeld", data=fields)
    assert prev.status_code == 200
    html = prev.get_data(as_text=True)
    if not commit:
        return html
    rows = [v.split('"')[0] for v in html.split('name="rows" value="')[1:]]
    return client.post(
        "/gegevens/import/uitvoeren", data={**fields, "rows": rows, "skip_dupes": "1"}
    )


def test_detects_presets_and_delimiters():
    for text, preset in ((ING, "ing"), (RABO, "rabobank"), (ABN, "abn")):
        parsed = importer.parse_file(text.encode())
        assert importer.detect_preset(parsed.headers) == preset
    assert importer.parse_file(ABN.encode()).headers[6] == "Transactiebedrag"
    assert importer.parse_file(b"a;b\n1;2\n").headers == ["a", "b"]
    assert importer.decode("Café".encode("cp1252")) == "Café"


def test_ing_preview_parses_signs_dates_and_flags_errors(app, configured, ids):
    html = preview_and_commit(configured, app, ING, ids["acc"]["Betaal Admin"], commit=False)
    assert "Albert Heijn 1234" in html and "€ 2.500,00" in html and "(bij)" in html
    assert "Ongeldige datum" in html
    assert 'class="num text-2xl font-bold text-emerald-600">4<' in html.replace(
        "\n", ""
    )  # 4 nieuw (incl. 2x koffie)
    assert 'text-red-600">1<' in html.replace("\n", "")


def test_import_commit_creates_transactions_with_audit(app, configured, ids):
    acc = ids["acc"]["Betaal Admin"]
    resp = preview_and_commit(configured, app, ING, acc)
    assert resp.status_code == 302 and "/transacties/" in resp.headers["Location"]
    with app.app_context():
        txs = db.session.query(Transaction).order_by(Transaction.date, Transaction.id).all()
        assert len(txs) == 4
        by_desc = {t.description: t for t in txs if t.description != "Koffietent"}
        ah, salary = by_desc["Albert Heijn 1234"], by_desc["Werkgever BV"]
        assert (ah.type, ah.amount_cents, ah.date) == ("expense", 2345, date(2026, 1, 5))
        assert (salary.type, salary.amount_cents) == ("income", 250000)
        assert ah.external_ref and ah.account_id == acc and ah.notes == "Pasvolgnr 001"
        assert (
            db.session.query(AuditLog).filter_by(entity="transaction", action="create").count() == 4
        )
        assert account_balance(acc) == 100050 - 2345 - 700 + 250000


def test_reimport_detects_duplicates_including_repeated_rows(app, configured, ids):
    acc = ids["acc"]["Betaal Admin"]
    preview_and_commit(configured, app, ING, acc)
    html = preview_and_commit(configured, app, ING, acc, commit=False).replace("\n", "")
    assert 'text-amber-600">4<' in html  # alles is nu een duplicaat (ook beide koffies)
    assert 'text-emerald-600">0<' in html
    preview_and_commit(configured, app, ING, acc)  # opnieuw importeren: duplicaten overgeslagen
    with app.app_context():
        assert db.session.query(Transaction).count() == 4


def test_manual_transaction_counts_as_duplicate(app, configured, ids):
    configured.post(
        "/transacties/",
        data=tx_form(ids, date="07-01-2026", amount="3,50", description="Koffietent", payee=""),
    )
    html = preview_and_commit(
        configured, app, ING, ids["acc"]["Betaal Admin"], commit=False
    ).replace("\n", "")
    assert 'text-amber-600">1<' in html and 'text-emerald-600">3<' in html


def test_rabobank_and_abn_imports(app, configured, ids):
    acc = ids["acc"]["Betaal Admin"]
    preview_and_commit(configured, app, RABO, acc)
    preview_and_commit(configured, app, ABN, acc)
    with app.app_context():
        txs = {t.description: t for t in db.session.query(Transaction)}
        assert (
            txs["Brood"].amount_cents == 1250
            and txs["Brood"].payee == "Bakker Jansen"
            and txs["Brood"].type == "expense"
        )
        assert txs["Factuur 42"].amount_cents == 100000 and txs["Factuur 42"].type == "income"
        abn = next(t for t in txs.values() if "Café Tien" in t.description)
        assert abn.amount_cents == 1950 and abn.date == date(2026, 1, 8)


def test_category_is_suggested_from_previous_payee(app, configured, ids):
    configured.post(
        "/transacties/",
        data=tx_form(
            ids, payee="Albert Heijn 1234", category_id=ids["cat"]["Uit eten"], description="x"
        ),
    )
    preview_and_commit(configured, app, ING, ids["acc"]["Betaal Admin"])
    with app.app_context():
        ah = db.session.query(Transaction).filter_by(description="Albert Heijn 1234").one()
        assert ah.category.name == "Uit eten"


def test_import_permissions_and_token_safety(app, configured, partner_client, ids):
    # partner mag niet importeren in de rekening van de admin of in een privérekening
    for name in ("Betaal Admin", "Prive Admin"):
        resp = upload(partner_client, ING, ids["acc"][name])
        assert resp.status_code == 302
    own = upload(partner_client, ING, ids["acc"]["Spaar Partner"])
    assert own.status_code == 200
    token = token_of(own.get_data(as_text=True))
    # token met padtraversal wordt geweigerd
    bad = configured.post(
        "/gegevens/import/voorbeeld",
        data={
            "token": "../../etc/passwd",
            "account_id": ids["acc"]["Betaal Admin"],
            "map_date": "Datum",
            "map_amount": "Bedrag",
        },
    )
    assert bad.status_code == 302
    assert (
        partner_client.post(
            "/gegevens/import/voorbeeld",
            data={
                "token": token,
                "account_id": ids["acc"]["Betaal Admin"],
                "map_date": "Datum",
                "map_amount": "Bedrag",
            },
        ).status_code
        == 404
    )


def test_transactions_csv_export_respects_visibility(app, configured, partner_client, ids):
    configured.post(
        "/transacties/",
        data=tx_form(ids, account_id=ids["acc"]["Prive Admin"], description="Geheim"),
    )
    configured.post("/transacties/", data=tx_form(ids, description="Openbaar", tags="a"))
    admin_csv = configured.get("/transacties/export.csv").get_data(as_text=True)
    partner_csv = partner_client.get("/transacties/export.csv").get_data(as_text=True)
    assert "Geheim" in admin_csv and "Openbaar" in admin_csv
    assert "Geheim" not in partner_csv and "Openbaar" in partner_csv
    assert "Datum;Type;Bedrag" in partner_csv and "12,50" in partner_csv


# --- instellingen --------------------------------------------------------------------------------


def test_change_password_flow(app, configured):
    assert (
        configured.post(
            "/instellingen/wachtwoord",
            data={"current": "fout", "new": "nieuw-wachtwoord-1", "confirm": "nieuw-wachtwoord-1"},
        ).status_code
        == 422
    )
    assert (
        configured.post(
            "/instellingen/wachtwoord",
            data={"current": SETUP_FORM["password"], "new": "kort", "confirm": "kort"},
        ).status_code
        == 422
    )
    assert (
        configured.post(
            "/instellingen/wachtwoord",
            data={
                "current": SETUP_FORM["password"],
                "new": "nieuw-wachtwoord-1",
                "confirm": "anders-wachtwoord",
            },
        ).status_code
        == 422
    )
    ok = configured.post(
        "/instellingen/wachtwoord",
        data={
            "current": SETUP_FORM["password"],
            "new": "nieuw-wachtwoord-1",
            "confirm": "nieuw-wachtwoord-1",
        },
    )
    assert ok.status_code == 302
    configured.post("/logout")
    assert (
        configured.post(
            "/login", data={"username": "admin", "password": SETUP_FORM["password"]}
        ).status_code
        == 401
    )
    assert (
        configured.post(
            "/login", data={"username": "admin", "password": "nieuw-wachtwoord-1"}
        ).status_code
        == 302
    )


def test_profile_theme_defaults(app, configured, ids):
    form = {
        "theme": "dark",
        "person": f"member:{ids['partner_member']}",
        "default_account": ids["acc"]["Gezamenlijk"],
    }
    assert configured.post("/instellingen/profiel", data=form).status_code == 302
    with app.app_context():
        user = db.session.query(User).filter_by(username="admin").one()
        assert user.theme_preference == "dark" and user.default_person_filter == form["person"]
        assert user.preferences["default_account"] == ids["acc"]["Gezamenlijk"]
    assert 'data-theme="dark"' in configured.get("/transacties/").get_data(as_text=True)
    assert f'<option value="{ids["acc"]["Gezamenlijk"]}" selected>' in configured.get(
        "/transacties/nieuw"
    ).get_data(as_text=True)
    assert configured.post("/instellingen/thema", data={"theme": "light"}).status_code == 204
    assert configured.post("/instellingen/thema", data={"theme": "neon"}).status_code == 400


def test_two_factor_setup_login_and_disable(app, configured):
    page = configured.get("/instellingen/2fa").get_data(as_text=True)
    assert "data:image/svg+xml;base64," in page
    with configured.session_transaction() as s:
        secret = s["totp_setup"]
    assert configured.post("/instellingen/2fa", data={"code": "000000"}).status_code == 422
    assert (
        configured.post("/instellingen/2fa", data={"code": pyotp.TOTP(secret).now()}).status_code
        == 302
    )
    configured.post("/logout")
    resp = configured.post("/login", data={"username": "admin", "password": SETUP_FORM["password"]})
    assert resp.headers["Location"].endswith("/login/2fa")
    assert configured.post("/login/2fa", data={"code": pyotp.TOTP(secret).now()}).status_code == 302
    assert (
        configured.post("/instellingen/2fa", data={"password": "fout"}).status_code == 200
    )  # blijft aan
    assert (
        configured.post("/instellingen/2fa", data={"password": SETUP_FORM["password"]}).status_code
        == 302
    )
    with app.app_context():
        assert db.session.query(User).filter_by(username="admin").one().totp_secret is None


def test_household_management(app, configured, ids):
    assert (
        configured.post(
            "/instellingen/huishouden/", data={"name": "Nieuw", "start_day": "1"}
        ).status_code
        == 302
    )
    configured.post("/instellingen/huishouden/", data={"name": "Fout", "start_day": "40"})
    with app.app_context():
        from app.models import Household

        hh = db.session.query(Household).one()
        assert (hh.name, hh.financial_month_start_day) == ("Nieuw", 1)
    partner = ids["partner_member"]
    configured.post(
        f"/instellingen/huishouden/leden/{partner}/login",
        data={"username": "sanne", "password": "kort"},
    )
    with app.app_context():
        assert db.session.query(User).filter_by(username="sanne").first() is None
    configured.post(
        f"/instellingen/huishouden/leden/{partner}/login",
        data={"username": "sanne", "password": PARTNER_PASSWORD},
    )
    client = app.test_client()
    assert (
        client.post("/login", data={"username": "sanne", "password": PARTNER_PASSWORD}).status_code
        == 302
    )
    with app.app_context():
        sanne = db.session.query(User).filter_by(username="sanne").one()
        assert sanne.member_id == partner and not sanne.is_admin
    configured.post(
        f"/instellingen/huishouden/leden/{partner}/login/wachtwoord",
        data={"password": "heel-ander-wachtwoord"},
    )
    assert (
        app.test_client()
        .post("/login", data={"username": "sanne", "password": "heel-ander-wachtwoord"})
        .status_code
        == 302
    )
    configured.post(f"/instellingen/huishouden/leden/{partner}/login/verwijder")
    with app.app_context():
        assert db.session.query(User).filter_by(username="sanne").first() is None
    admin_member = ids["admin_member"]
    configured.post(f"/instellingen/huishouden/leden/{admin_member}/login/verwijder")
    configured.post(f"/instellingen/huishouden/leden/{admin_member}/archiveer")
    with app.app_context():
        assert (
            db.session.query(User).filter_by(username="admin").one()
            and not db.session.get(Member, admin_member).is_archived
        )
    configured.post("/instellingen/huishouden/leden", data={"name": "Oma", "color": "#112233"})
    configured.post(f"/instellingen/huishouden/leden/{partner}/archiveer")
    with app.app_context():
        assert db.session.get(Member, partner).is_archived
        assert db.session.query(Member).filter_by(name="Oma").one().avatar_initials == "OM"
        assert (
            db.session.query(Account).filter_by(owner_member_id=partner).count() == 1
        )  # rekeningen blijven


def test_about_page(configured):
    html = configured.get("/instellingen/over").get_data(as_text=True)
    assert "Versie" in html and "Git-commit" in html and "Build-datum" in html
    assert "Back-up, import en export" in configured.get("/instellingen").get_data(as_text=True)
