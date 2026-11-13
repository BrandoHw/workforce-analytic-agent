# Shift Definitions and Staffing Standards

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
