from datetime import date

from app.extensions import db
from app.models import SavingsGoal, Transaction, User
from app.services import goals as svc
from app.services.people import HOUSEHOLD
from tests.conftest import make_app


def make_goal(ids, **over):
    data = {
        "household_id": 1,
        "account_id": ids["acc"]["Spaar Partner"],
        "member_id": ids["partner_member"],
        "name": "Vakantie",
        "target_cents": 100000,
        "target_date": date(2025, 12, 31),
    }
    data.update(over)
    goal = SavingsGoal(**data)
    db.session.add(goal)
    db.session.commit()
    return goal


def deposit(ids, d, cents):
    db.session.add(
        Transaction(
            household_id=1,
            date=d,
            amount_cents=cents,
            type="transfer",
            account_id=ids["acc"]["Betaal Admin"],
            to_account_id=ids["acc"]["Spaar Partner"],
            description="sparen",
        )
    )
    db.session.commit()


def test_months_until():
    assert svc.months_until(date(2025, 12, 31), date(2025, 3, 15)) == 9
    assert svc.months_until(date(2025, 3, 20), date(2025, 3, 15)) == 1  # minimaal 1


def test_goal_on_schedule_vs_behind(app, ids):
    with app.app_context():
        goal = make_goal(ids)  # saldo 25000, doel 100000, 9 maanden → 8334/mnd nodig
        for month in (1, 2, 3):
            deposit(ids, date(2025, month, 10), 10000)
        today = date(2025, 3, 15)
        progress = svc.evaluate(goal, 25000 + 30000, today)
        assert progress.remaining_cents == 45000 and progress.months_left == 9
        assert progress.needed_monthly_cents == 5000
        assert progress.actual_monthly_cents == 10000 and progress.status == "op_schema"
        behind = svc.evaluate(goal, 55000, date(2025, 11, 15))
        assert behind.needed_monthly_cents == 45000 and behind.status == "achter"


def test_goal_done_and_no_deadline(app, ids):
    with app.app_context():
        goal = make_goal(ids)
        assert svc.evaluate(goal, 100000, date(2025, 3, 1)).status == "behaald"
        goal.target_date = None
        assert svc.evaluate(goal, 5000, date(2025, 3, 1)).status == "geen_deadline"
        goal.target_date = date(2025, 1, 1)
        assert svc.evaluate(goal, 5000, date(2025, 3, 1)).status == "achter"


def test_goal_routes(app, configured, ids):
    form = {
        "name": "Buffer",
        "account_id": ids["acc"]["Spaar Partner"],
        "target": "2.500,00",
        "target_date": "2030-01-01",
        "member_id": ids["partner_member"],
    }
    assert configured.post("/spaardoelen/", data=form).status_code == 204
    assert (
        configured.post("/spaardoelen/", data={**form, "target": "0", "account_id": ""}).status_code
        == 422
    )
    html = configured.get("/spaardoelen/").get_data(as_text=True)
    assert "Buffer" in html and "€ 2.500,00" in html and "€ 250,00" in html
    with app.app_context():
        goal_id = db.session.query(SavingsGoal).one().id
    assert (
        configured.post(f"/spaardoelen/{goal_id}", data={**form, "name": "Buffer 2"}).status_code
        == 204
    )
    # persoonsfilter
    html = configured.get(f"/spaardoelen/?person=member:{ids['admin_member']}").get_data(
        as_text=True
    )
    assert "Buffer 2" not in html
    assert "Buffer 2" in configured.get("/spaardoelen/?person=household").get_data(as_text=True)
    assert configured.post(f"/spaardoelen/{goal_id}/verwijder").status_code == 204


def test_goal_on_private_account_hidden(app, configured, partner_client, ids):
    with app.app_context():
        goal = make_goal(
            ids,
            account_id=ids["acc"]["Prive Admin"],
            member_id=ids["admin_member"],
            name="Geheim doel",
        )
        goal_id = goal.id
        admin = db.session.query(User).filter_by(username="admin").one()
        partner = db.session.query(User).filter_by(username="partner").one()
        assert [p.goal.name for p in svc.progress_for(HOUSEHOLD, admin)] == ["Geheim doel"]
        assert svc.progress_for(HOUSEHOLD, partner) == []
    assert "Geheim doel" not in partner_client.get("/spaardoelen/").get_data(as_text=True)
    assert partner_client.get(f"/spaardoelen/{goal_id}/bewerken").status_code == 404
    assert partner_client.post(f"/spaardoelen/{goal_id}/verwijder").status_code == 404


def test_scheduler_lock_is_single_instance(tmp_path):
    from app.services.scheduler import acquire_lock, start_scheduler

    app = make_app(tmp_path)
    first = acquire_lock(app)
    assert first is not None
    assert acquire_lock(app) is None  # tweede proces krijgt de lock niet
    assert start_scheduler(app) is None
    first.close()
    scheduler = start_scheduler(app)
    assert scheduler is not None and scheduler.running
    assert {job.id for job in scheduler.get_jobs()} >= {"recurring"}
    assert start_scheduler(app) is None
    scheduler.shutdown(wait=False)


def test_scheduled_job_is_safe_on_empty_database(tmp_path, caplog):
    from app.services.scheduler import run_recurring

    app = make_app(tmp_path)
    with caplog.at_level("ERROR"):
        run_recurring(app)  # geen huishouden: mag niet crashen of loggen als fout
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
