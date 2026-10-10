from datetime import datetime, timezone

import pytest

from auth import get_current_user
from figure_print_terms import FIGURE_PRINT_TERMS_VERSION
from main import app
from models import User

ENDPOINT = "/api/users/me/figure_print_terms"
PAYLOAD = {"version": FIGURE_PRINT_TERMS_VERSION, "accepted": True}


@pytest.fixture
def printing_user(client, db):
    user = User(id="print-user", email="printing@example.test", terms_agreed=True)
    db.add(user)
    db.commit()
    app.dependency_overrides[get_current_user] = lambda: user
    return user


def test_print_acceptance_requires_login(client):
    assert client.get(ENDPOINT).status_code in (401, 403)
    assert client.post(ENDPOINT, json=PAYLOAD).status_code in (401, 403)


def test_general_terms_never_imply_printing_acceptance(client, printing_user):
    response = client.get(ENDPOINT)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["accepted_version"] is None
    assert response.json()["accepted_at"] is None


def test_acceptance_is_persisted_for_current_user_and_retry_is_idempotent(client, db, printing_user):
    other = User(id="another-user", email="other@example.test")
    db.add(other)
    db.commit()
    before = datetime.now(timezone.utc)
    response = client.post(ENDPOINT, json=PAYLOAD)
    assert response.status_code == 200
    result = response.json()
    assert result["required_version"] == result["accepted_version"] == FIGURE_PRINT_TERMS_VERSION
    accepted_at = datetime.fromisoformat(result["accepted_at"].replace("Z", "+00:00"))
    assert before <= accepted_at <= datetime.now(timezone.utc)
    db.refresh(printing_user)
    assert printing_user.figure_print_terms_version == FIGURE_PRINT_TERMS_VERSION
    assert printing_user.figure_print_terms_accepted_at is not None
    assert printing_user.terms_agreed is True
    assert other.figure_print_terms_version is None
    assert client.get(ENDPOINT).json() == result
    assert client.post(ENDPOINT, json=PAYLOAD).json() == result
    # Updating another profile property must not erase the acceptance snapshot.
    for response in [client.get('/api/users/me'), client.post('/api/users/me/username', json={'username': 'Updated'})]:
        assert response.json()['figure_print_terms_version'] == FIGURE_PRINT_TERMS_VERSION
        assert response.json()['figure_print_terms_accepted_at'] is not None


@pytest.mark.parametrize('payload,status', [
    ({'version': 'obsolete', 'accepted': True}, 409),
    ({'version': FIGURE_PRINT_TERMS_VERSION, 'accepted': False}, 400),
    ({'version': FIGURE_PRINT_TERMS_VERSION}, 422),
    ({**PAYLOAD, 'accepted': 'true'}, 422),
    ({**PAYLOAD, 'user_id': 'another-user'}, 422),
    ({**PAYLOAD, 'accepted_at': '2000-01-01T00:00:00Z'}, 422),
])
def test_invalid_acceptance_cannot_change_user(client, db, printing_user, payload, status):
    assert client.post(ENDPOINT, json=payload).status_code == status
    db.refresh(printing_user)
    assert printing_user.figure_print_terms_version is None
    assert printing_user.figure_print_terms_accepted_at is None


def test_new_version_needs_new_explicit_acceptance(client, db, printing_user, monkeypatch):
    first = client.post(ENDPOINT, json=PAYLOAD).json()
    import figure_print_terms
    import routers.auth
    monkeypatch.setattr(figure_print_terms, 'FIGURE_PRINT_TERMS_VERSION', '2.0')
    monkeypatch.setattr(routers.auth, 'FIGURE_PRINT_TERMS_VERSION', '2.0')
    status = client.get(ENDPOINT).json()
    assert status['required_version'] == '2.0'
    assert status['accepted_version'] == first['accepted_version']
    assert client.post(ENDPOINT, json=PAYLOAD).status_code == 409
    assert client.post(ENDPOINT, json={'version': '2.0', 'accepted': True}).json()['accepted_version'] == '2.0'


def test_db_failure_does_not_report_acceptance(client, db, printing_user, monkeypatch):
    def fail_commit():
        raise RuntimeError('simulated database outage')
    monkeypatch.setattr(db, 'commit', fail_commit)
    response = client.post(ENDPOINT, json=PAYLOAD)
    assert response.status_code == 500
    assert 'accepted_at' not in response.json()
    db.rollback()
    db.refresh(printing_user)
    assert printing_user.figure_print_terms_accepted_at is None


def test_legacy_api_alias_preserves_acceptance(client, printing_user):
    response = client.post('/skin' + ENDPOINT, json=PAYLOAD)
    assert response.status_code == 200
    assert client.get(ENDPOINT).json() == response.json()


def test_migration_keeps_existing_users_unaccepted_and_is_reversible():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    migration_path = Path(__file__).resolve().parents[1] / 'alembic/versions/e6b93f1a0c25_add_figure_print_terms_to_users.py'
    spec = importlib.util.spec_from_file_location('print_terms_migration', migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE users (id TEXT PRIMARY KEY, terms_agreed BOOLEAN)'))
        connection.execute(text("INSERT INTO users VALUES ('existing', 1)"))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            row = connection.execute(text('SELECT figure_print_terms_version, figure_print_terms_accepted_at FROM users')).one()
            assert tuple(row) == (None, None)
            migration.downgrade()
        assert {column['name'] for column in inspect(connection).get_columns('users')} == {'id', 'terms_agreed'}
        assert connection.execute(text('SELECT terms_agreed FROM users')).scalar() == 1
    engine.dispose()
