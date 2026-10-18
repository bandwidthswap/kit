"""Exact money.

Every amount in the simulator is a `fractions.Fraction`, so a day of 24 windows
at $0.125 adds up to exactly $3.00 and five percent of it is exactly $0.15.
Rounding happens once, when a number is printed. Prices in scenario files are
read through `money()`, which turns the float 0.1 into 1/10 and not into its
binary neighbour.

A gigabyte is 10**9 bytes, as bwkit prices it (`settled / 1e9 * price`).
"""

from fractions import Fraction

GB = 10 ** 9


def money(x):
    """A Fraction from an int, a float, a decimal string or a Fraction."""
    if isinstance(x, Fraction):
        return x
    if isinstance(x, bool):
        raise TypeError("a boolean is not an amount")
    if isinstance(x, int):
        return Fraction(x)
    if isinstance(x, float):
        return Fraction(repr(x))
    if isinstance(x, str):
        s = x.strip().replace("$", "").replace(",", "")
        return Fraction(s)
    raise TypeError("not an amount: %r" % (x,))


def gb(nbytes):
    """Bytes as an exact number of gigabytes."""
    return Fraction(int(nbytes), GB)


def fmt(x, unit="USD", symbol="$", places=None):
    """$7.60, $0.0063, 40.00 MRS. Two places, four when two would hide the number."""
    x = Fraction(x)
    if places is None:
        a = abs(x)
        if a == 0 or (a * 100).denominator == 1:
            places = 2
        elif a < 1:
            places = 4
            if round(a, 4) == 0:
                places = 6
        else:
            places = 2
    s = format(float(x), ".%df" % places)
    if s.startswith("-"):
        sign, s = "-", s[1:]
    else:
        sign = ""
    if symbol:
        return "%s%s%s" % (sign, symbol, s)
    return "%s%s %s" % (sign, s, unit)


def fmt_gb(nbytes):
    g = float(Fraction(int(nbytes), GB))
    if g == 0:
        return "0 GB"
    if g < 0.1:
        return "%.4f GB" % g
    return "%.2f GB" % g


def pct(x):
    """5%, 2.5%."""
    v = float(Fraction(x) * 100)
    s = ("%.4f" % v).rstrip("0").rstrip(".")
    return s + "%"
