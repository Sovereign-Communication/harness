"""Per-repo work queue and the laptop watcher that drains it.

The vision driver lane needs one durable place where work waits for a machine
instead of for a chat turn, and one auditable answer to "what happened to that
item". This module is that place. It owns exactly one thing: the item state
machine

    queued -> claimed -> running -> checkpointed -> done | blocked | parked

plus the auditable record of every move. It does not launch anything, does not
decide whether work is good, and does not spend: the watcher *claims* an item
and hands it to a launcher the caller supplies. Finishing is the lane's act,
after it has its own evidence, which is why the watcher never writes ``done``.

Layout (per repo, so one laptop can serve several repositories without the
items colliding)::

    <root>/<repo>/items/<item-id>.json        one item record
    <root>/<repo>/events.jsonl                append-only transition log
    <root>/<repo>/checkpoints/<item-id>.json  the last checkpointed payload
    <root>/<repo>/.lock                       mutation lock

Design rules this module enforces rather than documents:

* ``done`` is final. A finished item cannot be re-claimed, so a resume can
  never replay a completed pass.
* ``blocked`` and ``parked`` are holds, not verdicts. Only an operator requeue
  returns them to ``queued``, and that move is recorded with the operator's
  name.
* Ownership binds every move after the claim. A second watcher cannot advance
  an item another actor claimed, which is what makes "claimed" mean anything.
* The watcher is dormant on an empty queue. It does not create work, and it
  does not emit an event per idle poll: an empty queue is a quiet queue.
* Launches stay supervised. The watcher refuses to claim while
  ``authorized`` is false, because the owner's ruling (D3) keeps launches
  supervised until the calibration corpus is frozen and the TM proof is
  complete. Authorization is per run, never a sticky setting.
"""
import json
import os
import re
import time
from dataclasses import dataclass, field

from .errors import HarnessError
from .osal import (
    atomic_write_text, acquire_file_lock, release_file_lock,
)

QUEUED = "queued"
CLAIMED = "claimed"
RUNNING = "running"
CHECKPOINTED = "checkpointed"
DONE = "done"
BLOCKED = "blocked"
PARKED = "parked"

#: Every state an item can be in, in the order one successful pass visits them.
STATES = (QUEUED, CLAIMED, RUNNING, CHECKPOINTED, DONE, BLOCKED, PARKED)

#: The legal moves. Anything not listed here is refused by name, so a caller
#: cannot skip a stage and then claim the item was verified at that stage.
TRANSITIONS = {
    QUEUED: (CLAIMED, PARKED),
    CLAIMED: (RUNNING, BLOCKED, PARKED),
    RUNNING: (CHECKPOINTED, BLOCKED, PARKED),
    CHECKPOINTED: (RUNNING, DONE, BLOCKED, PARKED),
    DONE: (),
    BLOCKED: (QUEUED,),
    PARKED: (QUEUED,),
}

#: A finished item has no exits at all; this is the anti-replay rule.
FINAL = frozenset((DONE,))

#: States an operator (not the watcher) may return to the queue.
HOLDS = frozenset((BLOCKED, PARKED))

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class QueueError(HarnessError):
    """A queue request that cannot be honoured, with the reason attached."""


def _checked_id(value, what):
    """Reject anything that is not a plain identifier in one path segment."""
    text = str(value or "").strip()
    if not _ID.match(text) or text in (".", ".."):
        raise QueueError(
            f"{what} must be a plain identifier (letters, digits, '.', '_', "
            f"'-'), got {value!r}")
    return text


@dataclass
class QueueItem:
    """One unit of work and the state machine position it is in."""

    id: str
    repo: str
    summary: str
    state: str = QUEUED
    owner: str = ""
    attempts: int = 0
    created_at: str = ""
    updated_at: str = ""
    reason: str = ""
    checkpointed_at: str = ""
    payload: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "id": self.id,
            "repo": self.repo,
            "summary": self.summary,
            "state": self.state,
            "owner": self.owner,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reason": self.reason,
            "checkpointed_at": self.checkpointed_at,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data):
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items()
                      if key in known})


@dataclass
class WatchOutcome:
    """What one watcher step did, in a form a caller can branch on."""

    action: str
    repo: str = ""
    item_id: str = ""
    detail: str = ""


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class QueueBus:
    """The durable per-repo queue: items, transitions, and their history.

    ``root`` is the queue's home; every repo gets its own folder under it. All
    mutations take the repo's own lock, so two processes cannot interleave a
    read-modify-write. A held lock is refused with its name rather than waited
    on, because a watcher that blocks silently is indistinguishable from a
    watcher that died.
    """

    def __init__(self, root):
        self.root = os.path.abspath(str(root))

    # ---------------------------------------------------------------- paths

    def _repo_dir(self, repo):
        return os.path.join(self.root, _checked_id(repo, "repo"))

    def _items_dir(self, repo):
        return os.path.join(self._repo_dir(repo), "items")

    def _item_path(self, repo, item_id):
        return os.path.join(self._items_dir(repo),
                            _checked_id(item_id, "item id") + ".json")

    def _events_path(self, repo):
        return os.path.join(self._repo_dir(repo), "events.jsonl")

    def _checkpoint_path(self, repo, item_id):
        return os.path.join(self._repo_dir(repo), "checkpoints",
                            _checked_id(item_id, "item id") + ".json")

    # ---------------------------------------------------------------- reads

    def repos(self):
        """Repo slugs present under the root, in sorted order."""
        if not os.path.isdir(self.root):
            return []
        return sorted(
            name for name in os.listdir(self.root)
            if os.path.isdir(os.path.join(self.root, name))
            and _ID.match(name))

    def item(self, repo, item_id):
        """Read one item, or refuse with the path that was checked."""
        path = self._item_path(repo, item_id)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError as exc:
            raise QueueError(f"no queue item {item_id!r} in repo {repo!r} "
                             f"({path})") from exc
        except (OSError, ValueError) as exc:
            raise QueueError(
                f"queue item {item_id!r} is unreadable: {exc}") from exc
        item = QueueItem.from_dict(data)
        if item.state not in STATES:
            raise QueueError(
                f"queue item {item_id!r} holds unknown state {item.state!r}")
        return item

    def items(self, repo, state=None):
        """Every item in a repo, oldest first, optionally filtered by state."""
        folder = self._items_dir(repo)
        if not os.path.isdir(folder):
            return []
        found = []
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".json"):
                continue
            found.append(self.item(repo, name[:-5]))
        if state is not None:
            found = [item for item in found if item.state == state]
        return sorted(found, key=lambda item: (item.created_at, item.id))

    def snapshot(self, repo):
        """Per-state counts; the one read a status surface needs."""
        counts = {state: 0 for state in STATES}
        for item in self.items(repo):
            counts[item.state] += 1
        return {"repo": repo, "root": self._repo_dir(repo),
                "total": sum(counts.values()), "states": counts}

    def history(self, repo, item_id=None):
        """The append-only transition log, optionally for one item."""
        path = self._events_path(repo)
        if not os.path.isfile(path):
            return []
        events = []
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError as exc:
                    raise QueueError(
                        f"{path}:{number} is not a queue event: {exc}") from exc
                if item_id is None or event.get("item") == item_id:
                    events.append(event)
        return events

    # ------------------------------------------------------------ mutations

    def enqueue(self, repo, item_id, summary, *, creator="lane", payload=None):
        """Create an item in ``queued``; an existing item is never replaced."""
        _checked_id(item_id, "item id")
        stamp = _now()
        item = QueueItem(
            id=_checked_id(item_id, "item id"), repo=_checked_id(repo, "repo"),
            summary=str(summary or "").strip(), state=QUEUED,
            created_at=stamp, updated_at=stamp, payload=dict(payload or {}))
        if not item.summary:
            raise QueueError("a queue item needs a summary; an empty item is "
                             "not a unit of work")
        with self._locked(repo):
            if os.path.exists(self._item_path(repo, item.id)):
                raise QueueError(
                    f"queue item {item.id!r} already exists in repo {repo!r}; "
                    f"requeue it instead of overwriting its history")
            self._write_item(item)
            self._append_event(repo, item, None, QUEUED, creator,
                               "enqueued")
        return item

    def claim(self, repo, item_id, owner):
        """Take an item out of the queue for one owner."""
        return self._transition(repo, item_id, CLAIMED, owner,
                                expect=QUEUED, count_attempt=True)

    def start(self, repo, item_id, owner):
        """Mark the claimed item as actually running under its owner."""
        return self._transition(repo, item_id, RUNNING, owner, expect=CLAIMED)

    def checkpoint(self, repo, item_id, owner, payload):
        """Persist a resume point and move to ``checkpointed``.

        The payload lands in its own file *before* the transition, so a crash
        can lose the state change but never the resume point.
        """
        payload = dict(payload or {})
        if not payload:
            raise QueueError("a checkpoint with no payload is not a resume "
                             "point")
        path = self._checkpoint_path(repo, item_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True)
                          + "\n")
        item = self._transition(repo, item_id, CHECKPOINTED, owner,
                                expect=RUNNING)
        with self._locked(repo):
            item = self.item(repo, item_id)
            item.checkpointed_at = _now()
            item.updated_at = item.checkpointed_at
            self._write_item(item)
        return item

    def resume(self, repo, item_id, owner):
        """Re-open a checkpointed item for another pass under its owner."""
        return self._transition(repo, item_id, RUNNING, owner,
                                expect=CHECKPOINTED)

    def finish(self, repo, item_id, owner, outcome, reason=""):
        """Close an item as ``done``, ``blocked`` or ``parked``.

        ``done`` requires a checkpoint, because a pass that never wrote a
        resume point has no evidence it got anywhere. ``blocked`` and
        ``parked`` are allowed from claimed, running or checkpointed: a launch
        can fail before it ever reaches running, and losing that finding to a
        state check would be the same as hiding it. Only ``done`` is final;
        the holds stay requeueable, which is why the caller names the outcome
        instead of passing a boolean.
        """
        if outcome == DONE:
            allowed_from = (CHECKPOINTED,)
        elif outcome in (BLOCKED, PARKED):
            allowed_from = (CLAIMED, RUNNING, CHECKPOINTED)
        else:
            raise QueueError(f"finish outcome must be one of "
                             f"{[DONE, BLOCKED, PARKED]}, got {outcome!r}")
        return self._transition(repo, item_id, outcome, owner, reason=reason,
                                allowed_from=allowed_from)

    def requeue(self, repo, item_id, operator, reason=""):
        """Operator-only return of a held item to ``queued``."""
        return self._transition(repo, item_id, QUEUED, operator,
                                reason=reason, expect=None,
                                allowed_from=HOLDS, clear_owner=True)

    # ------------------------------------------------------------- internals

    def _transition(self, repo, item_id, to_state, actor, *, reason="",
                    expect=None, allowed_from=None, count_attempt=False,
                    clear_owner=False):
        actor = str(actor or "").strip()
        if not actor:
            raise QueueError("every queue transition names its actor; an "
                             "anonymous move has no one to ask")
        with self._locked(repo):
            item = self.item(repo, item_id)
            current = item.state
            legal_from = allowed_from if allowed_from is not None else (
                (expect,) if expect else TRANSITIONS.keys())
            if current not in legal_from:
                raise QueueError(
                    f"queue item {item_id!r} is {current!r}, not "
                    f"{' or '.join(sorted(legal_from))}")
            if to_state not in TRANSITIONS.get(current, ()):
                raise QueueError(
                    f"{current!r} -> {to_state!r} is not a legal queue move "
                    f"for {item_id!r}; legal from {current!r}: "
                    f"{list(TRANSITIONS.get(current, ()))}")
            if current in FINAL:
                raise QueueError(f"queue item {item_id!r} is {DONE!r}; a "
                                 f"finished item is never replayed")
            if not clear_owner and item.owner and actor != item.owner:
                raise QueueError(
                    f"queue item {item_id!r} is owned by {item.owner!r}; "
                    f"{actor!r} cannot move it")
            if to_state == CLAIMED and item.owner and actor != item.owner:
                raise QueueError(
                    f"queue item {item_id!r} is already claimed by "
                    f"{item.owner!r}")
            stamp = _now()
            item.state = to_state
            item.updated_at = stamp
            item.reason = str(reason or "")
            if to_state == CLAIMED:
                item.owner = actor
                if count_attempt:
                    item.attempts += 1
            if clear_owner:
                item.owner = ""
            self._write_item(item)
            self._append_event(repo, item, current, to_state, actor,
                               item.reason)
            return item

    def _write_item(self, item):
        path = self._item_path(item.repo, item.id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic_write_text(path, json.dumps(item.to_dict(), indent=2,
                                           sort_keys=True) + "\n")

    def _append_event(self, repo, item, from_state, to_state, actor, reason):
        path = self._events_path(repo)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        event = {
            "at": _now(),
            "repo": item.repo,
            "item": item.id,
            "from": from_state,
            "to": to_state,
            "actor": actor,
            "reason": str(reason or ""),
            "attempt": item.attempts,
        }
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    class _Lock:
        def __init__(self, bus, repo):
            self.bus = bus
            self.repo = repo
            self.fd = None

        def __enter__(self):
            directory = self.bus._repo_dir(self.repo)
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, ".lock")
            try:
                self.fd = acquire_file_lock(path)
            except OSError as exc:
                raise QueueError(
                    f"queue repo {self.repo!r} is busy ({exc}); another "
                    f"process holds its lock") from exc
            return self

        def __exit__(self, *exc_info):
            if self.fd is not None:
                release_file_lock(self.fd)
                self.fd = None
            return False

    def _locked(self, repo):
        return QueueBus._Lock(self, _checked_id(repo, "repo"))


class Watcher:
    """The laptop watcher: claim one queued item, hand it to a launcher.

    The watcher is deliberately dumb. It scans repos in name order, takes the
    oldest queued item, and stops after one item per step so a stuck launch
    cannot swallow the rest of the queue. It never writes ``done`` and never
    retries a failed launch: a launcher that raises moves the item to
    ``blocked`` with the error text, which is the only outcome a machine can
    honestly record on its own.
    """

    DORMANT = "dormant"
    SUPERVISED_HOLD = "supervised_hold"
    CLAIMED = "claimed"
    #: A claim that tried to launch and was refused. The item's own state is
    #: already ``blocked``; naming the outcome separately is what keeps a
    #: script from reading a refused launch as a started one.
    BLOCKED = "blocked"

    def __init__(self, bus, launcher=None, *, interval=15.0):
        self.bus = bus
        self.launcher = launcher
        self.interval = float(interval)
        self.owner = "laptop-watcher"

    def step(self, *, authorized=False):
        """One pass: dormant, held, or a claimed item handed to the launcher."""
        if not authorized:
            pending = [(repo, item) for repo in self.bus.repos()
                       for item in self.bus.items(repo, QUEUED)]
            if not pending:
                return WatchOutcome(self.DORMANT,
                                    detail="queue is empty")
            return WatchOutcome(
                self.SUPERVISED_HOLD, repo=pending[0][0],
                item_id=pending[0][1].id,
                detail=("launches stay supervised until the calibration "
                        "corpus is frozen and the TM proof is complete; pass "
                        "authorized=True for a supervised launch"))
        for repo in self.bus.repos():
            queued = self.bus.items(repo, QUEUED)
            if not queued:
                continue
            item = queued[0]
            try:
                claimed = self.bus.claim(repo, item.id, self.owner)
                self.bus.start(repo, claimed.id, self.owner)
            except QueueError as exc:
                return WatchOutcome(self.DORMANT, repo=repo, item_id=item.id,
                                    detail=f"claim refused: {exc}")
            if self.launcher is None:
                return WatchOutcome(self.CLAIMED, repo=repo,
                                    item_id=item.id,
                                    detail="claimed; no launcher configured, "
                                           "so nothing was started")
            try:
                detail = str(self.launcher(self.bus.item(repo, item.id))
                             or "launched")
            except Exception as exc:  # noqa: BLE001 - any launch failure blocks
                self.bus.finish(repo, item.id, self.owner, BLOCKED,
                                reason=f"launch failed: {exc}")
                return WatchOutcome(self.BLOCKED, repo=repo, item_id=item.id,
                                    detail=f"launch failed: {exc}")
            return WatchOutcome(self.CLAIMED, repo=repo, item_id=item.id,
                                detail=detail)
        return WatchOutcome(self.DORMANT, detail="queue is empty")

    def run(self, *, steps=1, authorized=False, sleep=None):
        """Bounded loop. ``steps`` is required to be explicit: no open-ended
        loop runs in a test or a script that forgot it."""
        if int(steps) < 1:
            raise QueueError("the watcher needs a positive step count")
        pause = sleep if sleep is not None else time.sleep
        outcomes = []
        for _ in range(int(steps)):
            outcome = self.step(authorized=authorized)
            outcomes.append(outcome)
            if outcome.action == self.DORMANT:
                pause(self.interval)
        return outcomes


def launch_item(settings=None, environ=None):
    """The launcher ``harness queue watch`` supplies: the real driver, gated.

    An item asks to start a program by carrying ``{"launch_app": <name>}`` in
    its payload. Nothing else about the item is interpreted here: the queue
    owns the state machine, the driver owns what may be launched and whether
    the gate is open.

    The driver's gates apply unchanged and in its own order -- write authority
    for a mutating action, the launch switch (``DRIVER_LAUNCH_APP``, which the
    host maps from ``HARNESS_NL_CONTROL``), the code-owned allowlist, and
    per-action consent. A closed gate is a refusal from the driver rather than
    a quiet no-op, so the watcher records the item ``blocked`` in the gate's
    own words instead of reporting success for something it did not do.

    ``driver_core`` is imported inside the launcher, not here: the queue stays
    startable on a machine with no provider key, and the driver loads only
    when an item actually asks for a launch.
    """
    def launch(item):
        payload = item.payload or {}
        app_name = payload.get("launch_app")
        if not app_name:
            return "no launch requested; claimed for the lane"
        from driver_core.config import load_settings
        from driver_core.driver import driver_from_settings
        from driver_core.executor import Consent
        from .server import nl_control_enabled

        resolved = load_settings() if settings is None else settings
        resolved = resolved.with_overrides(
            launch_app_enabled=(resolved.launch_app_enabled
                                or nl_control_enabled(environ)))
        driver = driver_from_settings(resolved)
        params = {"app_name": str(app_name)}
        result = driver.executor.execute(
            "launch_app", params, consent=Consent(True, "launch_app", params))
        # A refusal arrives as a result, not an exception. Reporting it as a
        # launch is the one thing this launcher must never do: the watcher
        # would record a started program that never started.
        if not getattr(result, "ok", False):
            detail = getattr(result, "detail", "") or str(result)
            raise QueueError(f"launch refused for {app_name!r}: {detail}")
        return f"launched {app_name}: {getattr(result, 'detail', '') or 'started'}"

    return launch


def run_cli(args):
    """``harness queue {status|enqueue|watch}`` -- exit code, no provider key.

    The queue is local state, so this face is deliberately intercepted before
    settings load: a watcher on a laptop must be startable while the machine
    has no provider credentials. Exit codes mirror the driver adapter: 0 for a
    completed read or a claimed item, 1 for a refusal or error, 3 for a
    supervised hold (work exists but no authorization was given), which lets a
    shell tell "nothing to do" from "waiting for its operator".
    """
    import argparse
    import json as _json
    import sys

    ap = argparse.ArgumentParser(prog="harness queue")
    sub = ap.add_subparsers(dest="queue_cmd", required=True)

    pstatus = sub.add_parser("status", help="per-repo item counts")
    pstatus.add_argument("--root", required=True, help="queue root folder")
    pstatus.add_argument("--repo", default=None,
                         help="one repo slug (default: every repo present)")
    pstatus.add_argument("--json", action="store_true")

    penq = sub.add_parser("enqueue", help="add one item in `queued`")
    penq.add_argument("--root", required=True)
    penq.add_argument("--repo", required=True)
    penq.add_argument("--id", required=True)
    penq.add_argument("--summary", required=True)
    penq.add_argument("--payload", default=None,
                      help="JSON object kept with the item")

    pwatch = sub.add_parser("watch", help="one supervised watcher pass")
    pwatch.add_argument("--root", required=True)
    pwatch.add_argument("--interval", type=float, default=15.0,
                        help="seconds to pause after an empty pass")
    pwatch.add_argument("--steps", type=int, default=1,
                        help="bounded number of passes (default: 1)")
    pwatch.add_argument("--authorized", action="store_true",
                        help="the operator is present for this launch")

    opts = ap.parse_args(args)
    bus = QueueBus(opts.root)
    try:
        if opts.queue_cmd == "status":
            repos = [opts.repo] if opts.repo else bus.repos()
            payload = [bus.snapshot(repo) for repo in repos]
            if opts.json:
                print(_json.dumps(payload, indent=2, sort_keys=True))
            elif not payload:
                print(f"queue {bus.root}: empty (no repos)")
            else:
                for entry in payload:
                    counts = ", ".join(
                        f"{state}={count}" for state, count
                        in entry["states"].items() if count)
                    print(f"{entry['repo']}: {entry['total']} items"
                          f" ({counts or 'none'})")
            return 0
        if opts.queue_cmd == "enqueue":
            payload = None
            if opts.payload:
                payload = _json.loads(opts.payload)
                if not isinstance(payload, dict):
                    raise QueueError("--payload must be a JSON object")
            item = bus.enqueue(opts.repo, opts.id, opts.summary,
                               creator="operator", payload=payload)
            print(f"enqueued {item.repo}/{item.id} as {item.state}")
            return 0
        watcher = Watcher(bus, launcher=launch_item(), interval=opts.interval)
        outcomes = watcher.run(steps=opts.steps,
                               authorized=opts.authorized)
        for outcome in outcomes:
            print(f"{outcome.action}: {outcome.detail}")
        if any(outcome.action == Watcher.BLOCKED for outcome in outcomes):
            # A refused launch is an error, not a completed claim: the item is
            # `blocked` in the queue and the process that asked for it must
            # not read exit 0 as "something started".
            return 1
        if any(outcome.action == Watcher.SUPERVISED_HOLD
               for outcome in outcomes):
            return 3
        return 0
    except QueueError as exc:
        print(f"[FATAL] queue: {exc}", file=sys.stderr)
        return 1
