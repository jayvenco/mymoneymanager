from datetime import date

from app.extensions import db
from app.models import Transaction
from app.services.balances import account_balance, account_balances, balance_series
from tests.conftest import tx_form


def post(client, ids, **over):
    resp = client.post("/transacties/", data=tx_form(ids, **over))
    assert resp.status_code == 204, resp.get_data(as_text=True)


def test_opening_balance_only(app, ids):
    with app.app_context():
        assert account_balance(ids["acc"]["Betaal Admin"]) == 100050


def test_income_expense_and_transfer_balances(app, configured, ids):
    a, p, j = ids["acc"]["Betaal Admin"], ids["acc"]["Spaar Partner"], ids["acc"]["Gezamenlijk"]
    post(configured, ids, type="income", amount="2.000,00", category_id=ids["cat"]["Salaris"])
    post(configured, ids, amount="100,25")
    post(configured, ids, type="transfer", amount="300", to_account_id=j, category_id="")
    post(configured, ids, type="transfer", amount="50", to_account_id=p, category_id="")
    with app.app_context():
        balances = account_balances()
        assert balances[a] == 100050 + 200000 - 10025 - 30000 - 5000
        assert balances[j] == 30000
        assert balances[p] == 25000 + 5000


def test_transactions_before_opening_date_are_ignored(app, configured, ids):
    post(configured, ids, date="31-12-2024", amount="999")
    with app.app_context():
        assert account_balance(ids["acc"]["Betaal Admin"]) == 100050


def test_balance_as_of_and_series(app, configured, ids):
    a = ids["acc"]["Betaal Admin"]
    post(configured, ids, date="10-01-2025", amount="10")
    post(configured, ids, date="20-01-2025", amount="20")
    with app.app_context():
        assert account_balance(a, date(2025, 1, 15)) == 100050 - 1000
        assert account_balance(a, date(2025, 1, 31)) == 100050 - 3000
        series = dict(balance_series(a, date(2025, 1, 1), date(2025, 1, 31)))
        assert series[date(2025, 1, 1)] == 100050
        assert series[date(2025, 1, 10)] == 99050
        assert series[date(2025, 1, 31)] == 97050
        assert len(series) == 31
        assert db.session.query(Transaction).count() == 2
