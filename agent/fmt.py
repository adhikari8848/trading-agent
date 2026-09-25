"""Readable price formatting ($84,271 rather than $8.427e+04)."""


def price(p: float | None) -> str:
    if p is None:
        return "n/a"
    if abs(p) >= 1000:
        return f"${p:,.0f}"
    if abs(p) >= 1:
        return f"${p:,.2f}"
    return f"${p:.4g}"
