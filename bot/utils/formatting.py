from __future__ import annotations

import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

_WARSAW = ZoneInfo("Europe/Warsaw")


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

def format_meal_preview(
    description: str,
    cals: int,
    protein: float,
    carbs: float,
    fat: float,
    profile_names: list[str],
    eaten_at: datetime,
) -> str:
    """Pre-log preview shown before the user approves or refines."""
    target = ", ".join(profile_names) if profile_names else "(no profile)"
    time_str = eaten_at.strftime("%H:%M")
    return (
        f"Preview — will log to: {target} at {time_str}\n"
        f"{description}\n"
        f"{cals} kcal | P: {protein:g}g | C: {carbs:g}g | F: {fat:g}g\n"
        "\n"
        "Reply /yes to log, or send a remark to refine.\n"
        'Example: "actually larger portion" or "add a tablespoon of butter".'
    )


def format_recipe_preview(
    dish_name: str,
    per_serving: dict,
    total: dict,
    servings: int,
    profile_names: list[str],
) -> str:
    """Pre-log preview for a recipe."""
    target = ", ".join(profile_names) if profile_names else "(no profile)"
    return (
        f"Recipe: {dish_name} ({servings} servings)\n"
        "\n"
        f"Per serving: {per_serving['calories']} kcal | "
        f"P: {per_serving['protein_g']:g}g | "
        f"C: {per_serving['carbs_g']:g}g | "
        f"F: {per_serving['fat_g']:g}g\n"
        f"Whole dish: {total['calories']} kcal | "
        f"P: {total['protein_g']:g}g | "
        f"C: {total['carbs_g']:g}g | "
        f"F: {total['fat_g']:g}g\n"
        "\n"
        f"Target: {target}. Reply /yes to log one serving, "
        "or send a remark to refine."
    )


def format_liquid_preview(
    description: str,
    amount_ml: int,
    cals: int,
    protein: float,
    carbs: float,
    fat: float,
    profile_names: list[str],
    drunk_at: datetime,
) -> str:
    """Pre-log preview for a drink."""
    target = ", ".join(profile_names) if profile_names else "(no profile)"
    time_str = drunk_at.strftime("%H:%M")
    return (
        f"Preview — will log drink to: {target} at {time_str}\n"
        f"{description} ({amount_ml}ml)\n"
        f"{cals} kcal | P: {protein:g}g | C: {carbs:g}g | F: {fat:g}g\n"
        "\n"
        "Reply /yes to log, or send a remark to refine."
    )


def _format_macro_progress(label: str, current: float, goal: float | None) -> str:
    if goal is None or goal <= 0:
        return f"{label}: {current:g}g"
    return f"{label}: {current:g} / {goal:g}g"


def format_meal_logged(
    profile_name: str,
    description: str,
    cals: int,
    protein: float,
    carbs: float,
    fat: float,
    daily_total: dict,
    goal: dict,
) -> str:
    total_cals = daily_total["calories"]
    goal_cals = goal["daily_calories"]
    diff = total_cals - goal_cals
    if diff > 0:
        remaining_part = f"({diff} over goal)"
    else:
        remaining_part = f"({goal_cals - total_cals} remaining)"

    protein_line = _format_macro_progress("P", daily_total["protein_g"], goal.get("daily_protein_g"))
    carbs_line = _format_macro_progress("C", daily_total["carbs_g"], goal.get("daily_carbs_g"))
    fat_line = _format_macro_progress("F", daily_total["fat_g"], goal.get("daily_fat_g"))

    return (
        f"[{profile_name}] Logged: {description}\n"
        f"{cals} kcal | P: {protein:g}g | C: {carbs:g}g | F: {fat:g}g\n"
        f"Daily: {total_cals} / {goal_cals} kcal {remaining_part}\n"
        f"{protein_line} | {carbs_line} | {fat_line}"
    )


def format_liquid_logged(
    profile_name: str,
    description: str,
    amount_ml: int,
    cals: int,
    protein: float,
    carbs: float,
    fat: float,
    daily_total: dict,
    goal: dict,
    hydration_ml: int,
) -> str:
    total_cals = daily_total["calories"]
    goal_cals = goal["daily_calories"]
    diff = total_cals - goal_cals
    if diff > 0:
        remaining_part = f"({diff} over goal)"
    else:
        remaining_part = f"({goal_cals - total_cals} remaining)"

    protein_line = _format_macro_progress("P", daily_total["protein_g"], goal.get("daily_protein_g"))
    carbs_line = _format_macro_progress("C", daily_total["carbs_g"], goal.get("daily_carbs_g"))
    fat_line = _format_macro_progress("F", daily_total["fat_g"], goal.get("daily_fat_g"))

    return (
        f"[{profile_name}] Logged Drink: {description} ({amount_ml}ml)\n"
        f"{cals} kcal | P: {protein:g}g | C: {carbs:g}g | F: {fat:g}g\n"
        f"Daily Hydration: {hydration_ml} ml\n"
        f"Daily: {total_cals} / {goal_cals} kcal {remaining_part}\n"
        f"{protein_line} | {carbs_line} | {fat_line}"
    )


def _format_eaten_at(eaten_at: str | datetime) -> str:
    """Return HH:MM (Europe/Warsaw) from a datetime object or a string.

    asyncpg returns TIMESTAMPTZ as UTC-aware datetimes, so we always convert
    timezone-aware values to Warsaw before formatting. Naive datetimes are
    assumed to already be in Warsaw local time (legacy path).
    """
    if isinstance(eaten_at, datetime):
        if eaten_at.tzinfo is not None:
            eaten_at = eaten_at.astimezone(_WARSAW)
        return eaten_at.strftime("%H:%M")
    # Try common ISO-ish formats (legacy string path — naive, already Warsaw)
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(eaten_at, fmt).strftime("%H:%M")
        except (ValueError, TypeError):
            continue
    # Fallback via datetime.fromisoformat (handles timezone offsets too)
    try:
        dt = datetime.fromisoformat(eaten_at)
        if dt.tzinfo is not None:
            dt = dt.astimezone(_WARSAW)
        return dt.strftime("%H:%M")
    except (ValueError, TypeError):
        pass
    # Already HH:MM or something short
    return str(eaten_at)


def format_summary(
    profile_name: str,
    meals: list[dict],
    liquids: list[dict],
    total: dict,
    goal: dict,
    hydration_ml: int,
) -> str:
    lines: list[str] = [f"Daily Summary for {profile_name}", ""]

    if meals:
        lines.append("--- Food ---")
        for meal in meals:
            t = _format_eaten_at(meal["eaten_at"])
            lines.append(f"{t} - {meal['description']} \u2014 {meal['calories']} kcal")
        lines.append("")

    if liquids:
        lines.append("--- Drinks ---")
        for liquid in liquids:
            t = _format_eaten_at(liquid["drunk_at"])
            lines.append(f"{t} - {liquid['description']} ({liquid['amount_ml']}ml) \u2014 {liquid['calories']} kcal")
        lines.append("")

    lines.append(f"Hydration: {hydration_ml} ml")
    lines.append("")
    goal_cals = goal["daily_calories"]
    lines.append(f"Calories: {total['calories']} / {goal_cals} kcal")
    
    p_line = _format_macro_progress("Protein", total["protein_g"], goal.get("daily_protein_g"))
    c_line = _format_macro_progress("Carbs", total["carbs_g"], goal.get("daily_carbs_g"))
    f_line = _format_macro_progress("Fat", total["fat_g"], goal.get("daily_fat_g"))
    
    lines.append(p_line)
    lines.append(c_line)
    lines.append(f_line)
    return "\n".join(lines)


def format_week(
    profile_name: str,
    daily_data: list[dict],
    goal: dict,
) -> str:
    lines: list[str] = [f"Weekly Summary for {profile_name}", ""]

    total_cals = 0
    count = 0
    goal_cals = goal["daily_calories"]
    for day in daily_data:
        cals = day["calories"]
        total_cals += cals
        count += 1
        diff = cals - goal_cals
        if diff > 0:
            note = f"({diff} over goal)"
        elif diff < 0:
            note = f"({-diff} under goal)"
        else:
            note = "(on goal)"
        lines.append(f"{day['date']}: {cals} kcal {note}")

    avg = round(total_cals / count) if count else 0
    lines.append("")
    lines.append(f"Weekly average: {avg} kcal/day (Goal: {goal_cals})")
    return "\n".join(lines)


def format_report(
    profile_name: str,
    date: str,
    meals: list[dict],
    liquids: list[dict],
    total: dict,
    hydration_ml: int,
    supplements_scheduled: list[dict] | None = None,
    supplements_taken: list[dict] | None = None,
) -> str:
    taken_names: set[str] = set()
    if supplements_taken:
        for s in supplements_taken:
            taken_names.add(s.get("name") or s.get("supplement_id", ""))

    lines: list[str] = [
        "Nutrition Report",
        f"Profile: {profile_name}",
        f"Date: {date}",
        "",
        "--- Meals ---",
    ]

    for meal in meals:
        t = _format_eaten_at(meal["eaten_at"])
        lines.append(f"{t}  {meal['description']}")
        lines.append(
            f"       Calories: {meal['calories']} | "
            f"Protein: {meal.get('protein_g', 0):g}g | "
            f"Carbs: {meal.get('carbs_g', 0):g}g | "
            f"Fat: {meal.get('fat_g', 0):g}g"
        )
        lines.append("")

    if liquids:
        lines.append("--- Drinks ---")
        for liquid in liquids:
            t = _format_eaten_at(liquid["drunk_at"])
            lines.append(f"{t}  {liquid['description']} ({liquid['amount_ml']}ml)")
            lines.append(
                f"       Calories: {liquid['calories']} | "
                f"Protein: {liquid.get('protein_g', 0):g}g | "
                f"Carbs: {liquid.get('carbs_g', 0):g}g | "
                f"Fat: {liquid.get('fat_g', 0):g}g"
            )
            lines.append("")

    lines.append("--- Daily Totals ---")
    lines.append(f"Hydration: {hydration_ml} ml")
    lines.append(f"Calories: {total['calories']}")
    lines.append(
        f"Protein: {total['protein_g']:g}g | "
        f"Carbs: {total['carbs_g']:g}g | "
        f"Fat: {total['fat_g']:g}g"
    )
    
    goal = total.get("goal")
    if goal and isinstance(goal, dict):
        lines.append(f"Goal: {goal['daily_calories']} kcal")
        if goal.get("daily_protein_g"):
            lines.append(
                f"Goal Macros: P: {goal['daily_protein_g']:g}g | "
                f"C: {goal['daily_carbs_g']:g}g | "
                f"F: {goal['daily_fat_g']:g}g"
            )
    elif goal:
        lines.append(f"Goal: {goal} kcal")

    if supplements_scheduled:
        lines.append("")
        lines.append("--- Supplements ---")
        for sup in supplements_scheduled:
            name = sup["name"]
            reminder = sup["reminder_time"]
            dose_str = f" ({sup['dose']})" if sup.get("dose") else ""
            check = "x" if name in taken_names else " "
            lines.append(f"[{check}] {name}{dose_str} ({reminder})")

    return "\n".join(lines)


def format_profile_list(
    profiles: list[dict],
    active_id: int | None,
) -> str:
    lines: list[str] = ["Your profiles:"]
    for p in profiles:
        marker = "\u2713 " if p["id"] == active_id else "  "
        lines.append(f"{marker}{p['name']}")
    return "\n".join(lines)


def format_supplement_list(supplements: list[dict]) -> str:
    lines: list[str] = ["Supplements:"]
    for s in supplements:
        dose_str = f" ({s['dose']})" if s.get("dose") else ""
        lines.append(f"  {s['name']}{dose_str} \u2014 {s['reminder_time']}")
    return "\n".join(lines)


def format_help() -> str:
    return (
        "Available commands:\n"
        "\n"
        "/cal <description> [@name] [at HH:MM]\n"
        "  Analyse a meal and show a preview. Send a remark to refine, or\n"
        "  /yes to log. Send a photo with optional caption for vision mode.\n"
        "\n"
        "/liquid <description and amount> [@name] [at HH:MM]\n"
        "  Log a drink (e.g. 500ml water or 250ml coffee). Same flow as /cal.\n"
        "\n"
        "/recipe <description> [for N]\n"
        "  Analyse a recipe and show a preview. Same refine/confirm flow.\n"
        "\n"
        "/summary [@name]\n"
        "  Show today's meal summary for a profile.\n"
        "\n"
        "/week [@name]\n"
        "  Show the weekly calorie overview.\n"
        "\n"
        "/report [@name]\n"
        "  Generate a dietitian-ready daily report.\n"
        "\n"
        "/goal <calories> [@name]\n"
        "  Set the daily calorie goal for a profile.\n"
        "\n"
        "/profile [add|remove|switch] <name>\n"
        "  Manage tracked profiles.\n"
        "\n"
        "/supplement [add|remove|list] <name> [at HH:MM]\n"
        "  Manage supplement reminders.\n"
        "\n"
        "/model [model_name]\n"
        "  View or change the AI model used for estimation.\n"
        "\n"
        "/yes\n"
        "  Confirm and log the pending meal/recipe preview."
    )


# ---------------------------------------------------------------------------
# Parser helpers
# ---------------------------------------------------------------------------

_TARGET_RE = re.compile(r"@(\w+)", re.IGNORECASE)
_TIME_RE = re.compile(r"\bat\s+(\d{1,2}:\d{2})\b", re.IGNORECASE)
_SERVINGS_RE = re.compile(r"\bfor\s+(\d+)\b", re.IGNORECASE)


def parse_target(text: str) -> tuple[str | None, bool]:
    """Extract ``@name`` or ``@both`` from *text*.

    Returns
    -------
    tuple[str | None, bool]
        ``(profile_name, is_both)``
        - ``@both`` (case-insensitive) -> ``(None, True)``
        - ``@SomeName``               -> ``("SomeName", False)``
        - no match                    -> ``(None, False)``

    The matched ``@target`` token is **not** stripped from *text*; use
    :func:`strip_command_args` to obtain the bare description.
    """
    m = _TARGET_RE.search(text)
    if m is None:
        return (None, False)
    name = m.group(1)
    if name.lower() == "both":
        return (None, True)
    return (name, False)


def parse_time(text: str) -> datetime | None:
    """Extract ``at HH:MM`` from *text* and return a datetime for today.

    Returns ``None`` when no match is found.
    """
    m = _TIME_RE.search(text)
    if m is None:
        return None
    parts = m.group(1).split(":")
    h, mi = int(parts[0]), int(parts[1])
    try:
        return datetime.combine(date.today(), time(h, mi))
    except ValueError:
        return None


def parse_servings(text: str) -> int | None:
    """Extract ``for N`` from *text* and return the integer, or ``None``."""
    m = _SERVINGS_RE.search(text)
    if m is None:
        return None
    return int(m.group(1))


def strip_command_args(text: str) -> str:
    """Strip ``@target``, ``at HH:MM``, and ``for N`` from *text*.

    Returns the remaining text (the food/recipe description) with collapsed
    whitespace.
    """
    text = _TARGET_RE.sub("", text)
    text = _TIME_RE.sub("", text)
    text = _SERVINGS_RE.sub("", text)
    return " ".join(text.split())
