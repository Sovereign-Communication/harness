"""Hermetic subprocess fixture for the served UI's real HTTP routes."""
import os
import sys


def main():
    port = int(sys.argv[1])
    config_dir = sys.argv[2]
    os.environ["HARNESS_CONFIG_DIR"] = config_dir
    for key in (
        "OPENROUTER_API_KEY",
        "JEV_API_KEY",
        "FIREWORKS_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        os.environ.pop(key, None)

    from harness import cli
    from harness import server
    from harness.config import resolve_api_key

    if resolve_api_key():
        raise RuntimeError("offline UI smoke fixture requires an isolated provider config")

    class OfflineAgent:
        def __init__(self, settings=None, root_dir=None):
            self.transport = None

        def run_prompt(self, prompt, **_kwargs):
            return {
                "status": "ok",
                "intent": "conversation",
                "prompt": prompt,
                "response": "E2E offline response: " + prompt,
                "model": "fixture/offline",
                "cost": 0.0,
            }

    class OfflineDriver:
        def health(self):
            return {"ok": False, "status": "down", "error": "offline fixture"}

        def schemas(self):
            return {"schemas": []}

        def vocabulary(self):
            return {"vocabulary": {"actions": []}}

        def verify(self):
            return {"audit": {"ok": True}}

    server.AutonomousAgent = OfflineAgent
    server._driver_adapter = lambda autostart=True: OfflineDriver()
    cli.main([
        "serve", "--host", "127.0.0.1", "--port", str(port),
        "--auth-token", "test-session-token",
    ])


if __name__ == "__main__":
    main()
