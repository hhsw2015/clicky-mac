"""V4 feasibility: bridge send_to_higher_model tool_call to higher-model.

Flow:
  1. Same as V1: get ek_ + WebSocket to realtime
  2. Record 5s mic (a "complex" question that forces realtime to defer)
  3. Wait for response.function_call_arguments.done for send_to_higher_model
  4. Take screenshot, call higher-model with query + screenshot
  5. Parse higher-model JSON, build a minimal tool_result
  6. Send conversation.item.create (function_call_output) + response.create
  7. Wait for final voice reply, play it

Success = the second response has audio, and its transcript reflects the
higher-model answer content.
"""
import base64
import io
import json
import os
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import time

import numpy as np
import sounddevice as sd

# Prefer clicky/state/account.env in the current repo; fall back to
# ~/dev/ccline/state/account.env so scripts still work standalone.
import os as _os
_here = _os.path.dirname(_os.path.abspath(__file__))
_repo_state = _os.path.abspath(_os.path.join(_here, "..", "..", "..", "state", "account.env"))
_ccline_state = "/Users/wowdd1/dev/ccline/state/account.env"
ACCOUNT_ENV = _repo_state if _os.path.exists(_repo_state) else _ccline_state
WORKER = "https://proxy.example.com"
RECORD_SECONDS = 6
SR = 24000
PROXY = ""


# ---------- session file ----------

def _jwt_exp(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p)).get("exp", 0)
    except Exception:
        return 0


def refresh_token():
    env = open(ACCOUNT_ENV).read()
    tok = env.split('ACCESS_TOKEN="')[1].split('"')[0]
    uid = env.split('USER_ID="')[1].split('"')[0]
    email = env.split('EMAIL="')[1].split('"')[0]
    rt = env.split('REFRESH_TOKEN="')[1].split('"')[0]
    if _jwt_exp(tok) - time.time() > 60:
        return tok, uid
    curl = ["curl", "-sk", "-X", "POST", "--http1.1"]
    curl += ["-H", "apikey: PUBLIC_ANON_KEY_PLACEHOLDER",
             "-H", "Content-Type: application/json",
             "-d", json.dumps({"refresh_token": rt}),
             "https://auth.example.com/auth/v1/token?grant_type=refresh_token"]
    r = subprocess.run(curl, capture_output=True, text=True, timeout=15)
    d = json.loads(r.stdout)
    new_env = (f'EMAIL="{email}"\nUSER_ID="{uid}"\n'
               f'ACCESS_TOKEN="{d["access_token"]}"\n'
               f'REFRESH_TOKEN="{d["refresh_token"]}"\n'
               f'EXPIRES_AT="{d.get("expires_at","")}"\n')
    open(ACCOUNT_ENV, "w").write(new_env)
    os.chmod(ACCOUNT_ENV, 0o600)
    return d["access_token"], uid


# ---------- worker calls ----------

def fetch_ephemeral(tok, uid):
    curl = ["curl", "-sk", "-X", "POST", "--http1.1", "--connect-timeout", "10",
            "-w", "\n---HTTP:%{http_code}"]
    curl += ["-H", f"Authorization: Bearer {tok}",
             "-H", f"X-Clicky-Distinct-Id: {uid}",
             "-H", "X-Clicky-Mode: normal",
             "-H", "Content-Type: application/json",
             "-d", "{}", f"{WORKER}/agent/realtime/session"]
    r = subprocess.run(curl, capture_output=True, text=True, timeout=30)
    out = r.stdout
    if "---HTTP:" in out:
        body, _, code = out.rpartition("\n---HTTP:")
    else:
        body, code = out, "?"
    print(f"   ek fetch HTTP {code} body[:200]={body[:200]!r}")
    if not body.strip():
        raise RuntimeError(f"empty (HTTP {code}) — check proxy/node")
    d = json.loads(body)
    return d["value"], d["session"]["model"]


def call_higher_model(tok, uid, query, image_b64=""):
    """POST /higher-model — this is the "higher model" higher-model lane."""
    body = {
        "query": query,
        "mimeType": "image/jpeg",
        "screenshotBase64": image_b64,
        "client_capabilities": ["clipboard_copy"],
        "frontmost_app_bundle_id": "com.example.clicky-mac",
        "environment": {
            "os_version": "27.0.0", "timezone": "Asia/Shanghai",
            "display_count": 1,
            "device_model": "Mac14,10, Apple M2 Pro (arm64)",
            "locale": "en_US",
            "preferred_languages": ["en-US", "zh-Hans-US"],
        },
    }
    body_path = "/tmp/v4-body.json"
    with open(body_path, "w") as f:
        json.dump(body, f, ensure_ascii=False)
    curl = ["curl", "-sk", "-X", "POST", "--http1.1", "--connect-timeout", "15"]
    curl += ["-H", f"Authorization: Bearer {tok}",
             "-H", f"X-Clicky-Distinct-Id: {uid}",
             "-H", "X-Clicky-Mode: normal",
             "-H", "Content-Type: application/json",
             "-d", f"@{body_path}",
             f"{WORKER}/higher-model"]
    r = subprocess.run(curl, capture_output=True, text=True, timeout=120)
    try:
        return json.loads(r.stdout)
    except Exception:
        print(f"   parse fail, stdout[:400]={r.stdout[:400]!r}")
        print(f"   stderr[:200]={r.stderr[:200]!r}")
        return {"text": "", "_error": r.stdout[:200]}


def take_screenshot_b64():
    """macOS `screencapture -x -t jpg`, downscale via `sips` to ~1024w JPEG."""
    raw = "/tmp/v4-frame.jpg"
    subprocess.run(["screencapture", "-x", "-t", "jpg", raw], check=True, timeout=5)
    # sips: scale to 1024 wide (quality drops naturally)
    subprocess.run(["sips", "-Z", "1024", "-s", "formatOptions", "70", raw],
                   check=True, timeout=5, capture_output=True)
    data = open(raw, "rb").read()
    return base64.b64encode(data).decode(), len(data)


# ---------- WebSocket ----------

class WSClient:
    def __init__(self, ek, model):
        sock = socket.create_connection(("api.openai.com", 443), timeout=15)
        ctx = ssl.create_default_context()
        ctx.set_alpn_protocols(["http/1.1"])
        self.sock = ctx.wrap_socket(sock, server_hostname="api.openai.com")
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        req = (
            f"GET /v1/realtime?model={model} HTTP/1.1\r\n"
            "Host: api.openai.com\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Authorization: Bearer {ek}\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode())
        self.sock.settimeout(15)
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        hdr, _, extra = buf.partition(b"\r\n\r\n")
        if b"101" not in hdr.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"handshake failed: {hdr[:200]}")
        self.recv_buf = bytearray(extra)
        self.sock.settimeout(120)

    def _recv_exact(self, n):
        while len(self.recv_buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("closed")
            self.recv_buf.extend(chunk)
        out = bytes(self.recv_buf[:n]); del self.recv_buf[:n]; return out

    def recv_text(self):
        while True:
            b1, b2 = self._recv_exact(2)
            fin = b1 & 0x80; op = b1 & 0x0F; ln = b2 & 0x7F
            if ln == 126: ln = struct.unpack(">H", self._recv_exact(2))[0]
            elif ln == 127: ln = struct.unpack(">Q", self._recv_exact(8))[0]
            data = self._recv_exact(ln) if ln else b""
            if op == 0x9: self._send(0xA, data); continue
            if op == 0x8: return None
            if op == 0x1:
                while not fin:
                    b1, b2 = self._recv_exact(2)
                    fin = b1 & 0x80; op2 = b1 & 0x0F; ln2 = b2 & 0x7F
                    if ln2 == 126: ln2 = struct.unpack(">H", self._recv_exact(2))[0]
                    elif ln2 == 127: ln2 = struct.unpack(">Q", self._recv_exact(8))[0]
                    data += self._recv_exact(ln2) if ln2 else b""
                return data.decode("utf-8")

    def _send(self, op, payload):
        h = bytearray([0x80 | op])
        mk = secrets.token_bytes(4); n = len(payload)
        if n < 126: h.append(0x80 | n)
        elif n < 65536: h.append(0x80 | 126); h += struct.pack(">H", n)
        else: h.append(0x80 | 127); h += struct.pack(">Q", n)
        h += mk
        self.sock.sendall(bytes(h) + bytes(b ^ mk[i%4] for i,b in enumerate(payload)))

    def send_text(self, s): self._send(0x1, s.encode("utf-8"))

    def close(self):
        try: self._send(0x8, b"")
        except Exception: pass
        self.sock.close()


# ---------- audio ----------

def record_mic(seconds, gain=10.0):
    print(f"🎤 recording {seconds}s in 3s… 3")
    time.sleep(1); print("   2")
    time.sleep(1); print("   1  ►►►  SPEAK NOW  (要触发 higher model — 例如'看看这个屏幕上有什么')  ◄◄◄")
    time.sleep(0.5)
    a = sd.rec(int(seconds*SR), samplerate=SR, channels=1, dtype="int16")
    sd.wait()
    rms = float(np.sqrt(np.mean(a.astype(np.float32)**2)))
    a = np.clip(a.astype(np.float32) * gain, -32768, 32767).astype(np.int16)
    return a.tobytes(), rms


# ---------- main ----------

def main():
    print("=" * 60)
    print("V4: send_to_higher_model tool bridge test")
    print("=" * 60)

    print("\n[1] refresh + fetch ek")
    tok, uid = refresh_token()
    ek, model = fetch_ephemeral(tok, uid)
    print(f"   ek={ek[:15]}... model={model}")

    print("\n[2] connect realtime")
    ws = WSClient(ek, model)
    hello = json.loads(ws.recv_text())
    print(f"   ← {hello.get('type')}")
    ws.send_text(json.dumps({
        "type": "session.update",
        "session": {"type": "realtime",
                    "audio": {"input": {"turn_detection": None}}}
    }))
    print(f"   ← {json.loads(ws.recv_text()).get('type')}")

    print(f"\n[3] record {RECORD_SECONDS}s (ask something complex to force tool call)")
    pcm, raw_rms = record_mic(RECORD_SECONDS)
    print(f"   raw_rms={raw_rms:.0f}")

    print("\n[4] commit + response.create")
    CHUNK = SR * 2
    for i in range(0, len(pcm), CHUNK):
        ws.send_text(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm[i:i+CHUNK]).decode(),
        }))
    ws.send_text(json.dumps({"type": "input_audio_buffer.commit"}))
    ws.send_text(json.dumps({"type": "response.create"}))

    print("\n[5] wait for tool_call OR direct audio reply")
    tool_call = None  # {call_id, args}
    heard = None
    initial_audio = bytearray()
    initial_transcript = None
    t0 = time.time()

    while True:
        raw = ws.recv_text()
        if raw is None:
            print("   ✗ server closed"); return 1
        ev = json.loads(raw)
        t = ev.get("type", "")

        if t == "conversation.item.input_audio_transcription.completed":
            heard = ev.get("transcript", "")
            print(f"   👂 heard: {heard}")
        elif t == "response.output_audio.delta":
            initial_audio.extend(base64.b64decode(ev["delta"]))
        elif t == "response.output_audio_transcript.done":
            initial_transcript = ev.get("transcript", "")
            print(f"   💬 initial (before tool): {initial_transcript}")
        elif t == "response.function_call_arguments.done":
            if ev.get("name") == "send_to_higher_model":
                tool_call = {
                    "call_id": ev.get("call_id"),
                    "args": json.loads(ev.get("arguments", "{}")),
                }
                print(f"   🔧 tool_call: send_to_higher_model({tool_call['args']})")
        elif t == "response.done":
            break
        elif t == "error":
            print(f"   ✗ {ev.get('error')}"); ws.close(); return 1
        if time.time() - t0 > 45:
            print("   ⚠ timeout"); break

    if not tool_call:
        print("\n   ⚠ no tool_call happened. Realtime answered directly.")
        print(f"   → played initial audio ({len(initial_audio)} bytes)")
        if initial_audio:
            sd.play(np.frombuffer(bytes(initial_audio), dtype=np.int16), samplerate=SR)
            sd.wait()
        ws.close()
        print("\nV4: ⚠ NO TOOL CALL — try a more complex question next time")
        return 2

    # Play the "hold-on" message realtime says before tool
    if initial_audio:
        print(f"\n[6a] play initial 'hold on' audio ({len(initial_audio)} bytes)")
        sd.play(np.frombuffer(bytes(initial_audio), dtype=np.int16), samplerate=SR)
        sd.wait()

    # Take screenshot for the higher model
    print("\n[6b] screencapture for higher_model context")
    img_b64, img_size = take_screenshot_b64()
    print(f"   {img_size} bytes PNG")

    # Call higher-model
    print("\n[7] call higher-model (higher-model)")
    query = tool_call["args"].get("query", "")
    t_h = time.time()
    higher = call_higher_model(tok, uid, query, img_b64)
    print(f"   ({time.time()-t_h:.1f}s)  text={higher.get('text','')[:200]!r}")

    # Build tool_result. Realtime wants function_call_output with `output` string.
    # We'll pass the JSON of the higher model as-is; realtime will decide what to say.
    result_str = json.dumps({
        "text": higher.get("text", ""),
        "clipboardText": higher.get("clipboardText"),
        "typing": higher.get("typing"),
        "point": higher.get("point"),
        "widgets": higher.get("widgets", []),
        # Mirror the fields the tool description mentions:
        "visual_guidance_shown": False,
        "visual_count": 0,
        "target_armed": False,
        "cursor_animated": False,
        "text_typed": bool(higher.get("typing")),
        "copied_to_clipboard": bool(higher.get("clipboardText")),
    }, ensure_ascii=False)

    print("\n[8] send tool_result back to realtime")
    ws.send_text(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": tool_call["call_id"],
            "output": result_str,
        },
    }))
    ws.send_text(json.dumps({"type": "response.create"}))

    print("\n[9] wait for final voice reply")
    final_audio = bytearray()
    final_transcript = None
    first_ms = None
    t_start = time.time()
    while True:
        raw = ws.recv_text()
        if raw is None: break
        ev = json.loads(raw)
        t = ev.get("type", "")
        if t == "response.output_audio.delta":
            if first_ms is None:
                first_ms = (time.time() - t_start) * 1000
                print(f"   ⚡ first byte @ {first_ms:.0f}ms")
            final_audio.extend(base64.b64decode(ev["delta"]))
        elif t == "response.output_audio_transcript.done":
            final_transcript = ev.get("transcript", "")
            print(f"   💬 final: {final_transcript}")
        elif t == "response.done":
            break
        elif t == "error":
            print(f"   ✗ {ev.get('error')}"); ws.close(); return 1
        if time.time() - t_start > 30:
            print("   ⚠ timeout"); break

    ws.close()

    if not final_audio:
        print("\nV4: ❌ no final audio")
        return 1

    print(f"\n[10] play final ({len(final_audio)} bytes)")
    sd.play(np.frombuffer(bytes(final_audio), dtype=np.int16), samplerate=SR)
    sd.wait()

    print("\n" + "=" * 60)
    print("V4: ✅ PASS — tool bridge full round-trip works")
    print(f"   heard: {heard}")
    print(f"   tool query: {tool_call['args'].get('query')}")
    print(f"   higher_model text: {higher.get('text','')[:100]}")
    print(f"   final transcript: {final_transcript}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
