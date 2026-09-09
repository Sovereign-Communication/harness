# Local model-fit advisory layer (opt-in).

try:
    from . import model  # noqa: F401  (historical; unused, kept for forward compat)
    from . import extract
    from . import advisory
    from . import hook
    from . import dispatch_hook
except Exception as e:  # pragma: no cover - gated import failures are informational
    pass
