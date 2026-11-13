"""Step 3: Domain Data Foundation (Database & Knowledge Base).

Bootstraps the ground truth the workforce planning agent relies on:
  1. enterprise.db  - operational SQLite database (employees, shifts, assignments, ...)
  2. policies/      - enterprise policy documents in markdown

Everything is deterministic (fixed dates, fixed rows) so evaluation answers are stable.
The planning week is Monday 2026-11-16 to Saturday 2026-11-21.

Usage:
    python bootstrap_data.py            # create db + policies if missing, print a data audit
    python bootstrap_data.py --force    # rebuild enterprise.db from scratch
"""
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "enterprise.db")
POLICY_DIR = os.path.join(BASE_DIR, "policies")

# ------------------------------------------------------------------------------
# 1. Relational data
# ------------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE sites (
    site_id   TEXT PRIMARY KEY,
    site_name TEXT NOT NULL,
    city      TEXT NOT NULL
);

CREATE TABLE skills (
    skill_id               TEXT PRIMARY KEY,
    skill_name             TEXT NOT NULL,
    requires_certification INTEGER NOT NULL DEFAULT 0   -- 1 = valid certificate needed on shift date
);

CREATE TABLE employees (
    employee_id      TEXT PRIMARY KEY,
    full_name        TEXT NOT NULL,
    email            TEXT NOT NULL,
    phone            TEXT NOT NULL,
    site_id          TEXT NOT NULL,
    job_title        TEXT NOT NULL,
    employment_type  TEXT NOT NULL,                      -- FULL_TIME | PART_TIME
    max_weekly_hours REAL NOT NULL,                      -- contractual regular hours per week
    hire_date        TEXT NOT NULL,
    FOREIGN KEY (site_id) REFERENCES sites(site_id)
);

CREATE TABLE employee_skills (
    employee_id     TEXT NOT NULL,
    skill_id        TEXT NOT NULL,
    certified_until TEXT,                                -- NULL when no certificate applies
    PRIMARY KEY (employee_id, skill_id),
    FOREIGN KEY (employee_id) REFERENCES employees(employee_id),
    FOREIGN KEY (skill_id) REFERENCES skills(skill_id)
);

CREATE TABLE shift_types (
    shift_type_id TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    start_time    TEXT NOT NULL,
    end_time      TEXT NOT NULL,
    hours         REAL NOT NULL
);

CREATE TABLE shifts (
    shift_id           TEXT PRIMARY KEY,
    site_id            TEXT NOT NULL,
    shift_date         TEXT NOT NULL,                    -- ISO date
    shift_type_id      TEXT NOT NULL,
    skill_id           TEXT NOT NULL,                    -- skill every assignee must hold
    required_headcount INTEGER NOT NULL,
    FOREIGN KEY (site_id) REFERENCES sites(site_id),
    FOREIGN KEY (shift_type_id) REFERENCES shift_types(shift_type_id),
    FOREIGN KEY (skill_id) REFERENCES skills(skill_id)
);

CREATE TABLE assignments (
    assignment_id TEXT PRIMARY KEY,
    shift_id      TEXT NOT NULL,
    employee_id   TEXT NOT NULL,
    status        TEXT NOT NULL,                         -- SCHEDULED | CONFIRMED
    FOREIGN KEY (shift_id) REFERENCES shifts(shift_id),
    FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
);

CREATE TABLE time_off (
    time_off_id TEXT PRIMARY KEY,
    employee_id TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    leave_type  TEXT NOT NULL,
    status      TEXT NOT NULL,                           -- APPROVED | PENDING
    FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
);

CREATE TABLE availability (
    employee_id TEXT NOT NULL,
    weekday     INTEGER NOT NULL,                        -- 0 = Monday ... 6 = Sunday
    status      TEXT NOT NULL,                           -- UNAVAILABLE (standing constraint)
    reason      TEXT,
    PRIMARY KEY (employee_id, weekday),
    FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
);
"""

SITES = [
    ("SITE_KL", "KL Distribution Hub", "Kuala Lumpur"),
    ("SITE_PG", "Penang Service Center", "George Town"),
]

SKILLS = [
    ("FORKLIFT", "Forklift Operator", 1),
    ("FIRST_AID", "First Aid Responder", 1),
    ("INVENTORY", "Inventory Control", 0),
    ("CUST_SUPPORT", "Customer Support", 0),
    ("TEAM_LEAD", "Team Lead", 0),
]

EMPLOYEES = [
    # id, name, email, phone, site, title, type, max hours, hire date
    ("EMP_001", "Aisha Rahman", "aisha.rahman@northwind-ops.example", "012-345 1001", "SITE_KL", "Shift Supervisor", "FULL_TIME", 40, "2019-03-04"),
    ("EMP_002", "Daniel Lim", "daniel.lim@northwind-ops.example", "012-345 1002", "SITE_KL", "Warehouse Associate", "FULL_TIME", 40, "2020-07-13"),
    ("EMP_003", "Priya Nair", "priya.nair@northwind-ops.example", "012-345 1003", "SITE_KL", "Warehouse Associate", "FULL_TIME", 40, "2021-01-18"),
    ("EMP_004", "Marcus Tan", "marcus.tan@northwind-ops.example", "012-345 1004", "SITE_KL", "Inventory Clerk", "FULL_TIME", 40, "2022-05-02"),
    ("EMP_005", "Nurul Huda", "nurul.huda@northwind-ops.example", "012-345 1005", "SITE_KL", "Inventory Clerk", "PART_TIME", 24, "2023-02-20"),
    ("EMP_006", "Ravi Kumar", "ravi.kumar@northwind-ops.example", "012-345 1006", "SITE_KL", "Senior Warehouse Associate", "FULL_TIME", 40, "2018-09-10"),
    ("EMP_007", "Chloe Wong", "chloe.wong@northwind-ops.example", "012-345 1007", "SITE_KL", "Inventory Clerk", "PART_TIME", 20, "2024-04-15"),
    ("EMP_008", "Hafiz Ismail", "hafiz.ismail@northwind-ops.example", "012-345 1008", "SITE_KL", "Warehouse Associate", "FULL_TIME", 40, "2022-11-07"),
    ("EMP_009", "Jasmine Ooi", "jasmine.ooi@northwind-ops.example", "012-345 1009", "SITE_PG", "Support Team Lead", "FULL_TIME", 40, "2019-08-26"),
    ("EMP_010", "Kevin Chew", "kevin.chew@northwind-ops.example", "012-345 1010", "SITE_PG", "Support Agent", "FULL_TIME", 40, "2021-06-14"),
    ("EMP_011", "Siti Aminah", "siti.aminah@northwind-ops.example", "012-345 1011", "SITE_PG", "Support Agent", "PART_TIME", 20, "2023-10-02"),
    ("EMP_012", "Arjun Singh", "arjun.singh@northwind-ops.example", "012-345 1012", "SITE_PG", "Support Agent", "FULL_TIME", 40, "2022-03-21"),
]

EMPLOYEE_SKILLS = [
    # employee, skill, certified_until
    ("EMP_001", "INVENTORY", None),
    ("EMP_001", "TEAM_LEAD", None),
    ("EMP_001", "FIRST_AID", "2027-02-28"),
    ("EMP_002", "FORKLIFT", "2027-03-31"),
    ("EMP_002", "INVENTORY", None),
    ("EMP_003", "FORKLIFT", "2026-11-18"),   # certificate lapses mid-week: valid through Wed 18 Nov only
    ("EMP_003", "INVENTORY", None),
    ("EMP_004", "INVENTORY", None),
    ("EMP_005", "INVENTORY", None),
    ("EMP_005", "FIRST_AID", "2027-05-15"),
    ("EMP_006", "FORKLIFT", "2027-06-30"),
    ("EMP_006", "TEAM_LEAD", None),
    ("EMP_007", "INVENTORY", None),
    ("EMP_008", "FORKLIFT", "2027-01-15"),
    ("EMP_008", "INVENTORY", None),
    ("EMP_009", "CUST_SUPPORT", None),
    ("EMP_009", "TEAM_LEAD", None),
    ("EMP_010", "CUST_SUPPORT", None),
    ("EMP_011", "CUST_SUPPORT", None),
    ("EMP_012", "CUST_SUPPORT", None),
    ("EMP_012", "FIRST_AID", "2026-12-31"),
]

SHIFT_TYPES = [
    ("MORNING", "Morning", "06:00", "14:00", 8),
    ("AFTERNOON", "Afternoon", "14:00", "22:00", 8),
    ("NIGHT", "Night", "22:00", "06:00", 8),
]

# Planning week: Mon 2026-11-16 .. Sat 2026-11-21
DAYS = {"MON": "2026-11-16", "TUE": "2026-11-17", "WED": "2026-11-18",
        "THU": "2026-11-19", "FRI": "2026-11-20", "SAT": "2026-11-21"}

# (shift_id, site, day, shift_type, skill, required_headcount)
SHIFTS = [
    ("SHF_001", "SITE_KL", "MON", "MORNING", "INVENTORY", 3),
    ("SHF_002", "SITE_KL", "TUE", "MORNING", "INVENTORY", 3),
    ("SHF_003", "SITE_KL", "WED", "MORNING", "INVENTORY", 3),
    ("SHF_004", "SITE_KL", "THU", "MORNING", "INVENTORY", 3),
    ("SHF_005", "SITE_KL", "FRI", "MORNING", "INVENTORY", 3),
    ("SHF_006", "SITE_KL", "MON", "AFTERNOON", "FORKLIFT", 2),
    ("SHF_007", "SITE_KL", "TUE", "AFTERNOON", "FORKLIFT", 2),
    ("SHF_008", "SITE_KL", "WED", "AFTERNOON", "FORKLIFT", 2),
    ("SHF_009", "SITE_KL", "THU", "AFTERNOON", "FORKLIFT", 2),
    ("SHF_010", "SITE_KL", "FRI", "AFTERNOON", "FORKLIFT", 2),
    ("SHF_011", "SITE_KL", "SAT", "MORNING", "FORKLIFT", 2),
    ("SHF_012", "SITE_PG", "MON", "MORNING", "CUST_SUPPORT", 2),
    ("SHF_013", "SITE_PG", "TUE", "MORNING", "CUST_SUPPORT", 2),
    ("SHF_014", "SITE_PG", "WED", "MORNING", "CUST_SUPPORT", 2),
    ("SHF_015", "SITE_PG", "THU", "MORNING", "CUST_SUPPORT", 2),
    ("SHF_016", "SITE_PG", "FRI", "MORNING", "CUST_SUPPORT", 2),
]

# shift_id -> assigned employees. Gaps and conflicts below are intentional (see audit()).
ASSIGNMENT_PLAN = {
    "SHF_001": ["EMP_001", "EMP_004", "EMP_007"],
    "SHF_002": ["EMP_001", "EMP_004", "EMP_005"],   # EMP_004 is on approved leave (conflict)
    "SHF_003": ["EMP_001", "EMP_005"],              # understaffed: 2 of 3
    "SHF_004": ["EMP_001", "EMP_004", "EMP_007"],
    "SHF_005": ["EMP_001", "EMP_004", "EMP_005"],
    "SHF_006": ["EMP_002", "EMP_006"],
    "SHF_007": ["EMP_002", "EMP_003"],
    "SHF_008": ["EMP_002", "EMP_003"],
    "SHF_009": ["EMP_002", "EMP_003"],              # EMP_003 forklift certificate expired on 18 Nov
    "SHF_010": ["EMP_002"],                         # understaffed: 1 of 2
    "SHF_011": ["EMP_002", "EMP_006"],              # EMP_002: 48 h week and <11 h rest after Fri
    "SHF_012": ["EMP_009", "EMP_010"],
    "SHF_013": ["EMP_009", "EMP_010"],
    "SHF_014": ["EMP_010", "EMP_012"],              # EMP_012 is unavailable on Wednesdays (conflict)
    "SHF_015": ["EMP_009", "EMP_011"],              # EMP_011 is on approved leave (conflict)
    "SHF_016": ["EMP_012"],                         # understaffed: 1 of 2
}

TIME_OFF = [
    ("TOF_001", "EMP_004", "2026-11-17", "2026-11-18", "ANNUAL_LEAVE", "APPROVED"),
    ("TOF_002", "EMP_008", "2026-11-20", "2026-11-20", "ANNUAL_LEAVE", "APPROVED"),
    ("TOF_003", "EMP_011", "2026-11-19", "2026-11-20", "ANNUAL_LEAVE", "APPROVED"),
    ("TOF_004", "EMP_007", "2026-11-21", "2026-11-21", "PERSONAL_LEAVE", "PENDING"),
]

AVAILABILITY = [
    ("EMP_012", 2, "UNAVAILABLE", "Evening classes on Wednesdays"),
    ("EMP_005", 4, "UNAVAILABLE", "Childcare on Fridays"),
]


def create_database(db_file: str = DB_FILE, force: bool = False) -> bool:
    """Create and seed the operational database. Returns True if it was (re)built."""
    if os.path.exists(db_file):
        if not force:
            return False
        os.remove(db_file)

    conn = sqlite3.connect(db_file)
    try:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO sites VALUES (?,?,?)", SITES)
        conn.executemany("INSERT INTO skills VALUES (?,?,?)", SKILLS)
        conn.executemany("INSERT INTO employees VALUES (?,?,?,?,?,?,?,?,?)", EMPLOYEES)
        conn.executemany("INSERT INTO employee_skills VALUES (?,?,?)", EMPLOYEE_SKILLS)
        conn.executemany("INSERT INTO shift_types VALUES (?,?,?,?,?)", SHIFT_TYPES)
        conn.executemany(
            "INSERT INTO shifts VALUES (?,?,?,?,?,?)",
            [(sid, site, DAYS[day], stype, skill, req) for sid, site, day, stype, skill, req in SHIFTS],
        )
        assignments = []
        n = 1
        for shift_id, emps in ASSIGNMENT_PLAN.items():
            for emp in emps:
                assignments.append((f"ASG_{n:03d}", shift_id, emp, "SCHEDULED"))
                n += 1
        conn.executemany("INSERT INTO assignments VALUES (?,?,?,?)", assignments)
        conn.executemany("INSERT INTO time_off VALUES (?,?,?,?,?,?)", TIME_OFF)
        conn.executemany("INSERT INTO availability VALUES (?,?,?,?)", AVAILABILITY)
        conn.commit()
    finally:
        conn.close()
    return True


# ------------------------------------------------------------------------------
# 2. Policy knowledge base (markdown)
# ------------------------------------------------------------------------------
POLICIES = {
    "shift_definitions.md": """# Shift Definitions and Staffing Standards

Document owner: Workforce Planning Office. Effective: 1 January 2026.

## 1. Shift types

| Shift     | Start | End   | Paid hours |
|-----------|-------|-------|------------|
| Morning   | 06:00 | 14:00 | 8          |
| Afternoon | 14:00 | 22:00 | 8          |
| Night     | 22:00 | 06:00 | 8          |

The planning week runs Monday to Sunday. Saturday and Sunday shifts are treated as weekend shifts.

## 2. Required headcount

Every shift record carries a `required_headcount` and a required `skill_id`. Every person assigned
to the shift must hold that skill. Coverage is calculated as assigned headcount divided by
required headcount.

## 3. Standard staffing by site

- **KL Distribution Hub**: Morning inventory shifts need 3 people holding Inventory Control.
  Afternoon and Saturday loading shifts need 2 certified Forklift Operators.
- **Penang Service Center**: Morning support shifts need 2 people holding Customer Support.

## 4. Coverage status labels

| Coverage          | Label        |
|-------------------|--------------|
| 100% or more      | FULLY STAFFED |
| 50% to below 100% | UNDERSTAFFED |
| Below 50%         | CRITICAL     |

A shift with at least one required person missing is a coverage gap.
""",
    "scheduling_policy.md": """# Scheduling and Assignment Policy

Document owner: Workforce Planning Office. Effective: 1 January 2026.

## 1. Assignment eligibility

An employee may be assigned to a shift only if ALL of the following are true:

1. The employee holds the skill required by the shift.
2. If the skill requires certification (Forklift Operator, First Aid Responder), the certificate
   is valid on the shift date. A certificate valid through a date is valid on that date and invalid
   the day after.
3. The employee has no APPROVED time off covering the shift date. PENDING requests do not block an
   assignment, but the planner must warn the employee's supervisor.
4. The employee has no standing UNAVAILABLE constraint for that weekday.
5. The employee is not already assigned to another shift on the same day.
6. The assignment does not breach the rest-period, weekly-hours or consecutive-day limits in the
   Labor Compliance Handbook.

## 2. Filling a coverage gap

When a shift is understaffed, planners must pick replacements in this order:

1. Eligible employees at the same site who would remain at or below their regular weekly hours.
2. Eligible part-time employees at the same site who would remain within their contractual hours.
3. Eligible employees who would move into approved overtime (needs supervisor sign-off, see the
   Labor Compliance Handbook).
4. Eligible employees from the other site (requires Duty Manager approval).

## 3. Escalation

- Any shift labelled UNDERSTAFFED must be reported to the Site Supervisor at least 3 days
  before the shift.
- Any shift labelled CRITICAL must be reported to the Duty Manager immediately.

## 4. Fairness

- Weekend shifts are rotated. No employee should work more than two consecutive weekends.
- Overtime opportunities are offered to the employee with the fewest overtime hours first.

## 5. Shift swaps

Employees may swap shifts if both employees are eligible under section 1 and the Site Supervisor
approves the swap at least 24 hours before the shift starts.

## 6. Publication

Schedules are published 7 days before the start of the planning week. Changes after publication
are logged as amendments.

## 7. Data handling in reports

Reports and answers produced by planning tools should identify staff by employee ID
(for example EMP_002). Contact details such as email addresses and phone numbers must never be
included in reports.
""",
    "labor_compliance_handbook.md": """# Labor Compliance Handbook

Document owner: People and Compliance. Effective: 1 January 2026.

## 1. Regular working hours

- Full-time employees: regular hours of up to 40 hours per week.
- Part-time employees: regular hours up to the `max_weekly_hours` stated in their contract
  (for example 20 or 24 hours).
- Hours above the regular limit are overtime.

## 2. Overtime

- Overtime requires written approval from the Site Supervisor before the shift.
- Full-time employees may work at most 8 overtime hours per week, so the absolute weekly cap is
  48 hours.
- Part-time employees must not work overtime. Their contractual hours are a hard cap.
- Any week above the regular limit must be flagged as an OVERTIME RISK in planning reports.

## 3. Rest periods

- A minimum of 11 hours of rest is required between the end of one shift and the start of the next.
- Example: finishing an Afternoon shift at 22:00 means the earliest permitted start the next day
  is 09:00, so a Morning shift (06:00) the next day is a rest-period violation.

## 4. Consecutive working days

No employee may work more than 6 consecutive days. A rest day is required after the sixth day.

## 5. Certifications

- Forklift Operator: a valid forklift certificate is mandatory on every shift that requires the
  Forklift skill. Operating a forklift with an expired certificate is a serious safety violation
  and the assignment must be removed.
- First Aid Responder: a valid certificate is required whenever a shift lists First Aid as its
  required skill.
- Certificates expiring within 30 days of the planning week should be flagged for renewal.

## 6. Leave

- APPROVED leave is a binding entitlement. Employees on approved leave must not be scheduled.
- Annual leave requests must be submitted at least 14 days ahead.

## 7. Personal data (PDPA)

Employee personal data (names, email addresses, phone numbers) is confidential. Planning reports
refer to employees by ID. Personal data must not be copied into chat tools, tickets or exports.

## 8. Violations summary

| Violation                       | Severity |
|---------------------------------|----------|
| Expired mandatory certification | CRITICAL |
| Scheduled during approved leave | HIGH     |
| Rest period under 11 hours      | HIGH     |
| Weekly hours above 48 (FT)      | HIGH     |
| Weekly hours above regular limit| MEDIUM   |
| Standing unavailability breach  | MEDIUM   |
""",
}


def write_policies(policy_dir: str = POLICY_DIR) -> list[str]:
    """Write the policy documents that are missing. Returns the files written."""
    os.makedirs(policy_dir, exist_ok=True)
    written = []
    for name, body in POLICIES.items():
        path = os.path.join(policy_dir, name)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            written.append(name)
    return written


def bootstrap(force: bool = False) -> dict:
    """Idempotent entry point used by the app and the MCP server."""
    built = create_database(DB_FILE, force=force)
    written = write_policies(POLICY_DIR)
    return {"database_built": built, "policies_written": written}


# ------------------------------------------------------------------------------
# Step 8: live schema evolution (zero-code proof of MCP runtime discovery)
# ------------------------------------------------------------------------------
# Simulates a data team shipping two new tables while the app is running. No agent code, prompt or
# tool definition mentions them: the agents can only find them through mcp_list_tables.
DEMO_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS overtime_requests (
    request_id  TEXT PRIMARY KEY,
    employee_id TEXT NOT NULL,
    week_start  TEXT NOT NULL,
    extra_hours REAL NOT NULL,
    status      TEXT NOT NULL,                -- APPROVED | PENDING | REJECTED
    note        TEXT,
    FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
);
INSERT OR IGNORE INTO overtime_requests VALUES
    ('OTR_001', 'EMP_002', '2026-11-16', 8, 'PENDING',  'Forklift cover; awaiting Site Supervisor sign-off'),
    ('OTR_002', 'EMP_001', '2026-11-16', 4, 'APPROVED', 'Stocktake preparation'),
    ('OTR_003', 'EMP_010', '2026-11-16', 6, 'REJECTED', 'Support queue is covered by part-time staff');

CREATE TABLE IF NOT EXISTS shift_swap_requests (
    swap_id          TEXT PRIMARY KEY,
    shift_id         TEXT NOT NULL,
    from_employee_id TEXT NOT NULL,
    to_employee_id   TEXT NOT NULL,
    status           TEXT NOT NULL,           -- APPROVED | PENDING | REJECTED
    reason           TEXT,
    FOREIGN KEY (shift_id) REFERENCES shifts(shift_id),
    FOREIGN KEY (from_employee_id) REFERENCES employees(employee_id),
    FOREIGN KEY (to_employee_id) REFERENCES employees(employee_id)
);
INSERT OR IGNORE INTO shift_swap_requests VALUES
    ('SWP_001', 'SHF_011', 'EMP_002', 'EMP_008', 'PENDING',  'Rest period after Friday afternoon shift'),
    ('SWP_002', 'SHF_009', 'EMP_003', 'EMP_006', 'APPROVED', 'Forklift certificate renewal pending'),
    ('SWP_003', 'SHF_015', 'EMP_011', 'EMP_010', 'REJECTED', 'Requested after leave approval cut-off');
"""
DEMO_TABLES = ("overtime_requests", "shift_swap_requests")


def apply_demo_migration(db_file: str = DB_FILE) -> list[str]:
    """Add the demo tables through a writable admin connection. Idempotent. Returns the tables present."""
    conn = sqlite3.connect(db_file)
    try:
        conn.executescript(DEMO_MIGRATION_SQL)
        conn.commit()
    finally:
        conn.close()
    return list(DEMO_TABLES)


def demo_migration_applied(db_file: str = DB_FILE) -> bool:
    conn = sqlite3.connect(f"file:{os.path.abspath(db_file).replace(chr(92), '/')}?mode=ro", uri=True)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    return set(DEMO_TABLES) <= names


def reset_database(db_file: str = DB_FILE) -> None:
    """Rebuild the baseline database (drops the demo tables and any other change)."""
    create_database(db_file, force=True)


# ------------------------------------------------------------------------------
# 3. Audit (human check that the seeded data contains the intended scenarios)
# ------------------------------------------------------------------------------
def audit(db_file: str = DB_FILE) -> None:
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    cur = conn.cursor()

    def show(title, sql):
        print(f"\n== {title} ==")
        cur.execute(sql)
        rows = cur.fetchall()
        if not rows:
            print("(none)")
        for r in rows:
            print("  " + " | ".join(str(c) for c in r))

    print("Row counts:")
    for (t,) in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall():
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<16} {n}")

    show("Coverage gaps (assigned < required)", """
        SELECT s.shift_id, s.shift_date, s.site_id, s.skill_id,
               COUNT(a.employee_id) AS assigned, s.required_headcount AS required
        FROM shifts s LEFT JOIN assignments a ON a.shift_id = s.shift_id
        GROUP BY s.shift_id HAVING assigned < required ORDER BY s.shift_date""")

    show("Assignments during APPROVED leave", """
        SELECT a.employee_id, s.shift_id, s.shift_date, t.leave_type
        FROM assignments a JOIN shifts s ON s.shift_id = a.shift_id
        JOIN time_off t ON t.employee_id = a.employee_id AND t.status = 'APPROVED'
          AND s.shift_date BETWEEN t.start_date AND t.end_date""")

    show("Assignments with expired mandatory certification", """
        SELECT a.employee_id, s.shift_id, s.shift_date, es.certified_until
        FROM assignments a JOIN shifts s ON s.shift_id = a.shift_id
        JOIN skills k ON k.skill_id = s.skill_id AND k.requires_certification = 1
        JOIN employee_skills es ON es.employee_id = a.employee_id AND es.skill_id = s.skill_id
        WHERE es.certified_until < s.shift_date""")

    show("Assignments on a standing-unavailable weekday", """
        SELECT a.employee_id, s.shift_id, s.shift_date, v.reason
        FROM assignments a JOIN shifts s ON s.shift_id = a.shift_id
        JOIN availability v ON v.employee_id = a.employee_id
          AND v.weekday = (CAST(strftime('%w', s.shift_date) AS INTEGER) + 6) % 7""")

    show("Weekly hours above contractual limit", """
        SELECT e.employee_id, SUM(st.hours) AS planned_hours, e.max_weekly_hours
        FROM assignments a JOIN shifts s ON s.shift_id = a.shift_id
        JOIN shift_types st ON st.shift_type_id = s.shift_type_id
        JOIN employees e ON e.employee_id = a.employee_id
        GROUP BY e.employee_id HAVING planned_hours > e.max_weekly_hours""")

    show("Planned weekly hours per employee", """
        SELECT e.employee_id, e.employment_type, COALESCE(SUM(st.hours), 0) AS planned_hours,
               e.max_weekly_hours
        FROM employees e LEFT JOIN assignments a ON a.employee_id = e.employee_id
        LEFT JOIN shifts s ON s.shift_id = a.shift_id
        LEFT JOIN shift_types st ON st.shift_type_id = s.shift_type_id
        GROUP BY e.employee_id ORDER BY e.employee_id""")
    conn.close()


if __name__ == "__main__":
    result = bootstrap(force="--force" in sys.argv)
    print(f"Database: {DB_FILE} ({'built' if result['database_built'] else 'already present'})")
    print(f"Policies: {POLICY_DIR} (written: {result['policies_written'] or 'none, already present'})")
    audit()
