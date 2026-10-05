#!/usr/bin/env python3
"""Submit one Qwen Image job, poll it, and download its PNG."""
import argparse
import json
from pathlib import Path
import time
from urllib.parse import urljoin
from urllib.request import Request, urlopen


def call(method, url, token, payload=None, timeout=30):
    body = json.dumps(payload).encode() if payload is not None else None
    req = Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1024)
    ap.add_argument("--steps", type=int, default=25)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    token = Path(args.token_file).read_text().strip()
    base = args.base.rstrip("/") + "/"
    submitted = call("POST", urljoin(base, "v1/images/generations"), token, {
        "prompt": args.prompt,
        "width": args.width,
        "height": args.height,
        "steps": args.steps,
        "seed": args.seed,
        "response_format": "url",
    }, timeout=45)
    job_id = submitted.get("id") or submitted.get("job_id")
    if not job_id and isinstance(submitted.get("data"), list) and submitted["data"]:
        watch_url = submitted["data"][0].get("url", "")
        job_id = watch_url.rstrip("/").rsplit("/", 1)[-1]
    if not job_id:
        raise RuntimeError(f"Image API returned no job id: {submitted}")
    for attempt in range(720):
        state = call("GET", urljoin(base, f"jobs/{job_id}"), token, timeout=30)
        status = str(state.get("status", "")).lower()
        print(f"PROGRESS {attempt + 1}/720", flush=True)
        if status in {"failed", "error", "cancelled"}:
            raise RuntimeError(f"Image generation failed: {state}")
        if status in {"completed", "complete", "succeeded", "success", "done"}:
            output_url = state.get("output_url")
            if not output_url and isinstance(state.get("data"), list) and state["data"]:
                output_url = state["data"][0].get("url")
            if not output_url:
                output_url = f"/files/{job_id}.png"
            file_url = urljoin(base, output_url.lstrip("/"))
            req = Request(file_url)
            req.add_header("Authorization", f"Bearer {token}")
            with urlopen(req, timeout=120) as response:
                image = response.read()
            if not image.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError(f"Image API returned a non-PNG artifact ({len(image)} bytes)")
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(image)
            print(f"WROTE {out} ({len(image)} bytes)", flush=True)
            return
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for image job {job_id}")


if __name__ == "__main__":
    main()
