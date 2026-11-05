"""Number formatting shared by the text and HTML reports."""


def tokens(value) -> str:
    value = float(value)
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= size:
            return "%.1f%s" % (value / size, unit)
    return "%d" % round(value)


def percent(part, whole) -> str:
    return "%.1f%%" % (100.0 * part / whole) if whole else "0.0%"


def dollars(value) -> str:
    return "-" if value is None else "$%.2f" % value


def average(value) -> str:
    return "-" if value is None else tokens(value)
