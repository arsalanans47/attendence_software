# Design decisions

1. **Indexes.** Unique employee-code and `(emp_code, date)` indexes enforce identity and one daily attendance record. Department/code and department/join-date indexes support listings and headcount. Attendance indexes support date/code listing, employee/date/status filters and late queries. I rejected a standalone status index because status has low selectivity.

2. **Punch-in race.** Requests can both find the employee and calculate the same day, then insert the same key. MongoDB's unique index accepts one and rejects the duplicate. The winner returns 201; the duplicate-key handler returns 409.

3. **Ties.** MongoDB `$rank` gives equal totals equal ranks and skips the next rank. I filter by rank after ranking, so every employee tied at or before the limit is included. Employee code sorts ties consistently.

4. **Headcount.** Department summary begins with eligible employee documents and left-joins their monthly logs. Employees without logs stay in the result; people who joined after month end are excluded before grouping.

5. **100x data.** I would benchmark plans, archive old attendance by date, and consider monthly pre-aggregates to reduce repeated historical work.
