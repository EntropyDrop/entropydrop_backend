# EntropyDrop Backend

FastAPI backend for EntropyDrop skin generation, collections, orders, subscriptions, and the public ledger API.

## Local Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Start local infrastructure:

```bash
docker compose up -d db redis
alembic upgrade head
```

Run the API and singleton background worker in separate terminals:

```bash
uvicorn main:app --reload --port 8000
python background_service.py
```

The API is mounted under `/skin`; for example:

```text
http://localhost:8000/skin/api/health
```

## Production Notes

- Keep real credentials in your deployment secret manager or local `.env` files.
- Do not commit `.env`, `.env.prod`, private keys, virtualenvs, coverage output, or deployment scripts with environment-specific details.
- Run migrations with `alembic upgrade head` before starting new API versions.

## Tests

```bash
python -m pytest
```
