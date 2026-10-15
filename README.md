# Employee Attendance & Analytics API

FastAPI and MongoDB implementation of the HROne attendance assignment. It follows `openapi.yaml`; all API logic is in `app/main.py`.

## Requirements

- Python 3.12 or 3.13
- MongoDB 6.0 or newer (MongoDB 7 is recommended)

## Setup (PowerShell)

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set `MONGO_URI` and `MONGO_DB` in `.env` (or the process environment; process variables take precedence). For a local MongoDB using Docker:

```powershell
docker run --name attendance-mongo -p 27017:27017 -d mongo:7
```

Seed the sample records and start the API:

```powershell
python sample_seed.py
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

On macOS/Linux, use `python -m venv .venv`, activate with `source .venv/bin/activate`, copy `.env.example` to `.env`, then run the same install, seed and Uvicorn commands.

Indexes are created automatically at application startup. `GET /health` checks MongoDB availability. Interactive API docs are available at `/docs`.

## Project notes

- Calendar dates and shift times use IST; BSON instants are stored in UTC and exposed as epoch milliseconds.
- Analytics read stored derived attendance fields and execute as MongoDB aggregation pipelines.
- See [REVIEW.md](REVIEW.md) for the code review and [DECISIONS.md](DECISIONS.md) for design choices.
- Do not commit `.env`, credentials, local virtual environments, or database dumps.
