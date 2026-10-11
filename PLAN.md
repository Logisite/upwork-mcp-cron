# upcroc — native Android job watcher

Native Android only. No applying to jobs inside the app.

---

## Goal

Catch new Upwork AI jobs in ~2 minutes, notify with sound, survive being
swiped away, and come back exactly where you were. Must run on a 2 GB phone.

---

## Step 0 — prerequisite (~10 min, ~1 GB)

Install JDK 17 (Temurin), Android cmdline-tools, platform-tools, one platform
image. Set `ANDROID_HOME`, write `local.properties` with `sdk.dir`. Skip the
emulator; test on the physical 2 GB phone over USB.

Verify: `java -version` and `./gradlew --version` respond. Gate for everything
after this.

---

## Step 1 — MCP client (~2 h, highest risk)

Only genuinely uncertain piece. Verify against the live API before building
anything on top.

- `io.modelcontextprotocol:kotlin-sdk` (MIT, Maven Central), client module
- `StreamableHttpClientTransport`, which is what `mcp.upwork.com` speaks
- Ktor `HttpClient` with OkHttp engine, Bearer token in default headers
- Session held across checks, no per-check handshake
- `probe()` that calls `find_jobs` and prints results

Success = a live job list from Upwork. Everything after this is bookkeeping.

---

## Step 2 — shared KMP core

KMP module with `androidTarget()` + `jvm()`. One engine, two future shells.

- `models/` — Job, ClientStats, Envelope as data classes
- `poll/Bucket.kt` — draw `90 + random(0,150)`, persist, drain 1s per tick
- `poll/Pager.kt` — page until a page inserts zero new rows, cap 8
- Ordering guard — if a page's oldest item is not older than the previous
  page's oldest, stop and log (silent skipping becomes visible)
- `data/JobStore` — Room KMP, `insertIgnoreAll` rowcount == new-job count
- `port/Notifier.kt`, `Logger.kt`, `OAuthPort.kt` — the only seam, strings and
  primitives only

---

## Step 3 — engine

`PollingEngine` owns the ticker, runs Pager, decides notify, calls Notifier.
Client-side sort by `publishedDate`, always: Upwork's `recency` is unreliable
(observed a 3-day-old post mid-"newest-first" page, and a `smart_search` set
spanning 61 h).

Verify: run the Python watcher and the Kotlin engine against the same query
and diff the output. Same jobs, same ordering, same dedupe decisions.

---

## Step 4 — Android shell

- `WatcherService`: foreground, `START_STICKY`, 4 h session cap enforced in code
- Bucket persisted to DataStore so a kill resumes, never resets
- Heartbeat coroutine every 30s, restart engine if `lastTick` has not advanced
  (the one silent-death path — must not be forgotten)
- `AndroidNotifier`: per-job fixed id so notifications replace rather than stack
- Custom sound, vibration, priority HIGH
- Status notification: three counters (experiences, jobs won, proposals sent)
  plus session clock; Live Updates on API 36+, standard below

---

## Step 5 — OAuth

AppAuth-Android with deep-link redirect. Token cached in
EncryptedSharedPreferences, Ktor interceptor for refresh. Never reuse the
`opencode.ai` client document — it is another application's registered
identity and invites throttling or revocation.

---

## Step 6 — UI

Compose feed over Room. `LazyColumn(key = Job.id)` so 500 cached jobs cost
what 12 visible ones cost. `notifiedAt IS NULL` is the watermark, so crash
recovery is a database read.

Memory target under 80 MB: close every response body (one leaked body/check
≈ 50 MB/day), `StateFlow` holds ids not objects, OkHttp pool capped at 2.

Notification body (layout 3, already chosen):

    Summary: 2m ago · Fewer than 5 proposals
    Body:    HIRING: Business Systems Architect & AI Automation Strategist

             $14.00–$27.00/hr hourly · United States · Expert
             https://www.upwork.com/jobs/~022108750913066553406
             15 Connects · 2 jobs · 0% hire · sent 6 invites

Tracking params stripped. Invite flag fires at >=1.

---

## Step 7 — prove it

Run on the phone for a day. `upwork-cron metrics`-equivalent gives the real
latency distribution against the 2-minute target.

---

## Dependencies (~8–12 MB dex)

Compose BOM, activity-compose, lifecycle-runtime-compose, Room (runtime +
ktx), OkHttp, kotlinx-serialization, kotlinx-coroutines. KSP for Room only.
Manual DI via `AppContainer`, not Hilt.

---

## Module layout

    com.upcroc
      core/mcp/      McpClient, JsonRpc, SseParser
      core/auth/     PkceFlow, TokenStore
      core/poll/     PollingEngine, Bucket
      model/         Job, ClientStats, Profile
      data/db/       UpcrocDatabase, JobDao
      data/prefs/    BucketStore, SettingsStore
      notify/        JobNotifier, StatusNotifier
      tools/         ToolRegistry, Search, Experience, Draft
      ui/            FeedScreen, FeedViewModel, JobCard
      service/       WatcherService
      di/            AppContainer

`core/` isolated so a desktop JVM entrypoint is additive later.

---

## Tools layer

    interface UpcrocTool {
      val name: String
      suspend fun run(input: String): String
    }

| Tool | Network |
|---|---|
| `SearchTool` — word/skill/pay/client-history over Room | none |
| `ExperienceTool` — read + rank profile experiences | none |
| `DraftTool` — proposals from your real experience | LLM only (server-hosted default) |

First two fully offline.

---

## Framework verdict (researched)

1. Kotlin-native + official SDK — chosen. No interpreter, ~35–50 MB, best service APIs.
2. Chaquopy + stdlib Python — viable, strictly worse on memory (+15–30 MB) with no compensating advantage now the SDK exists.
3. Kivy/p4a — most complete Python-only, weakest fit for a service-first app.
4. React Native — last. 60–120 MB baseline vs an 80 MB target, weakest background story, no first-party MCP.

---

## Risks

| Item | Status |
|---|---|
| No JVM or Android SDK on the build machine | Cannot compile here. First real compile happens after Step 0. |
| Live Updates (API 36+) surface | Unconfirmed — verify before building the status clock. |
| Python watcher still running | Keep alive until Kotlin catches a real job. Retire after proof. |
| Desktop JVM entrypoint | Open — layout already leaves room for it. |

---

## The one thing to watch

Do not build the UI before the transport works. The UI is the fun part and
works the moment you sit down with it. The transport is the part that can fail
in ways you cannot predict, and everything else is cheap once it works.
