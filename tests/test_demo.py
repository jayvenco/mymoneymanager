from datetime import date

from app.extensions import db
from app.models import Account, Budget, RecurringRule, SavingsGoal, Transaction
from app.services import report_core as core
from app.services.people import HOUSEHOLD
from tests.test_reports import SLUGS


def test_seed_demo_command_and_reports(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["seed-demo"])
    assert result.exit_code == 0, result.output
    assert "demo / demo-wachtwoord-1" in result.output
    again = runner.invoke(args=["seed-demo"])
    assert again.exit_code != 0 and "lege database" in again.output

    with app.app_context():
        txs = db.session.query(Transaction).all()
        assert 250 < len(txs) < 600
        assert db.session.query(Account).count() == 6
        assert db.session.query(Budget).count() >= 5 and db.session.query(SavingsGoal).count() == 3
        assert db.session.query(RecurringRule).count() >= 5
        months = {(t.date.year, t.date.month) for t in txs}
        assert len(months) >= 12
        assert {t.type for t in txs} == {"income", "expense", "transfer"}
        assert all(t.date <= date.today() for t in txs)
        assert any(t.description == "Salaris" for t in txs) and any(
            t.description == "Huur" for t in txs
        )
        rent = [t for t in txs if t.description == "Huur"]
        assert all(t.account.name == "Gezamenlijke rekening" for t in rent)

    client = app.test_client()
    assert (
        client.post(
            "/login", data={"username": "demo", "password": "demo-wachtwoord-1"}
        ).status_code
        == 302
    )
    for person in ("household", "joint", "member:1", "member:2"):
        client.post("/persoon", data={"person": person})
        for slug in SLUGS:
            url = "/" if slug == "overzicht" else f"/rapportages/{slug}"
            resp = client.get(url + "?periode=last_12m&vergelijk=yoy")
            assert resp.status_code == 200, (person, slug)
    client.post("/persoon", data={"person": "household"})
    html = client.get("/?periode=last_12m").get_data(as_text=True)
    assert "Wij samen" in html and "Te bevestigen" in html and "Contributie hockeyclub" in html
    # Sanne's spaarrekening is privé: een tweede (niet-beheerder) login ziet haar niet
    with app.app_context():
        params_period = core.resolve_period("last_12m", 25, date.today())
        params = core.ReportParams("last_12m", params_period, "none", None, HOUSEHOLD)
        from app.models import User

        user = db.session.query(User).filter_by(username="demo").one()
        assert core.totals(params, params_period, user=user).income > 0
