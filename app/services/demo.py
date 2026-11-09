"""Demo-huishouden met 12 maanden realistische Nederlandse transacties (`flask seed-demo`)."""

import json
import random
from datetime import date, timedelta

from sqlalchemy import select

from ..extensions import db
from ..models import (
    Account,
    Budget,
    Category,
    Household,
    Member,
    RecurringRule,
    SavingsGoal,
    Tag,
    Transaction,
    User,
)
from .periods import add_months_clamped
from .seed import seed_default_categories
from .setup import setup_needed
from .transactions import serialize, write_audit

DEMO_USERNAME = "demo"
DEMO_PASSWORD = "demo-wachtwoord-1"  # alleen voor demo's; nooit in productie gebruiken


class DemoError(Exception):
    pass


def _cat(name: str, kind: str) -> Category:
    return db.session.execute(
        select(Category).where(Category.name == name, Category.kind == kind)
    ).scalar_one()


def seed_demo(today: date | None = None, start_day: int = 25) -> dict[str, int]:
    """Maakt het demo-huishouden. Weigert te draaien als er al gebruikers bestaan."""
    if not setup_needed():
        raise DemoError("Er bestaan al gebruikers; seed-demo werkt alleen op een lege database.")
    rng = random.Random(2026)
    today = today or date.today()
    begin = add_months_clamped(today.replace(day=1), -11)

    household = Household(name="Familie Demo", financial_month_start_day=start_day)
    db.session.add(household)
    db.session.flush()
    jay = Member(
        household_id=household.id,
        name="Jayvenco",
        color="#4f46e5",
        avatar_initials="JA",
        sort_order=0,
    )
    sanne = Member(
        household_id=household.id, name="Sanne", color="#ec4899", avatar_initials="SA", sort_order=1
    )
    db.session.add_all([jay, sanne])
    db.session.flush()
    admin = User(username=DEMO_USERNAME, member_id=jay.id, is_admin=True)
    admin.set_password(DEMO_PASSWORD)
    db.session.add(admin)
    seed_default_categories(household.id)

    def account(name, owner, bank, kind, balance, color, visibility="shared", order=0):
        acc = Account(
            household_id=household.id,
            owner_member_id=owner.id if owner else None,
            name=name,
            bank=bank,
            type=kind,
            visibility=visibility,
            opening_balance_cents=balance,
            opening_date=begin,
            color=color,
            icon={"savings": "piggy-bank", "checking": "wallet"}.get(kind, "wallet"),
            sort_order=order,
        )
        db.session.add(acc)
        db.session.flush()
        return acc

    jay_pay = account("Betaalrekening Jayvenco", jay, "ING", "checking", 215000, jay.color, order=0)
    jay_save = account("Spaarrekening Jayvenco", jay, "ING", "savings", 820000, jay.color, order=1)
    sanne_pay = account(
        "Betaalrekening Sanne", sanne, "Rabobank", "checking", 184000, sanne.color, order=2
    )
    sanne_save = account(
        "Spaarrekening Sanne", sanne, "Rabobank", "savings", 560000, sanne.color, "private", order=3
    )
    joint = account(
        "Gezamenlijke rekening", None, "ABN AMRO", "checking", 120000, "#0ea5e9", order=4
    )
    joint_save = account("Vakantiepot", None, "ABN AMRO", "savings", 90000, "#10b981", order=5)

    cats = {
        name: _cat(name, "expense")
        for name in (
            "Huur/Hypotheek", "Energie", "Water", "Verzekeringen", "Internet & telefoon", "Abonnementen",
            "Boodschappen", "Uit eten", "Afhalen", "Kleding", "Uitjes", "Hobby's", "Streaming", "Games",
            "Investeren", "Losse grote uitgaven",
        )
    }  # fmt: skip
    salary = _cat("Salaris", "income")
    other_income = _cat("Overige inkomsten", "income")
    tags: dict[str, Tag] = {}

    def tag(name: str) -> Tag:
        if name not in tags:
            tags[name] = Tag(household_id=household.id, name=name)
            db.session.add(tags[name])
            db.session.flush()
        return tags[name]

    count = 0

    def add(d: date, cents: int, acc: Account, cat: Category | None, description: str, payee: str = "",
            tx_type: str = "expense", to: Account | None = None, tag_names=()):  # fmt: skip
        nonlocal count
        if d > today:
            return
        tx = Transaction(
            household_id=household.id, date=d, amount_cents=cents, type=tx_type, account_id=acc.id,
            to_account_id=to.id if to else None, category_id=cat.id if cat else None,
            description=description, payee=payee, created_by_user_id=admin.id, updated_by_user_id=admin.id,
        )  # fmt: skip
        tx.tags = [tag(n) for n in tag_names]
        db.session.add(tx)
        db.session.flush()
        write_audit("transaction", tx.id, "create", None, serialize(tx), admin)
        count += 1

    shops = [
        ("Albert Heijn", 3500, 9500),
        ("Jumbo", 2800, 8000),
        ("Lidl", 2200, 6000),
        ("Plus", 1800, 5200),
    ]
    eats = [
        "Pizzeria Roma",
        "Sushi Bar Tokyo",
        "Café De Hoek",
        "Thais Restaurant Bangkok",
        "Brasserie Het Plein",
    ]
    takeaway = ["Thuisbezorgd", "Domino's", "Febo"]
    clothes = ["H&M", "Zara", "Decathlon", "Bever", "Primark"]

    month_start = begin
    while month_start <= today:
        y, m = month_start.year, month_start.month
        d = lambda day, y=y, m=m: date(y, m, min(day, 28))  # noqa: E731

        add(
            d(25),
            342000 + rng.randrange(-5000, 5000, 100),
            jay_pay,
            salary,
            "Salaris",
            "Werkgever BV",
            "income",
        )
        add(d(25), 289000, sanne_pay, salary, "Salaris", "Zorggroep Nederland", "income")
        if m in (5, 12):
            add(
                d(20),
                95000 if m == 5 else 120000,
                jay_pay,
                other_income,
                "Vakantiegeld" if m == 5 else "Eindejaarsuitkering",
                "Werkgever BV",
                "income",
            )

        # Bijdragen aan de gezamenlijke rekening en sparen
        add(
            d(26),
            80000,
            jay_pay,
            None,
            "Bijdrage gezamenlijke rekening",
            tx_type="transfer",
            to=joint,
        )
        add(
            d(26),
            70000,
            sanne_pay,
            None,
            "Bijdrage gezamenlijke rekening",
            tx_type="transfer",
            to=joint,
        )
        add(d(27), 40000, jay_pay, None, "Sparen", tx_type="transfer", to=jay_save)
        add(d(27), 30000, sanne_pay, None, "Sparen", tx_type="transfer", to=sanne_save)
        add(d(28), 15000, joint, None, "Vakantiepot", tx_type="transfer", to=joint_save)

        # Vaste lasten vanaf de gezamenlijke rekening
        add(d(1), 115000, joint, cats["Huur/Hypotheek"], "Huur", "Woonbedrijf Utrecht")
        add(
            d(3),
            14500 + rng.randrange(-2500, 4000, 100),
            joint,
            cats["Energie"],
            "Gas en licht",
            "Vattenfall",
        )
        add(d(5), 2250, joint, cats["Water"], "Water", "Vitens")
        add(d(7), 20900, joint, cats["Verzekeringen"], "Zorgverzekering", "Zilveren Kruis")
        add(d(7), 4800, joint, cats["Verzekeringen"], "Inboedelverzekering", "Centraal Beheer")
        add(d(9), 4999, joint, cats["Internet & telefoon"], "Internet", "Ziggo")
        add(d(10), 1399, jay_pay, cats["Streaming"], "Netflix", "Netflix")
        add(d(10), 1099, sanne_pay, cats["Streaming"], "Spotify", "Spotify")
        add(d(12), 2999, jay_pay, cats["Abonnementen"], "Sportschool", "Basic-Fit")
        add(d(14), 1950, sanne_pay, cats["Abonnementen"], "Yoga-abonnement", "Yogastudio Zen")
        add(d(15), 2500, jay_pay, cats["Investeren"], "Maandelijks beleggen", "DEGIRO")

        # Dagelijkse uitgaven
        for _ in range(rng.randint(7, 10)):
            shop, lo, hi = rng.choice(shops)
            payer = rng.choice([jay_pay, sanne_pay, joint])
            add(d(rng.randint(1, 28)), rng.randrange(lo, hi, 5), payer, cats["Boodschappen"], shop, shop,
                tag_names=["weekboodschappen"] if rng.random() < 0.3 else ())  # fmt: skip
        for _ in range(rng.randint(1, 3)):
            place = rng.choice(eats)
            payer = rng.choice([jay_pay, sanne_pay])
            add(
                d(rng.randint(1, 28)),
                rng.randrange(3500, 11000, 50),
                payer,
                cats["Uit eten"],
                place,
                place,
                tag_names=["uit"],
            )
        add(
            d(rng.randint(1, 28)),
            rng.randrange(1800, 3600, 50),
            rng.choice([jay_pay, sanne_pay]),
            cats["Afhalen"],
            "Bezorgd eten",
            rng.choice(takeaway),
        )
        if rng.random() < 0.6:
            shop = rng.choice(clothes)
            add(
                d(rng.randint(1, 28)),
                rng.randrange(2500, 12000, 50),
                rng.choice([jay_pay, sanne_pay]),
                cats["Kleding"],
                "Kleding",
                shop,
            )
        if rng.random() < 0.7:
            add(
                d(rng.randint(1, 28)),
                rng.randrange(1500, 6500, 50),
                rng.choice([jay_pay, sanne_pay, joint]),
                cats["Uitjes"],
                "Uitje",
                "Pathé / museum",
            )
        if rng.random() < 0.5:
            add(
                d(rng.randint(1, 28)),
                rng.randrange(1500, 7000, 50),
                jay_pay,
                cats["Hobby's"],
                "Hobby",
                "Bol.com",
            )
        if m == 11:
            add(d(11), 4999, jay_pay, cats["Games"], "Nieuw spel", "Steam")

        # Seizoenen en uitschieters
        if m == 7:
            add(
                d(2),
                185000,
                joint_save,
                cats["Uitjes"],
                "Zomervakantie Italië",
                "Booking.com",
                tag_names=["vakantie"],
            )
            add(
                d(18),
                42000,
                joint,
                cats["Uitjes"],
                "Camping en eten onderweg",
                "Diversen",
                tag_names=["vakantie"],
            )
        if m == 3:
            add(
                d(14), 64900, joint, cats["Losse grote uitgaven"], "Nieuwe wasmachine", "MediaMarkt"
            )
        if m == 10:
            add(d(9), 118000, joint, cats["Losse grote uitgaven"], "Laptop", "Coolblue")
        month_start = add_months_clamped(month_start, 1)

    # Budgetten (standaard elke maand), één persoonlijk en één met rollover
    for name, cents, member, roll in (
        ("Boodschappen", 70000, None, False), ("Uit eten", 15000, None, False), ("Kleding", 12000, None, False),
        ("Uitjes", 12000, None, False), ("Streaming", 3000, None, True), ("Hobby's", 8000, jay.id, False),
        ("Afhalen", 6000, None, False),
    ):  # fmt: skip
        db.session.add(
            Budget(
                household_id=household.id,
                category_id=cats[name].id,
                member_id=member,
                amount_cents=cents,
                rollover=roll,
            )
        )

    # Terugkerende regels
    first_next = add_months_clamped(today.replace(day=1), 1)
    for name, cents, acc, cat, day, auto in (
        ("Huur", 115000, joint, "Huur/Hypotheek", 1, True),
        ("Zorgverzekering", 20900, joint, "Verzekeringen", 7, True),
        ("Internet", 4999, joint, "Internet & telefoon", 9, True),
        ("Netflix", 1399, jay_pay, "Streaming", 10, False),
        ("Sportschool", 2999, jay_pay, "Abonnementen", 12, False),
    ):  # fmt: skip
        nxt = first_next.replace(day=day)
        db.session.add(
            RecurringRule(
                household_id=household.id, name=name, type="expense", amount_cents=cents, account_id=acc.id,
                category_id=cats[cat].id, description=name, frequency="monthly", interval=1, next_date=nxt,
                anchor_day=day, auto_create=auto, tag_ids_json=json.dumps([]),
            )
        )  # fmt: skip
    # Eén termijn die nu bevestigd moet worden
    db.session.add(
        RecurringRule(
            household_id=household.id, name="Contributie hockeyclub", type="expense", amount_cents=6500,
            account_id=sanne_pay.id, category_id=cats["Hobby's"].id, description="Contributie hockeyclub",
            frequency="monthly", interval=1, next_date=today - timedelta(days=2), anchor_day=max(1, (today - timedelta(days=2)).day),
            auto_create=False, tag_ids_json="[]",
        )
    )  # fmt: skip

    # Spaardoelen
    db.session.add_all(
        [
            SavingsGoal(household_id=household.id, account_id=joint_save.id, member_id=None, name="Zomervakantie volgend jaar",
                        target_cents=250000, target_date=add_months_clamped(today, 8), color="#10b981"),
            SavingsGoal(household_id=household.id, account_id=jay_save.id, member_id=jay.id, name="Noodbuffer",
                        target_cents=1500000, target_date=add_months_clamped(today, 14), color="#4f46e5"),
            SavingsGoal(household_id=household.id, account_id=sanne_save.id, member_id=sanne.id, name="Verbouwing keuken",
                        target_cents=800000, target_date=add_months_clamped(today, 5), color="#ec4899"),
        ]
    )  # fmt: skip
    db.session.commit()
    return {"transacties": count, "rekeningen": 6, "leden": 2}
