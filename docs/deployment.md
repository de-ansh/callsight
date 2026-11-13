# Deploying Callsight

A production setup on one Linux host with Docker: TLS at an nginx proxy, nothing else exposed,
required secrets, backups. Everything here was run end to end before release (see "Checked").

## 1. Prepare

```bash
git clone https://github.com/de-ansh/callsight && cd callsight
cp .env.example .env
```

Set in `.env` (Compose refuses to start the production stack until these are set):

| Variable | Value |
|---|---|
| `POSTGRES_PASSWORD`, `MINIO_ROOT_PASSWORD` | long random values (`openssl rand -hex 24`) |
| `CALLSIGHT_SECRET_KEY` | `openssl rand -hex 32`. Signs sessions and recording links; keep it stable |
| `CALLSIGHT_PUBLIC_URL` | the https URL people use, e.g. `https://calls.example.com` |
| Optional: `CALLSIGHT_OIDC_*` | single sign-on, see the README |
| Optional: `CALLSIGHT_RETENTION_DAYS` | delete calls older than N days |

TLS certificates go in `deploy/certs/` as `fullchain.pem` and `privkey.pem` (or point
`CALLSIGHT_TLS_DIR` elsewhere), for example from Let's Encrypt via certbot on the host.

## 2. Start

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api \
  python -m callsight.admin allow-domain --org acme --domain acme.com   # with SSO
# then make yourself an admin, to manage keys and admins from Settings → Access:
#   ... exec api python -m callsight.admin grant-admin --org acme --email you@acme.com
# or, without SSO: ... exec api python -m callsight.admin create-key --org acme --name alice --admin
```

What the production override changes:
- **nginx** terminates TLS (HTTP redirects to HTTPS) and sets HSTS, `nosniff`, `Referrer-Policy`
  and `frame-ancestors 'none'`. It caps request bodies at 510 MB and rate-limits each client
  to 20 requests/s (burst 60), returning 429 beyond that. Edit `deploy/nginx.conf` to tune these.
- **The API, Postgres and MinIO are not published on the host.** Only ports 80 and 443 are.
- **Every service restarts** unless stopped.
- The HTTP→HTTPS redirect assumes the standard port 443.

To separate speakers in mono recordings, build with `--build-arg EXTRAS=asr,diarize` and set
`CALLSIGHT_DIARIZATION=pyannote` and `HF_TOKEN` (see the README).

## 3. Back up and restore

```bash
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml" scripts/backup.sh
# -> backups/<timestamp>/database.dump + recordings/
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml" scripts/restore.sh backups/<timestamp>
```

- **Backup** takes a `pg_dump` of the database and copies every recording (object by object, not
  MinIO's internal files). Schedule it with cron and copy `backups/` off the host.
- **Restore** *replaces* the current database and puts the recordings back.
- A backup taken while calls are being processed may miss in-flight work. Run it at a quiet
  time, or stop the worker first.

## 4. Upgrade

```bash
git pull && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Database migrations apply automatically when the API and worker start. Take a backup first.

## Checked

Before this guide shipped, all of the following was run on a local Docker host, using a separate
Compose project and a self-signed certificate:
- **Proxy:** the HTTP redirect, the API over TLS, and all four security headers.
- **Exposure:** the API, Postgres and MinIO had no published ports.
- **Limits:** a 601 MB request got 413 from nginx; a burst of 150 requests got 73 through and 77
  refused with 429.
- **Secrets:** Compose refused to start without `POSTGRES_PASSWORD`.
- **Backup and restore:** backup, then delete a call (its recording removed from storage), then
  restore. The call came back with a byte-identical 720 KB recording, and no temporary files were
  left behind.
