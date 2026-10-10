# Employee Attendance & Analytics API

Solution for the HROne engineering assignment. Implements FastAPI backend conforming strictly to `openapi.yaml`, rules R1–R10, and `DATA_MODEL.md`.

## Features Implemented
- **Part A: Code Review (`REVIEW.md`)**: Identified and fixed 18 defects spanning concurrency races, business rule calculations (R1–R5), pagination, filtering, input validation, and startup index initialization.
- **Part B: API Contract & Concurrency (`app/main.py`)**:
  - `GET /health`: Readiness probe testing live MongoDB connection.
  - `POST /employees` & `GET /employees`: Unique indexing, strict schema validations, filtered count, and sorted pagination.
  - `POST /attendance/punch-in` & `POST /attendance/punch-out`: Race-safe punch events with overnight shift handling, half-up rounding (R4), overtime thresholding (R3), and half-day derivation (R5).
  - `PATCH /attendance/{emp_code}/{date}`: Concurrency-guarded regularization, derived field recomputation, and audit trail appending (`history`).
  - `GET /analytics/employees/{emp_code}/monthly`: Monthly aggregation with weekday working-day calculation from join date (R7).
  - `GET /analytics/departments/summary`: Cross-collection department rollup including zero-log employees (R9).
  - `GET /analytics/leaderboard/late`: Standard competition ranking (1, 2, 2, 4) using MongoDB window functions (`$rank`).
  - `GET /analytics/departments/{department}/trend`: Database-driven gap filling (`$densify`) and 7-day moving average (`$setWindowFields`).
  - `GET /admin/explain/{endpoint}`: MongoDB executionStats explain output running index scans (`IXSCAN`) with zero collection scans.
- **Part C: Architecture Decisions (`DECISIONS.md`)**: Concise technical rationale covering indexes, race handling, cutoff ties, headcount integrity, and 100x scalability.

## Running Locally

1. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   # Windows:
   .\.venv\Scripts\activate
   # Linux/macOS:
   source .venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure environment variables (optional; defaults are shown):
   - `MONGO_URI`: `mongodb://localhost:27017`
   - `MONGO_DB`: `attendance_db`

4. Run the API:
   ```bash
   uvicorn app.main:app --port 8000
   ```

All indexes are created automatically at application startup.
