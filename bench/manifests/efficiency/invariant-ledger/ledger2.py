class Ledger:
    def __init__(self):
        self.posted = []

    def post(self, entry):
        """entry: {"lines": [{"account": str, "debit": num, "credit": num}, ...]}"""
        self.posted.append(entry)
        return len(self.posted)
