"""
db.item_master — Per-Shop Item GST Rate Memory
-------------------------------------------------
"""

from db.models import ShopItemMaster
from db.session import db_session


def get_item_master(shop_id: str, item_name: str) -> dict | None:
    """Get a shop's saved item by name. Returns dict or None."""
    name = item_name.lower().strip()
    with db_session() as session:
        row = session.query(ShopItemMaster).filter_by(
            shop_id=shop_id, item_name=name,
        ).first()
        if not row:
            return None
        return {
            "item_name": row.item_name,
            "hsn": row.hsn,
            "gst_rate": row.gst_rate,
            "confirmed": row.confirmed,
            "use_count": row.use_count,
        }


def save_item_master(shop_id: str, item_name: str, hsn: str, gst_rate: int,
                     confirmed: bool = False, is_bos: bool = False):
    """Upsert an item in the shop's item master. Increments use_count.

    is_bos=True signals the source bill was a Bill of Supply, where every
    item carries gst_rate=0 by definition (no GST applies). Saving such
    rows would either:
      - poison new entries with 0% (next Tax Invoice bill for the same
        shop hits Step 0 of get_gst_rate_smart and gets 0% silently), OR
      - overwrite an existing valid rate (e.g., kurta saved at 5% from a
        prior Tax Invoice gets clobbered to 0% by a BOS bill).
    Both are wrong. Skip the save entirely when is_bos=True; the explicit
    GST_RATES dict and Claude lookup will repopulate correctly when the
    shop next bills under a Tax Invoice. use_count is intentionally NOT
    incremented either — BOS sales don't represent GST-bearing usage.
    """
    if is_bos:
        return

    name = item_name.lower().strip()
    with db_session() as session:
        row = session.query(ShopItemMaster).filter_by(
            shop_id=shop_id, item_name=name,
        ).first()
        if row:
            row.hsn = hsn
            row.gst_rate = gst_rate
            if confirmed:
                row.confirmed = True
            row.use_count += 1
        else:
            session.add(ShopItemMaster(
                shop_id=shop_id, item_name=name,
                hsn=hsn, gst_rate=gst_rate,
                confirmed=confirmed, use_count=1,
            ))


def get_top_items(shop_id: str, limit: int = 20) -> list[dict]:
    """Get top items by use_count for a shop."""
    with db_session() as session:
        rows = session.query(ShopItemMaster).filter_by(
            shop_id=shop_id,
        ).order_by(ShopItemMaster.use_count.desc()).limit(limit).all()
        return [
            {
                "item_name": r.item_name,
                "hsn": r.hsn,
                "gst_rate": r.gst_rate,
                "confirmed": r.confirmed,
                "use_count": r.use_count,
            }
            for r in rows
        ]


def update_item_gst(shop_id: str, item_name: str, gst_rate: int) -> bool:
    """Update GST rate for an existing item and mark confirmed. Returns True if found."""
    name = item_name.lower().strip()
    with db_session() as session:
        row = session.query(ShopItemMaster).filter_by(
            shop_id=shop_id, item_name=name,
        ).first()
        if not row:
            return False
        row.gst_rate = gst_rate
        row.confirmed = True
        return True
