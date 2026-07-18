# clicky-mac — Build Spec

Single-source spec for turning this fork into a working macOS voice
companion. This document is the source of truth: goal, boundaries,
requirements, protocol, ports, day-by-day plan, tests, and debug.

---

## 1. Goal

A native macOS menu-bar voice assistant that:
- Listens to the user via push-to-talk or toggle-to-talk
- Streams audio in/out through **OpenAI Realtime API** using an
  **ephemeral session token** obtained from a **user-configured proxy**
- When the model needs deeper reasoning or screen understanding, it
  emits a `send_to_higher_model` tool call; the client screenshots the
  active display and forwards the query to another proxy path
- Overlays a blue buddy cursor next to the real cursor that reacts,
  points at UI elements, and speaks

## 2. In scope / out of scope

**In scope (MUST for MVP)**
- R1. Push-to-talk voice input via a global `CGEvent` tap hotkey
- R2. Realtime WebSocket to `wss://api.openai.com/v1/realtime` using
  ephemeral bearer token fetched from a proxy path
- R3. PCM16 24 kHz mic capture, sent as base64 in
  `input_audio_buffer.append` frames
- R4. Streaming audio out via `AVAudioPlayerNode`, decoded from
  `response.output_audio.delta` events
- R5. `send_to_higher_model` tool bridge: screenshot → proxy POST →
  build `function_call_output` → `response.create`
- R6. Overlay cursor: idle triangle, listening waveform, thinking
  spinner, speaking triangle, pointing bezier arc animation to
  `[POINT:x,y:label:screenN]` coordinates emitted by the model
- R7. Session credentials (access + refresh token) persisted in
  Keychain; access token auto-refreshed when < 60s remaining
- R8. Ephemeral session refresh loop (every 8 min while active) with
  seamless WebSocket reconnect
- R9. All URLs, paths, and public keys read from `Config.plist`
  (gitignored); nothing sensitive in source

**Out of scope (MUST NOT for MVP)**
- Multi-provider STT (AssemblyAI/OpenAI Whisper/Apple Speech from
  upstream) — Realtime API handles STT server-side
- ElevenLabs TTS — Realtime handles TTS
- ClaudeAPI direct SSE — replaced by proxy POST
- PostHog / Mux / FormSpark integrations — removed for privacy
- Sparkle auto-update — defer to v2 (needs signing infra)
- On-device Ollama fallback — no local model path
- Notch UI (needs macOS private APIs)

**Deferred to v2**
- Skills system (markdown-driven persona modes) — fine as-is from
  dabit3 style if desired, but not required for MVP
- Multi-account manager
- Reset daemon integration (see companion Python daemon at
  `~/dev/heyclicky/heyclicky.py`)
- Overlay onboarding video, welcome bubble

## 3. Stop conditions (MVP is done when)

- S1. Hold hotkey, speak a simple sentence, hear a spoken reply within
  3 seconds of releasing (Realtime direct-response path)
- S2. Ask "what's on my screen" — model emits `send_to_higher_model`,
  client sends screenshot to proxy, reply is spoken back
- S3. Say "point at the button that says 'Save'" — cursor flies to the
  correct location via `[POINT:...]` protocol
- S4. Run continuously for 30 minutes across at least one ephemeral
  refresh boundary (8 min) without crash or audible glitch
- S5. First launch on a clean machine: system prompts for Mic, Screen
  Recording, Accessibility exactly once each; app functions afterward

## 4. Boundary conditions

- B1. Access token has < 60s left → refresh via proxy auth endpoint
  before any request; on refresh failure with 400/401, clear
  credentials and route to sign-in screen (no crash)
- B2. Ephemeral session expires mid-turn → detect via WS close/401,
  fetch fresh ephemeral, reconnect within 2 s; if reconnect fails,
  surface `TransportLost` state to overlay
- B3. Refresh token burned (returns empty body) → hold on to current
  access token until natural expiry, then require sign-in. Never crash
- B4. Network drops entirely → show offline state on overlay; user
  taps to retry (no automatic exponential retry storms)
- B5. Higher-model POST returns non-2xx or times out (>90s) → build
  a synthetic tool result `{"text":"","error":"upstream_unavailable"}`
  and still send it back to Realtime so the turn completes
- B6. Server VAD false-triggers mid-response (barge-in) → prefer
  disabling server VAD via `session.update` and driving turns
  manually with `input_audio_buffer.commit` + `response.create`
- B7. Concurrent refresh attempts → single `actor` owns the
  `SessionCredentialStore`; parallel callers await the same in-flight
  refresh Task
- B8. macOS TCC permission denied for mic → show settings deep-link;
  disable hotkey until granted

## 5. Architecture

```
                 ┌──────────────────────────────────────────────┐
                 │  clicky-mac (this app, on the user's Mac)   │
                 │                                              │
   Push-to-talk  │  ┌──────────────┐    ┌───────────────────┐  │
   hotkey        │  │ Mic (24kHz)  │    │ Speaker (24kHz)   │  │
   ⌃⌥ held ──────┼─▶│ AVAudioEngine│    │ AVAudioPlayerNode │  │
                 │  └──────┬───────┘    └────────▲──────────┘  │
                 │         │                     │             │
                 │  ┌──────▼─────────────────────┴─────────┐   │
                 │  │  RealtimeAudioTransport              │   │
                 │  │  (URLSessionWebSocketTask)           │   │
                 │  │  session events → EventRouter        │   │
                 │  └───┬──────────────┬─────────────┬─────┘   │
                 │      │              │             │         │
                 │      │ tool_call    │ audio deltas│         │
                 │      ▼              │             ▼         │
                 │  ┌─────────────────────┐  ┌───────────────┐│
                 │  │ ToolBridge          │  │ Overlay UI    ││
                 │  │  send_to_higher     │  │  buddy cursor ││
                 │  │  + open_url + …     │  │  [POINT] anim ││
                 │  └────────┬────────────┘  └───────────────┘│
                 │           │                                 │
                 │  ┌────────▼─────────────────────────┐      │
                 │  │  ProxyClient (HTTPS POST)         │      │
                 │  │  bearer = access_token            │      │
                 │  └────────┬─────────────────────────┘      │
                 │           │                                 │
                 │  ┌────────▼──────────┐                     │
                 │  │ CredentialStore   │  Keychain           │
                 │  │ Authenticator     │  auto-refresh       │
                 │  └───────────────────┘                     │
                 └──────────┬───────────────────────────────┬─┘
                            │                               │
              ┌─────────────▼─────────┐         ┌───────────▼──────────┐
              │  Realtime endpoint    │         │  User-configured     │
              │  wss://api.openai.com │         │  Proxy service       │
              │    /v1/realtime       │         │                      │
              │  (accepts ephemeral   │         │  POST realtime path  │
              │   `ek_...` bearer)    │         │    → { ek_, model }  │
              └───────────────────────┘         │  POST higher path    │
                                                │    → { text, ... }   │
                                                │  POST auth refresh   │
                                                │    → { access, ref } │
                                                └──────────────────────┘
```

**Two lanes of work per user turn**

- **Lane A (fast)** — gpt-realtime answers directly (greetings,
  acknowledgements, "hold on let me check", short conversational)
- **Lane B (deep)** — gpt-realtime emits `send_to_higher_model`; our
  ToolBridge screenshots the display, POSTs to the proxy higher-model
  path, gets structured JSON back, feeds it back as
  `function_call_output`. Realtime then reads the answer aloud in its
  own voice.

## 6. Terminology (used consistently below and in code)

| Term | Meaning |
|---|---|
| **Realtime endpoint** | `wss://api.openai.com/v1/realtime` (public OpenAI URL) |
| **Ephemeral token** (`ek_...`) | Short-lived Bearer minted by the proxy; ~10 min TTL |
| **Session token** | The user's long-lived OAuth-style access_token; refreshable |
| **Refresh token** | Single-use rotating credential; exchanged for new session token |
| **Proxy** | User-configurable HTTPS service that mints ephemeral tokens and forwards higher-model requests. The proxy itself is out of scope of this fork; users configure their own |
| **Higher model / higher-model client** | The deeper reasoning path (`chat` completion with screenshot) triggered by `send_to_higher_model` |
| **Buddy cursor / Overlay** | The blue triangle+states cursor rendered next to the real cursor |
| **PTT** | Push-to-talk |
| **VAD** | Voice-activity detection (server-side, disabled by us in favor of manual commit) |

**Sensitive information rule**: no source file, comment, or commit
message references any specific vendor name for the proxy, no
real-world hostname of a proxy, no anon key, no third-party product
name. All such values live in `Config.plist`, which is gitignored.

## 7. Config.plist schema (gitignored)

**Location (canonical)**: `leanring-buddy/Config.plist`. A
`leanring-buddy/Config.example.plist` sits next to it with placeholder
values and IS committed. `Bundle.main` resolves the plist. Do NOT put
it under `Configuration/` — pick ONE path and enforce via
`ProviderConfig.load()`.

| Key | Type | Example | Comment |
|---|---|---|---|
| `ProxyBaseURL` | String | `https://proxy.example.com` | Base URL of your LLM proxy. All agent traffic is signed and routed through here. |
| `EphemeralTokenPath` | String | `/agent/realtime/session` | POST path on the proxy that mints short-lived credentials for the realtime audio channel. |
| `HigherModelPath` | String | `/chat-tool-call` | POST path on the proxy for text/vision requests to the higher-tier model. |
| `RealtimeEndpoint` | String | `wss://api.openai.com/v1/realtime` | WebSocket endpoint the ephemeral credential authorises. Public OpenAI URL. |
| `RealtimeModelOverride` | String | `` (empty) | If non-empty, appends `?model=<value>`; else the endpoint uses whatever model the ephemeral was minted for. |
| `AuthTokenEndpoint` | String | `https://auth.example.com/token` | OAuth token endpoint for refresh. |
| `AuthPublicKey` | String | `PUBLIC_ANON_KEY_PLACEHOLDER` | Public client key sent with refresh requests. Not a secret; may ship with binary — but leave the plist gitignored anyway. |
| `AuthGoogleAuthorizeURL` | String | `https://auth.example.com/authorize?provider=google&redirect_to=clicky-mac://auth-callback` | First-run OAuth URL. |
| `ClientBundleId` | String | `com.example.clicky-mac` | Sent as `X-Client-Distinct-Id` context. |
| `ClientBundleIdOverride` | String | `` (empty) | If non-empty, sent instead of the real bundle id for proxy-side routing. |
| `HigherModelClientCapabilities` | Array<String> | `[clipboard_copy]` | Advertises what our client-side actions can do. |
| `HigherModelEnvOverride` | Dictionary | `{}` | Overrides for the `environment` sub-object (os_version, timezone, etc). Empty = auto-detect. |
| `RequestTimeoutSeconds` | Integer | `30` | HTTP timeout for proxy requests. |
| `AudioSampleRate` | Integer | `24000` | PCM sample rate for realtime mic in/out. |
| `RefreshLeadTimeSeconds` | Integer | `60` | How early to refresh access token before expiry. |
| `EphemeralRefreshIntervalSeconds` | Integer | `480` | 8 min. Cadence to remint the realtime ephemeral. |

Everything the code needs to talk to a real proxy lives here. Nobody
reading the public repo can tell which proxy it's for.

`.gitignore` entry:
```
Configuration/Config.plist
Configuration/Config.local.plist
```

## 8. Reference protocol (verified in Python, translate to Swift)

Reference scripts (Python, working, verified end-to-end) live at
`~/dev/clicky/docs/mac-port/reference/`:

- `heyclicky_ask.py` (~350 LoC): proxy auth + higher-model POST bridge
- `v1_realtime_voice.py` (~350 LoC): full realtime voice roundtrip via
  ephemeral key, hand-rolled WebSocket handshake
- `v4_tool_bridge.py` (~400 LoC): `send_to_higher_model` end-to-end

**Key facts distilled from those**:

1. **Auth is Supabase-style OAuth**: `POST {AuthTokenEndpoint}?grant_type=refresh_token`
   with `apikey: <AuthPublicKey>` header and JSON body
   `{"refresh_token":"…"}`. Response includes new `access_token` and
   a rotated `refresh_token`. **Refresh token is single-use** —
   concurrent refreshes will invalidate the account. Guard with a
   single `actor`.

2. **Ephemeral fetch**: `POST {ProxyBaseURL}{EphemeralTokenPath}` with
   `Authorization: Bearer <access_token>`, `X-Client-Distinct-Id:
   <user_id>`, empty JSON body. Response is
   `{value, expires_at, session}` where `session` is the full
   pre-configured Realtime session (instructions, tools, audio format,
   VAD settings, voice). **You don't need to `session.update` to
   apply these — they're server-baked.**

3. **WebSocket handshake gotcha**: Python `websockets` library hangs
   during handshake to OpenAI unless ALPN is forced to `http/1.1`.
   Swift's `URLSessionWebSocketTask` should be fine (it defaults to
   HTTP/1.1 for upgrade), but keep an eye on TLS version. **Do not
   send `OpenAI-Beta: realtime=v1` header — that Beta API was
   deprecated and now returns `beta_api_shape_disabled`.**

4. **Server VAD interference**: default session has server_vad on with
   `create_response: true` and `interrupt_response: true`. This causes
   spurious "user is barging in" cancellations of our reply. **Send
   `session.update` right after `session.created` to set
   `audio.input.turn_detection: null`** and drive turns manually.

5. **Audio format**: 24 kHz mono PCM16 both ways. Chunks of ~1s
   (48000 bytes) per `input_audio_buffer.append` frame work fine; do
   not exceed OpenAI's advisory upper bound.

6. **Tool bridge**: on `response.function_call_arguments.done` with
   `name == "send_to_higher_model"`, parse `arguments` JSON, take
   screenshot as JPEG 1024w quality 70 (~100KB, avoids proxy upload
   timeouts), POST to `{ProxyBaseURL}{HigherModelPath}` with `query`
   + `screenshotBase64` + `client_capabilities` + `environment`. Take
   the response and build `function_call_output.output` (a **string**,
   not object) with these 11 fields:

    | Field | Source |
    |---|---|
    | `text` | from higher-model response `.text` |
    | `clipboardText` | from response `.clipboardText` (nullable) |
    | `typing` | from response `.typing` (nullable) |
    | `point` | from response `.point` (nullable) |
    | `widgets` | from response `.widgets` (array, may be empty) |
    | `visual_guidance_shown` | our client (`false` for MVP) |
    | `visual_count` | our client (`0` for MVP) |
    | `target_armed` | our client (`false`) |
    | `cursor_animated` | our client (`false` unless we ran `[POINT]`) |
    | `text_typed` | derived: `bool(typing)` |
    | `copied_to_clipboard` | derived: `bool(clipboardText)` |

7. **`[POINT:x,y:label:screenN]` protocol**: model may embed
   coordinates in its response text. Parse with a regex, look up
   the correct `NSScreen` by index (`screenN` is 1-based), animate
   the buddy cursor via cubic bezier arc. Full pattern is captured in
   upstream `CompanionManager.parsePointingCoordinates` — port verbatim.

## 9. File map — what to keep, patch, replace, add

Starting point: **upstream MIT fork** already present at this repo
root (see `leanring-buddy/`). We inherit its 7,637 LoC as base.

### 9.1 Keep unchanged (base infrastructure)

| File | LoC | Why kept |
|---|---|---|
| `AppBundleConfiguration.swift` | 28 | Runtime Info.plist reader — small, generic |
| `CompanionScreenCaptureUtility.swift` | 132 | Multi-monitor SCK screenshot, dependency-free |
| `WindowPositionManager.swift` | 262 | Permission checks + AX window-shrinking |
| `GlobalPushToTalkShortcutMonitor.swift` | 132 | CGEventTap hotkey monitor |
| `DesignSystem.swift` | 880 | DS tokens referenced by all UI |
| `OverlayWindow.swift` | 881 | Per-screen transparent NSPanel + BlueCursorView |
| `CompanionResponseOverlay.swift` | 217 | Response bubble (may or may not be wired in MVP) |
| `MenuBarPanelManager.swift` | 243 | Menu bar NSStatusItem + NSPanel |
| `leanring_buddyApp.swift` | 89 | App entry (Sparkle removal only) |
| `BuddyAudioConversionSupport.swift` | 108 | PCM16 conversion helpers — needed for Realtime |
| `AppleSpeechTranscriptionProvider.swift` | 147 | Optional offline fallback (v2, defer) |
| `BuddyTranscriptionProvider.swift` | 100 | Protocol; keep for future STT provider swap |
| `ElementLocationDetector.swift` | 335 | Optional higher-precision pointing fallback (v2) |

### 9.2 Patch (sanitize hardcodes + adapt)

| File | Line | Patch |
|---|---|---|
| `CompanionManager.swift` | 73 | `workerBaseURL` → read from `ProviderConfig.proxyBaseURL` |
| `CompanionManager.swift` | 111 | model id → read from `ProviderConfig.realtimeModelOverride` (default `"default"`) |
| `CompanionManager.swift` | 167 | delete FormSpark email-capture flow entirely |
| `CompanionManager.swift` | 543-577 | keep `companionVoiceResponseSystemPrompt` verbatim — it's the [POINT] protocol contract |
| `CompanionManager.swift` | 640-680 | `sendTranscriptToClaudeWithScreenshot` → replace body with `HigherModelClient.ask()` call |
| `CompanionManager.swift` | 762 | fallback text mentioning the upstream author → rewrite to something neutral |
| `CompanionManager.swift` | 830 | delete Mux onboarding video URL and its use |
| `CompanionManager.swift` | 950-962 | delete PostHog wiring |
| `OverlayWindow.swift` | 173 | welcome text "hey! i'm clicky" → keep or rebrand as `AssistantIdentity.displayName` from Config |
| `OverlayWindow.swift` | 217-260, 371-386, 842-881 | delete `OnboardingVideoPlayerView` / `AVPlayerNSView` and the video invocation block |
| `CompanionPanelView.swift` | (multiple) | remove email-capture, PostHog opt-in, Sparkle "check for updates" if we're not shipping updater in MVP |
| `ClaudeAPI.swift` | file | delete (higher-model client replaces it) |
| `ElevenLabsTTSClient.swift` | file | delete (Realtime handles TTS) |
| `ClickyAnalytics.swift` | file | delete (privacy) |
| `AssemblyAIStreamingTranscriptionProvider.swift` | file | delete (Realtime handles STT) |
| `OpenAIAudioTranscriptionProvider.swift` | file | delete |
| `OpenAIAPI.swift` | file | delete |
| `BuddyDictationManager.swift` | file | **partially** delete — keep only `BuddyPushToTalkShortcut` enum (~50 LoC) needed by GlobalPushToTalkShortcutMonitor; delete the audio capture pipeline (Realtime does it) |

### 9.3 Add new (the CloudProvider layer)

New group `AgentBackend/` under `leanring-buddy/`. Naming is neutral;
no source file mentions the specific vendor.

| File | LoC est | Purpose | Public API |
|---|---|---|---|
| `AgentBackend/ProviderConfig.swift` | 120 | Load `Config.plist`, typed accessors, fallback to `Config.example.plist` | `struct ProviderConfig { static func load() throws -> Self; let proxyBaseURL: URL; ... }` |
| `AgentBackend/SessionCredentialStore.swift` | 140 | Persist access/refresh/user_id in Keychain; JWT expiry parser | `actor SessionCredentialStore { func current() -> Credentials?; func update(_:); func clear() }` |
| `AgentBackend/SessionAuthenticator.swift` | 130 | Refresh access token when <60s left; single in-flight refresh | `actor SessionAuthenticator { func ensureFresh() async throws -> String }` |
| `AgentBackend/AgentProxyClient.swift` | 160 | Bearer + client-id headers, JSON POST wrapper | `struct AgentProxyClient { func post<T,U>(_ path:String, body:T) async throws -> U }` |
| `AgentBackend/RealtimeSessionClient.swift` | 320 | Ephemeral fetch, WS open, event pump, PCM stream in/out | `actor RealtimeSessionClient { func connect() async throws; func sendMicChunk(_:Data); var events: AsyncStream<RealtimeEvent> }` |
| `AgentBackend/HigherModelClient.swift` | 180 | POST higher-model path with query + screenshot, decode response | `struct HigherModelClient { func ask(query:String, screenshot:Data?) async throws -> HigherModelResponse }` |
| `AgentBackend/AgentToolBridge.swift` | 200 | Route `send_to_higher_model` + future tools; serialize `function_call_output` | `struct AgentToolBridge { func handle(_ call: ToolCall) async throws -> ToolResult }` |
| `AgentBackend/RealtimeEvent.swift` | 150 | Codable event enum for WS wire format | `enum RealtimeEvent { case sessionCreated, audioDelta(Data), toolCall(ToolCall), ... }` |
| `AgentBackend/AgentBackendError.swift` | 80 | Typed errors + LocalizedError | `enum AgentBackendError: LocalizedError { case notAuthenticated, refreshFailed, transportLost, ... }` |
| `AgentBackend/Environment.swift` | 90 | Build per-request `environment` dictionary (OS, locale, device) | `struct DeviceEnvironment { static func current() -> [String:Any] }` |

Total new code: ~1500-1800 LoC.

### 9.4 The one file that must be rewritten

`CompanionManager.swift` (1026 LoC) — the central state machine.
Existing structure is right, but the audio + LLM implementation is
wrong for us. Approach:

1. Keep: `voiceState` enum, conversation history buffer,
   `parsePointingCoordinates()`, coord math around
   line 640-680, `companionVoiceResponseSystemPrompt` string
2. Delete: AssemblyAI/ClaudeAPI/ElevenLabs/PostHog/FormSpark/Mux
   plumbing (~250 LoC)
3. Replace: audio + LLM lifecycle with `RealtimeSessionClient` +
   `AgentToolBridge`

Estimated after-diff size: ~700 LoC. Add a thin
`VoiceConversationController` if you want to split responsibilities.

## 10. Sequence diagrams

**App boot**
```
App.launch()
  → ProviderConfig.load()
  → SessionCredentialStore.current()
      nil        → OnboardingView shows sign-in
      has creds  → SessionAuthenticator.ensureFresh()
                     jwt exp > 60s   → done
                     else            → refresh via AuthTokenEndpoint
                                        → store new pair
  → CompanionManager.start()
  → OverlayWindowManager.installOnAllScreens()
```

**Push-to-talk press**
```
CGEventTap → PushToTalkShortcut.pressed
  → CompanionManager.handlePress()
     if not connected:
        RealtimeSessionClient.connect()
           SessionAuthenticator.ensureFresh()
           AgentProxyClient.post(EphemeralTokenPath) → { ek_, session_cfg }
           WebSocket.connect(RealtimeEndpoint, bearer:ek_)
           await session.created
           send session.update (turn_detection: null)
     mic tap installed → start streaming
        each 100ms buffer → WSSend(input_audio_buffer.append b64 pcm16)
```

**Push-to-talk release**
```
CGEventTap → PushToTalkShortcut.released
  → WSSend(input_audio_buffer.commit)
  → WSSend(response.create)
  → EventRouter loops:
      response.output_audio.delta → decode → schedule on AVAudioPlayerNode
      response.output_audio_transcript.done → update overlay text
      response.function_call_arguments.done (name: send_to_higher_model)
         → AgentToolBridge.handle(call)
             screenshot = CompanionScreenCaptureUtility.capture(cursorScreen)
             out = HigherModelClient.ask(query: args.query, screenshot: jpeg)
             WSSend(conversation.item.create {function_call_output, call_id, output: JSON(out)})
             WSSend(response.create)
             ... realtime speaks the final answer ...
             if answer contains [POINT:x,y:label:screenN]:
                overlay.animateCursorTo(coord, screen: N, label)
      response.done → mark idle; disconnect after 30s idle
```

**Session refresh (8 min timer while active)**
```
Timer fires → mint fresh ek_ (SessionAuthenticator ensures access is fresh first)
  → open second WS in parallel with new ek_
  → wait for session.created on the second WS
  → transfer conversation state (last N conversation.items)
  → close old WS
```

## 11. Failure modes → user-visible states

| Failure | Detection | UI state | Retry |
|---|---|---|---|
| No `Config.plist` at first launch | `ProviderConfig.load()` throws | Onboarding "Configure your proxy" | User taps `Import Config` |
| Access token expired | 401 on any request | Automatic refresh, no UI blip | Silent |
| Refresh token invalid | 400/401 on refresh | Sign-in screen "Session expired" | User signs in again |
| Ephemeral expired mid-turn | WS 401/close | Overlay: "reconnecting…" (200ms max) | Auto |
| WS transient drop | close code != 1000 | Overlay: "reconnecting…" | Auto once, then manual |
| Network fully offline | `URLError.notConnectedToInternet` | Overlay: "you're offline" | User taps mic to retry |
| Higher-model 5xx or timeout | non-2xx in `HigherModelClient` | Still sends `function_call_output` with `error:"upstream_unavailable"` so Realtime finishes turn gracefully | Silent |
| Mic permission denied | AVCapture returns `.denied` | Menubar panel: "Enable microphone in Settings" with deep-link button | User grants → re-enable hotkey |

## 12. Acceptance tests

- T1. `Config.plist` populated with real proxy values, launch app. See
  Realtime session established in logs within 3 s of first PTT press.
- T2. Hold PTT, say "hi". Get spoken reply. Elapsed time < 4 s.
- T3. Hold PTT, say "what's on my screen right now". See
  `send_to_higher_model` event in logs; get spoken reply that
  actually references screen contents. Elapsed time < 20 s.
- T4. Say "point at the address bar". Cursor animates to browser
  address bar coordinates.
- T5. Leave app idle 15 min. Come back, PTT. Still works (access
  token has been silently refreshed).
- T6. Kill Wi-Fi mid-turn. Overlay shows offline state within 2 s.
  Re-enable Wi-Fi. PTT works again.
- T7. Grep sources for sensitive strings — expect ZERO matches:
   ```
   git grep -i 'clicker-proxy\|farza\|humansongs\|supabase\|mrpvynsdsn\|fable-5\|assemblyai\|elevenlabs\|posthog\|formspark\|mux.com' -- '*.swift'
   ```
- T8. Verify `Config.plist` is in `.gitignore` and `Config.example.plist`
   contains only placeholder URLs.

## 13. Day-by-day plan (10-14 days one engineer)

**Day 0 — Setup (0.5 day)**
- [ ] `git init && git add . && git commit -m "initial commit"`
- [ ] Create `.gitignore` with `Configuration/Config.plist`,
  `Configuration/Config.local.plist`, `.DS_Store`, `xcuserdata/`,
  `*.xcuserdatad/`
- [ ] Create `Configuration/Config.example.plist` with all placeholders
- [ ] Copy real values to `Configuration/Config.plist` locally
- [ ] Confirm Python reference scripts run end-to-end from
  `~/dev/clicky/docs/mac-port/reference/` (V1 + V4)

**Day 1 — Sanitize (0.5 day)**
- [ ] Delete `ClickyAnalytics.swift`, `ClaudeAPI.swift`,
  `ElevenLabsTTSClient.swift`,
  `AssemblyAIStreamingTranscriptionProvider.swift`,
  `OpenAIAudioTranscriptionProvider.swift`, `OpenAIAPI.swift`
- [ ] Delete FormSpark + Mux + PostHog blocks in `CompanionManager.swift`
- [ ] Delete `OnboardingVideoPlayerView` in `OverlayWindow.swift`
- [ ] Run acceptance test T7 (grep for sensitive strings)
- [ ] Xcode: fix compile errors from deletions; stub methods to
  `fatalError("not wired yet")`

**Day 2-3 — AgentBackend foundation (1.5 days)**
- [ ] Create `AgentBackend/` group
- [ ] `ProviderConfig.swift` + `Environment.swift`
- [ ] `SessionCredentialStore.swift` (Keychain-backed, actor-isolated)
- [ ] `SessionAuthenticator.swift` (refresh on <60s, single-flight)
- [ ] `AgentProxyClient.swift`
- [ ] `HigherModelClient.swift`
- [ ] Standalone unit test: sign in with test creds, `ensureFresh()`
  returns valid access token; higher-model POST with a dummy
  screenshot returns non-empty text

**Day 4-6 — Realtime transport + audio (2.5 days)**
- [ ] `RealtimeEvent.swift` — decode wire format
- [ ] `RealtimeSessionClient.swift` — WS open, session.created,
  session.update, event AsyncStream
- [ ] `BuddyAudioConversionSupport` — reuse for PCM16 out
- [ ] Wire AVAudioEngine input tap → `input_audio_buffer.append`
- [ ] Wire `response.output_audio.delta` → AVAudioPlayerNode
- [ ] Standalone smoke test: connect, send 5s of mic, hear reply
  (mirror of V1)

**Day 7-8 — Tool bridge + CompanionManager rewrite (1.5 days)**
- [ ] `AgentToolBridge.swift` with `send_to_higher_model` handler
- [ ] Rewrite `CompanionManager` core loop to use RealtimeSessionClient
- [ ] Preserve `voiceState`, conversation history, `parsePointingCoordinates`
- [ ] Wire ToolBridge into event router
- [ ] Manual test: T2 and T3 pass

**Day 9 — Overlay integration (1 day)**
- [ ] Feed `voiceState` to overlay (idle → listening → speaking)
- [ ] Feed mic RMS to waveform
- [ ] Wire `[POINT:x,y:label:screenN]` parser to
  `OverlayWindowManager.animateCursorTo`
- [ ] Manual test: T4 passes

**Day 10 — Refresh + reconnect (1 day)**
- [ ] `EphemeralRefreshTimer` — every 8 min
- [ ] WS graceful reconnect with fresh ek_
- [ ] Access token refresh integration
- [ ] Test: T5 passes

**Day 11 — Failure modes + UI polish (1 day)**
- [ ] Wire `AgentBackendError` to overlay states
- [ ] Offline detection
- [ ] Sign-in flow (deep-link, PKCE if needed, or manual paste of
  refresh token as MVP shortcut)
- [ ] Menubar panel: remove FormSpark, add "Sign out" button
- [ ] Test: T6, T8 pass

**Day 12-13 — Packaging (1.5 days)**
- [ ] Xcode: correct bundle id, entitlements
  (mic, screen recording, network client)
- [ ] Info.plist: `NSMicrophoneUsageDescription`,
  `NSAppleEventsUsageDescription`
- [ ] Signing (or leave unsigned for personal use; document how to
  bypass Gatekeeper)
- [ ] DMG if desired; skip Sparkle for MVP
- [ ] Test: T1 passes on a fresh macOS user account

**Day 14 — Buffer + integration testing (1 day)**
- [ ] Run for 30 continuous minutes crossing at least 3 ephemeral
  refresh boundaries. No crash. No audio gap > 500ms.
- [ ] Regression: T1-T8 all still pass

**Total: 12-14 days**. Add ~50% padding for macOS TCC / signing /
first-run permission fights → **14-21 days realistic**.

## 14. Debug ladder

**No sound out**
- Confirm `AVAudioPlayerNode` is scheduled on the correct engine
- Log every `response.output_audio.delta` byte count; if 0 bytes,
  check event routing
- Verify PCM16 sample rate matches (24000)

**`session.created` never arrives**
- Check ALPN and TLS handshake in URLSessionTask delegate
- Confirm ephemeral token in Authorization header, no other headers
- If you see `beta_api_shape_disabled`: remove `OpenAI-Beta` header

**`response.done` fires with `reason: turn_detected`**
- Server VAD is still on. Send `session.update` with
  `audio.input.turn_detection: null` immediately after `session.created`

**`input_audio_buffer_commit_empty`**
- Not enough audio in the buffer. Check mic RMS on device; must be
  > 200. If quiet mic hardware, apply 10× digital gain before send.

**Higher-model POST returns empty body**
- Check payload size (screenshot > 2MB is unreliable through some
  network paths — downscale to 1024w JPEG q70)
- Check Authorization header has current access token

**Refresh returns empty body**
- Refresh token was already used elsewhere. Do NOT overwrite the
  stored refresh_token with empty. Keep the access token and let it
  expire naturally, then force re-sign-in.

**`[POINT:...]` cursor lands on wrong screen**
- Verify screen index math — `screenN` in the model output is
  1-based, `NSScreen.screens[N-1]`.

## 15. What lives outside this repo (dependencies to know about)

- Python reference implementation: `~/dev/clicky/docs/mac-port/reference/`
- Optional companion daemon (Python) that auto-recreates the account
  when quota is burned: `~/dev/heyclicky/heyclicky.py`. Not required.
- Config values that populate `Config.plist`: obtained separately;
  documented in a private note, not in this repo.

## 16. Non-goals we might revisit

- Overlay welcome bubble ("hey! i'm clicky") — rebrand or delete
- Skills system from upstream `CompanionPanelView` — port later
- Sparkle auto-updates — needs Developer ID + release infra
- Multi-STT provider swap UI — only if Realtime becomes unavailable

---

**End of spec.** Everything an engineer needs is above. Reference
scripts are at `~/dev/clicky/docs/mac-port/reference/`; on day 0 you
run them to confirm the network path works, then port section by
section following §13.
