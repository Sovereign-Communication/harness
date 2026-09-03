def luhn_valid(number):
    s = str(number)
    total = 0
    for i, ch in enumerate(reversed(s)):
        d = int(ch)
        if i % 2 == 0:  # BUG: doubles the wrong parity (rightmost digit instead of every second digit)
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0