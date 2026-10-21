"""Pure calculation of cumulative capacity margin at task deadlines."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation


_DAILY_CAPACITY = Decimal("22")


def _parse_date(value):
    """Accept date objects and canonical ISO calendar dates."""
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _parse_hours(value):
    """Convert ordinary numeric inputs to Decimal for exact threshold checks."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _public_number(value):
    """Return an ordinary int/float while keeping integral hours tidy."""
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _result(
    business_date,
    *,
    hours=None,
    bottleneck_date=None,
    bottleneck_days=None,
    sufficient=False,
    reason=None,
):
    return {
        "business_date": business_date,
        "hours": hours,
        "bottleneck_date": bottleneck_date,
        "bottleneck_days": bottleneck_days,
        "sufficient": sufficient,
        "reason": reason,
    }


def calculate_schedule_margin(tasks, business_day: date):
    """Calculate the smallest cumulative hour margin at unique task deadlines.

    Each task is placed on its final ``remaining_days`` days through its
    deadline. Work assigned to a later-deadline task is therefore included at
    an earlier checkpoint when that late-placement window has already begun.
    Threshold decisions use Decimal arithmetic so exact boundaries stay
    strict: the minimum must exceed 22 hours and every checkpoint must exceed
    four hours per elapsed day.
    """
    if isinstance(business_day, datetime) or not isinstance(business_day, date):
        return _result(None, reason="invalid_plan")

    business_date = business_day.isoformat()
    if not isinstance(tasks, list):
        return _result(business_date, reason="invalid_plan")
    if not tasks:
        return _result(business_date, reason="no_pending_tasks")

    parsed_tasks = []
    invalid_plan = False
    insufficient_days = False

    for task in tasks:
        if not isinstance(task, dict):
            invalid_plan = True
            continue

        remaining_days = task.get("remaining_days")
        if (
            isinstance(remaining_days, bool)
            or not isinstance(remaining_days, int)
            or remaining_days <= 0
        ):
            invalid_plan = True
            continue

        daily_hours = _parse_hours(task.get("daily_hours"))
        deadline = _parse_date(task.get("deadline"))
        if daily_hours is None or deadline is None:
            invalid_plan = True
            continue

        deadline_days = (deadline - business_day).days + 1
        parsed_tasks.append((remaining_days, daily_hours, deadline, deadline_days))
        if remaining_days > deadline_days:
            insufficient_days = True

    if invalid_plan:
        return _result(business_date, reason="invalid_plan")
    if insufficient_days:
        return _result(business_date, reason="insufficient_days")

    checkpoints = sorted({task[3] for task in parsed_tasks})
    margins = []
    for checkpoint_days in checkpoints:
        cumulative_work = Decimal(0)
        for remaining_days, daily_hours, _, task_deadline_days in parsed_tasks:
            scheduled_days = max(
                0,
                remaining_days - max(0, task_deadline_days - checkpoint_days),
            )
            cumulative_work += daily_hours * scheduled_days

        capacity = _DAILY_CAPACITY * checkpoint_days
        margins.append((capacity - cumulative_work, checkpoint_days))

    # On equal minimum margins, report the later deadline as requested.
    bottleneck_margin, bottleneck_days = min(
        margins, key=lambda item: (item[0], -item[1])
    )
    sufficient = bottleneck_margin > _DAILY_CAPACITY and all(
        margin > Decimal(4 * checkpoint_days)
        for margin, checkpoint_days in margins
    )
    bottleneck_date = next(
        deadline.isoformat()
        for _, checkpoint_days in margins
        if checkpoint_days == bottleneck_days
        for _, _, deadline, deadline_days in parsed_tasks
        if deadline_days == checkpoint_days
    )

    return _result(
        business_date,
        hours=_public_number(bottleneck_margin),
        bottleneck_date=bottleneck_date,
        bottleneck_days=bottleneck_days,
        sufficient=sufficient,
    )
