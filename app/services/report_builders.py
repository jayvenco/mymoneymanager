"""Bouwt de afzonderlijke rapporten uit de rekenlaag."""

from flask import url_for
from sqlalchemy import func, select

from ..extensions import db
from ..models import Account, Category, Tag, Transaction, TransactionTag
from ..utils.dates import MONTHS_SHORT_NL, format_date
from ..utils.money import format_eur
from . import access
from . import budgets as budget_svc
from . import goals as goal_svc
from . import recurring as recurring_svc
from . import report_core as core
from .household import get_household, household_id, members, members_by_id
from .people import HOUSEHOLD, JOINT, PersonFilter, person_transaction_clause
from .report_model import Cell, D, M, N, P, Report, Section, T, series

JOINT_COLOR = "#0ea5e9"
INCOME_COLOR = "#10b981"
EXPENSE_COLOR = "#f43f5e"
NET_COLOR = "#6366f1"


def ym_label(ym: str) -> str:
    year, month = ym.split("-")
    return f"{MONTHS_SHORT_NL[int(month) - 1]} {year[2:]}"


def _pct(part: int, whole: int) -> float | None:
    return (part / whole * 100) if whole else None


def _signed_pct(cur: int, prev: int | None) -> float | None:
    if prev is None or not prev:
        return None
    return (cur - prev) / abs(prev) * 100


def _cat_href(params: core.ReportParams, category: Category, has_children: bool) -> str:
    if has_children:
        return url_for("reports.show", slug="categorieen", cat=category.id, **params.query())
    return url_for(
        "transactions.list_view",
        category=category.id,
        **{"from": format_date(params.period.start), "to": format_date(params.period.end)},
    )


def _owner_list() -> list[tuple[int | None, str, str]]:
    """[(owner_id of None, naam, kleur)] voor alle leden + gezamenlijk."""
    result = [(m.id, m.name, m.color) for m in members()]
    result.append((None, "Gezamenlijk", JOINT_COLOR))
    return result


# --- Overzicht ----------------------------------------------------------------------------------


def build_overview(params: core.ReportParams, user=None) -> Report:
    start_day, today = params.start_day, params.today
    period, cp = params.period, params.compare_period
    cur = core.totals(params, period, user=user)
    prev = core.totals(params, cp, user=user) if cp else None
    as_of = min(period.end, today)
    balance = core.balance_total(params, as_of, user)
    prev_balance = core.balance_total(params, cp.end, user) if cp else None
    elapsed = max(1, (as_of - period.start).days + 1)
    avg = cur.expense // elapsed
    prev_avg = (prev.expense // cp.days) if prev and cp else None

    def kpi(label, value, kind, prev_value, good):
        d = core.delta(value, prev_value) if prev_value is not None else None
        return {"label": label, "value": value, "kind": kind, "delta": d, "good": good}

    sr = cur.savings_rate
    prev_sr = prev.savings_rate if prev else None
    kpis = [
        kpi("Inkomsten", cur.income, "money", prev.income if prev else None, "up"),
        kpi("Uitgaven", cur.expense, "money", prev.expense if prev else None, "down"),
        kpi("Netto resultaat", cur.net, "money", prev.net if prev else None, "up"),
        {
            "label": "Spaarquote",
            "value": sr,
            "kind": "pct",
            "delta": (sr - prev_sr, None) if sr is not None and prev_sr is not None else None,
            "good": "up",
        },
        kpi("Totaal saldo", balance, "money", prev_balance, "up"),
        kpi("Gem. uitgaven per dag", avg, "money", prev_avg, "down"),
    ]

    report = Report("overzicht", "Overzicht")
    report.extras["kpis"] = kpis

    # "Wij samen"-strook
    if params.person.is_household:
        totals_by_owner = core.owner_totals(params, period, user)
        total_expense = sum(t.expense for t in totals_by_owner.values()) or 0
        strip = []
        for owner_id, name, color in _owner_list():
            t = totals_by_owner.get(owner_id, core.Totals())
            strip.append(
                {
                    "name": name,
                    "color": color,
                    "income": t.income,
                    "expense": t.expense,
                    "savings_rate": t.savings_rate,
                    "share": (t.expense / total_expense * 100) if total_expense else 0,
                }
            )
        report.extras["wij_samen"] = strip

    # Sectie: kerncijfers (ook voor export)
    kpi_section = Section(
        "kerncijfers",
        "Kerncijfers",
        ["Kengetal", "Waarde", "Vergelijking", "Verschil"],
        numeric_from=1,
    )
    for k in kpis:
        cell = P if k["kind"] == "pct" else M
        previous = None
        if k["delta"] is not None:
            previous = k["value"] - k["delta"][0]
        kpi_section.rows.append(
            [
                T(k["label"]),
                cell(k["value"]),
                cell(previous) if previous is not None else T("—"),
                (P(k["delta"][0]) if k["kind"] == "pct" else D(k["delta"][0]))
                if k["delta"]
                else T("—"),
            ]
        )
    report.sections.append(kpi_section)

    # Sectie: 12 maanden inkomsten vs uitgaven
    ym_to = ym_to_month = core.months_in(period, start_day)[-1]
    ym_from = core.shift_ym(ym_to_month, -11)
    monthly = core.monthly_income_expense(params, ym_from, ym_to, user=user)
    months = list(monthly)
    section = Section(
        "maanden",
        "Inkomsten en uitgaven per maand",
        ["Maand", "Inkomsten", "Uitgaven", "Netto"],
        chart={
            "type": "bar",
            "labels": [ym_label(m) for m in months],
            "datasets": [
                series("Inkomsten", [monthly[m][0] for m in months], INCOME_COLOR, borderRadius=4),
                series("Uitgaven", [monthly[m][1] for m in months], EXPENSE_COLOR, borderRadius=4),
            ],
        },
    )
    for m in months:
        i, e = monthly[m]
        section.rows.append([T(ym_label(m)), M(i), M(e), D(i - e)])
    section.total = [
        T("Totaal"),
        M(sum(v[0] for v in monthly.values())),
        M(sum(v[1] for v in monthly.values())),
        D(sum(v[0] - v[1] for v in monthly.values())),
    ]
    report.sections.append(section)

    # Sectie: uitgaven per categorie (donut)
    tree = core.category_tree(core.category_totals(params, period, "expense", user=user), "expense")
    grand = sum(n.total for n in tree)
    cat_section = Section(
        "categorieen",
        "Uitgaven per categorie",
        ["Categorie", "Bedrag", "% van totaal"],
        chart={
            "type": "doughnut",
            "labels": [n.category.name for n in tree],
            "datasets": [
                {
                    "data": [round(n.total / 100, 2) for n in tree],
                    "backgroundColor": [n.category.color for n in tree],
                    "borderWidth": 0,
                }
            ],
            "links": [_cat_href(params, n.category, bool(n.children)) for n in tree],
            "legend": True,
        },
    )
    for n in tree:
        cat_section.rows.append(
            [
                T(n.category.name, href=_cat_href(params, n.category, bool(n.children))),
                M(n.total),
                P(_pct(n.total, grand)),
            ]
        )
    cat_section.total = [T("Totaal"), M(grand), P(100.0 if grand else None)]
    report.sections.append(cat_section)

    # Sectie: vermogen per eigenaar (gestapeld vlak)
    nw_months, accounts, balances = core.account_month_end_balances(
        ym_from, ym_to, user=user, person=params.person
    )
    nw = Section(
        "vermogen", "Vermogensontwikkeling", ["Eigenaar"] + [ym_label(m) for m in nw_months]
    )
    datasets = []
    for owner_id, name, color in _owner_list():
        values = [
            sum(balances[a.id][i] for a in accounts if a.owner_member_id == owner_id)
            for i in range(len(nw_months))
        ]
        if any(values) or any(a.owner_member_id == owner_id for a in accounts):
            datasets.append(series(name, values, color, fill=True, pointRadius=0, tension=0.25))
            nw.rows.append([T(name)] + [M(v) for v in values])
    nw.chart = {
        "type": "line",
        "labels": [ym_label(m) for m in nw_months],
        "datasets": datasets,
        "stacked": True,
    }
    report.sections.append(nw)

    # Actiewidgets
    cur_ym = budget_svc.current_ym(today)
    lines = budget_svc.budget_lines(cur_ym, params.person, user=user, today=today)
    lines.sort(key=lambda line: -line.pct)
    report.extras["budget_lines"] = lines[:6]
    report.extras["budget_warnings"] = sum(1 for line in lines if line.state != "ok")
    report.extras["goals"] = goal_svc.progress_for(params.person, user, today)[:4]
    report.extras["pending"] = recurring_svc.pending_confirmations(today, user)
    report.extras["upcoming"] = recurring_svc.upcoming(30, today, user)[:8]
    last = access.visible_transactions(user).where(Transaction.household_id == household_id())
    clause = person_transaction_clause(params.person)
    if clause is not None:
        last = last.where(clause)
    report.extras["recent"] = list(
        db.session.execute(
            last.order_by(Transaction.date.desc(), Transaction.id.desc()).limit(10)
        ).scalars()
    )
    return report


# --- Cashflow -----------------------------------------------------------------------------------


def build_cashflow(params: core.ReportParams, user=None) -> Report:
    start_day = params.start_day
    ym_from, ym_to = core.window_for(params.period, start_day, 24)
    monthly = core.monthly_income_expense(params, ym_from, ym_to, user=user)
    months = list(monthly)
    show_transfers = not params.person.is_household
    columns = ["Maand", "Inkomsten", "Uitgaven", "Netto"] + (
        ["Netto overboekingen"] if show_transfers else []
    )
    section = Section(
        "cashflow",
        "Cashflow per maand",
        columns,
        chart={
            "type": "bar",
            "labels": [ym_label(m) for m in months],
            "datasets": [
                series("Inkomsten", [monthly[m][0] for m in months], INCOME_COLOR, borderRadius=4),
                series("Uitgaven", [monthly[m][1] for m in months], EXPENSE_COLOR, borderRadius=4),
                series(
                    "Netto",
                    [monthly[m][0] - monthly[m][1] for m in months],
                    NET_COLOR,
                    type="line",
                    backgroundColor=NET_COLOR,
                    tension=0.3,
                    pointRadius=3,
                ),
            ],
        },
        note=(
            "Overboekingen tellen niet mee als inkomsten of uitgaven; voor dit persoonsfilter staan ze "
            "apart als netto (inkomend minus uitgaand)."
            if show_transfers
            else "Overboekingen tussen rekeningen zijn weggelaten: ze beïnvloeden inkomsten en uitgaven niet."
        ),
    )
    total_i = total_e = total_t = 0
    for ym in months:
        i, e = monthly[ym]
        row = [T(ym_label(ym)), M(i), M(e), D(i - e)]
        if show_transfers:
            window = core.financial_month(ym, start_day)
            incoming, outgoing = core.transfer_net(params, window, user)
            row.append(D(incoming - outgoing))
            total_t += incoming - outgoing
        section.rows.append(row)
        total_i, total_e = total_i + i, total_e + e
    section.total = [T("Totaal"), M(total_i), M(total_e), D(total_i - total_e)] + (
        [D(total_t)] if show_transfers else []
    )
    return Report("cashflow", "Cashflow", [section])


# --- Categorieën --------------------------------------------------------------------------------


def build_categories(params: core.ReportParams, user=None, cat_id: int | None = None) -> Report:
    report = Report("categorieen", "Categorieën")
    cp = params.compare_period
    for kind, title in (("expense", "Uitgaven"), ("income", "Inkomsten")):
        cur_tree = core.category_tree(
            core.category_totals(params, params.period, kind, user=user), kind
        )
        prev_totals = core.category_totals(params, cp, kind, user=user) if cp else {}
        prev_by_parent = (
            {n.category.id: n.total for n in core.category_tree(prev_totals, kind)} if cp else {}
        )
        prev_by_cat = prev_totals
        nodes = cur_tree
        drill = None
        if cat_id:
            nodes = [n for n in cur_tree if n.category.id == cat_id]
            if not nodes:
                continue
            drill = nodes[0].category
        grand = sum(n.total for n in cur_tree) or 0
        columns = ["Categorie", "Bedrag", "% van totaal"] + (
            ["Vergelijking", "Verschil", "Verschil %"] if cp else []
        )
        section = Section(
            f"cat-{kind}",
            f"{title}{' — ' + drill.name if drill else ''}",
            columns,
            chart={
                "type": "doughnut",
                "labels": [n.category.name for n in nodes],
                "datasets": [
                    {
                        "data": [round(n.total / 100, 2) for n in nodes],
                        "backgroundColor": [n.category.color for n in nodes],
                        "borderWidth": 0,
                    }
                ],
                "links": [_cat_href(params, n.category, bool(n.children)) for n in nodes],
                "legend": True,
            }
            if kind == "expense" and not drill
            else None,
        )

        def comparison(cur_value: int, prev_value: int | None) -> list[Cell]:
            if not cp:
                return []
            prev_value = prev_value or 0
            return [M(prev_value), D(cur_value - prev_value), P(_signed_pct(cur_value, prev_value))]

        for n in nodes:
            if drill:
                section.rows.append(
                    [
                        T(n.category.name + " (algemeen)", bold=True),
                        M(n.own),
                        P(_pct(n.own, n.total)),
                    ]
                    + comparison(n.own, prev_by_cat.get(n.category.id))
                )
            else:
                section.rows.append(
                    [
                        T(
                            n.category.name,
                            bold=True,
                            href=_cat_href(params, n.category, bool(n.children)),
                        ),
                        M(n.total),
                        P(_pct(n.total, grand)),
                    ]
                    + comparison(n.total, prev_by_parent.get(n.category.id))
                )
            for child in n.children:
                section.rows.append(
                    [
                        T(
                            child.category.name,
                            indent=1,
                            href=_cat_href(params, child.category, False),
                        ),
                        M(child.total),
                        P(_pct(child.total, n.total if drill else grand)),
                    ]
                    + comparison(child.total, prev_by_cat.get(child.category.id))
                )
        total = sum(n.total for n in nodes)
        section.total = [T("Totaal"), M(total), P(100.0 if total else None)]
        if cp:
            prev_total = sum(prev_by_parent.get(n.category.id, 0) for n in nodes)
            section.total += [
                M(prev_total),
                D(total - prev_total),
                P(_signed_pct(total, prev_total)),
            ]
        if section.rows:
            report.sections.append(section)
    if cat_id:
        report.extras["drill_back"] = url_for("reports.show", slug="categorieen", **params.query())
        report.extras["drill_transactions"] = url_for(
            "transactions.list_view",
            category=cat_id,
            **{"from": format_date(params.period.start), "to": format_date(params.period.end)},
        )
    return report


# --- Per persoon --------------------------------------------------------------------------------


def build_per_person(params: core.ReportParams, user=None) -> Report:
    """Verschillen tussen de partners: inzicht, geen verrekening."""
    report = Report("per-persoon", "Per persoon")
    period, cp, start_day = params.period, params.compare_period, params.start_day
    people = members()
    first = people[0] if people else None
    second = people[1] if len(people) > 1 else None
    owners: list[tuple[int | None, str, str]] = [(m.id, m.name, m.color) for m in people[:2]] + [
        (None, "Gezamenlijk", JOINT_COLOR)
    ]
    ot = core.owner_totals(params, period, user)
    deposits = core.owner_savings_deposits(params, period, [o[0] for o in owners], user)
    household = core.Totals(
        sum(t.income for t in ot.values()),
        sum(t.expense for t in ot.values()),
        sum(deposits.values()),
    )

    def t_of(owner: int | None) -> core.Totals:
        return ot.get(owner, core.Totals())

    show_diff = first is not None and second is not None
    columns = (
        ["Onderdeel"]
        + [o[1] for o in owners]
        + ["Huishouden totaal"]
        + ([f"Verschil ({first.name} − {second.name})", "Verschil %"] if show_diff else [])
    )

    def metric_row(label: str, getter, kind: str = "money", bold: bool = False) -> list[Cell]:
        make = P if kind == "pct" else M
        vals = [getter(t_of(o[0]), o[0]) for o in owners]
        total_value = getter(household, "household")
        row = [T(label, bold=bold)] + [make(v) for v in vals] + [make(total_value)]
        if show_diff:
            a, b = vals[0], vals[1]
            if a is None or b is None:
                row += [T("—"), T("—")]
            elif kind == "pct":
                row += [P(a - b), T("")]
            else:
                row += [D(a - b), P(_signed_pct(a, b))]
        return row

    summary = Section("samenvatting", "Totalen per persoon", columns)
    summary.rows += [
        metric_row("Inkomsten", lambda t, o: t.income),
        metric_row("Uitgaven", lambda t, o: t.expense),
        metric_row("Netto", lambda t, o: t.net, bold=True),
        metric_row("Spaarquote", lambda t, o: t.savings_rate, kind="pct"),
        metric_row(
            "Stortingen op spaarrekeningen",
            lambda t, o: (
                deposits.get(o, household.deposits) if o != "household" else household.deposits
            ),
        ),
    ]
    summary.note = (
        "Het persoonsfilter geldt niet voor dit rapport: alle personen staan naast elkaar. "
        "Huishouden totaal = "
        + " + ".join(o[1] for o in owners)
        + "; overboekingen zijn weggelaten."
    )
    report.sections.append(summary)

    # Uitgaven per categorie
    cat_cur = core.owner_category_totals(params, period, "expense", user)
    cat_prev = core.owner_category_totals(params, cp, "expense", user) if cp else {}
    cats = list(
        db.session.execute(
            select(Category).where(
                Category.household_id == household_id(), Category.kind == "expense"
            )
        ).scalars()
    )
    by_parent: dict[int | None, list[Category]] = {}
    for c in sorted(cats, key=lambda c: (c.sort_order, c.name)):
        by_parent.setdefault(c.parent_id, []).append(c)

    def own_amount(owner, cid, source=None):
        return (source or cat_cur).get((owner, cid), 0)

    def node_amount(owner, category: Category, source=None) -> int:
        return own_amount(owner, category.id, source) + sum(
            own_amount(owner, c.id, source) for c in by_parent.get(category.id, [])
        )

    cat_section = Section("categorieen", "Uitgaven per categorie", columns)
    share_section = Section(
        "aandeel",
        "Aandeel in de uitgaven per categorie",
        ["Categorie"] + [o[1] for o in owners] + ["Wie geeft meer uit?"],
    )
    chart_labels, chart_a, chart_b = [], [], []
    parent_rows = []
    for parent in by_parent.get(None, []):
        amounts = [node_amount(o[0], parent) for o in owners]
        total = sum(amounts)
        if total:
            parent_rows.append((parent, amounts, total))
    parent_rows.sort(key=lambda r: -r[2])

    for parent, amounts, total in parent_rows:
        row = [T(parent.name, bold=True)]
        a, b = amounts[0], amounts[1] if show_diff else 0
        color = (first.color if a > b else second.color) if show_diff and a != b else None
        for i, value in enumerate(amounts):
            winner = show_diff and i < 2 and a != b and ((i == 0) == (a > b))
            row.append(M(value, color=owners[i][2] if winner else None, bold=winner))
        row.append(M(total))
        if show_diff:
            row += [D(a - b), P(_signed_pct(a, b))]
        cat_section.rows.append(row)
        share_section.rows.append(
            [T(parent.name, bold=True)]
            + [P(_pct(v, total)) for v in amounts]
            + [
                T(
                    (first.name if a > b else second.name) if show_diff and a != b else "Gelijk",
                    color=color,
                )
            ]
        )
        for child in by_parent.get(parent.id, []):
            sub = [node_amount(o[0], child) for o in owners]
            if not sum(sub):
                continue
            ca, cb = sub[0], sub[1] if show_diff else 0
            crow = [T(child.name, indent=1)] + [M(v) for v in sub] + [M(sum(sub))]
            if show_diff:
                crow += [D(ca - cb), P(_signed_pct(ca, cb))]
            cat_section.rows.append(crow)
        if len(chart_labels) < 8:
            chart_labels.append(parent.name)
            chart_a.append(a)
            chart_b.append(b)
    cat_section.total = (
        [T("Totaal")] + [M(t_of(o[0]).expense) for o in owners] + [M(household.expense)]
    )
    if show_diff:
        cat_section.total += [
            D(t_of(first.id).expense - t_of(second.id).expense),
            P(_signed_pct(t_of(first.id).expense, t_of(second.id).expense)),
        ]
    if show_diff:
        cat_section.chart = {
            "type": "bar",
            "labels": chart_labels,
            "datasets": [
                series(first.name, chart_a, first.color, borderRadius=4),
                series(second.name, chart_b, second.color, borderRadius=4),
            ],
        }
    report.sections += [cat_section, share_section]

    # Bijdragen aan gezamenlijke rekeningen
    contrib = core.joint_contributions(params, period, user)
    contrib_section = Section(
        "bijdragen",
        "Bijdragen aan gezamenlijke rekeningen",
        ["Persoon", "Overgemaakt", "% van eigen inkomsten"],
    )
    for m in people[:2]:
        amount = contrib.get(m.id, 0)
        contrib_section.rows.append(
            [T(m.name, color=m.color), M(amount), P(_pct(amount, t_of(m.id).income))]
        )
    contrib_section.total = [T("Totaal"), M(sum(contrib.get(m.id, 0) for m in people[:2])), T("")]
    report.sections.append(contrib_section)

    # Verloop per maand: uitgaven per persoon en aandeel
    ym_from, ym_to = core.window_for(period, start_day, 12)
    months = core.months_between(ym_from, ym_to)
    per_owner = {
        o[0]: core.monthly_income_expense(
            params,
            ym_from,
            ym_to,
            person=JOINT if o[0] is None else PersonFilter(f"member:{o[0]}", "member", o[0]),
            user=user,
        )
        for o in owners
    }
    line_section = Section(
        "verloop",
        "Uitgaven per persoon per maand",
        ["Maand"] + [o[1] for o in owners],
        chart={
            "type": "line",
            "labels": [ym_label(m) for m in months],
            "datasets": [
                series(
                    o[1],
                    [per_owner[o[0]][m][1] for m in months],
                    o[2],
                    fill=False,
                    tension=0.3,
                    backgroundColor=o[2],
                )
                for o in owners
            ],
        },
    )
    share_chart_sets = []
    for o in owners:
        values = []
        for m in months:
            total = sum(per_owner[x[0]][m][1] for x in owners)
            values.append(round(per_owner[o[0]][m][1] / total * 100, 1) if total else 0)
        share_chart_sets.append(
            {"label": o[1], "data": values, "backgroundColor": o[2], "borderColor": o[2]}
        )
    for m in months:
        line_section.rows.append([T(ym_label(m))] + [M(per_owner[o[0]][m][1]) for o in owners])
    report.sections.append(line_section)
    report.sections.append(
        Section(
            "aandeel-maand",
            "Aandeel in de uitgaven van het huishouden per maand",
            chart={
                "type": "bar",
                "labels": [ym_label(m) for m in months],
                "datasets": share_chart_sets,
                "stacked": True,
                "currency": False,
                "options": {"scales": {"y": {"max": 100, "stacked": True}, "x": {"stacked": True}}},
            },
            note="Elke balk is 100% van de uitgaven van het huishouden in die maand.",
        )
    )

    # Vergelijking met de vorige periode per persoon
    if cp:
        for m in people[:2]:
            for parent, _amounts, _total in parent_rows:
                now_value = node_amount(m.id, parent)
                before = node_amount(m.id, parent, cat_prev)
                change = _signed_pct(now_value, before)
                if change is not None and abs(change) >= 5 and max(now_value, before) >= 1000:
                    direction = "meer" if change > 0 else "minder"
                    report.insights.append(
                        f"{m.name} gaf {abs(change):.0f}% {direction} uit aan {parent.name} "
                        f"({format_eur(now_value)} tegenover {format_eur(before)})."
                    )
        report.insights = report.insights[:12]
    return report


# --- Trends -------------------------------------------------------------------------------------


def build_trends(
    params: core.ReportParams, user=None, selected: list[int] | None = None, stacked: bool = False
) -> Report:
    start_day = params.start_day
    ym_from, ym_to = core.window_for(params.period, start_day, 24)
    months = core.months_between(ym_from, ym_to)
    matrix = core.category_month_matrix(params, ym_from, ym_to, "expense", user=user)
    cats = {
        c.id: c
        for c in db.session.execute(
            select(Category).where(
                Category.household_id == household_id(), Category.kind == "expense"
            )
        ).scalars()
    }
    # Rol subcategorieën op naar hoofdcategorieën.
    parents: dict[tuple[int, str], int] = {}
    for (cid, ym), value in matrix.items():
        top = cats[cid].parent_id or cid
        parents[(top, ym)] = parents.get((top, ym), 0) + value
    totals_by_cat: dict[int, int] = {}
    for (cid, _ym), value in parents.items():
        totals_by_cat[cid] = totals_by_cat.get(cid, 0) + value
    ranked = [cid for cid, _ in sorted(totals_by_cat.items(), key=lambda kv: -kv[1])]
    chosen = [c for c in (selected or []) if c in cats and not cats[c].parent_id] or ranked[:5]

    section = Section(
        "trends",
        "Uitgaven per categorie over tijd",
        ["Categorie"] + [ym_label(m) for m in months] + ["Totaal"],
        chart={
            "type": "line",
            "labels": [ym_label(m) for m in months],
            "datasets": [
                series(
                    cats[c].name,
                    [parents.get((c, m), 0) for m in months],
                    cats[c].color,
                    fill=stacked,
                    tension=0.3,
                    pointRadius=2,
                    backgroundColor=cats[c].color + ("99" if stacked else ""),
                )
                for c in chosen
            ],
            "stacked": stacked,
        },
    )
    for c in chosen:
        values = [parents.get((c, m), 0) for m in months]
        section.rows.append(
            [T(cats[c].name, color=cats[c].color)] + [M(v) for v in values] + [M(sum(values))]
        )
    section.total = (
        [T("Totaal")]
        + [M(sum(parents.get((c, m), 0) for c in chosen)) for m in months]
        + [M(sum(parents.get((c, m), 0) for c in chosen for m in months))]
    )
    report = Report("trends", "Trends", [section])
    report.extras["selectable"] = [(c, cats[c].name, cats[c].color) for c in ranked]
    report.extras["selected"] = chosen
    report.extras["stacked"] = stacked
    return report


# --- Tags ---------------------------------------------------------------------------------------


def build_tags(params: core.ReportParams, user=None, selected: list[int] | None = None) -> Report:
    period = params.period
    tags = {
        t.id: t
        for t in db.session.execute(
            access.visible_tags(user).where(Tag.household_id == household_id())
        ).scalars()
    }
    rows = db.session.execute(
        core.base_query(
            params,
            period,
            [
                TransactionTag.c.tag_id,
                Transaction.type,
                core._sum_amount(),
                func.count(Transaction.id),
            ],
            user=user,
        )
        .join(TransactionTag, TransactionTag.c.transaction_id == Transaction.id)
        .group_by(TransactionTag.c.tag_id, Transaction.type)
    )
    data: dict[int, dict] = {}
    for tag_id, tx_type, amount, count in rows:
        d = data.setdefault(tag_id, {"income": 0, "expense": 0, "count": 0})
        d[tx_type] = int(amount)
        d["count"] += int(count)
    section = Section(
        "tags", "Totalen per tag", ["Tag", "Transacties", "Inkomsten", "Uitgaven", "Netto"]
    )
    ordered = sorted(data.items(), key=lambda kv: -(kv[1]["expense"] + kv[1]["income"]))
    for tag_id, d in ordered:
        if tag_id in tags:
            section.rows.append(
                [
                    T("#" + tags[tag_id].name, color=tags[tag_id].color),
                    N(d["count"]),
                    M(d["income"]),
                    M(d["expense"]),
                    D(d["income"] - d["expense"]),
                ]
            )
    section.chart = (
        {
            "type": "bar",
            "labels": ["#" + tags[t].name for t, _ in ordered[:12] if t in tags],
            "datasets": [
                series(
                    "Uitgaven",
                    [d["expense"] for t, d in ordered[:12] if t in tags],
                    EXPENSE_COLOR,
                    borderRadius=4,
                )
            ],
            "legend": False,
        }
        if section.rows
        else None
    )
    report = Report("tags", "Tags", [section])
    chosen = [t for t in (selected or []) if t in tags]
    if chosen:
        sub = (
            core.base_query(
                params,
                period,
                [Transaction.type, core._sum_amount(), func.count(Transaction.id)],
                user=user,
            )
            .where(
                Transaction.id.in_(
                    select(TransactionTag.c.transaction_id).where(
                        TransactionTag.c.tag_id.in_(chosen)
                    )
                )
            )
            .group_by(Transaction.type)
        )
        combined = {t: (int(a), int(n)) for t, a, n in db.session.execute(sub)}
        income, expense = combined.get("income", (0, 0))[0], combined.get("expense", (0, 0))[0]
        count = combined.get("income", (0, 0))[1] + combined.get("expense", (0, 0))[1]
        report.sections.append(
            Section(
                "gecombineerd",
                "Gecombineerd: " + ", ".join("#" + tags[t].name for t in chosen),
                ["Transacties", "Inkomsten", "Uitgaven", "Netto"],
                rows=[[N(count), M(income), M(expense), D(income - expense)]],
                note="Transacties met meerdere geselecteerde tags worden maar één keer geteld.",
            )
        )
    report.extras["all_tags"] = [
        (t.id, t.name, t.color) for t in sorted(tags.values(), key=lambda t: t.name.lower())
    ]
    report.extras["selected"] = chosen
    return report


# --- Budget vs werkelijk ------------------------------------------------------------------------


def build_budget_vs_actual(params: core.ReportParams, user=None) -> Report:
    start_day = params.start_day
    ym_from, ym_to = core.window_for(params.period, start_day, 12)
    months = core.months_between(ym_from, ym_to)
    person = params.person
    all_budgets = budget_svc.budgets_in_range(ym_from, ym_to)
    if person.kind == "member":
        keys = {
            (b.category_id, b.member_id) for b in all_budgets if b.member_id == person.member_id
        }
        kwargs = {"member_id": person.member_id}
    else:
        keys = {(b.category_id, b.member_id) for b in all_budgets if b.member_id is None}
        kwargs = {"joint_only": True} if person.kind == "joint" else {}
    spent = budget_svc.spent_map(ym_from, ym_to, user=user, **kwargs)
    cats = {
        c.id: c
        for c in db.session.execute(
            select(Category).where(Category.household_id == household_id())
        ).scalars()
    }
    section = Section(
        "budget",
        "Budget versus werkelijk per maand",
        ["Categorie"] + [ym_label(m) for m in months],
        heatmap=True,
        note="Groen: onder 80% van het budget · oranje: 80–100% · rood: boven 100%. Rollover is hier niet meegeteld.",
    )
    for category_id, member_id in sorted(
        keys, key=lambda k: (cats[k[0]].sort_order, cats[k[0]].name)
    ):
        row = [T(cats[category_id].name, bold=True)]
        for ym in months:
            b = budget_svc._amount_for(all_budgets, category_id, member_id, ym)
            s = spent.get((category_id, ym), 0)
            if b is None:
                row.append(T("—"))
                continue
            pct = (s / b.amount_cents * 100) if b.amount_cents else (0 if not s else 999)
            heat = "over" if pct > 100 else "warn" if pct >= 80 else "ok"
            row.append(Cell(s, "money", heat=heat, note=f"van {format_eur(b.amount_cents)}"))
        section.rows.append(row)
    return Report("budget", "Budget vs werkelijk", [section])


# --- Jaaroverzicht ------------------------------------------------------------------------------


def build_year(params: core.ReportParams, user=None, year: int | None = None) -> Report:
    start_day = params.start_day
    year = year or int(core.ym_of(params.period.end, start_day).split("-")[0])
    ym_from, ym_to = f"{year}-01", f"{year}-12"
    months = core.months_between(ym_from, ym_to)
    report = Report("jaar", f"Jaaroverzicht {year}")
    cats = {
        c.id: c
        for c in db.session.execute(
            select(Category).where(Category.household_id == household_id())
        ).scalars()
    }
    for kind, title in (("income", "Inkomsten"), ("expense", "Uitgaven")):
        matrix = core.category_month_matrix(params, ym_from, ym_to, kind, user=user)
        section = Section(
            f"jaar-{kind}", title, ["Categorie"] + [ym_label(m)[:3] for m in months] + ["Totaal"]
        )
        parents: dict[tuple[int, str], int] = {}
        children: dict[tuple[int, str], int] = {}
        for (cid, ym), value in matrix.items():
            top = cats[cid].parent_id or cid
            parents[(top, ym)] = parents.get((top, ym), 0) + value
            if cats[cid].parent_id:
                children[(cid, ym)] = value
        order = sorted({k[0] for k in parents}, key=lambda c: (cats[c].sort_order, cats[c].name))
        col_totals = [0] * len(months)
        for top in order:
            values = [parents.get((top, m), 0) for m in months]
            section.rows.append(
                [T(cats[top].name, bold=True)]
                + [M(v) for v in values]
                + [M(sum(values), bold=True)]
            )
            col_totals = [a + b for a, b in zip(col_totals, values, strict=True)]
            for child in sorted(
                {k[0] for k in children if cats[k[0]].parent_id == top}, key=lambda c: cats[c].name
            ):
                cv = [children.get((child, m), 0) for m in months]
                section.rows.append(
                    [T(cats[child].name, indent=1)] + [M(v) for v in cv] + [M(sum(cv))]
                )
        section.total = [T("Totaal")] + [M(v) for v in col_totals] + [M(sum(col_totals))]
        if section.rows:
            report.sections.append(section)
    income = next((s for s in report.sections if s.key == "jaar-income"), None)
    expense = next((s for s in report.sections if s.key == "jaar-expense"), None)
    if income and expense and income.total and expense.total:
        net = Section(
            "jaar-netto", "Netto per maand", [""] + [ym_label(m)[:3] for m in months] + ["Totaal"]
        )
        net.rows.append(
            [T("Netto")]
            + [D(a.v - b.v) for a, b in zip(income.total[1:], expense.total[1:], strict=True)]
        )
        report.sections.append(net)
    report.extras["year"] = year
    return report


# --- Vermogen & sparen --------------------------------------------------------------------------


def build_net_worth(params: core.ReportParams, user=None) -> Report:
    start_day = params.start_day
    ym_from, ym_to = core.window_for(params.period, start_day, 24)
    months, accounts, balances = core.account_month_end_balances(
        ym_from, ym_to, user=user, person=params.person
    )
    owners = members_by_id()
    report = Report("vermogen", "Vermogen & sparen")

    per_owner = Section(
        "vermogen-lid", "Vermogen per persoon", ["Eigenaar"] + [ym_label(m) for m in months]
    )
    datasets = []
    for owner_id, name, color in _owner_list():
        mine = [a for a in accounts if a.owner_member_id == owner_id]
        if not mine:
            continue
        values = [sum(balances[a.id][i] for a in mine) for i in range(len(months))]
        per_owner.rows.append([T(name, color=color)] + [M(v) for v in values])
        datasets.append(
            series(
                name,
                values,
                color,
                fill=True,
                tension=0.25,
                pointRadius=0,
                backgroundColor=color + "99",
            )
        )
    per_owner.total = [T("Totaal")] + [
        M(sum(balances[a.id][i] for a in accounts)) for i in range(len(months))
    ]
    per_owner.chart = {
        "type": "line",
        "labels": [ym_label(m) for m in months],
        "datasets": datasets,
        "stacked": True,
    }
    report.sections.append(per_owner)

    per_account = Section(
        "vermogen-rekening",
        "Saldo per rekening",
        ["Rekening", "Eigenaar", "Type", "Huidig saldo"],
        numeric_from=3,
    )
    type_label = {
        "checking": "Betaalrekening",
        "savings": "Spaarrekening",
        "investment": "Beleggen",
        "cash": "Contant",
    }
    for a in accounts:
        owner = owners.get(a.owner_member_id)
        per_account.rows.append(
            [
                T(a.name),
                T(
                    owner.name if owner else "Gezamenlijk",
                    color=owner.color if owner else JOINT_COLOR,
                ),
                T(type_label[a.type]),
                M(balances[a.id][-1] if balances[a.id] else 0),
            ]
        )
    per_account.total = [
        T("Totaal"),
        T(""),
        T(""),
        M(sum(balances[a.id][-1] for a in accounts if balances[a.id])),
    ]
    report.sections.append(per_account)

    goals_section = Section(
        "spaardoelen",
        "Spaardoelen",
        ["Doel", "Gespaard", "Doel", "Voortgang", "Nodig per maand", "Status"],
        numeric_from=1,
    )
    status_text = {
        "behaald": "Behaald",
        "op_schema": "Op schema",
        "achter": "Achter",
        "geen_deadline": "Geen deadline",
    }
    for p in goal_svc.progress_for(params.person, user, params.today):
        goals_section.rows.append(
            [
                T(p.goal.name),
                M(p.current_cents),
                M(p.goal.target_cents),
                P(p.pct),
                M(p.needed_monthly_cents) if p.needed_monthly_cents is not None else T("—"),
                T(status_text[p.status]),
            ]
        )
    if goals_section.rows:
        report.sections.append(goals_section)
    return report


# --- Toplijsten ---------------------------------------------------------------------------------


def build_top_lists(params: core.ReportParams, user=None) -> Report:
    period = params.period
    report = Report("toplijsten", "Toplijsten")
    payee = func.coalesce(func.nullif(func.trim(Transaction.payee), ""), "(geen begunstigde)")
    rows = db.session.execute(
        core.base_query(
            params,
            period,
            [payee.label("p"), core._sum_amount().label("s"), func.count(Transaction.id)],
            types=("expense",),
            user=user,
        )
        .group_by(payee)
        .order_by(func.sum(Transaction.amount_cents).desc())
        .limit(15)
    ).all()
    payees = Section(
        "payees",
        "Top begunstigden (uitgaven)",
        ["Begunstigde", "Transacties", "Totaal"],
        numeric_from=1,
    )
    payees.rows = [[T(p), N(n), M(int(s))] for p, s, n in rows]
    payees.chart = (
        {
            "type": "bar",
            "labels": [r[0].v for r in payees.rows[:10]],
            "datasets": [
                series(
                    "Uitgaven", [r[2].v for r in payees.rows[:10]], EXPENSE_COLOR, borderRadius=4
                )
            ],
            "legend": False,
            "options": {"indexAxis": "y"},
        }
        if payees.rows
        else None
    )
    report.sections.append(payees)

    big = (
        db.session.execute(
            core.base_query(params, period, [Transaction], types=("expense",), user=user)
            .order_by(Transaction.amount_cents.desc(), Transaction.id)
            .limit(10)
        )
        .scalars()
        .all()
    )
    largest = Section(
        "grootste",
        "10 grootste uitgaven",
        ["Datum", "Omschrijving", "Categorie", "Bedrag"],
        numeric_from=3,
    )
    for tx in big:
        largest.rows.append(
            [
                T(format_date(tx.date)),
                T(
                    tx.description or tx.payee or "—",
                    href=url_for("transactions.detail", tx_id=tx.id),
                ),
                T(tx.category.name if tx.category else "—"),
                M(tx.amount_cents),
            ]
        )
    report.sections.append(largest)

    tag_rows = db.session.execute(
        core.base_query(
            params, period, [TransactionTag.c.tag_id, func.count(Transaction.id)], user=user
        )
        .join(TransactionTag, TransactionTag.c.transaction_id == Transaction.id)
        .group_by(TransactionTag.c.tag_id)
        .order_by(func.count(Transaction.id).desc())
        .limit(10)
    ).all()
    tags = {
        t.id: t
        for t in db.session.execute(
            access.visible_tags(user).where(Tag.household_id == household_id())
        ).scalars()
    }
    used = Section("tags-gebruik", "Meest gebruikte tags", ["Tag", "Transacties"], numeric_from=1)
    used.rows = [
        [T("#" + tags[tid].name, color=tags[tid].color), N(n)] for tid, n in tag_rows if tid in tags
    ]
    report.sections.append(used)
    return report


# --- Losse grote uitgaven -----------------------------------------------------------------------


def build_one_offs(params: core.ReportParams, user=None) -> Report:
    period = params.period
    txs = (
        db.session.execute(
            core.base_query(
                params,
                period,
                [Transaction],
                types=("expense",),
                user=user,
                one_off_only=True,
                respect_one_off_toggle=False,
            ).order_by(Transaction.date.desc(), Transaction.id.desc())
        )
        .scalars()
        .all()
    )
    section = Section(
        "losse",
        "Losse grote uitgaven",
        ["Datum", "Omschrijving", "Rekening", "Bedrag"],
        numeric_from=3,
    )
    for tx in txs:
        section.rows.append(
            [
                T(format_date(tx.date)),
                T(
                    tx.description or tx.payee or "—",
                    href=url_for("transactions.detail", tx_id=tx.id),
                ),
                T(tx.account.name),
                M(tx.amount_cents),
            ]
        )
    section.total = [T("Totaal"), T(""), T(""), M(sum(t.amount_cents for t in txs))]
    normal = core.totals(
        core.ReportParams(
            params.period_key,
            params.period,
            params.compare,
            params.compare_period,
            params.person,
            True,
            params.today,
        ),
        period,
        user=user,
    )
    section.note = (
        f"Uitgaven zonder losse grote uitgaven in deze periode: {format_eur(normal.expense)}. "
        "Schakel hieronder het filter in om dit in alle rapporten te zien."
    )
    return Report("losse-uitgaven", "Losse grote uitgaven", [section])


BUILDERS = {
    "cashflow": build_cashflow,
    "categorieen": build_categories,
    "per-persoon": build_per_person,
    "trends": build_trends,
    "tags": build_tags,
    "budget": build_budget_vs_actual,
    "jaar": build_year,
    "vermogen": build_net_worth,
    "toplijsten": build_top_lists,
    "losse-uitgaven": build_one_offs,
}

REPORT_TABS = [
    ("overview", "Overzicht"),
    ("cashflow", "Cashflow"),
    ("categorieen", "Categorieën"),
    ("per-persoon", "Per persoon"),
    ("trends", "Trends"),
    ("tags", "Tags"),
    ("budget", "Budget vs werkelijk"),
    ("jaar", "Jaaroverzicht"),
    ("vermogen", "Vermogen & sparen"),
    ("toplijsten", "Toplijsten"),
    ("losse-uitgaven", "Losse grote uitgaven"),
]

__all__ = ["BUILDERS", "REPORT_TABS", "build_overview", "get_household", "Account", "HOUSEHOLD"]
