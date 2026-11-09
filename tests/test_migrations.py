from sqlalchemy import inspect

from app import create_app
from app.extensions import db


def test_migrations_match_models(tmp_path):
    from flask_migrate import upgrade

    app = create_app(
        {
            "SECRET_KEY": "t",
            "DATA_DIR": tmp_path / "d",
            "BACKUP_DIR": tmp_path / "b",
            "TESTING": True,
            "SCHEDULER_ENABLED": False,
        }
    )
    with app.app_context():
        upgrade()
        tables = set(inspect(db.engine).get_table_names())
        assert set(db.metadata.tables) <= tables
        with db.engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        db.engine.dispose()
