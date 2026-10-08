# Scam Autopsy

Original, primary-source-backed explanations of scams on [@ScamAutopsyTV](https://www.youtube.com/@ScamAutopsyTV). The operating target is to build a trusted, returning audience and eventually a large channel; no subscriber or income outcome is promised.

The studio runs on GitHub-hosted Linux runners. This Mac is a development copy, not a server. Python, Pillow, FFmpeg and the Apache-licensed Kokoro-82M model produce original illustrations, timed captions and narration without a paid rendering or voice API. Bounded research uses the owner's existing Gemini free-tier project, at most four generation requests per run. Model and project availability are checked; failures stop new content rather than inventing a substitute. Quota is shared with the owner's existing project. No paid API or platform billing was enabled for this project.

## Operating schedule

- Tuesday and Saturday, 17:30 UTC: select one reviewed case, render, upload privately, verify YouTube processing, then publish. This is 23:00 IST. GitHub schedules may be delayed.
- Daily, 08:15 UTC: collect channel statistics and delayed YouTube Analytics. Comparisons require at least 100 engaged views per video; the report records insufficient evidence at smaller samples.
- Each daily insights run checks whether to refill the reviewed-script queue. It attempts FTC research on Mondays (UTC), when explicitly forced, after a previous research failure, or when fewer than four ready scripts remain. The queue stops refilling at eight ready scripts; each attempt generates at most two and uses at most four model requests. It verifies exact evidence passages and digit-based numerical claims against those passages, then requests a different model's factual and editorial review. A rejected candidate is never marked publishable. Sources and review records accompany each accepted script. These automated checks reduce errors but do not guarantee every claim is correct.

The first six scripts have been checked directly against FBI source records. Every recreated interface is labelled **Illustration**. Earlier channel videos are explicitly fictional dramatizations. The [strategy](STRATEGY.md) sets editorial standards and a long-form expansion plan; automatic long-form production is not implemented in this initial studio.

## Cloud operation and recovery

Run the **Scam Autopsy cloud studio** workflow from Actions. Blank case ID chooses the queue head. Manual runs default to **private** for quality preview; a private preview needs a new, explicit dispatch naming that case and choosing **public**. The schedule skips private previews. The exact reviewed JSON hash is pinned before rendering; a changed script requires a new case ID. The studio saves a reservation to the repository before attempting an upload. If interrupted, it searches the owner's recent uploads for a unique case tag and resumes that video. An ambiguous upload result blocks another insert and opens an issue, preventing blind duplicate uploads.

Every YouTube write verifies the channel ID `UCz0W-lSVvEeWYufkUVaVUKA`. Credentials are GitHub Actions secrets (`YOUTUBE_OAUTH_JSON`, `GEMINI_API_KEY`) and never committed. The YouTube grant is separate from other channels and uses the owner's existing OAuth application, whose **In production** status was inspected on 8 October 2026. Public code and aggregate channel reports live here; no commenter identities or comment text are saved.

A failed run opens a repository issue with its run link if the matching issue is not already open. State is in `state/`, daily reports in `reports/`, and MP4 quality previews are retained as Actions artifacts for 14 days. Disable both workflows to pause. No local launch agent, Chrome process, or laptop cron is needed after setup.

OAuth revocation, platform policy changes, or exhaustion of a free service can still interrupt operation. Applying to YouTube monetization and supplying legal, tax, identity and payment information remain account-owner actions. Reaching a threshold does not guarantee YouTube's channel review will approve monetization.

## Development

Install Python 3.12, FFmpeg, espeak-ng and DejaVu fonts, then run `uv sync --locked --extra test` and `uv run pytest -q`. Preview one frame without downloading speech weights:

```sh
uv run python -m scam_autopsy.render content/01-house-wire-r2.json --preview --output build/preview.png
```

Render locally only for development:

```sh
uv run python -m scam_autopsy.render content/01-house-wire-r2.json --output build/episode.mp4
```

The workflows run the same locked dependencies. Code-native brand assets in `brand/` can be regenerated with the included Pillow script. The Kokoro model revision and voice are pinned in the renderer. Do not commit a local token or put one in a workflow input.
