DEFAULT_RATE = 0.05


def interest(principal, years, rate=DEFAULT_RATE):
    return principal * rate * years


def compound(principal, years, rate=DEFUALT_RATE):
    return principal * (1 + rate) ** years
