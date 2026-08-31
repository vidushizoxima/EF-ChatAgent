# Deploying the EF Social Chat Agent to Azure App Service

`.github/workflows/deploy.yml` zip-deploys the source to the App Service on every push
to `main`. The App Service builds it: `SCM_DO_BUILD_DURING_DEPLOYMENT=true` makes Oryx
run `pip install -r requirements.txt` server-side.

**This is a code deploy, not a container deploy.** The `Dockerfile` in this repo is for
running the app locally — nothing in the deploy path reads it, and no image is pushed
to a registry.

Repo: `vidushizoxima/EF-ChatAgent`

---

## Two constraints that shape everything below

**1. State is a SQLite file.** Sessions, conversation buffers, rolling summaries and the
interaction watermark all live in one file. It must sit on `/home`, which is the only
path that survives a restart or redeploy — and the app must run on **exactly one
instance**. Scale it out and each instance gets its own state: customers get answered
twice, and interactions log twice.

**2. The interaction logger is a background task.** It writes conversations to the CRM
two minutes after the last message. If App Service unloads the app when idle, that
never runs. **Always On is required**, not optional.

---

## 1. The App Service

The app already exists — it is **`EurekaForbes-Chat`** in resource group
`Eureka-forbes-demo`, on the shared `ASP-goedai-850d` plan (B3, capacity 1). That name
is what `AZURE_WEBAPP_NAME` in `deploy.yml` must say; there is no `ef-chat-agent`.

```bash
RG=Eureka-forbes-demo
APP=EurekaForbes-Chat             # must match AZURE_WEBAPP_NAME in deploy.yml
```

The runtime stack and startup command it must be on:

```bash
az webapp config set -n $APP -g $RG \
  --linux-fx-version "PYTHON|3.11" \
  --startup-file "python chatbot/main.py"
```

`chatbot/main.py` binds `0.0.0.0` on `$PORT`, which App Service sets for the worker.
Do **not** set `WEBSITES_PORT` — that is a custom-container setting and does nothing
for a code app.

To create one from scratch instead:

```bash
PLAN=ef-chat-plan
LOC=southindia

az group create -n $RG -l $LOC
# B1 is enough — one instance, no fleet of subprocesses. Always On needs B1+, not F1.
az appservice plan create -n $PLAN -g $RG --is-linux --sku B1 -l $LOC
az webapp create -n $APP -g $RG -p $PLAN --runtime "PYTHON:3.11"
```

The workflow does not need the hostname — it reads it back from the deploy action, so
the randomised suffix new App Services get is handled on its own. To see it yourself:

```bash
az webapp show -n $APP -g $RG --query defaultHostName -o tsv
```

## 2. Let App Service build the source

The workflow ships source only — no wheels, no venv. Oryx installs
`requirements.txt` on the App Service, which requires:

```bash
az webapp config appsettings set -n $APP -g $RG --settings \
  SCM_DO_BUILD_DURING_DEPLOYMENT=true
```

Without it the zip is dropped on disk unbuilt, `import fastapi` fails at startup, and
the site 503s with nothing useful in the HTTP response — the reason is in
`az webapp log tail` only.

## 3. App settings

Fill the 13 placeholders in `azure-appsettings.json`, then apply the whole file:

```bash
az webapp config appsettings set -n $APP -g $RG --settings @azure-appsettings.json
```

`azure-appsettings.json` is committed with placeholders only — never put real keys in
it. If you would rather not have the file on disk at all, set them from your `.env`:

```bash
az webapp config appsettings set -n $APP -g $RG --settings \
  $(grep -v '^#' .env | grep -v '^$' | xargs)
```

The settings that are not optional:

| Setting | Why |
|---|---|
| `SCM_DO_BUILD_DURING_DEPLOYMENT=true` | Oryx installs `requirements.txt`; without it the app starts with no dependencies |
| `EF_DB_PATH=/home/data/ef_chat.db` | `/home` is the only path that survives a redeploy on a code app, so the SQLite file lives there |
| `ADMIN_API_KEY` | `/admin/*` and `/session/*` are public routes otherwise refused — set a long random value |

## 4. Always On, health check, single instance

```bash
az webapp config set -n $APP -g $RG \
  --always-on true \
  --generic-configurations '{"healthCheckPath": "/health"}'

# Hard-pin to one instance. This is a correctness requirement, not a cost choice.
az monitor autoscale delete -g $RG --name $APP 2>/dev/null || true
az appservice plan update -n $PLAN -g $RG --number-of-workers 1
```

## 5. Wire up GitHub

```bash
az webapp deployment list-publishing-profiles -n $APP -g $RG --xml > publish-profile.xml
```

Add its contents as the repo secret **`AZURE_WEBAPP_PUBLISH_PROFILE`**
(GitHub → Settings → Secrets and variables → Actions). Then delete the local file —
it contains deployment credentials and is git-ignored for that reason.

```bash
git add .
git commit -m "Deploy to Azure App Service"
git push origin main
```

The push triggers the workflow: compile-check → stage → zip-deploy → poll `/health`
until it returns 200 → warn if Dataverse or the channel credentials did not come
through.

## 6. Point Meta at the new host

Once `/health` is green, update the callback URLs in the Meta dashboard. Both path
styles are served, so either works:

| Channel | Callback URL |
|---|---|
| WhatsApp | `https://<hostname>/whatsapp` |
| Messenger | `https://<hostname>/facebook` |
| Instagram | `https://<hostname>/instagram` |

Verify tokens must match `WHATSAPP_VERIFY_TOKEN` / `FACEBOOK_VERIFY_TOKEN` /
`INSTAGRAM_VERIFY_TOKEN` in the app settings.

---

## Checking a deployment

```bash
curl -s https://<hostname>/health | python3 -m json.tool
curl -s -H "X-Admin-API-Key: $ADMIN_API_KEY" https://<hostname>/admin/diagnostics

# live logs
az webapp log tail -n EurekaForbes-Chat -g Eureka-forbes-demo
```

`/health` reporting `"facebook": false` or `"dataverse_configured": false` means an app
setting did not land — the app still starts, and the workflow flags it as a warning
rather than failing the deploy.

## Troubleshooting

| Symptom | Cause |
|---|---|
| 503 on every request, `ModuleNotFoundError` in the log | `SCM_DO_BUILD_DURING_DEPLOYMENT` not true — the zip shipped but was never built |
| 503 with nothing obvious | startup command is not `python chatbot/main.py`, or the app bound a port other than `$PORT` |
| Deploy green, old behaviour still served | the previous worker answered the health poll before the new one swapped in; re-check `/health` a minute later |
| Sessions reset on every deploy | `EF_DB_PATH` not under `/home` — only `/home` survives a redeploy |
| Interactions never reach the CRM | Always On disabled, so the background logger is not running |
| Customers answered twice | more than one instance — SQLite state is per-instance |
| Health check passes but replies never send | channel token missing; check `/health` |
