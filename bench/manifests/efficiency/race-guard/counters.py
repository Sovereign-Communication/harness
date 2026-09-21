class Counter:
    def __init__(self):
        self.value = 0

    def bump(self, n=1):
        current = self.value
        current += n
        self.value = current
        return current
