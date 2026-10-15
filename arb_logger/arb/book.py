def vwap(levels, qty):
    """Average fill price for `qty` walking `levels` [(price, size), ...].
    Returns (avg_price, filled_qty). filled < qty means the book was too thin."""
    remaining, cost = qty, 0.0
    for price, size in levels:
        take = min(size, remaining)
        cost += take * price
        remaining -= take
        if remaining <= 1e-15:
            break
    filled = qty - max(remaining, 0.0)
    return (cost / filled if filled > 0 else None), filled
