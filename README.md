# Employee Attendance & Analytics API (HROne assignment)

FastAPI service backed by MongoDB: employee master data, attendance punch-in/out, manual regularization, analytics aggregations, and an admin explain endpoint. All application code lives in **`app/main.py`**. The HTTP contract is in `openapi.yaml`; storage rules are in `sample_data/DATA_MODEL.md`.

## Repository structure (Git root)

Initialize git in this folder (`candidate_kit/`). Recommended layout:

```
candidate_kit/          ← Git root
├── app/                # Application code (main.py)
├── requirements.txt
├── README.md
├── REVIEW.md
├── DECISIONS.md
├── openapi.yaml
├── tests/
├── sample_data/
├── .env.example
└── .gitignore
```

Supporting material (not at repo root): `sample_data/DATA_MODEL.md`, `sample_data/sample_seed.py`, sprint notes under `tests/SPRINT*.md`, optional local checks in `devtools/` (gitignored).

## Requirements

| Component | Version |
|-----------|---------|
| **Python** | 3.11+ (tested on 3.13) |
| **MongoDB** | 6.0+ (grader uses 7.0; Atlas M0 works) |

## Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |

Set them in the shell or in a local `.env` file (see `.env.example`). **Do not commit `.env`.** Real environment variables override `.env` when both are set.

Example (PowerShell):

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

## Install dependencies

From the repository root (the folder that contains `app/` and `requirements.txt`):

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/macOS:  source .venv/bin/activate
pip install -r requirements.txt
```

## Database setup

1. Start MongoDB (e.g. `docker run -d -p 27017:27017 mongo:7`).
2. Indexes are created **automatically** on application startup (`lifespan`) and on first successful `/health` or write. No manual `createIndex` is required.
3. Optional sample data (nine documents):

```bash
python sample_data/sample_seed.py
```

Tests use **separate database names** (`MONGO_DB` per test module) and do not require seeding.

## Run the application

From the repository root:

```bash
uvicorn app.main:app --port 8000
```

With reload during development:

```bash
uvicorn app.main:app --port 8000 --reload
```

OpenAPI UI: `http://localhost:8000/docs`

### Health check

```bash
curl http://localhost:8000/health
```

Expect `{"status":"ok"}` when MongoDB is reachable; `503` with `{"detail":"mongodb unavailable"}` when it is not.

## API testing

- **Manual:** Swagger at `/docs`, or `curl` against endpoints listed in `openapi.yaml`.
- **Automated:** from the repo root with MongoDB running:

```bash
python -m unittest discover -s tests -p "test_*.py"
# or
python -m pytest -c tests/pytest.ini tests/ -q
```

Explain admin endpoint (example):

```bash
curl "http://localhost:8000/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31&page=1&page_size=20"
```

## Submit

Public Git repository with these **required** paths at the repo root: `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, `README.md`.

Before pushing, confirm `.env`, `.venv`, `__pycache__/`, and `.pytest_cache/` are **not** committed (see `.gitignore`).

Keep the repo **public until 31 October 2026**. Do not publish the solution elsewhere before **15 November 2026**.

**Deadline:** 11:59 PM IST, **15 October 2026**.

## Known limitations

- Grader-scale (~100k logs) is not in CI; `tests/test_sprint4_admin.py` has a 5k explain smoke test. Optional local check: `python devtools/explain_at_scale.py` (see `devtools/README.md`; folder is gitignored for a minimal public tree).
- `department_trend` uses a **database-level** aggregation (`$documents`); explain output shape differs from collection explains.
- Unique indexes are not created if duplicate `emp_code` or `(emp_code, date)` already exist (data is never auto-deleted).
- PATCH regularization uses an `attendance_locks` mutex (not multi-document transactions on standalone MongoDB).
- See `REVIEW.md` for the starter defect list and QA notes.
