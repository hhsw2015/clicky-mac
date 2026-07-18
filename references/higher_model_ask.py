#!/usr/bin/env python3
"""proxy-ask — free higher-model access via Proxy's higher-model.

Standalone bridge for ccline. No dependency on the proxy project — just
needs a supabase session (access_token + refresh_token) in the account env
file. Auto-refreshes the access_token when it's within 60s of expiry.

Session file (KEY="value" per line):
    EMAIL="..."
    USER_ID="<supabase uid>"
    ACCESS_TOKEN="<jwt>"
    REFRESH_TOKEN="<opaque>"
    EXPIRES_AT="<unix ts, optional>"

Default path: $PROXY_ACCOUNT or ~/.config/ccline/account.env.

Usage:
    proxy-ask "question"           one-shot
    echo "question" | proxy-ask    stdin
    proxy-ask                      REPL
    proxy-ask --ccline "..."       text-only stdout (for ccline)
    proxy-ask --raw "..."          raw JSON

REPL:
    :q          quit
    :clear      clear screen (worker still remembers account history)
    :raw        toggle raw JSON print
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


# ---------- constants ----------

BUNDLE = "com.example.clicky-mac"
WORKER = "https://proxy.example.com"
SUPABASE_URL = "https://auth.example.com"
# Supabase anon apikey — shipped in the Proxy app binary, not a secret.
SUPABASE_ANON = (
    "PUBLIC_ANON_KEY_PLACEHOLDER"
)

# In this repo the reference script lives at
# docs/mac-port/reference/proxy_ask.py. Prefer the repo root's
# state/account.env; fall back to the standalone ~/dev/ccline install.
_here = Path(__file__).resolve().parent
_repo_state = (_here.parent.parent.parent / "state" / "account.env")
_ccline_state = Path("/Users/wowdd1/dev/ccline/state/account.env")
DEFAULT_ACCOUNT = Path(os.environ.get(
    "PROXY_ACCOUNT",
    str(_repo_state if _repo_state.exists() else _ccline_state),
))
HTTPS_PROXY = os.environ.get("HTTPS_PROXY", "")


# ---------- HTTP ----------

def _curl(method: str, url: str, headers: dict, body=None,
          timeout: int = 30):
    args = ["curl", "-sk", "--max-time", str(timeout), "-X", method]
    for k, v in headers.items():
        args.extend(["-H", f"{k}: {v}"])
    if body is not None:
        args.extend(["-d", body])
    if HTTPS_PROXY:
        args.extend(["-x", HTTPS_PROXY])
    args.extend(["-w", "\n---HTTP:%{http_code}"])
    args.append(url)
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return 0, ""
    out = r.stdout
    m = re.search(r"\n---HTTP:(\d+)$", out)
    if not m:
        return 0, out
    return int(m.group(1)), out[: m.start()]


# ---------- JWT / session ----------

def jwt_claims(tok: str) -> dict:
    try:
        part = tok.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def jwt_valid(tok: str, min_seconds_left: int = 60) -> bool:
    if not tok:
        return False
    return jwt_claims(tok).get("exp", 0) - time.time() >= min_seconds_left


def supabase_refresh(refresh_token: str):
    body = json.dumps({"refresh_token": refresh_token})
    headers = {"apikey": SUPABASE_ANON, "Content-Type": "application/json"}
    status, out = _curl(
        "POST",
        f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
        headers, body, timeout=20,
    )
    if not (200 <= status < 300):
        return None
    try:
        d = json.loads(out)
        if "access_token" in d and "refresh_token" in d:
            return d
    except Exception:
        pass
    return None


class Session:
    __slots__ = ("email", "user_id", "access_token", "refresh_token",
                 "expires_at", "path")

    def __init__(self, path: Path):
        self.path = path
        self.email = ""
        self.user_id = ""
        self.access_token = ""
        self.refresh_token = ""
        self.expires_at = ""
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"')
            if k == "EMAIL": self.email = v
            elif k == "USER_ID": self.user_id = v
            elif k == "ACCESS_TOKEN": self.access_token = v
            elif k == "REFRESH_TOKEN": self.refresh_token = v
            elif k == "EXPIRES_AT": self.expires_at = v

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            f'EMAIL="{self.email}"\n'
            f'USER_ID="{self.user_id}"\n'
            f'ACCESS_TOKEN="{self.access_token}"\n'
            f'REFRESH_TOKEN="{self.refresh_token}"\n'
            f'EXPIRES_AT="{self.expires_at}"\n'
        )
        os.chmod(self.path, 0o600)

    @property
    def valid(self) -> bool:
        return bool(self.user_id and self.access_token and self.refresh_token)

    def ensure_fresh(self) -> bool:
        if jwt_valid(self.access_token, 60):
            return True
        if not self.refresh_token:
            return False
        d = supabase_refresh(self.refresh_token)
        if not d:
            return False
        self.access_token = d["access_token"]
        self.refresh_token = d["refresh_token"]
        self.expires_at = str(d.get("expires_at", ""))
        self.save()
        return True


# ---------- higher-model ----------

def chat_tool_call(s: Session, query: str, image_b64: str = "",
                   mime: str = "image/jpeg", timeout: int = 90):
    body = json.dumps({
        "query": query,
        "mimeType": mime,
        "screenshotBase64": image_b64,
        "client_capabilities": ["clipboard_copy"],
        "frontmost_app_bundle_id": BUNDLE,
        "environment": {
            "os_version": "27.0.0",
            "timezone": "Asia/Shanghai",
            "display_count": 1,
            "device_model": "Mac14,10, Apple M2 Pro (arm64)",
            "locale": "en_US",
            "preferred_languages": ["en-US", "zh-Hans-US"],
        },
    }, ensure_ascii=False)
    headers = {
        "Authorization": f"Bearer {s.access_token}",
        "X-Clicky-Distinct-Id": s.user_id,
        "X-Clicky-Mode": "normal",
        "Content-Type": "application/json",
    }
    status, out = _curl("POST", WORKER + "/higher-model",
                        headers, body, timeout=timeout)
    return (200 <= status < 300), out


# ---------- output ----------

def _one_shot(session: Session, query: str, raw: bool,
              ccline: bool = False, image_b64: str = "",
              mime: str = "image/jpeg") -> int:
    t0 = time.time()
    ok, resp = chat_tool_call(session, query, image_b64, mime)
    elapsed = time.time() - t0
    if raw:
        print(resp)
        return 0 if ok else 1
    if not ok:
        print(f"failed after {elapsed:.1f}s: {resp[:200]}", file=sys.stderr)
        return 1
    try:
        d = json.loads(resp)
    except Exception as e:
        print(f"parse err: {e}\n{resp[:500]}", file=sys.stderr)
        return 1
    print(d.get("text", "") or "")
    if ccline:
        return 0
    if d.get("clipboardText"):
        try:
            subprocess.run(["pbcopy"], input=d["clipboardText"],
                           text=True, check=True)
            print(f"\n[✓ clipboard]: {d['clipboardText'][:80]}",
                  file=sys.stderr)
        except Exception as e:
            print(f"\n[clipboard err]: {e}", file=sys.stderr)
    if d.get("typing"):
        print(f"\n[typing]: {d['typing']}", file=sys.stderr)
    if d.get("point"):
        print(f"\n[point]: {d['point']}", file=sys.stderr)
    if d.get("widgets"):
        print(f"\n[widgets]: {json.dumps(d['widgets'], ensure_ascii=False)[:200]}",
              file=sys.stderr)
    print(f"\n[{elapsed:.1f}s]", file=sys.stderr)
    return 0


def _repl(session: Session, raw: bool = False) -> int:
    try:
        import readline  # noqa: F401
    except Exception:
        pass
    print(f"# proxy-ask REPL ({session.email or session.user_id[:8]})",
          file=sys.stderr)
    print("# :q quit · :raw toggle raw · :clear clear screen",
          file=sys.stderr)
    while True:
        try:
            line = input("\n>>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("", file=sys.stderr)
            return 0
        if not line:
            continue
        if line in (":q", ":quit", ":exit"):
            return 0
        if line == ":raw":
            raw = not raw
            print(f"# raw={raw}", file=sys.stderr)
            continue
        if line == ":clear":
            os.system("clear")
            continue
        if not session.ensure_fresh():
            print("session refresh failed", file=sys.stderr)
            return 1
        _one_shot(session, line, raw)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("query", nargs="*", help="question (empty → REPL)")
    p.add_argument("--raw", action="store_true", help="print raw JSON")
    p.add_argument("--ccline", action="store_true",
                   help="text-only stdout, no markers/trailer")
    p.add_argument("--account", default=str(DEFAULT_ACCOUNT),
                   help=f"account env file (default {DEFAULT_ACCOUNT})")
    p.add_argument("-i", "--image",
                   help="path to image file (jpg/png). Sent as screenshot.")
    p.add_argument("--clipboard-image", action="store_true",
                   help="grab image from macOS pasteboard (Cmd-Ctrl-Shift-4)")
    args = p.parse_args()

    session = Session(Path(args.account))
    if not session.valid:
        print(f"no session in {args.account}", file=sys.stderr)
        print("write EMAIL/USER_ID/ACCESS_TOKEN/REFRESH_TOKEN and retry.",
              file=sys.stderr)
        return 1
    if not session.ensure_fresh():
        print("session refresh failed — refresh_token may be expired.",
              file=sys.stderr)
        return 1

    q = " ".join(args.query).strip()
    if not q and not sys.stdin.isatty():
        q = sys.stdin.read().strip()

    # Load image if requested
    image_b64 = ""
    mime = "image/jpeg"
    if args.image:
        import base64 as _b64
        p_img = Path(args.image).expanduser()
        if not p_img.exists():
            print(f"image not found: {p_img}", file=sys.stderr)
            return 1
        image_b64 = _b64.b64encode(p_img.read_bytes()).decode()
        if p_img.suffix.lower() in (".png",):
            mime = "image/png"
    elif args.clipboard_image:
        # osascript pipeline: pasteboard PNG -> stdout -> base64
        import base64 as _b64
        r = subprocess.run(
            ["osascript", "-e",
             'set thePng to the clipboard as «class PNGf»\n'
             'set theFile to (open for access POSIX file "/tmp/ccline-clip.png" '
             'with write permission)\n'
             'write thePng to theFile\n'
             'close access theFile'],
            capture_output=True, text=True,
        )
        clip_png = Path("/tmp/ccline-clip.png")
        if r.returncode != 0 or not clip_png.exists():
            print("no image on clipboard (copy an image with Cmd-Ctrl-Shift-4 first)",
                  file=sys.stderr)
            return 1
        image_b64 = _b64.b64encode(clip_png.read_bytes()).decode()
        mime = "image/png"

    if q or image_b64:
        return _one_shot(session, q or "describe this image",
                         args.raw, ccline=args.ccline,
                         image_b64=image_b64, mime=mime)
    return _repl(session, args.raw)


if __name__ == "__main__":
    sys.exit(main())
