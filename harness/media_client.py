"""harness.media_client — adapter to the sovereign-media service.

Thin, zero-dependency client (stdlib only) so harness (and anything embedding
harness) can generate images/videos through the cost-bounded media service
without ever holding provider credentials. Budgets, sign-in state, and
hash-chained spend ledger all live service-side; harness only ever receives
job envelopes (status, cost, artifact paths).

Honest-failure contract (mirrors HARNESS_DEFER): if the service is down,
unsigned-in, or refuses on budget, the caller gets a refusal envelope with
the reason — never a silent empty result.

Usage (Python):
    from harness.media_client import MediaAdapter
    media = MediaAdapter()
    result = media.image("a cabin in snowy woods", project="scmessenger")
    result["status"]        # "succeeded" / "failed" / "refused" / "service_unavailable"
    result["artifacts"]     # local artifact paths (same machine) or URLs
    result["cost"]          # actual USD spent, when settled

Usage (CLI, wired into harness.cli as `harness media ...`):
    harness media image "a cabin in snowy woods" --project scmessenger
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

# No provider/service brand strings are hardcoded here: the endpoint the
# adapter talks to is always resolved from config or environment, in this
# order: explicit constructor args > config file > env vars > loopback
# default. The default is a *local* fallback (not a brand), matching the
# `media serve` quickstart in docs/media.md.
DEFAULT_BASE = os.environ.get("MEDIA_BASE_URL", "http://127.0.0.1:8765")
CONFIG_PATH = os.environ.get(
    "MEDIA_CONFIG_PATH",
    os.path.join(os.path.expanduser("~"), ".config", "harness", "media.json"),
)


def _load_endpoint():
    env_token = os.environ.get("MEDIA_TOKEN")
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            return cfg.get("base_url", DEFAULT_BASE), cfg.get("token") or env_token
        except (OSError, ValueError):
            pass
    return DEFAULT_BASE, env_token


class MediaUnavailable(Exception):
    """The media service could not be reached or refused the request."""


class MediaAdapter:
    def __init__(self, base_url=None, token=None, timeout=60, opener=None):
        base, env_token = _load_endpoint()
        self.base = (base_url or base).rstrip("/")
        self.token = token or env_token
        self.timeout = timeout
        # Injectable transport seam for hermetic tests: defaults to the real
        # urllib opener, but tests pass a fake so nothing touches the network.
        self._opener = opener or urllib.request.urlopen

    # ---- transport ----

    def _request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Media-Token"] = self.token
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=headers)
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                try:
                    err = json.loads(e.read().decode("utf-8"))
                except Exception:
                    err = {"error": "HTTP {}".format(e.code)}
            finally:
                e.close()
            if err.get("error") == "budget_refused":
                return {"status": "refused", "error": err.get("message"),
                        "math": err.get("math")}
            raise MediaUnavailable(str(err.get("error") or err)) from e
        except (urllib.error.URLError, OSError) as e:
            raise MediaUnavailable(
                "media service unreachable at {} ({}). Start it with: media serve".format(self.base, e)
            ) from e

    # ---- core API ----

    def image(self, prompt, project="default", wait=True, timeout=600.0, **params):
        return self._generate("image", prompt, project, wait, timeout, params)

    def video(self, prompt, project="default", wait=True, timeout=1200.0, **params):
        return self._generate("video", prompt, project, wait, timeout, params)

    def _generate(self, kind, prompt, project, wait, timeout, params):
        job = self._request("POST", "/v1/jobs", {
            "kind": kind, "prompt": prompt, "project": project, "params": params,
        })
        if job.get("status") == "refused":
            return job  # budget refusal envelope with math
        if not wait:
            return job
        return self.wait(job["id"], timeout=timeout)

    def wait(self, job_id, timeout=600.0, interval=2.0):
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self._request("GET", "/v1/jobs/{}".format(job_id))
            if job.get("status") in ("succeeded", "failed", "refused"):
                return self._envelope(job)
            time.sleep(interval)
        return {"status": "timeout", "job_id": job_id}

    def job(self, job_id):
        return self._envelope(self._request("GET", "/v1/jobs/{}".format(job_id)))

    def jobs(self, project=None, limit=20):
        q = "/v1/jobs?limit={}".format(int(limit))
        if project:
            q += "&project={}".format(urllib.parse.quote(project))
        return [self._envelope(j) for j in self._request("GET", q).get("jobs", [])]

    def balance(self, project=None):
        path = "/v1/balance" + ("?project={}".format(urllib.parse.quote(project)) if project else "")
        return self._request("GET", path)

    @staticmethod
    def _envelope(job):
        return {
            "job_id": job.get("id"),
            "status": job.get("status"),
            "kind": job.get("kind"),
            "provider": job.get("provider"),
            "model": job.get("model"),
            "cost_estimate": job.get("cost_estimate"),
            "cost": job.get("cost_actual"),
            "artifacts": job.get("artifact_paths") or [],
            "error": job.get("error"),
            "project": job.get("project"),
        }


# ---- CLI handler (wired as `harness media ...`) ----

def run_cli(args, settings=None):
    """harness media {image|video|job|jobs|balance} — returns exit code."""
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="harness media")
    sub = ap.add_subparsers(dest="media_cmd", required=True)

    # --provider/--model are free strings: the provider catalog (openai,
    # google, higgsfield, fal, replicate, luma, ...) lives service-side in
    # sovereign-media, not hardcoded here — harness never brand-ladders
    # media providers in code.
    p = sub.add_parser("image")
    p.add_argument("prompt")
    p.add_argument("--project", default="default")
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--size", default=None)
    p.add_argument("--quality", default=None, choices=["low", "medium", "high"])
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--timeout", type=float, default=600)

    p = sub.add_parser("video")
    p.add_argument("prompt")
    p.add_argument("--project", default="default")
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--seconds", type=int, default=None)
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--timeout", type=float, default=1200)

    p = sub.add_parser("job")
    p.add_argument("job_id")

    p = sub.add_parser("jobs")
    p.add_argument("--project", default=None)
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("balance")
    p.add_argument("--project", default=None)

    opts = ap.parse_args(args)
    adapter = MediaAdapter()

    def show(env):
        print("job {}  [{}]  {}/{}  project={}".format(
            env.get("job_id"), env.get("status"), env.get("provider"),
            env.get("model"), env.get("project")))
        est = env.get("cost_estimate") or {}
        if est:
            print("  estimate: ${:.4f} - ${:.4f}".format(est.get("low", 0), est.get("high", 0)))
        if env.get("cost") is not None:
            print("  cost:     ${:.4f}".format(env["cost"]))
        for a in env.get("artifacts") or []:
            print("  artifact: {}".format(a))
        if env.get("error"):
            print("  error:    {}".format(env["error"]))
        if env.get("math"):
            print("  math:     {}".format(json.dumps(env["math"], sort_keys=True)))

    try:
        if opts.media_cmd == "image":
            params = {k: v for k, v in (("size", opts.size), ("quality", opts.quality)) if v}
            env = adapter.image(opts.prompt, project=opts.project, wait=not opts.no_wait,
                                timeout=opts.timeout, provider=opts.provider,
                                model=opts.model, **params)
            show(env)
            return 0 if env.get("status") == "succeeded" else 1
        if opts.media_cmd == "video":
            params = {"seconds": opts.seconds} if opts.seconds else {}
            env = adapter.video(opts.prompt, project=opts.project, wait=not opts.no_wait,
                                timeout=opts.timeout, provider=opts.provider,
                                model=opts.model, **params)
            show(env)
            return 0 if env.get("status") == "succeeded" else 1
        if opts.media_cmd == "job":
            show(adapter.job(opts.job_id))
            return 0
        if opts.media_cmd == "jobs":
            for env in adapter.jobs(project=opts.project, limit=opts.limit):
                show(env)
            return 0
        if opts.media_cmd == "balance":
            print(json.dumps(adapter.balance(opts.project), indent=2))
            return 0
    except MediaUnavailable as e:
        # defer-style honest failure: state the reason, spend nothing
        print("[defer] media: {}".format(e), file=sys.stderr)
        return 4
    return 2
