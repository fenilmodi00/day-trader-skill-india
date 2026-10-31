"""Number formatting for the skills: plain floats, prices and money."""

from decimal import Decimal


def as_float(x):
    """float(x), or None when it is not a number."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def price_text(x):
    """50, 50.5, 50.25: a price without trailing zeros."""
    value = float(x)
    if value % 1:
        return ("%f" % value).rstrip("0").rstrip(".")
    return "%d" % round(value)


def money_text(x, prefix="Rs", trim=True):
    """Money for people: "Rs 1,234" / "Rs 1,234.50" trimmed, "INR 1,234.00" not."""
    value = float(x)
    if not trim:
        return "%s %s" % (prefix, format(Decimal(str(value)).quantize(Decimal("0.01")), ",f"))
    if abs(value - round(value)) < 0.005:
        return "%s %s" % (prefix, "{:,.0f}".format(round(value)))
    return "%s %s" % (prefix, "{:,.2f}".format(value))
