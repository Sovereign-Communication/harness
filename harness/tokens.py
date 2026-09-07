"""Token estimation shared by every cost preflight."""


def estimate_prompt_tokens(text):
    """Approximate the prompt's token count without a tokenizer.

    max(words * 1.5, chars / 4): the words heuristic alone badly undercounts
    symbol-dense source code (JSON, Rust generics), where ~4 chars/token
    dominates. Taking the larger of the two keeps preflight ceilings honest
    in the direction of over- rather than under-estimating cost.
    """
    if not text:
        return 50
    return max(int(len(text.split()) * 1.5) + 50, int(len(text) / 4) + 1)
