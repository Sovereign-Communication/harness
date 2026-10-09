"""One-request child used by cancellable :class:`HttpTransport` calls.

The parent owns retries and accounting. This process only owns one urllib
socket, returning the raw HTTP body and retry-after header over stdout.
Credentials and the request body arrive on stdin, never in process arguments.
"""
import json
import base64
import sys
import urllib.error
import urllib.request


def main():
    request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    method = request.get("method", "POST")
    data = request.get("data_b64")
    data = base64.b64decode(data) if data is not None else None
    req = urllib.request.Request(
        request["url"],
        data=data,
        headers=request.get("headers", {}),
        method=method,
    )
    try:
        try:
            opener = (urllib.request.build_opener(_NoRedirect)
                      if request.get("no_redirect") else urllib.request.build_opener())
            response = opener.open(req, timeout=request["timeout"])
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            max_bytes = request.get("max_bytes")
            body = response.read(max_bytes) if max_bytes else response.read()
            result = {
                "status": response.getcode(),
                "body_b64": base64.b64encode(body).decode("ascii"),
                "retry_after": response.headers.get("Retry-After")
                if response.headers else None,
                "final_url": response.geturl(),
            }
    except BaseException as exc:
        result = {"exception": {"type": type(exc).__name__,
                                "message": str(exc)}}
    sys.stdout.write(json.dumps(result))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


if __name__ == "__main__":
    main()

