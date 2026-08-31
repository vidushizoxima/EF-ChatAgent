# Deploying the EF Social Chat Agent to Azure App Service

`.github/workflows/deploy.yml` builds the `Dockerfile`, pushes it to GHCR, and repoints
the App Service at the new SHA-tagged image on every push to `main`.

Repo: `vidushizoxima/EF-ChatAgent` → image `ghcr.io/vidushizoxima/ef-chatagent`

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

## 1. Create the App Service (one time)

```bash
RG=eureka-forbes-rg
PLAN=ef-chat-plan
APP=ef-chat-agent                 # must match AZURE_WEBAPP_NAME in deploy.yml
LOC=centralindia
IMG=ghcr.io/vidushizoxima/ef-chatagent:latest

az group create -n $RG -l $LOC

# B1 is enough — one instance, no fleet of subprocesses. Always On needs B1+, not F1.
az appservice plan create -n $PLAN -g $RG --is-linux --sku B1 -l $LOC

az webapp create -n $APP -g $RG -p $PLAN --container-image-name $IMG
```

The workflow does not need the hostname — it reads it back from the deploy action, so
the randomised suffix new App Services get is handled on its own. To see it yourself:

```bash
az webapp show -n $APP -g $RG --query defaultHostName -o tsv
```

## 2. Let App Service pull from GHCR

The package is private until you change it. Either make it public after the first
workflow run (`https://github.com/users/vidushizoxima/packages/container/ef-chatagent/settings`
→ *Change visibility*), or hand App Service a read-only PAT:

```bash
az webapp config appsettings set -n $APP -g $RG --settings \
  DOCKER_REGISTRY_SERVER_URL=https://ghcr.io \
  DOCKER_REGISTRY_SERVER_USERNAME=vidushizoxima \
  DOCKER_REGISTRY_SERVER_PASSWORD=<ghcr-read-packages-PAT>
```

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
| `WEBSITES_PORT=8000` | the container listens on 8000; without this App Service probes :80 and 504s |
| `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true` | mounts persistent `/home` — **without it every redeploy wipes all sessions** |
| `EF_DB_PATH=/home/data/ef_chat.db` | keeps the SQLite file on that persistent mount |
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

The push triggers the workflow: build → GHCR → deploy → poll `/health` until it
returns 200 → warn if Dataverse or the channel credentials did not come through.

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
az webapp log tail -n ef-chat-agent -g eureka-forbes-rg
```

`/health` reporting `"facebook": false` or `"dataverse_configured": false` means an app
setting did not land — the app still starts, and the workflow flags it as a warning
rather than failing the deploy.

## Troubleshooting

| Symptom | Cause |
|---|---|
| 504 on every request | `WEBSITES_PORT` missing or not 8000 |
| Container never starts | GHCR package still private and no PAT set |
| Sessions reset on every deploy | `WEBSITES_ENABLE_APP_SERVICE_STORAGE` not true, or `EF_DB_PATH` not under `/home` |
| Interactions never reach the CRM | Always On disabled, so the background logger is not running |
| Customers answered twice | more than one instance — SQLite state is per-instance |
| Health check passes but replies never send | channel token missing; check `/health` |
