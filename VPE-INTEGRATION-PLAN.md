# Veo Pro Experimental (VPE) — integration analysis and implementation plan

> **Pre-GA / experimental.** This describes the Veo Pro Experimental (VPE) program, which is
> allowlist-gated and subject to the Pre-GA Offerings Terms. Capabilities may be rotated or removed
> on 30 days' notice, and nothing here is a commitment that any of it ships.
>
> Do **not** open a PR against `GoogleCloudPlatform/gcc-creative-studio` from this branch until the
> VPE program confirms the material may be published there.

---

## 1. What VPE actually is

"VPE" is **Veo Pro Experimental** — a private-preview program, not a single model. Omni-Cine is one
of eight capabilities in it. Access is via a single new publisher endpoint, `veo-experimental`, in
Gemini Enterprise Agent Platform (formerly Vertex AI), which routes to different checkpoints based
on `parameters.experiments.modelName`.

| Capability | `experiments.modelName` | One-line summary |
|---|---|---|
| Upscaler | `veo3p1_upscale` | 720p/1080p → 1080p or 4K. Standalone post-process. |
| Omni-Cine | `omni-cine` | The Omni model, with EXR/ProRes/DNxHR I/O and 240-frame output. |
| Video transform | `veo-exp-video-transform` | V2V restyle, masked (localised) edits, multi-keyframe I2V. |
| Performance controls | `veo-exp-perf-estimation` → `veo-exp-perf-generation` | Two-step: extract an actor's performance as a "blue mesh", then drive a generated character with it. |
| Dialogue-driven generation | `veo-exp-a2v-generation` | Animate a start frame lip-synced to a **supplied** audio track. No new audio generated. |
| Video textures | `veo-exp-video-textures` | Seamlessly tessellating and/or looping video. |
| Upscaling tessellation/looping | `veo3p1_upscale` + `seamless{}` | 4K upscale that preserves seams for the above. |
| Professional formats | (cross-cutting) | ProRes/DNxHR/16-bit PNG/EXR output config for all of the above. |

---

## 2. The core architectural finding: this is a third API surface

The app currently speaks **two** different video APIs, and VPE is a **third**, unlike either:

| | Current Veo | Current Omni | **VPE** |
|---|---|---|---|
| Call | `client.models.generate_videos` (GenAI SDK) | `client.interactions.create` | raw REST `:predictLongRunning` |
| Endpoint | SDK-managed | SDK-managed | `us-central1-aiplatform.googleapis.com/.../publishers/google/models/veo-experimental` |
| Poll | `client.operations.get` | synchronous | `:fetchPredictOperation` with `{"operationName": ...}` in the **body** |
| Payload | typed SDK config | `steps`/`user_input` | `instances[]` + `parameters{}` with a nested `experiments{}` block |
| I/O | inline or GCS | GCS URI out | **GCS only, both directions** |
| Required header | — | — | `X-Vertex-AI-LLM-Request-Type: shared` |

**Verified:** `google.genai.types.GenerateVideosConfig` has no `experiments` field and no passthrough
for arbitrary parameters (checked by introspecting the installed SDK). So VPE **cannot** be bolted
onto the existing `generate_videos` call — it needs its own REST client. This is the single biggest
driver of the plan's shape.

Secondary but important: `config_service.LOCATION` defaults to `"global"`, while every VPE sample
is `us-central1`. VPE needs its own region setting rather than reusing `LOCATION`.

---

## 3. Findings that change what's worth building

These came out of cross-referencing all nine docs against this codebase and this engagement's
actual usage. They should be settled before committing to scope.

### 3.1 Only the Upscaler can produce 9:16 — and the telenovela work is 9:16

Three tiers, distinguished by what the docs *state* versus what is *inferred*:

| Tier | Capability | Documented wording |
|---|---|---|
| **Portrait supported** ✅ | Upscaler | "1280x720 (720p) or 1920x1080 (1080p). **Both landscape and portrait orientations are supported.**" Its `aspectRatio` parameter accepts `"16:9"` and `"9:16"`. |
| **Explicitly landscape-only** ❌ | Performance controls, Video transform | "1280x720 (720p). **Landscape 16:9 orientation only**; source video is not rescaled." |
| **Unstated — 1280x720 output, no orientation wording** ⚠️ | Dialogue-driven, Video textures, Omni-Cine | Only a resolution is given. Dialogue-driven and Video textures add "Resized and padded internally if not exact." |

The decisive evidence is the parameter tables rather than the prose: **`aspectRatio` is documented as
a parameter for the Upscaler only.** No other capability exposes any way to request a 9:16 output,
and all of them state a 1280x720 output resolution. So for the tier-3 capabilities the practical
answer matches tier 2 even though the docs never say "landscape only" — you cannot ask for a
vertical frame.

Worse, the "resized and padded internally" behaviour on Dialogue-driven and Video textures is a
trap rather than a feature: feed a 720x1280 portrait frame and it is **pillarboxed** into a
1280x720 landscape frame. Cropping the result back to vertical leaves roughly 405x720 of real
subject pixels — a resolution loss that defeats the point of a professional-formats pipeline. It
will not error; it will just quietly produce a bad deliverable.

**The upside:** the single highest-value capability *is* the portrait-capable one. Generate 9:16 at
720p with Omni (what this customer already does) → upscale to 4K vertical is a complete, useful
vertical pipeline available today.

**Still the headline question for TU/ViX:** is their VPE interest vertical or landscape? If vertical,
the Upscaler is essentially the whole addressable suite right now, and Phases 2–4 should wait.

*Confidence note: tiers 1 and 2 are quoted from the docs. Tier 3 is inferred from the absence of an
`aspectRatio` parameter plus a fixed 1280x720 output — worth confirming with the VPE team rather
than treating as settled.*

### 3.2 Omni-Cine is not an upgrade to the Omni already in the app

Different API, and it **loses** capabilities the current integration relies on:
- No audio references, no video references (current Edit Video sends video + images).
- No multi-turn edit conversation — the `interaction.steps` replay this app depends on for
  "edit this clip again" has no equivalent.
- No seed.

What it **gains**: EXR/ProRes/DNxHR, 16-bit output, and 240 frames (10s) vs Omni's 8s.

So it's a parallel professional-finishing path, not a replacement. Presenting it in the UI as just
another model in the existing dropdown would be actively misleading.

### 3.3 Output is a *directory*, not a file

Every VPE job writes a folder: `sample_0.<ext>` (preview + audio), `sample_0/frame_####.png|exr`
(the frame sequence — up to 240 files), `sample_0_audio.wav`, `request.json`, `prompt.txt`.

The gallery's `MediaItem` model assumes one video URI. A frame sequence has no natural
representation there. Plan: ingest the **preview video** as the gallery item, and record the output
directory URI alongside it so the frame sequence and audio stem are retrievable, without trying to
model hundreds of frames as media items.

### 3.4 Input constraints are strict and the error messages are bad

24 fps **exactly**; 4–8s (Upscaler) or ≤192/240 frames; exact resolutions; declared aspect ratio
must match actual pixels. A known issue states the model "doesn't return accurate error messages
(e.g. wrong fps on input video returns high load message instead)" — so a violated constraint can
surface as a *spurious capacity error*.

Therefore **client-side preflight is not optional**; without it, users get misleading failures on a
paid long-running job. `ffmpeg` is already installed in the backend image (Dockerfile:43, added for
the Omni audio-stripping work) so probing and transcoding is available at no new infra cost.

### 3.5 Bootstrap work that has nothing to do with the app code

VPE requires three service agents to hold `roles/storage.admin` on the I/O buckets — and two of
them (`gcp-sa-vertex-bp`, `gcp-sa-vertex-tune`) **do not exist** until a batch-prediction job and a
tuning job have each been started once in the project. The documented procedure is to start and
immediately cancel a throwaway job of each type. Skipping this yields a `Principal does not exist`
error. This belongs in `bootstrap.sh`/Terraform, not in the request path.

Also: input **and** output buckets must live in the allowlisted project. If a customer's media
bucket is in a different project from the allowlisted one, that's a blocking infra problem, not a
code problem.

---

## 4. Recommended scope and sequencing

Ordered by (value to the telenovela use case) ÷ (integration risk).

### Phase 0 — Foundation, no user-visible feature
- `VpeClient`: authenticated REST wrapper for `:predictLongRunning` + `:fetchPredictOperation`
  polling, with the `X-Vertex-AI-LLM-Request-Type: shared` header and its own region config.
- Config: `VPE_ENABLED` (default **off**), `VPE_LOCATION` (default `us-central1`),
  `VPE_ENDPOINT_MODEL` (default `veo-experimental`).
- A `VpeCapability` registry mirroring the frontend's existing `ModelCapability` pattern — one
  declarative entry per capability holding its `modelName`, accepted mime types, fps/resolution/
  duration/orientation constraints, and required vs optional fields.
- `MediaPreflight` (ffmpeg-backed): probe fps/resolution/duration/orientation, and either reject
  with an accurate message or transcode to spec. This is what turns the API's bad errors into
  good ones.
- **Everything gated behind `VPE_ENABLED=false`** so it ships dark and cannot affect existing users.

### ⚠ Phase 1 blocker found by measurement: most real shots are the wrong length

The Upscaler accepts **4–8 seconds**. Omni — the app's default video model — offers
`supportedDurations: [3,4,5,6,7,8,9,10]`. I ffprobed every generated clip in `_review/`:

| Set | Inside 4–8s | Rejected |
|---|---|---|
| Test-harness clips (`_review/videos/`, 5–6s) | 39 / 39 | 0 |
| **Real production shots** (micronovela + cantina) | **4 / 16** | **12 / 16 — 75%** |

All clips are exactly 24 fps and 720x1280, so fps and resolution are fine. It is **duration alone**,
and the constraint is self-inflicted: the producer naturally reached for 9s and 10s for dramatic
beats, and the app's own duration picker offered them.

So "find a video in the library and upscale it" — the exact flow requested — fails on three quarters
of the real work. A user selects 12 shots, 9 grey out, and the feature reads as broken.

**Phase 1 must therefore ship one of these, not just the happy path:**
- **Segment selection** for >8s clips: an in/out selector over the `<video controls>` already in the
  lightbox (it tracks `currentTime`), snapped to 24 fps frame boundaries, submitting a frame range.
- **Split-and-rejoin** as an explicit multi-job chain — note a 10s clip cannot be halved into legal
  jobs (8s + 2s fails the 4s minimum); it needs 0–5s and 5–10s, then a rejoin. The app already owns
  both halves: `/api/videos/concatenate` and `media_utils.concatenate_videos`.
- **Prevention at source**: on a VPE-enabled deployment, annotate the duration picker — "clips
  outside 4–8s can't be upscaled later". One line, and it stops the problem being created.

### Phase 1 — Upscaler (the first vertical slice)
Best first capability, by a distance:
- Supports **portrait**, so it actually fits the micronovela work.
- Semantically a post-process on an existing gallery clip — "Upscale to 4K" as an action on a
  clip the user already has, rather than a new generation mode. Smallest possible UI surface.
- Video in → video out, so it fits `MediaItem` without the frame-sequence problem (unless 16-bit
  PNG output is requested — offer that as an "export frames" option, not the default).
- Exercises the entire Phase 0 stack end-to-end: preflight, submit, poll, ingest.

Known issues to design around, both documented: 4K 16-bit PNG output may not return the operation
ID or output path (so treat that combination as unsupported for now, or poll the bucket); and 1080p
input requires `aspectRatio` to be set explicitly despite being documented as optional (so always
send it).

### Phase 2 — Dialogue-driven generation (`veo-exp-a2v-generation`)
Highest *creative* value for a Spanish-language telenovela: lip-sync a generated character to a
**real recorded voice track**, which is precisely the gap that came up repeatedly in the earlier
content/language work — it sidesteps the model's Spanish dialogue generation entirely by letting
production supply the audio.

Gated on resolving §3.1: it's a 1280x720 landscape frame today.

### Phase 3 — Video transform + masked transform
The most versatile editing capability (restyle, costume/prop swaps via grayscale mask,
multi-keyframe interpolation). Also the most parameters (`videoTransformStrength`,
`numDiffusionSteps`, mask URI, `conditioningFrames[]`), and needs a mask-authoring story the app
has no UI for. Landscape-only.

### Phase 4 — Omni-Cine, Performance controls, Video textures
- **Omni-Cine**: build when there's a confirmed VFX/finishing pipeline that consumes EXR/ProRes.
  Ship as a distinct "professional finishing" path, never as a drop-in Omni replacement (§3.2).
- **Performance controls**: two chained jobs plus a blue-mesh intermediate artifact to store and
  re-use — a genuinely new asset type. High value for consistent character performance, but the
  largest data-model change.
- **Video textures**: least relevant to serialized drama; real value for signage/backgrounds.
  Cheap to add once Phase 0 exists (it's mostly three booleans).

---

## 5. How to build this without being able to test it

The project is not allowlisted, so **no VPE call can be executed**. That constraint should shape
the engineering, not just be a caveat at the end.

1. **Payload-contract tests.** Assert the exact JSON emitted for each capability against fixtures
   transcribed from the doc samples. This is the highest-value test available: every documented
   sample becomes an expected payload. It catches field-name and nesting errors — which, given
   `experiments{}` is nested inside `parameters{}` and differs per capability, is where mistakes
   will actually happen.
2. **Preflight tests run for real.** ffmpeg is local, so fps/resolution/duration validation and
   transcoding can be tested properly with generated fixture clips. No allowlist needed.
3. **Polling/LRO tests against recorded responses.** The docs contain real success and error
   response bodies (including the five distinct error shapes in the user guide's troubleshooting
   section) — replay them against the client.
4. **A dry-run mode.** `VPE_DRY_RUN=true` logs the fully-rendered request JSON and returns a
   synthetic operation, so the whole path can be walked in a browser without an allowlist. This is
   also what someone *with* access uses to sanity-check a payload before spending on a job.
5. **Label it honestly.** Every VPE surface stays behind `VPE_ENABLED`, documented as
   unverified-against-live-API until someone with allowlist access confirms it — consistent with
   how the Firebase rollback path was marked in `UPGRADING.md`.

**The first person with an allowlisted project should run Phase 1 end-to-end before Phase 2 starts.**
Building three capabilities on an unvalidated client just multiplies the rework if the client is
wrong.

---

## 6. Concrete work items

**Backend**
- `backend/src/videos/vpe/client.py` — REST client, LRO polling, error mapping.
- `backend/src/videos/vpe/capabilities.py` — declarative per-capability constraint/schema registry.
- `backend/src/videos/vpe/preflight.py` — ffmpeg probe + transcode-to-spec.
- `backend/src/videos/vpe/payloads.py` — one builder per capability.
- `backend/src/videos/dto/` — a VPE request DTO with per-capability cross-field validation, in the
  style of the existing `CreateVeoDto.validate_cross_fields`.
- Output ingestion: preview video → `MediaItem`; store output directory URI + artifact manifest.
- Config additions (`VPE_ENABLED`, `VPE_LOCATION`, `VPE_DRY_RUN`).

**Frontend**
- Extend `ModelCapability`/`MODEL_CONFIGS` with VPE entries, hidden unless the backend reports VPE
  enabled (needs a small capabilities endpoint or config echo — the UI must not advertise what the
  deployment can't do).
- Phase 1 UI: an "Upscale" action on a gallery clip + a small options dialog (resolution,
  sharpness, codec/quality). Deliberately not a new generation mode.

**Infra / docs**
- ~~`bootstrap.sh` + Terraform~~ → done as `scripts/vpe_bootstrap_project.sh` (`§8a`), kept
  separate from `bootstrap.sh` because the allowlisted project is usually not the deployment
  project: create/verify the three service agents, including the
  start-and-cancel dance for `vertex-bp` and `vertex-tune`, and grant `roles/storage.admin`.
- ~~`UPGRADING.md`~~ → done (`§8a`): a VPE section covering allowlisting prerequisites, the
  service-agent bootstrap, and the fact that the feature ships off by default.

---

## 6a. How this gets surfaced in the UI

Designed by putting four competing approaches through three adversarial judges (mental model,
implementation cost, constraint survival) plus a completeness critic. All three judges independently
ranked the **asset-action** approach first and the **new-generation-mode** approach last.

### The verdict: post-process actions on assets, never new modes

The instinct to model this like "Edit with Omni" is correct, and the judges were emphatic about why
the alternative fails. Making VPE capabilities into `GenerationMode` entries would:
- Expand a closed union of display strings from 10 to 18 across ~85 comparison sites, 23 of them in
  Angular templates where the compiler cannot catch a typo — on a feature nobody can execute once
  before shipping.
- Leak seven pseudo-models into `MODEL_CONFIGS`, which has 13 non-config importers, two of which
  build the **gallery and admin model-filter dropdowns** — so they would appear for every user in
  every deployment, including the ~all with VPE off.
- Route Upscale through `applyRemixState`, which opens with an unconditional `resetInputs()` —
  clicking "upscale this finished clip" would **destroy the prompt the user was writing**.
- Turn the mode menu into a catalogue of what this customer cannot do (7 of 8 landscape-only).

### Phase 1 surface — concretely

One promoted button, first and primary in the lightbox rail (`media-lightbox.component.html`, in the
`showOmni` slot), reading **"Upscale to 4K"**. Reachable from exactly the two places asked for: the
media library (`media-detail`) and immediately after generation (`video.component`'s result
lightbox) — the only two hosts that already bind lightbox action outputs.

Mechanics, chosen deliberately over the obvious alternatives:
- **Host opt-in `@Input() showUpscaleButton = false`**, not another `return true` getter. The
  existing `showOmni` is hardcoded `true`, which is why Extend/Concatenate render dead in 4 of 6
  lightbox hosts. Do not ship a fifth dead button — least of all one that spends money.
- **Never gate visibility on metadata.** Open the dialog always; it POSTs a **server ffprobe
  preflight on open** and resolves green/amber/red in ~1s. This is load-bearing (see §6b).
- **Never route through `applyRemixState`.** The handler opens a dialog; it does not touch the
  composer.
- Say the customer's format out loud: the target card reads **"4K vertical · 2160×3840"** for a
  portrait source.
- Per-capability server gating (`GET /api/options/capabilities`), not one `vpeEnabled` boolean, so a
  deployment with only the Upscaler collapses to a single button and renders no menu at all.

**Gating rule** (reconciles with commit `ce58363`, which deliberately rejected hiding capabilities):
*deployment cannot do this* → **absent** (don't advertise). *This asset fails preflight* → **visible,
disabled, with the measured reason and the remedy** ("9.0s — trim to 8s"). The earlier fix's point
was that silently removing entries is the wrong remedy; that applies to the per-asset case, not the
per-deployment one.

**Note on SSR:** SSR is enabled (`main.server.ts`, `provideClientHydration`) and there is no
`APP_INITIALIZER` anywhere in the app. A capability flag fed through an initializer into a
synchronous getter will read `false` through hydration. Use an observable + guard, or transfer-state,
and fail closed.

### 6b. Corrections the design pass turned up — all verified against the code

1. **`MediaItem.duration` is a phantom field.** Backend `duration_seconds` serialises as
   `durationSeconds`; `mapUnifiedItem` reads `item.duration`. Neither is ever populated, and
   `duration_seconds` is not in the `unified_gallery_view` whitelist. `resolution` is never written
   for videos at all. Worse, `aspect_ratio` and `duration_seconds` are **copied off the request DTO**,
   not measured — which is exactly the bug class fixed in `2fd1506` (*"stop the settings chips
   disagreeing with what is sent"*). **Any client-side duration/resolution gate is impossible today.**
   Orientation via `aspectRatio` is the only reliable client signal; everything else must be server
   ffprobe.
2. **Audio was missed by every proposal.** Every clip this app produces carries an AAC track
   (verified), and the VPE output directory contains a *separate* `sample_0_audio.wav`. If the
   preview has no audio stream and that stem exists, it must be ffmpeg-muxed before writing
   `gcs_uris[0]` — otherwise the "4K master" of a dialogue-driven telenovela plays **silent**.
3. **`gcs_uris` must only ever hold the H.264 preview.** `MimeTypeEnum` has 8 members with no
   `video/quicktime`, `video/mxf` or EXR, and `BaseRepository` runs `model_validate` on every read —
   a row written with a ProRes mime type becomes **unreadable through the repository**. Frames never
   enter `gcs_uris`; that array means "N sibling clips" end-to-end. The directory goes in a
   `raw_data.vpe` manifest.
4. **Backend job-state gaps that will strand VPE jobs**: status is written exactly once, so "running"
   and "the worker died" are indistinguishable; `operation.name` is only logged, never persisted; and
   the admin reaper is an unconditional `PROCESSING AND created_at < 1h → STOPPED` with no exemption
   — which will mislabel legitimately slow VPE jobs. `STOPPED` is also not terminal in the frontend
   poller, so a reaped job polls forever. Persist `operation_name`, write intermediate `phase`, and
   exempt operation-bearing rows.
5. **Bulk upscale is a money bug as designed.** Gallery multi-select is a `Set<"type:id">` that
   **discards the clip index**, while `numberOfMedia` defaults to 4. Selecting 40 cards is between 40
   and 160 paid 4K jobs, and the selection model cannot express which clip was meant. Either expand
   selections into individual clips at confirmation, or don't ship bulk in v1.
6. **`showOmni` is hardcoded `return true`**, so "Edit with Omni" will render on VPE outputs that
   have no `raw_data.interactions` to replay. Pre-existing latent bug that VPE makes user-facing.

### 6c. Two capabilities have free reuse nobody would guess

- **Dialogue-driven generation**: the audio-reference slot **already exists** in `flow-prompt-box`
  and is already gated on `capabilities.supportsAudioReference` — a flag no current video model sets.
  VPE a2v would be its first real consumer. Nearly free.
- **TTS → lip-sync is a complete in-app chain today.** The app already generates Spanish speech
  (`gemini-2.5-flash-tts`, `chirp_3`, both with voice + language support). TTS → dialogue-driven is
  the strongest demo this integration has, and both halves already exist.

### 6d. Missing primitives worth building regardless of VPE

- **Extract a frame from a clip.** Does not exist anywhere, yet the customer is *already doing it
  manually* (`_review/micronovela-shots/frames/*_t3.2.png`). Three VPE capabilities need it. The
  lightbox already renders `<video controls>` and tracks `currentTime`; grab to canvas → upload via
  the existing asset path is ~40 lines and unlocks dialogue-driven, keyframe conditioning and
  shot continuity.
- **`/workbench` is the app's real timeline editor and no design used it.** Its `/workbench/render`
  returns a **Blob download that never becomes a MediaItem** — so the assembled cut, the one asset
  most worth mastering at 4K, is the one asset that cannot be upscaled. Giving render a
  "save to gallery" path fixes that and makes a "Finishing Room" unnecessary.
- **`ImageCropperDialogComponent` already exists** with 16:9 / 9:16 presets and already fires on
  every image upload. It is the crop-don't-pad answer to the pillarbox trap, and no proposal cited it.

## 6e. Live results - measured, not inferred

First run against an allowlisted project. Each line below replaces a guess with a fact, and two of
them overturn what this document previously assumed.

| Case | Result | What it settles |
|---|---|---|
| `upscale` | SUCCESS, 315s -> **3840x2160** | The Phase 1 capability works. |
| `upscale_portrait` | SUCCESS, 314s -> **2160x3840** | **Genuine vertical 4K**, not a rotated or padded landscape frame. |
| `omni_cine` | SUCCESS, 193s | The output really is a directory: 192 PNG frames, a separate WAV, an MP4 preview. |
| `omni_cine_portrait` | **REJECTED in 10s** | `Input video must be exactly 1280x720, but got 720x1280`. |
| `video_transform` | **SUCCESS, 194s** on real footage | The capability works; the earlier filter was the fixture - see below. |
| `video_transform_masked` | RAI filtered, 193s | `Recitation check failed` on the same fixture, not yet retried. |

**Vertical 4K is real, so the Phase 1 pipeline works end to end.** Generate 9:16 at 720p with Omni,
upscale to 2160x3840. The whole vertical case rested on that assumption and it now has a measured
answer rather than a documented one.

**Omni-Cine is landscape only, and the service says so itself.** §3.1 filed it as tier three,
"unstated", and warned the failure would be silent pillarboxing on a paid job. It is not: the
request is rejected outright in ten seconds, naming the exact expected dimensions. That is the good
failure mode, and preflight can now state the rule rather than hedge. This does **not** settle the
same question for dialogue-driven or video textures - those take a still rather than a video and are
documented as "resized and padded internally", so the silent-pillarbox hazard may still apply there
and needs its own probe.

**The audio stem is real, so the silent-master risk in §6b is not theoretical.** `omni_cine`
returned a WAV beside the preview. Ingesting only the preview, for a capability whose output carries
a separate audio track, yields a 4K master that plays silent.

**The RAI filtering was our fixture, not the capability - retried and confirmed.** Both transform
variants first filtered on `Recitation check failed` while every other case using the same generated
media passed. The hypothesis was that video transform is a structure-preserving restyle and the
fixture was an ffmpeg `testsrc` card - canonical colour bars plus a seven-segment counter. Asked to
restyle that while holding structure, the output is a near-verbatim reproduction of one of the most
reproduced images there is, which is what a recitation check exists to catch.

Re-running `video_transform` against a real 720p clip returned **SUCCESS in 193.9s with
`raiMediaFilteredCount: 0`**. So the capability is viable and the capability matrix changes: video
transform moves from "blocked by RAI" to "works, subject to the usual content policy". Two things
follow. **The smoke fixture is misleading for any structure-preserving capability** and should use
real footage rather than a test card. And **`video_transform_masked` has not been retried** - it
filtered on the same card, so the same explanation almost certainly covers it, but that is an
inference and it is written here as one.

**Timing sets a floor for the UX.** Roughly 315s for a 4K upscale, 193s for Omni-Cine. Phase 1
cannot be a dialog with a spinner; it needs a real background job with progress, which in turn needs
the intermediate phase writes described in §6b.

## 7. Open questions to resolve before Phase 1 code

1. ~~**Orientation (§3.1).**~~ **Answered for the two that matter.** The upscaler produces genuine
   2160x3840 vertical; Omni-Cine rejects portrait outright. Still open for dialogue-driven and video
   textures, which take stills and may pad silently. What remains is the commercial question: is
   TU's slate vertical or landscape, and therefore is anything past Phase 1 worth building?
2. **Allowlisted project vs. deployment project.** VPE requires input and output buckets in the
   allowlisted project. Is the intended allowlisted project the same one Creative Studio is
   deployed into? If not, the app needs a separate VPE bucket and a copy step.
3. **Who has allowlist access, and when?** Determines when Phase 1 can be validated and therefore
   when Phase 2 can responsibly start.
4. **Is there a real EXR/ProRes finishing pipeline downstream?** If not, Omni-Cine's main advantage
   is unused and it should stay at the back of the queue.
5. **Upstream or fork-only?** This is confidential, pre-GA, allowlist-gated functionality. It
   probably should not go to `GoogleCloudPlatform/gcc-creative-studio` in the open until the
   program says so — which argues for keeping it on a branch in the fork, feature-flagged, until
   authorised.

## 6f. Doc review of `preflight.py` and `client.py`

These two modules were the only ones never checked line-by-line against the documentation. The
earlier review pass had a reviewer-matching bug that handed all three reviewers the same
`payloads.py` report, so 2,700 lines went unread. This closes that gap. Both findings below were
reproduced by running the code, not inferred from reading it.

### `client.py` — no defects found

Checked against the user guide's API contract: the shared publisher endpoint, `predictLongRunning`
/ `fetchPredictOperation`, the required `X-Vertex-AI-LLM-Request-Type: shared` header, the
`done: true` + `error` envelope, and the eight documented failure bodies. All present and correct.

Two things worth recording as verified rather than assumed:

* Every documented error is represented in `errors.py`, and the classifier matches on message text
  before status code — correct, because duration, frame rate, aspect ratio and frame size
  rejections all arrive as a bare `code: 3` and only the text distinguishes them.
* The 1800s default timeout clears the slowest observed live job (315s) by a wide margin.

One judgement call is undocumented and should be: a `done: true` failure raises out of the polling
loop rather than being retried, including `VpeServiceOverloadedError`, which is marked `retryable`.
That is right — a finished-and-failed operation cannot be re-polled into success, so retrying means
submitting a new job — but nothing says so, and `retryable` on an error the poller never retries
invites the opposite reading.

### `preflight.py` — one real gap

**A still sent to omni-cine is under-checked.** `validate` infers the slot from the file's kind,
and justifies it in its own docstring by claiming the still slots of a capability share one set of
constraints. That is false for omni-cine, which takes stills in two roles: reference images, at
**any resolution**, and frames of a PNG or EXR input sequence, which the input table requires to be
**1280x720**. Both probe as `IMAGE` and both are checked against the reference-image constraints.

Confirmed by running it — a 3000x2000 PNG returns zero findings against `omni_cine`:

```
--- offspec_frame.png vs omni_cine: blocking=0
      (no findings at all)
```

The byte cap is 30 MiB in both roles, so only resolution goes unchecked. The fix is to let a caller
say which slot it means instead of guessing from the file; that changes `validate`'s signature, so
it belongs with the omni-cine work rather than as a drive-by. The docstring no longer claims
otherwise. **No impact on Phase 1**, which is the upscaler and sends no reference images.

### Not a code defect: the upscaler's mime type list contradicts itself

The Upscaler page states the input may be *"ProRes, DNxHR, H264 (video/mp4), or PNG frames"*, then
states of the `mimeType` field: *"The following mime type is accepted: video/mp4."* Those cannot
both be true — a ProRes `.mov` has no legal value to declare. The registry accepts
`video/quicktime` and `application/mxf` as well, and a ProRes file passes preflight clean.

The registry is probably right: the Professional formats page says the pipeline detects the format
*"based on the input container metadata or the specified mimeType"* and lists ProRes and DNxHR as
accepted upscaler input, which reads as the more specific and more current statement. Left as is
deliberately — warning on every ProRes job on the strength of one abbreviated sentence would be
noise. **Worth raising with the VPE team**, along with the same page's `resolution` parameter, whose
documented default `720p` is not among its own accepted values (`1080p`, `4k`).

Note the codebase already handles this exact shape for audio: `_check_audio` warns that AAC is named
as accepted while no AAC mime type is listed as declarable. Video gets no equivalent warning. If the
VPE team confirms the upscaler page is stale, that asymmetry is fine; if they confirm `video/mp4` is
genuinely the only accepted value, the registry is wrong and preflight should block.

## 6g. The Phase 1 scope question — settled: split-and-rejoin works

The upscaler accepts **4 to 8 seconds**. Of the customer's 16 measured shots, **4 fit**. A plain
"Upscale to 4K" button would therefore be greyed out on 12 of 16 assets, which is not a feature
anyone would call shipped.

There are two ways out and only one of them is useful:

- **Segment selection** — let the user pick an 8 second window of a 10 second shot. Trivial to
  build, and it hands the customer 8 seconds of a shot they need 10 seconds of. Useless for
  delivery.
- **Split and rejoin** — cut 240 frames into 2x120, upscale each as its own job, concatenate. This
  is what they actually need, and it has one real risk: the two halves are **independent jobs**, and
  nothing documented promises deterministic grain, sharpening or face texture between two of them.
  If they do not match, the join is a visible pop and split-and-rejoin is not shippable.

That risk is measurable, so it is being measured rather than argued about.

**`backend/scripts/vpe_seam_check.py`** cuts a source into balanced upscalable segments, upscales
each through `build_payload` and `VpeClient` — the shipped path, not a bespoke one — concatenates by
stream copy so no re-encode can mask or invent a seam, and then measures the join. The measurement
is a ratio, not an absolute: `tblend=all_mode=difference,signalstats` gives a per-transition average
luma delta, frames inside a segment establish what a normal step looks like for this footage, and
the step across the seam is read against that distribution. Under 1.5x the p95 is invisible; over 4x
is a pop.

The detector was validated against synthetic joins before being trusted with a paid job — a
deliberately mismatched pair (brightness +0.10) reads **VISIBLE at 9.08x p95**, a mild 0.02 shift
reads **INVISIBLE at 1.05x**, and two identical halves read **INVISIBLE at 0.72x**. Worth recording
why: the first version of `_analyse` reported an identical clean verdict for the mismatched pair and
the control, which is what exposed the bug. `tblend` emits one value per *transition*, so a 240
frame clip yields 239 deltas and the seam at frame 120 sits at index **119**. Reading index 120 read
an ordinary step inside the second segment, and would have returned "no visible seam" for any
footage whatsoever — a green result that meant nothing, on the strength of which Phase 1's scope
would have been decided.

    python -m scripts.vpe_seam_check --project P --bucket gs://B \
        --source shot.mp4 --out seam_report

Two upscale jobs, roughly 5 minutes in parallel. `--dry-run` exercises the split and the analysis
wiring without calling VPE and needs no allowlist. Exit code is 0 only on INVISIBLE.

**What the answer decides.** INVISIBLE and Phase 1 covers all 16 shots with a split-and-rejoin path
behind the same button. VISIBLE and the honest answer to the customer is that the upscaler is for
4-8 second shots, Phase 1 covers 4 of 16, and the gap goes to the VPE programme as a product
request rather than being papered over in the UI.

### The answer: INVISIBLE, at 0.34x the p95

Run against a real 10 second 16:9 shot, split into two 120 frame halves and upscaled as two
independent jobs. Results are checked in under `backend/seam_report/`.

| Measure | Value |
|---|---|
| Verdict | **INVISIBLE** |
| Seam delta at frame 120 | 7.4825 |
| Baseline median | 8.122 |
| Baseline p95 | 21.7286 (238 transitions) |
| Seam as a multiple of p95 | **0.34x** |

The number to notice is not the ratio but the comparison with the median: **the step across the join
is smaller than a typical frame-to-frame step in this footage.** The seam is not merely inside the
normal spread, it is below the middle of it. There is nothing marginal about this result.

Confirmed visually as well as numerically, because the two are not the same claim. The detector
measures average luma difference, which is sensitive to an exposure or brightness pop and much less
sensitive to a change in grain or sharpening that leaves average brightness alone. Inspecting the
two 3840x2160 frames either side of the join at native resolution — a static horizon and water
region, where a texture mismatch would be obvious and motion would not disguise it — the detail
level, edge sharpness and noise character are consistent across the join. Motion advances; the
rendering does not change.

**So Phase 1 covers all 16 shots, not 4.** The upscale button splits anything over 8 seconds,
upscales the pieces and rejoins them, and the customer gets a full-length 4K master rather than a
truncated one. The output was also confirmed as genuine 3840x2160.

### The 9:16 case, confirmed the same way

Run again against a real 10 second **9:16** shot (720x1280 to 2160x3840), split into two 120 frame
halves and upscaled as two independent jobs, same as the 16:9 run above.

| Measure | Value |
|---|---|
| Verdict | **INVISIBLE** |
| Seam delta at frame 120 | 12.4561 |
| Baseline median | 12.011 |
| Baseline p95 | 19.9789 (238 transitions) |
| Seam as a multiple of p95 | **0.62x** |

Output confirmed as genuine 2160x3840 at exactly 240 frames, 24 fps. This is the orientation this
customer's slate actually needs, so split-and-rejoin is no longer only proven for 16:9 — the
vertical case now has the same measured answer.

**What still has not been established.** A shot long enough to need three or more segments and
therefore two or more seams, and footage whose baseline variation is much lower than either run
above — a locked-off dialogue two-shot has far less frame-to-frame motion, so the same absolute
mismatch would read as a much larger multiple of a much smaller p95. **A near-static shot is the
adversarial case and it has not been run**: a repeat attempt measured the same source clip as the
original 16:9 run above rather than genuinely low-motion footage, so this gap is still open. The
script exists and is cheap to re-run against the right source, so this is a gap to close rather than
a risk to carry silently.

### Consequence: the job count doubles, and the executor cannot take it

Splitting means **two VPE jobs per shot**, so the customer's 16 shots become **32 jobs of ~315s
each**. That runs headlong into `main.py`, which creates a single `ThreadPoolExecutor(max_workers=4)`
shared by every background job in the application, and into the existing long-job pattern in
`veo_service.py`, which polls `while not operation.done` and therefore **pins one of those four
threads for the entire job**.

Four upscales is enough to consume the app's whole background capacity. Thirty-two of them is
roughly **42 minutes during which no Veo generation, no concatenation and no workflow can start.**
This is a pre-existing ceiling rather than something Phase 1 introduces, but Phase 1 is the first
feature that gives a user an obvious reason to queue sixteen five-minute jobs at once.

**Minimum fix for Phase 1: a separate executor for VPE.** A second pool of 2-3 workers, so VPE
saturation degrades VPE and leaves the rest of the app responsive. The correct fix is a durable job
queue with resumable polling, which is real work and does not belong in Phase 1.

## 6h. Phase 1 run end to end, through the app - measured

§6e ran the capabilities directly against the API. This ran **the feature**: a gallery row, through
`VpeService.start_upscale_job`, the screening gate, the split, both segments, the rejoin, the audio
restore and the write-back. Everything up to this point was either a direct API call or an argument
about code. This is the first evidence that the pipeline the app actually contains works.

| | |
|---|---|
| Source | 720x1280, 24 fps, 240 frames, AAC - a real 10s vertical shot |
| Plan | 2 segments of 120 frames, submitted 1.6s apart |
| Wall clock | **5m31s** |
| Row | `status=completed`, `error_message=None` |
| Master | **2160x3840**, 24 fps, 240 frames, AAC 10.005s |

**Concurrency is real, not designed-for.** Two segments at ~315s each ran in 5m31s. Sequential would
have been ~10.5 minutes. The submit-all-then-wait ordering in the worker does what §6g claimed it
would.

**Nothing was lost in the round trip.** 240 frames in, 240 frames out; the audio is the original
track at its original length. The frame count surviving the split, the upscale and the rejoin is the
thing the whole split-and-rejoin argument rests on, and it is now measured rather than reasoned.

**This clip turned out to be the adversarial case §6g was asking for**, which was not the intent
when it was seeded - see §6i.

### What auditing the path before this run turned up

Three defects, all found before spending anything, all fixed and covered by tests:

- **`probe_media` read the coded frame size and ignored the display matrix.** ffmpeg autorotates on
  decode, so a phone clip coded 1920x1080 with a quarter turn is cut, uploaded and billed as
  1080x1920. The old measurement validated a geometry that was never sent and declared an aspect
  ratio the delivered pixels contradict - which the upscaler rejects outright.
- **`cut_segment` trusted ffmpeg's exit code.** ffmpeg exits zero having written whatever it could.
  The planner divides up the container's declared length while `select` keeps the frames the file
  presents, and an mp4 edit list makes those disagree - a 240 frame container holding 216 plans as
  two halves and delivers a second half of 96, under the API minimum, after the first has been paid
  for. It now counts what it wrote, and every cut happens before the first upload.
- **`check_upscalable` refused any source that was not H.264 in MP4.** Every segment is re-encoded
  on the way out, so the API never sees the source codec; a ProRes master was turned into an error
  by a rule that did not apply to it.

The first two are only reachable with a source this run did not have - a turned phone clip, a
trimmed master. Neither would have been caught by running the happy path again, which is the
argument for auditing the path rather than only widening the test corpus.

### Still blocking a real deployment

The run used a local backend against an allowlisted project. The deployed backend runs as a
different service account in `nflx-media-analysis`, and **whether that identity is allowlisted on
the VPE project is unverified**. That is the deployment blocker, not anything in this pipeline.

## 6i. The low-motion seam gap - closed, and the seam is real

§6g left one thing open: every seam run so far had been on ordinary-motion footage, and the
prediction was that quiet footage would read much worse because the same absolute mismatch is a
larger multiple of a smaller p95. The clip seeded for the live run in §6h turned out to *be* that
case - a seated figure dabbing his face, whose frame-to-frame variation is about **twelve times
lower** than either earlier run.

| Run | Baseline p95 | Seam delta | x p95 | Verdict |
|---|---|---|---|---|
| 16:9 standalone | 21.73 | 7.48 | 0.34x | INVISIBLE |
| 9:16 standalone | 19.98 | 12.46 | 0.62x | INVISIBLE |
| **9:16, live app pipeline** | **1.72** | **2.05** | **1.19x** | **INVISIBLE** |

Still inside the threshold, and the two frames either side of the join are continuous to the eye -
same pose, lighting and exposure, no pop. But the headroom is much thinner than the earlier runs
implied, and one number says it better than the verdict does: **the seam is the second largest
frame-to-frame step in the whole clip**, out of 239. Only 0.4% of transitions exceed it.

### The source control: the seam is caused by the split, not by the footage

The baseline above is measured on the upscaled clip, so on its own it cannot separate *"the upscale
introduced a discontinuity at the join"* from *"the cut happened to land on the busiest moment in the
shot"*. Measuring the 720x1280 original the same way settles it. Frame 120 is an arbitrary midpoint,
and in the source it looks like one:

| | Source 720x1280 | Master 2160x3840 |
|---|---|---|
| Step at the cut (t119) | 1.086 | 2.049 |
| Rank among 239 transitions | **68th** | **2nd** |
| Share of transitions exceeding it | 28% | 0.4% |

Normalising each transition to its own clip's median and asking what the upscale did to it makes the
mechanism explicit. **Ordinary transitions are left alone** - median factor 0.985, p95 1.313, and
most are nudged slightly *down*, which is what an upscale's smoothing should do. **The join is
amplified 1.62x**, the 3rd largest amplification of any transition and 1.64x what a typical
transition receives. Its immediate neighbours are untouched (t114 0.88 -> 0.80, t118 1.15 -> 1.04,
t120 1.54 -> 1.56).

So the seam is real, it is attributable to the two segments being independent jobs, and it is now
measured rather than inferred. It is simply small.

### Two things this rules out

**A sustained texture mismatch between halves.** Independently upscaled segments could differ in
grain or sharpness for their whole length, which would be far more visible than a single-frame pop
and which the seam metric - reading exactly one transition - would miss completely. It is not
happening: segment 1's mean step is 0.79x segment 0's, and per-block means oscillate with the action
(0.72, 1.54, 0.86, 1.37, 0.67) with no step at the join.

**A worse cut point on this footage.** Had the join landed on the busiest transition in the source
(t47) and been amplified the same 1.62x, the resulting step would be roughly 2.16x p95 - the
MARGINAL band, still well under the 4.0x that reads as VISIBLE. On this clip no choice of cut point
produces a visible seam.

### What is still open, and one idea worth costing

A shot needing **three or more segments**, and therefore two or more seams, is still unrun. So is
footage quieter than this one - a locked-off tripod shot with no character movement at all. The
amplification factor is a single measurement from a single clip, so it should not be extrapolated
far: what is established is the mechanism and its size here, not a law.

The mechanism does suggest a cheap improvement. `plan_segments` balances the halves, which puts the
join at the midpoint for reasons that have nothing to do with the picture. The constraint is only
that each segment stays within 96-192 frames, so for a 240 frame clip the cut may fall anywhere from
96 to 144 - a 48 frame window. **Choosing the quietest transition in that window would put the join
where a 1.6x amplification is least visible**, at no extra cost in jobs or time. That is a real
option this data opens up rather than something to build now, and it belongs in the same discussion
as the three-segment case.

## 6j. Phase 1 frontend tests - the last untested surface

Every audit so far had been on the backend, where the VPE modules carry ~1000 tests. The frontend
VPE surface had **none**: no spec existed for the service, the dialog, or the gallery entry point.
That is the whole of the code a user actually touches.

Closed with 41 specs across three files:

| File | Tests | |
|---|---|---|
| `frontend/src/app/services/vpe/vpe.service.spec.ts` | 19 | new |
| `frontend/src/app/common/components/vpe-upscale-dialog/vpe-upscale-dialog.component.spec.ts` | 12 | new |
| `frontend/src/app/gallery/media-detail/media-detail.component.spec.ts` | 10 | rewritten |

Suite went 97 passing / 21 failing to **138 passing / 20 failing**; `gts lint` stays at 0 errors and
the warning count is unchanged, so the new files add none. The 20 remaining failures are all
pre-existing and none are VPE - stale `ng generate` scaffolds missing `HttpClient`, `MatDialogRef`
or `ActivatedRoute` providers, two Material `NG0304` template errors, and three `HomeComponent`
assertion drifts. `media-detail` was the 21st and its rewrite cleared it.

What the tests actually pin down, beyond construction: the polling lifecycle (first poll at 5s, 15s
cadence after, stops on each of COMPLETED / FAILED / STOPPED, stops on clear, gives up after a 500,
and a second `startUpscale` replaces the first subscription rather than running two in parallel);
the dialog's double-click guard, retry-after-failure and warning filter; and the three screening
guards in media-detail - image, missing mime type and source asset are all skipped, and a screening
failure leaves the action unoffered rather than raising a snackbar.

**One deliberate testing constraint, recorded because it is not obvious.** The snackbar helpers in
`utils/handleMessageSnackbar.ts` do not use the injected `MatSnackBar` at all - they resolve
`NotificationService` through the module-global `AppInjector`, which the first spec to call
`setAppInjector` owns for the entire karma run (today that is `login.component.spec.ts`). Asserting
on notifications would therefore make these specs order-dependent. They assert instead on the
`console.error` that `handleErrorSnackbar` emits unconditionally before it notifies, which is the
same signal `search.service.spec.ts` already relies on.

### A gating gap the tests surfaced

`§6a` requires that the UI not advertise what the deployment cannot do. It does, slightly. The
button renders on `*ngIf="isVideo && showUpscaleButton"` with `showUpscaleButton` hardcoded `true`
from `media-detail.component.html`; only the *disabled* state consults the screening. So with
`VPE_ENABLED=false` the screening call fails, `vpeScreening` stays `undefined`, and the user gets a
permanently disabled **Upscale** button tooltipped *"Checking eligibility..."* - a transient-sounding
message that never resolves. Harmless, but it both advertises the feature and misstates why it is
unavailable. The cheap fix is a distinct absent state for "screening failed" rather than the
capabilities endpoint originally proposed.

## 8. Phase 1 exit status and the Phase 2 gate

### Where Phase 1 actually stands

Measured against the `§6` work items rather than impression:

| Item | State |
|---|---|
| Backend modules (`client`, `capabilities`, `preflight`, `payloads`, + `errors`, `gating`, `media_ops`, `segmentation`) | done |
| VPE request DTO, output ingestion, `VPE_ENABLED` / `VPE_LOCATION` / `VPE_DRY_RUN` | done |
| Separate VPE executor (`§6g`) | done |
| Frontend upscale action + options dialog | done |
| Backend tests / frontend tests | 1007 / 41 |
| End-to-end run through the app | done, `§6h` |
| Seam question | settled, `§6g` + `§6i` |
| Service-agent dance for `vertex-bp` / `vertex-tune` | done - `scripts/vpe_bootstrap_project.sh`, `§8a` |
| `UPGRADING.md` VPE section | done - `§8a` |
| Deployed runtime SA allowlisted on the VPE project | **unverified** - the deployment blocker of `§6h` |

So Phase 1 is **code-complete, proven on an allowlisted project, and now documented for a
deployer**. One row remains open, and it is the one that was never ours: the allowlist itself.

### `§8a` Closing the deployment gap

Two of the three open rows above were closed together, because they are the same omission seen
from two sides - nothing in the repo told a deployer what to prepare, and nothing prepared it.

**`backend/scripts/vpe_bootstrap_project.sh`.** Deliberately not folded into `bootstrap.sh`:
that script prepares the project Creative Studio is *deployed into*, and the allowlisted VPE
project is frequently a different one. It enables the APIs, creates or verifies the I/O bucket,
provisions the service agents, grants all three `roles/storage.admin`, grants the deployed
runtime SA, and prints the settings filled in. Idempotent, with `--dry-run`.

Two findings worth recording, because both shape what the script can promise:

- **The bucket-ownership check is a hard failure, not a warning.** A bucket in the deployment
  project rather than the allowlisted one is the cross-project mistake `§7.2` predicts, and it
  fails at job time - minutes in, after a download and a cut - rather than at startup. Catching
  it during setup is the only cheap place to catch it.
- **VPE's outputs are never cleaned up, and nobody had noticed.** `_discard_segments` deletes the
  input segments the worker uploaded, on success only - but the upscaled output VPE wrote under
  `vpe/upscale/<id>/out_NN/` is left behind, and that is the 4K half. It accumulates for the life
  of the deployment. The script therefore offers `--lifecycle-days N`, scoped to the
  `vpe/upscale/` prefix so it is safe on a bucket shared with app media, and it refuses to
  replace an existing lifecycle policy rather than silently discarding one. This is a bound, not
  a fix; making the worker clean up after itself is the better answer and is not done.
- **`gcp-sa-vertex-tune` cannot be automated.** There is no non-interactive gcloud surface for
  starting a tuning job, so the start-and-cancel dance works for `vertex-bp` and not for
  `vertex-tune`. The script detects the gap, prints the Console path, completes everything else,
  and exits reporting what is still missing rather than pretending to have finished. The
  batch-prediction arm is also best-effort: the agent is created on job *submission*, so a job
  rejected downstream still provisions it, which is why a failed submission warns rather than
  aborts.

Verified against stubbed-gcloud scenarios rather than only read: a bare project, a fully
prepared one (no changes, correct idempotent output), a bucket owned by another project (exits
1 with the diagnosis), the full `--dry-run`, all four `--lifecycle-days` paths, and the emitted
lifecycle JSON parsed back to confirm it is valid. Two dry-run reporting bugs surfaced that way -
call-site `>/dev/null` swallowing the `would run` line so a grant it never performed reported as
done, and all three agents printing an identical `service-<number>` prefix instead of their
distinguishing names.

**The single-project case.** The allowlisted project is often, but not always, a second project;
where the app is deployed into the allowlisted project itself, `VPE_PROJECT_ID` simply names it
and the worker's two storage clients resolve into one project. Nothing breaks: VPE traffic stays
under `vpe/upscale/`, the master goes to `upscaled_videos/`, and cleanup only ever touches URIs
it uploaded. A **dedicated bucket is still the right call**, for blast radius rather than
correctness - the three service agents need `roles/storage.admin` on whatever `VPE_BUCKET` names,
which on a shared media bucket is delete rights over every user's media, and a prefix-scoped
lifecycle rule is easier to reason about on a bucket that holds nothing else.

**`UPGRADING.md`.** A VPE section covering the no-migration finding, the per-project allowlist,
the five settings with the two that ship empty on purpose, the cross-project bucket requirement,
the bootstrap step, per-segment billing, and rollback by setting `VPE_ENABLED=false`.

What this does **not** close: the allowlist itself. IAM is necessary and not sufficient, and the
failure mode when a project is not allowlisted looks like an ordinary permission error - so the
script says so explicitly rather than leaving a deployer to debug IAM that is already correct.

### The Phase 2 gate, and why it should be measured before any Phase 2 code

`§4` gates Phase 2 on resolving `§3.1`, and `§7.1` sharpens it to the commercial question: is the
slate vertical, and therefore is anything past Phase 1 worth building? The capability registry
already carries a doc-derived answer, and it is unfavourable.

`veo-exp-a2v-generation` is recorded as `FIXED_FRAME_UNSTATED`: a 1280x720 output frame with **no
`aspectRatio` parameter at all**. Off-spec stills are, per the doc, "resized and padded internally"
rather than rejected - so a 720x1280 portrait still comes back **pillarboxed into landscape, leaving
roughly 405x720 of real subject pixels, with no error raised**. For a 9:16 micronovela slate that is
close to a non-feature, and it fails silently rather than loudly, which is worse.

A second constraint deserves recording because it shapes the product, not just the code: **input
audio must be exactly 8.0 seconds**. Longer tracks are truncated, shorter ones padded with silence,
and the audio is converted internally to 2-channel 48 kHz. Dialogue beats therefore have to be cut
to an exact 8s, or the capability silently clips a performance mid-word. Output is capped at 192
frames of 720p.

**All of that is read off the documentation, not measured.** The seam work is the precedent for why
that distinction matters: the doc-derived expectation there was wrong in both directions. One live
call settles it, and there are only three outcomes:

1. **Rejected** with a clean error - Phase 2 is landscape-only, and that is a straightforward
   over-restriction report for the VPE programme.
2. **Pillarboxed 1280x720** - the documented behaviour is real, Phase 2 delivers ~405x720 of subject
   for a vertical slate, and it drops down the queue behind Phase 3.
3. **Genuine 720x1280 out** - the docs understate the model, and Phase 2 is worth building now.

Only outcome 3 justifies the service path, and the difference between them is one generation.

### Closing the gate: `scripts/vpe_a2v_orientation.py`

Written and dry-run verified; it needs an allowlisted project to produce the answer.

    python -m scripts.vpe_a2v_orientation --project P --bucket gs://B \
        --audio line.wav --landscape still_16x9.png --portrait still_9x16.png

It submits a **matched pair** - identical audio and prompt, one 16:9 still and one 9:16 still - and
the landscape arm is the control. Without it a refusal cannot be attributed: it might be the
portrait frame, or the audio, the prompt, the bucket or the allowlist. Only if the control succeeds
and the portrait arm does not is orientation the cause. The verdict is derived rather than eyeballed,
and where the portrait arm comes back landscape the script computes how much of the frame is
actually subject.

Two guards run before anything is billed. The audio must be within 50ms of **8.0s**, because an
off-length track is silently truncated or silence-padded and would confound a run that is supposed
to be measuring orientation alone; and the two stills must genuinely be the orientations they are
passed as. Both exit 2 rather than proceeding.

The payload goes through the shipped `build_payload`, so a green dry run says the *service's* path
accepts these inputs rather than that the script's own idea of them is well-formed. `--dry-run`
builds and validates both arms with no allowlist and no calls.

### The actual Phase 2 delta, once the gate clears

Smaller than Phase 1, because the generic layer is already capability-agnostic. `client.py`,
`payloads.py`, `preflight.py` and all eight capability definitions need nothing. What is
upscale-specific, and therefore what Phase 2 has to add alongside:

- `vpe_service.py` - `check_upscalable`, `plan_upscale`, `build_segment_request` and
  `_process_vpe_upscale_in_background` are all written against `VpeCapabilityId.UPSCALE`.
- `vpe_controller.py` - two routes, both upscale.
- A dialogue-driven preflight: exactly-8s audio, 1280x720 still, and the WAV/MP3/M4A/AAC mime set.
- Frontend: unlike the upscaler this is *not* a post-process on an existing clip, so `§6a`'s
  "post-process actions on assets, never new modes" rule does not decide the surface for it. That
  question is open and should be settled before the UI is built.

Note that the 8s audio rule and the 1280x720 still check are worth building **regardless of which
outcome the probe returns**, since they are required under all three.

## 8b. Phase 2 Gate Resolution & Live Measurement Findings

The live matched-pair probe (`scripts.vpe_a2v_orientation.py`) and live test generations using
real production assets (Asset #44: `cantina_vertical_rise.mp4`) were executed against Google Vertex AI
(`veo-experimental`).

### Live Probe Verdict: `REJECTED` on Raw Portrait / Landscape-Only Wire Format

| Arm | Input Still | Output Video | Time | Status / Result |
|---|---|---|---|---|
| **Landscape Control** | `1280x720` PNG | `1280x720` MP4 | 253.8s | **SUCCESS** — 192 frames (8.0s), 24 fps, synchronized audio |
| **Raw Portrait Test** | `720x1280` PNG | None | 675.1s | **REFUSED** — Deadline exceeded / timeout on raw 9:16 input |

**Conclusion**: As documented, `veo-exp-a2v-generation` strictly expects a `1280x720` (16:9) frame and
carries no native `aspectRatio` wire parameter.

---

### The Two Solutions for Full Portrait & Cinematic Workflows

#### 1. Automated Pre-Pad & Post-Crop Pipeline (Pure 9:16 Vertical Video)
To deliver true 9:16 vertical videos for micronovela and social mobile formats without API refusals:
1. **Pre-Pad**: The 9:16 portrait still (`720x1280` or `405x720`) is centered on a `1280x720` 16:9 canvas with black pillarbox bars (`pad=1280:720:(1280-405)/2:0:black`).
2. **Generate**: Submitted to `veo-exp-a2v-generation` alongside the 8.0s audio track.
3. **Post-Crop**: When the completed 1280x720 video returns, the active center `405x720` region is cropped and scaled back to `720x1280` (`crop=405:720:(1280-405)/2:0,scale=720:1280`).
- **Verified Output**: **MediaItem #13** — Full resolution `720x1280` vertical video, 24 fps, 8.0s, perfect lip-sync, zero black bars.

#### 2. Nano Banana AI Outpainting (Widescreen 16:9 Expansion)
For cinematic widescreen production:
1. **Outpaint**: The portrait still is expanded left-and-right via Gemini Image Generation (`gemini-2.5-flash-image` / Nano Banana) into a native 16:9 widescreen environment (`1280x720`) while preserving character identity and lighting.
2. **Generate**: Submitted to `veo-exp-a2v-generation` with the 8.0s audio track.
- **Verified Output**: **MediaItem #11** (16:9 Still) and **MediaItem #12** (16:9 Dialogue Video) — Complete scene animation without letterboxing or framing loss.

---

### Official Documentation Confirmation

Verified against [`docs.cloud.google.com/.../dialogue-driven-generation`](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/experimental/dialogue-driven-generation):
- **Model ID**: `veo-exp-a2v-generation`
- **Audio Constraint**: Exactly 8.0 seconds (48 kHz, stereo WAV/MP3/AAC). Longer audio is truncated, shorter is padded with silence.
- **Text Prompt**: Required (`instances[].prompt`, max 1024 chars) to guide facial emotion, character description, and camera stability (`"static camera, no cuts"`).
- **Outputs**: 720p 24 fps (192 frames) H264 MP4, ProRes, DNxHR, or 16-bit PNG frame sequences.

