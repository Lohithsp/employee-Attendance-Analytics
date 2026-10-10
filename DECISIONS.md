# DECISIONS.md

1. **Indexes.**
   - `employees`: unique on `emp_code` (uniqueness enforcement and 409 conflicts); `(department, emp_code)` (sorted department listings); `(joined_on, department)` (headcount filtering).
   - `attendance_logs`: unique on `(emp_code, date)` (one-record-per-day guarantee and race safety); `(date, -1), (emp_code, 1)` (fast pagination); `(emp_code, 1), (punch_in, -1)` (punch-out lookups); `(date, 1), (late_minutes, 1)` (leaderboard).
   - *Rejected*: A wide covering index spanning all attendance fields, rejected due to index maintenance write overhead on high-frequency punch traffic without query benefit.

2. **Punch-in race.** Both requests compute the same attendance date and attempt `insert_one`. The database compound unique index on `(emp_code, date)` guarantees exactly one insert succeeds (201 Created). The concurrent insert triggers `DuplicateKeyError`, which is caught and converted into HTTP 409 Conflict.

3. **Ties.** Both employees receive the identical rank via MongoDB's `$rank` window function (standard competition ranking: 1, 2, 2, 4). Because `limit` applies after ranking (`rank <= limit`), both employees are returned, which may exceed `limit` rows. Tied rows are ordered by `emp_code` ascending.

4. **Headcount.** The aggregation pipeline starts on `employees` (matching `joined_on <= month_end`), grouping by department to establish baseline headcount. Attendance records are joined using `$lookup`. Employees with zero logs yield an empty array, preserving their headcount inclusion.

5. **One thing you would change.** At 100x scale (~10M documents), incrementally pre-aggregate daily and monthly metrics via Change Streams or scheduled rollups rather than executing runtime cross-collection lookups and window scans.
