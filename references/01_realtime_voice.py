"""V1 pure socket, hand-rolled WebSocket frame parser.
Realtime is TEXT frames only. We only need masked send + unmasked read.
"""
import base64
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
RECORD_SECONDS = 5
SR = 24000

PROXY = ""  # transparent proxy in place

# Some transparent proxies mis-route *.example.com to CN IPs and TLS breaks.
# We pin real hosted IPs so curl bypasses the poisoned DNS.


def _jwt_exp(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p)).get("exp", 0)
    except Exception:
        return 0


def refresh_token():
    """Return (access_token, user_id). Refresh only if access token has <60s left."""
    env = open(ACCOUNT_ENV).read()
    tok = env.split('ACCESS_TOKEN="')[1].split('"')[0]
    uid = env.split('USER_ID="')[1].split('"')[0]
    email = env.split('EMAIL="')[1].split('"')[0]
    rt = env.split('REFRESH_TOKEN="')[1].split('"')[0]

    remaining = _jwt_exp(tok) - time.time()
    if remaining > 60:
        print(f"   token still valid for {int(remaining)}s — no refresh needed")
        return tok, uid

    print(f"   token expires in {int(remaining)}s — refreshing")
    curl = ["curl", "-sk", "-X", "POST", "--http1.1"]
    curl += ["-H", "apikey: PUBLIC_ANON_KEY_PLACEHOLDER",
             "-H", "Content-Type: application/json",
             "-d", json.dumps({"refresh_token": rt}),
             "https://auth.example.com/auth/v1/token?grant_type=refresh_token"]
    r = subprocess.run(curl, capture_output=True, text=True, timeout=15)
    if not r.stdout.strip():
        raise RuntimeError("refresh returned empty; refresh_token likely burned. "
                           "Delete state/account.env in ccline and re-authenticate.")
    d = json.loads(r.stdout)
    if "access_token" not in d:
        raise RuntimeError(f"refresh failed: {r.stdout[:300]}")
    new_env = (f'EMAIL="{email}"\nUSER_ID="{uid}"\n'
               f'ACCESS_TOKEN="{d["access_token"]}"\n'
               f'REFRESH_TOKEN="{d["refresh_token"]}"\n'
               f'EXPIRES_AT="{d.get("expires_at","")}"\n')
    open(ACCOUNT_ENV, "w").write(new_env)
    os.chmod(ACCOUNT_ENV, 0o600)
    return d["access_token"], uid


def fetch_ephemeral(tok, uid):
    curl = ["curl", "-sk", "-X", "POST", "-w", "\n---HTTP:%{http_code}",
            "--connect-timeout", "10", "--http1.1"]
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
    print(f"   HTTP {code} body[:200]={body[:200]!r}")
    if not body.strip():
        raise RuntimeError(f"empty body from worker (HTTP {code})")
    d = json.loads(body)
    if "value" not in d:
        raise RuntimeError(f"ek fetch failed: {body[:300]}")
    return d["value"], d["session"]["model"]


def _socks5_connect(host, port, timeout=15):
    """Open a TCP tunnel to host:port through the local socks5 proxy."""
    import struct as _s
    s = socket.create_connection(("127.0.0.1", 10808), timeout=timeout)
    # greeting: version 5, 1 auth method, no auth
    s.sendall(b"\x05\x01\x00")
    r = s.recv(2)
    if r != b"\x05\x00":
        raise RuntimeError(f"socks5 greeting failed: {r!r}")
    # connect: cmd=1, addr type=3 (domain), domain, port
    host_b = host.encode()
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + _s.pack(">H", port))
    r = s.recv(10)
    if len(r) < 2 or r[1] != 0:
        raise RuntimeError(f"socks5 connect failed: {r!r}")
    return s


class WSClient:
    def __init__(self, ek, model):
        if PROXY.startswith("socks5://"):
            sock = _socks5_connect("api.openai.com", 443, timeout=15)
        else:
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
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("closed during handshake")
            buf += chunk
        hdr, _, extra = buf.partition(b"\r\n\r\n")
        status = hdr.split(b"\r\n", 1)[0].decode()
        if "101" not in status:
            raise RuntimeError(f"handshake failed: {status}")
        self.recv_buf = bytearray(extra)
        self.sock.settimeout(60)

    def _recv_exact(self, n):
        while len(self.recv_buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("connection closed")
            self.recv_buf.extend(chunk)
        out = bytes(self.recv_buf[:n])
        del self.recv_buf[:n]
        return out

    def recv_text(self):
        """Read one text frame, handle continuations, ignore pings."""
        while True:
            b1, b2 = self._recv_exact(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            mask = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            if mask:
                raise RuntimeError("server MUST NOT mask")
            payload = self._recv_exact(length) if length else b""
            if opcode == 0x9:  # ping → pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:  # close
                return None
            if opcode == 0x1:  # text
                data = payload
                while not fin:
                    b1, b2 = self._recv_exact(2)
                    fin = b1 & 0x80
                    op2 = b1 & 0x0F
                    ln2 = b2 & 0x7F
                    if ln2 == 126:
                        ln2 = struct.unpack(">H", self._recv_exact(2))[0]
                    elif ln2 == 127:
                        ln2 = struct.unpack(">Q", self._recv_exact(8))[0]
                    data += self._recv_exact(ln2) if ln2 else b""
                return data.decode("utf-8")

    def _send_frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        mask_key = secrets.token_bytes(4)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask_key
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, s):
        self._send_frame(0x1, s.encode("utf-8"))

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        self.sock.close()


def record_mic(seconds, gain=10.0):
    print(f"🎤 recording {seconds}s in 3s… 3")
    time.sleep(1); print("   2")
    time.sleep(1); print("   1  ►►►  SPEAK LOUDLY NOW  ◄◄◄")
    time.sleep(0.5)
    audio = sd.rec(int(seconds * SR), samplerate=SR, channels=1, dtype="int16")
    sd.wait()
    rms_raw = float(np.sqrt(np.mean(audio.astype(np.float32)**2)))
    # Apply digital gain — clip to int16 range
    audio_f = audio.astype(np.float32) * gain
    audio_amp = np.clip(audio_f, -32768, 32767).astype(np.int16)
    rms_amp = float(np.sqrt(np.mean(audio_amp.astype(np.float32)**2)))
    print(f"   recorded {audio.shape}  raw_rms={rms_raw:.0f}  after {gain}× gain={rms_amp:.0f}")
    return audio_amp.tobytes()


def main():
    print("=" * 60)
    print("V1: Proxy → OpenAI Realtime voice roundtrip")
    print("=" * 60)

    print("\n[1] refresh supabase token if needed")
    tok, uid = refresh_token()

    print("\n[2] fetch ek_ from Proxy worker")
    t0 = time.time()
    ek, model = fetch_ephemeral(tok, uid)
    print(f"   ek={ek[:15]}... model={model} ({time.time()-t0:.1f}s)")

    print("\n[3] WebSocket handshake")
    t0 = time.time()
    ws = WSClient(ek, model)
    print(f"   ✓ connected ({time.time()-t0:.2f}s)")

    # Get first server event (session.created OR error)
    hello = json.loads(ws.recv_text())
    print(f"   ← {json.dumps(hello, ensure_ascii=False)[:500]}")
    if hello.get("type") == "error":
        return 1

    # Disable server VAD so it doesn't cancel our response mid-generation.
    # We handle turn boundaries manually.
    ws.send_text(json.dumps({
        "type": "session.update",
        "session": {
            "type": "realtime",
            "audio": {
                "input": {"turn_detection": None},
            },
        },
    }))
    upd = json.loads(ws.recv_text())
    print(f"   ← {upd.get('type')} (VAD off)")

    print(f"\n[4] recording {RECORD_SECONDS}s")
    pcm = record_mic(RECORD_SECONDS)

    print(f"\n[5] sending {len(pcm)} bytes PCM")
    CHUNK = SR * 2
    for i in range(0, len(pcm), CHUNK):
        ws.send_text(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm[i:i+CHUNK]).decode(),
        }))
    ws.send_text(json.dumps({"type": "input_audio_buffer.commit"}))
    ws.send_text(json.dumps({"type": "response.create"}))
    print("   ✓ committed")

    print("\n[6] streaming reply — LOGGING ALL EVENTS")
    reply = bytearray()
    first_ms = None
    t_start = time.time()
    heard = said = None
    while True:
        raw = ws.recv_text()
        if raw is None:
            print("   ✗ server closed")
            break
        ev = json.loads(raw)
        t = ev.get("type", "")
        if t == "response.output_audio.delta":
            if first_ms is None:
                first_ms = (time.time() - t_start) * 1000
                print(f"   ⚡ first audio byte @ {first_ms:.0f}ms")
            reply.extend(base64.b64decode(ev["delta"]))
        elif t == "response.output_audio_transcript.delta":
            pass
        else:
            trimmed = {k:v for k,v in ev.items() if k not in ("type","event_id")}
            print(f"   → {t}  {json.dumps(trimmed, ensure_ascii=False)[:300]}")

        if t == "response.output_audio_transcript.done":
            said = ev.get("transcript", "")
        elif t == "conversation.item.input_audio_transcription.completed":
            heard = ev.get("transcript", "")
        elif t == "response.done":
            break
        elif t == "error":
            print(f"   ✗ ERROR: {ev.get('error', ev)}")
            ws.close()
            return 1
        if time.time() - t_start > 45:
            print("   ⚠ 45s timeout")
            break
    print(f"\n   heard: {heard}")
    print(f"   said: {said}")

    ws.close()

    if not reply:
        print("   ✗ no reply audio")
        return 1
    print(f"   ✓ {len(reply)} bytes ({len(reply)/48000:.1f}s of audio)")

    print("\n[7] playing reply")
    sd.play(np.frombuffer(bytes(reply), dtype=np.int16), samplerate=SR)
    sd.wait()

    print("\n" + "=" * 60)
    print(f"V1: ✅ PASS  first-byte={first_ms:.0f}ms")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
