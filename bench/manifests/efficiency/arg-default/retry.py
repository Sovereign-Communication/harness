def fetch_retries(key, seen=[]):
    seen.append(key)
    return len(seen)
