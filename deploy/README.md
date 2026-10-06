# Deploy / architecture

How this repo relates to OpenClaw, and how to ship a change.

## Three layers, three homes

| Layer | Lives in | Contents | In git? |
|---|---|---|---|
| **App** | this repo (`~/Desktop/…/Financial_Reporting_Bot`) | all bot code, `Dockerfile`, `requirements.txt` | yes — public `Panbear1983/Financial_Reporting_Bot` |
| **Platform** | `~/Agents/openclaw/openclaw-infra` | `oc-manage`, `docker-compose.yml` (builds from this repo) | should be its own **private** repo |
| **State** | `~/Agents/openclaw/agents/financial-bot` | `.env` (live keys), `data/` (portfolio, reports), `logs/` | no — never committed |

The app is the "what to run." OpenClaw is the "how/where to run it." The silo is the private
runtime state. They stay separate on purpose: the app repo is public and secret-scanned, while
OpenClaw holds secrets and can host other agents (e.g. Hermes) besides this one.

## The one thing that used to bite

The live bot runs a Docker container whose code is **baked into the image**. Editing this repo
does nothing to the running bot until the image is **rebuilt** and the container **recreated**.
That is the whole job of `../deploy.sh`.

## Shipping a change

```bash
./deploy.sh dev       # run today's report with your current code, throwaway container,
                      # NO Telegram, NO schedule — the fast edit->see loop
./deploy.sh deploy    # rebuild the image + recreate the LIVE container (resumes pushes)
./deploy.sh logs      # watch it
```

`deploy` snapshots the **entire current working tree**, so it also picks up any other
uncommitted or unpushed changes — commit what you mean to ship first.

## Do not use the retired path

`~/Agents/openclaw/oc-manage` and `~/Agents/openclaw/docker-compose.yml` are the pre-consolidation
(2026-07-07) build path; they build from a **stale code copy** and will silently ship old code.
Those leftovers were moved to `~/Agents/openclaw/_archive_pre-consolidation_*/`. `deploy.sh`
only ever calls the consolidated `openclaw-infra/oc-manage`.
