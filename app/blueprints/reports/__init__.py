"""Rapportages: het startscherm van de app, met alle rapporten en exports."""

from datetime import date

from flask import Blueprint, Response, abort, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ...extensions import db
from ...services import report_core as core
from ...services import report_export as export
from ...services.household import get_household, members_by_id
from ...services.people import current_person
from ...services.report_builders import (
    BUILDERS,
    REPORT_TABS,
    build_categories,
    build_overview,
    build_tags,
    build_trends,
    build_year,
)
from ...services.transactions import _int, describe_for_person
from ...utils.dates import format_date, parse_date

bp = Blueprint("reports", __name__)

COMPARE_LABELS = dict(core.COMPARE_CHOICES)
PERIOD_LABELS = dict(core.PERIOD_CHOICES)


def _pref(name: str, default: str) -> str:
    return current_user.preferences.get(name, default)


def build_params() -> core.ReportParams:
    """Leest periode, vergelijking en filters uit de URL; valt terug op (en bewaart) voorkeuren."""
    args = request.args
    hh = get_household()
    period_key = args.get("periode") or _pref("report_period", "this_month")
    if period_key not in PERIOD_LABELS:
        period_key = "this_month"
    compare = args.get("vergelijk") if "vergelijk" in args else _pref("report_compare", "none")
    if compare not in COMPARE_LABELS:
        compare = "none"
    hide = (args.get("losse") if "losse" in args else _pref("report_hide_one_off", "")) == "uit"

    custom_from = custom_to = None
    if period_key == "custom":
        try:
            custom_from, custom_to = (
                parse_date(args.get("van", "")),
                parse_date(args.get("tot", "")),
            )
        except ValueError:
            period_key = "this_month"
    today = date.today()
    period = core.resolve_period(
        period_key, hh.financial_month_start_day, today, custom_from, custom_to
    )
    if period_key == "custom" and period.label != "Aangepast":
        period_key = "this_month"

    changed = False
    for name, value in (
        ("report_period", period_key),
        ("report_compare", compare),
        ("report_hide_one_off", "uit" if hide else ""),
    ):
        if (
            any(k in args for k in ("periode", "vergelijk", "losse"))
            and current_user.preferences.get(name) != value
        ):
            current_user.set_preference(name, value)
            changed = True
    if changed:
        db.session.commit()
    return core.ReportParams(
        period_key=period_key,
        period=period,
        compare=compare,
        compare_period=core.comparison_period(period, compare, hh.financial_month_start_day),
        person=current_person(),
        hide_one_off=hide,
        today=today,
    )


def meta_for(params: core.ReportParams) -> dict[str, str]:
    person = {"household": "Huishouden (totaal)", "joint": "Gezamenlijk"}.get(params.person.key)
    if person is None:
        person = members_by_id()[params.person.member_id].name
    meta = {
        "Periode": f"{format_date(params.period.start)} t/m {format_date(params.period.end)}",
        "Persoon": person,
    }
    if params.compare_period:
        meta["Vergelijking"] = (
            f"{COMPARE_LABELS[params.compare]} ({format_date(params.compare_period.start)} t/m "
            f"{format_date(params.compare_period.end)})"
        )
    if params.hide_one_off:
        meta["Filter"] = "zonder losse grote uitgaven"
    return meta


def _list_arg(name: str) -> list[int]:
    return [i for i in (_int(v) for v in request.args.getlist(name)) if i]


def make_report(slug: str, params: core.ReportParams):
    if slug == "overview":
        return build_overview(params)
    if slug == "categorieen":
        return build_categories(params, cat_id=_int(request.args.get("cat")))
    if slug == "trends":
        return build_trends(
            params, selected=_list_arg("cats"), stacked=request.args.get("stijl") == "gestapeld"
        )
    if slug == "tags":
        return build_tags(params, selected=_list_arg("tags"))
    if slug == "jaar":
        year = _int(request.args.get("jaar"))
        return build_year(params, year=year if year and 2000 <= year <= 2100 else None)
    builder = BUILDERS.get(slug)
    if builder is None:
        abort(404)
    return builder(params)


def _context(slug: str, params: core.ReportParams, report) -> dict:
    return {
        "slug": slug,
        "tabs": REPORT_TABS,
        "params": params,
        "report": report,
        "query": params.query(),
        "period_choices": core.PERIOD_CHOICES,
        "compare_choices": core.COMPARE_CHOICES,
        "meta": meta_for(params),
        "extra_query": {
            k: v for k, v in request.args.items(multi=False) if k in {"cat", "stijl", "jaar"}
        },
    }


@bp.get("/")
@login_required
def overview():
    params = build_params()
    report = build_overview(params)
    members = members_by_id()
    recent = report.extras["recent"]
    return render_template(
        "reports/overview.html",
        **_context("overview", params, report),
        members=members,
        infos={tx.id: describe_for_person(tx, params.person) for tx in recent},
        freq_labels={
            "weekly": "wekelijks",
            "monthly": "maandelijks",
            "quarterly": "per kwartaal",
            "yearly": "jaarlijks",
        },
    )


@bp.get("/rapportages/<slug>")
@login_required
def show(slug: str):
    if slug in {"overzicht", "overview"}:
        return redirect(url_for("reports.overview", **request.args))
    params = build_params()
    report = make_report(slug, params)
    return render_template("reports/report.html", **_context(slug, params, report))


@bp.get("/rapportages/<slug>/export/<fmt>")
@login_required
def export_report(slug: str, fmt: str):
    if slug == "overzicht":
        slug = "overview"
    params = build_params()
    report = make_report(slug, params)
    meta = meta_for(params)
    if fmt == "csv":
        data, mimetype = export.to_csv(report, meta), "text/csv"
    elif fmt == "xlsx":
        data, mimetype = (
            export.to_xlsx(report, meta),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    elif fmt == "pdf":
        data, mimetype = export.to_pdf(report, meta), "application/pdf"
    else:
        abort(404)
    return Response(
        data,
        mimetype=mimetype,
        headers={"Content-Disposition": f'attachment; filename="{export.filename(report, fmt)}"'},
    )
