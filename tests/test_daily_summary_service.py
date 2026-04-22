"""Tests for daily summary service and schema."""
import main


def test_shop_schema_has_new_columns():
    """Verify last_summary_sent_at and summary_opt_out exist on shops table."""
    from sqlalchemy import inspect as sa_inspect
    from db.session import engine

    main.init_database()

    inspector = sa_inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("shops")}
    assert "last_summary_sent_at" in cols, "missing column: last_summary_sent_at"
    assert "summary_opt_out" in cols, "missing column: summary_opt_out"
