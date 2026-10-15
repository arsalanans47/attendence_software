# Code review: starter defects and fixes

| # | Where | What is wrong | How to notice | Fix |
|---|---|---|---|---|
| 1 | Employee validation | Codes, names, email, shifts and equal shift times were not contract-validated. | Create malformed fields or identical shift boundaries. | Added schema constraints and time/shift validation. |
| 2 | `create_employee` | Duplicate check races and the database had no unique guarantee. | Parallel create requests for one code. | Unique index; translate duplicate-key errors to 409. |
| 3 | `create_employee` | `created_at` used local naive time and was returned as a datetime instead of epoch ms. | Inspect response from a non-UTC host. | Store naive UTC BSON date and serialize epoch milliseconds. |
| 4 | `/health` | Always claimed readiness without checking MongoDB. | Stop MongoDB and request health. | Ping MongoDB; return 503 on failure. |
| 5 | `compute_late_minutes` | Relied on local datetime fields and did not model IST/overnight attendance day safely. | Punch around midnight or run under another host timezone. | Compare IST punch time against the attendance day's shift start. |
| 6 | `compute_overtime` | Mixed aware IST values with naive UTC punches, causing incorrect math or exceptions. | Punch-out at/after a shift end. | Build shift boundary in IST, convert to UTC, then compare. |
| 7 | Timestamp helpers | Input instants were not consistently truncated to whole seconds; integer/range validation was incomplete. | Submit fractional-second epoch values and seconds-looking timestamps. | Strict integer bounds and second truncation at conversion. |
| 8 | `/attendance/punch-in` | Unknown employee caused a `None` dereference (500). | Punch in with an unknown code. | Return 404 before reading employee shift. |
| 9 | `/attendance/punch-in` | Used host-local time and calendar date instead of UTC input and R1 IST shift day. | Timestamp near IST midnight / overnight shift. | Convert epoch to UTC and derive attendance date in IST, including overnight rule. |
| 10 | `/attendance/punch-in` | Accepted non-presence statuses. | Send `ABSENT` as punch-in status. | Restrict input status to PRESENT, WFH or ON_DUTY. |
| 11 | `/attendance/punch-in` | Check-then-insert allowed duplicate records under concurrency. | Send concurrent identical punches. | Unique compound index on employee/date makes one insert win. |
| 12 | `/attendance/punch-out` | Original endpoint was absent. | Call documented route. | Added lookup, validation, derived values and atomic conditional update. |
| 13 | `/attendance/punch-out` | If only the starter punch-in were used, naive local timestamp storage would break cross-midnight matching. | Punch out after midnight on overnight shift. | All persisted instants are UTC-naive and lookup is by latest eligible punch. |
| 14 | `/employees` GET | Pagination skipped `page * page_size`, and total ignored department filtering; no stable sort or bounds. | Request page 1 or filter by department. | Correct offset, filtered count, sort, and validated pagination. |
| 15 | `/attendance` GET | Loaded the entire collection then sorted/sliced in Python; unsuitable at 100k rows. | Measure memory/query time on a large collection. | Indexed Mongo query with sort, count, skip and limit. |
| 16 | `/attendance` GET | Date bounds were unchecked and sort omitted employee tie-break. | Invalid dates or same-day rows. | Validate inclusive date filters and sort date descending/code ascending. |
| 17 | Attendance serialization | Returned Mongo `_id` as a public `id` and left nested history times unconverted. | Inspect a seeded corrected record response. | Serialize punch/history instants to epoch ms and omit `_id`. |
| 18 | Regularization | Starter implementation was incomplete and had no robust legacy-history concurrency handling. | Correct legacy record with no `history`, then issue concurrent corrections. | Recompute derived fields, append one audit entry, and compare-and-update. |
| 19 | Analytics and explain routes | Five contract endpoints were missing. | Call any route listed in the OpenAPI contract. | Implemented monthly, department, leaderboard, trend and executionStats explain operations. |
| 20 | Analytics | In-memory rollups would ignore stored derived values or fail on no-log employees/ties/weekends. | Use seeded edge cases. | Analytics use Mongo aggregations; headcount starts from employees, ranking uses `$rank`, trend uses `$densify` and window functions. |

## Reviewed and retained

- `compute_work_hours` uses `Decimal` and `ROUND_HALF_UP`, matching R4.
- The half-day comparison is made against the rounded work-hours value, as R5 requires.
- The PATCH rule that rejects a correction with no changed values is correct and avoids empty audit entries.

## Verification

`python -m py_compile app/main.py` completed successfully. Live MongoDB behavior and aggregation execution require a configured MongoDB 6.0+ instance; no database was available in this review environment.
