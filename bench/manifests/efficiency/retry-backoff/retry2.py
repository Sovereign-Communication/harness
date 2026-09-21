import time


def retry_call(fn, *, retries=5, base_delay=0.01, max_delay=0.2, sleep=time.sleep):
    return fn()
