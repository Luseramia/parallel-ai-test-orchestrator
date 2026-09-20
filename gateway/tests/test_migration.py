from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.repositories.models import Base

GATEWAY_ROOT = Path(__file__).resolve().parents[1]


class MigrationTests(unittest.TestCase):
    def test_upgrade_and_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = (Path(directory) / "migration.db").as_posix()
            database_url = f"sqlite+pysqlite:///{database_path}"
            config = Config(str(GATEWAY_ROOT / "alembic.ini"))
            config.set_main_option("script_location", str(GATEWAY_ROOT / "migrations"))
            config.set_main_option("sqlalchemy.url", database_url)

            command.upgrade(config, "head")
            engine = create_engine(database_url)
            self.assertTrue(
                {
                    "alembic_version",
                    "ai_test_jobs",
                    "ai_test_verify_attempts",
                    "ai_test_job_events",
                    "ai_test_artifacts",
                    "ai_test_idempotency_keys",
                }.issubset(set(inspect(engine).get_table_names()))
            )
            with engine.connect() as connection:
                migration_context = MigrationContext.configure(connection)
                self.assertEqual([], compare_metadata(migration_context, Base.metadata))
            engine.dispose()

            command.downgrade(config, "base")
            engine = create_engine(database_url)
            self.assertEqual(
                {"alembic_version"}, set(inspect(engine).get_table_names())
            )
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
