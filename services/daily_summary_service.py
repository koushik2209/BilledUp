"""
services.daily_summary_service — Daily Summary Data Layer
----------------------------------------------------------
Queries the bills table for today + month aggregates per shop.
Returns a dict shaped for format_daily_summary().
"""
import re
import logging
from datetime import date

from sqlalchemy import func

from db.session import db_session
from db.models import Bill, Shop

log = logging.getLogger("billedup.daily_summary_service")

_GSTIN_RE       = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
_PLACEHOLDER    = "GSTIN00000000000"


def _has_valid_gstin(gstin: str) -> bool:
    g = (gstin or "").strip().upper()
    return bool(g and g != _PLACEHOLDER and _GSTIN_RE.match(g))


def get_daily_summary_data(shop_id: str, target_date: date) -> dict:
    """
    Return billing aggregates for today and the current month.

    grand_total / sale_amount / total_gst cover sales only (is_return=False).
    returns_amount covers returns only (is_return=True), always positive.
    """
    month_start = target_date.replace(day=1)
    sid = shop_id.upper()

    with db_session() as session:
        shop = session.query(Shop).filter_by(shop_id=sid).first()
        if not shop:
            raise ValueError(f"Shop not found: {shop_id}")

        shop_name = shop.name
        has_gstin = _has_valid_gstin(shop.gstin)

        def _sales_agg(date_filters):
            return session.query(
                func.count(Bill.id).label("count"),
                func.coalesce(func.sum(Bill.grand_total), 0).label("grand_total"),
                func.coalesce(func.sum(Bill.subtotal),    0).label("sale_amount"),
                func.coalesce(func.sum(Bill.total_gst),   0).label("total_gst"),
            ).filter(Bill.shop_id == sid, *date_filters,
                     Bill.is_return.is_(False)).first()

        def _returns_agg(date_filters):
            return session.query(
                func.count(Bill.id).label("count"),
                func.coalesce(func.sum(Bill.grand_total), 0).label("returns_amount"),
            ).filter(Bill.shop_id == sid, *date_filters,
                     Bill.is_return.is_(True)).first()

        today_f = [func.date(Bill.created_at) == target_date]
        month_f = [
            func.date(Bill.created_at) >= month_start,
            func.date(Bill.created_at) <= target_date,
        ]

        ts = _sales_agg(today_f)
        tr = _returns_agg(today_f)
        ms = _sales_agg(month_f)
        mr = _returns_agg(month_f)

    date_str = f"{target_date.day} {target_date.strftime('%b %Y')}"

    return {
        "shop_name": shop_name,
        "has_gstin": has_gstin,
        "date":      date_str,
        "today": {
            "total_bills":    (ts.count or 0) + (tr.count or 0),
            "grand_total":    round(float(ts.grand_total  or 0), 2),
            "sale_amount":    round(float(ts.sale_amount  or 0), 2),
            "total_gst":      round(float(ts.total_gst    or 0), 2),
            "returns_count":  tr.count or 0,
            "returns_amount": round(abs(float(tr.returns_amount or 0)), 2),
        },
        "month": {
            "name":          target_date.strftime("%B"),
            "total_bills":    (ms.count or 0) + (mr.count or 0),
            "grand_total":    round(float(ms.grand_total  or 0), 2),
            "sale_amount":    round(float(ms.sale_amount  or 0), 2),
            "total_gst":      round(float(ms.total_gst    or 0), 2),
            "returns_count":  mr.count or 0,
            "returns_amount": round(abs(float(mr.returns_amount or 0)), 2),
        },
    }
