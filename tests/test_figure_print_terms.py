"""The 3D printing terms are Section 18 of the Terms of Service.

The separate acceptance endpoints were removed, the old per-user acceptance columns are
dropped, and every user must accept the updated Terms of Service.
"""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

VERSIONS = Path(__file__).resolve().parents[1] / "alembic/versions"


def load_migration(filename):
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), VERSIONS / filename)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_separate_printing_agreement_endpoints_are_gone(client):
    url = "/api/users/me/figure_print_terms"
    assert client.get(url).status_code in (404, 405)
    assert client.post(url, json={"version": "1.0", "accepted": True}).status_code in (404, 405)


def test_user_responses_no_longer_expose_printing_agreement_fields():
    from schemas import UserResponse

    assert "figure_print_terms_version" not in UserResponse.model_fields
    assert "figure_print_terms_accepted_at" not in UserResponse.model_fields


def test_migration_keeps_existing_users_unaccepted_and_is_reversible():
    migration = load_migration("e6b93f1a0c25_add_figure_print_terms_to_users.py")
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id TEXT PRIMARY KEY, terms_agreed BOOLEAN)"))
        connection.execute(text("INSERT INTO users VALUES ('existing', 1)"))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            row = connection.execute(text("SELECT figure_print_terms_version, figure_print_terms_accepted_at FROM users")).one()
            assert tuple(row) == (None, None)
            migration.downgrade()
        assert {column["name"] for column in inspect(connection).get_columns("users")} == {"id", "terms_agreed"}
        assert connection.execute(text("SELECT terms_agreed FROM users")).scalar() == 1
    engine.dispose()


def test_terms_reset_migration_asks_everyone_again():
    migration = load_migration("a3d58e1f7c94_reset_terms_agreed_for_merged_3d_printing_terms.py")
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id TEXT PRIMARY KEY, terms_agreed BOOLEAN, figure_print_terms_version TEXT)"))
        connection.execute(text("INSERT INTO users VALUES ('agreed', 1, '1.0'), ('pending', 0, NULL)"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        rows = dict(connection.execute(text("SELECT id, terms_agreed FROM users")).all())
        assert rows == {"agreed": 0, "pending": 0}
    engine.dispose()


def test_printing_agreement_columns_are_dropped_without_touching_other_user_data():
    migration = load_migration("c5e8a17d3b92_drop_figure_print_terms_from_users.py")
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE users (id TEXT PRIMARY KEY, terms_agreed BOOLEAN, "
            "figure_print_terms_version TEXT, figure_print_terms_accepted_at TIMESTAMP)"
        ))
        connection.execute(text("INSERT INTO users VALUES ('u1', 0, '1.0', '2026-01-01 00:00:00')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert {c["name"] for c in inspect(connection).get_columns("users")} == {"id", "terms_agreed"}
            assert tuple(connection.execute(text("SELECT id, terms_agreed FROM users")).one()) == ("u1", 0)
            migration.downgrade()
        assert {c["name"] for c in inspect(connection).get_columns("users")} >= {
            "figure_print_terms_version", "figure_print_terms_accepted_at"}
        assert connection.execute(text("SELECT figure_print_terms_version FROM users")).scalar() is None
    engine.dispose()


def test_user_model_no_longer_maps_printing_agreement_columns():
    from models import User

    assert not hasattr(User, "figure_print_terms_version")
    assert not hasattr(User, "figure_print_terms_accepted_at")
