"""HTTP for the remote-service packs' clients (film/music/qwen_remote_client.py).

Not a pack: the loader skips module names that start with "_".

One Service object per configured base address. Every request it makes:
  - goes ONLY to that base's scheme + host + port; a URL the service hands
    back that points anywhere else is refused, so the token never leaves;
  - never follows a redirect (a 3xx is refused, token or not);
  - never goes through a proxy from the environment;
  - reads at most MAX_JSON bytes of JSON, and a download at most MAX_DOWNLOAD;
  - puts caller values into the path only as quoted path segments.
A plain http:// base is refused unless it is this machine (loopback) or
BWF_REMOTE_ALLOW_HTTP=1 is set, because the token would cross the network
in the clear.

Stdlib only, Python 3.8+.
"""
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_DOWNLOAD = 4 * 1024 ** 3      # 4 GiB: far above any clip, song or picture
MAX_JSON = 8 * 1024 ** 2          # 8 MiB of JSON per reply
ERROR_CHARS = 300                 # how much of a service's error text is kept
ALLOW_HTTP_ENV = "BWF_REMOTE_ALLOW_HTTP"
NO_TOKEN = "none"                 # a token-file setting that means "send no token"


class RemoteError(Exception):
    """A plain sentence saying what went wrong; never retried."""


class _Transient(RemoteError):
    """A failure a later poll may not repeat (network, timeout, HTTP 5xx)."""


def short(text):
    """A service's error text, trimmed to ERROR_CHARS on one line."""
    text = " ".join(str(text).split())
    return text if len(text) <= ERROR_CHARS else text[:ERROR_CHARS - 3] + "..."


def _origin(url):
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme)
    return scheme, (parts.hostname or "").lower(), port


def _is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_base(base, setting="the service address"):
    """Refuse a base address that is not http(s), or plain http off this machine."""
    origin = _origin(base or "")
    if not origin or origin[0] not in ("http", "https") or not origin[1]:
        raise RemoteError("%s (%s) is not an http:// or https:// address." % (setting, base))
    if (origin[0] == "http" and not _is_loopback(origin[1])
            and os.environ.get(ALLOW_HTTP_ENV) != "1"):
        raise RemoteError(
            "%s (%s) is plain http:// to another machine, which would send the token "
            "unencrypted; use https://, or set %s=1 if that network is private."
            % (setting, base, ALLOW_HTTP_ENV))
    return base


def read_token(path, setting, allow_none=False):
    """The token in `path`, or None when allow_none and the setting says "none".

    A missing, unreadable or empty file is an error naming the setting, so a
    typo in the path never turns into a silent unauthenticated request.
    """
    if allow_none and str(path).strip().lower() == NO_TOKEN:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError:
        hint = (" or to %s if the service takes no token" % NO_TOKEN) if allow_none else ""
        raise RemoteError("The token file %s set by %s cannot be read; set %s to the file "
                          "that holds the service's token%s." % (path, setting, setting, hint))
    if not token:
        raise RemoteError("The token file %s set by %s is empty." % (path, setting))
    return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RemoteError("The service answered with a redirect (HTTP %d) to %s, which is "
                          "not followed." % (code, short(newurl)))


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _error_text(exc):
    """The `error` field of an HTTP error body, else its first ERROR_CHARS characters."""
    try:
        body = exc.read(64 * 1024).decode("utf-8", "replace")
    except Exception:
        body = ""
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        if isinstance(err, dict):
            err = err.get("message") or err
        return short(err if isinstance(err, str) else json.dumps(err))
    return short(body) or (exc.reason if isinstance(exc.reason, str) else "")


class Service:
    """One remote service: its base address, its token and a name for sentences."""

    def __init__(self, base, token, name, setting="the service address"):
        check_base(base, setting)
        self.base = base.rstrip("/") + "/"
        self.origin = _origin(self.base)
        self.token = token
        self.name = name

    def url(self, *segments):
        """base + each segment quoted as one path segment ("/" included)."""
        return self.base + "/".join(urllib.parse.quote(str(s), safe="") for s in segments)

    def resolve(self, ref):
        """A URL the service sent back: relative joins the base; absolute must be ours."""
        ref = str(ref or "").strip()
        parts = urllib.parse.urlsplit(ref)
        if not parts.scheme and not parts.netloc:
            url = self.base + ref.lstrip("/")
        else:
            url = urllib.parse.urljoin(self.base, ref)
        self._same_origin(url)
        return url

    def _same_origin(self, url):
        if _origin(url) != self.origin:
            raise RemoteError("%s pointed at another address (%s); only %s is used."
                              % (self.name, short(url), self.base))

    def _open(self, url, payload=None, timeout=60):
        self._same_origin(url)
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            return _OPENER.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            text = _error_text(exc)
            exc.close()
            cls = _Transient if exc.code >= 500 else RemoteError
            raise cls("%s answered HTTP %d%s" % (self.name, exc.code,
                                                 (": " + text) if text else "."))
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            raise _Transient("%s at %s could not be reached (%s)."
                             % (self.name, self.base, short(reason)))

    def json(self, url, payload=None, timeout=60):
        """GET (or POST `payload`) and return the parsed JSON object."""
        with self._open(url, payload, timeout) as response:
            try:
                body = response.read(MAX_JSON + 1)
            except (socket.timeout, TimeoutError, OSError) as exc:
                raise _Transient("%s stopped answering (%s)." % (self.name, short(exc)))
        if len(body) > MAX_JSON:
            raise RemoteError("%s sent a reply larger than %d MiB; refused."
                              % (self.name, MAX_JSON // (1024 * 1024)))
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError:
            raise RemoteError("%s sent a reply that is not JSON." % self.name)
        if not isinstance(data, dict):
            raise RemoteError("%s sent a JSON reply that is not an object." % self.name)
        return data

    def alive(self, timeout=5):
        """One short GET /health; a sentence on failure."""
        try:
            with self._open(self.url("health"), timeout=timeout) as response:
                response.read(MAX_JSON)
        except RemoteError as exc:
            raise RemoteError("%s is not answering at %s; start it and try again (%s)"
                              % (self.name, self.base, str(exc).rstrip(".")))

    def download(self, ref, output, timeout=300, limit=MAX_DOWNLOAD):
        """Save the file at `ref` (relative or on our base) to `output`, atomically."""
        url = self.resolve(ref)
        part = output + ".part"
        too_big = "%s sent a file larger than %d MiB; refused." % (self.name, limit // (1024 * 1024))
        try:
            with self._open(url, timeout=timeout) as response:
                length = response.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > limit:
                    raise RemoteError(too_big)
                got = 0
                with open(part, "wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        got += len(chunk)
                        if got > limit:
                            raise RemoteError(too_big)
                        handle.write(chunk)
            if got == 0:
                raise RemoteError("%s sent an empty file." % self.name)
            os.replace(part, output)
        finally:
            if os.path.exists(part):
                os.remove(part)

    def poll(self, url, finished, deadline, interval=5, max_failures=6, on_tick=None):
        """GET `url` until finished(state) returns something other than None.

        `deadline` is a time.monotonic() value; past it the wait ends with a
        sentence. More than `max_failures` transient failures IN A ROW also end
        it. finished() may raise RemoteError for a job the service failed.
        """
        failures = 0
        tick = 0
        while True:
            if time.monotonic() > deadline:
                raise RemoteError("%s did not finish the job in time." % self.name)
            try:
                state = self.json(url, timeout=30)
                failures = 0
            except _Transient:
                failures += 1
                if failures > max_failures:
                    raise
                time.sleep(interval)
                continue
            tick += 1
            if on_tick:
                on_tick(tick)
            result = finished(state)
            if result is not None:
                return result
            time.sleep(interval)


def job_failed(state, default):
    """A RemoteError for a job whose status says it failed, with its own reason."""
    reason = state.get("error")
    if isinstance(reason, dict):
        reason = reason.get("message") or json.dumps(reason)
    return RemoteError(short(reason) if reason else default)


FAILED = ("failed", "error", "cancelled", "canceled")
DONE = ("complete", "completed", "done", "succeeded", "success")


def main_guard(main):
    """Run a client's main(); a failure prints one `ERROR: <sentence>` line and exits 1."""
    import sys
    try:
        sys.exit(main())
    except RemoteError as exc:
        print("ERROR: %s" % exc, flush=True)
    except Exception as exc:  # an unexpected bug: say so, without a traceback dump
        print("ERROR: the client stopped unexpectedly (%s: %s)." % (type(exc).__name__, short(exc)),
              flush=True)
    sys.exit(1)
