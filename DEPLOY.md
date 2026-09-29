# Deploying on an Oracle Cloud (OCI) instance

What runs on the server:

```
Internet ──443/80──▶ Caddy (automatic HTTPS) ──▶ app container :8000
                                                  ├─ FastAPI  (api/server.py)  /api/*
                                                  └─ React UI (web/dist)       /
app container ──▶ Hindsight Cloud (memory)   ──▶ Groq or Anthropic (LLM)
```

Everything is in `docker-compose.yml`. Secrets stay in `.env` on the server; nothing secret is baked into the image.

---

## 1. Create the instance (OCI console, once)

1. **Compute → Instances → Create instance.**
   - **Image:** Canonical **Ubuntu 22.04** or **24.04**.
   - **Shape:** **VM.Standard.A1.Flex** (Ampere, Always Free eligible) with **2 OCPU / 12 GB**. The 1 GB micro shape is too small to build the UI.
   - **Networking:** a public subnet with **Assign a public IPv4 address** checked.
   - **SSH keys:** upload your public key, or download the generated private key (e.g. `~/.ssh/oracle_key`).
2. **Open ports 80 and 443 in the VCN.** Go to *Instance → Primary VNIC → Subnet → Security List → Add Ingress Rules*. Add two rules, both with source `0.0.0.0/0`, protocol TCP:
   - destination port **80**
   - destination port **443**
3. Note the instance's **public IP**, e.g. `129.146.1.2`.

## 2. Pick the site address

- **With a domain:** create a DNS **A record** (e.g. `scm.example.com`) pointing at the public IP. Set `DOMAIN=scm.example.com`.
- **Without a domain:** use the free wildcard DNS name `129-146-1-2.sslip.io` (your IP with dashes). Set `DOMAIN=129-146-1-2.sslip.io`.

In both cases Caddy gets a Let's Encrypt certificate automatically on first start.

## 3. Prepare `.env` on your machine

In your local `.env`, add or confirm these lines:

```bash
DOMAIN=129-146-1-2.sslip.io        # or your domain
UI_USER=demo                       # the site asks for this login
UI_PASSWORD=choose-a-long-password
MAX_CONCURRENT_RUNS=1              # one model run at a time (protects the free Groq limit)
```

The Hindsight and LLM keys already in `.env` are reused as they are.

## 4. Copy the project to the server

From the project folder on your Mac:

```bash
deploy/sync_to_server.sh ubuntu@129.146.1.2 ~/.ssh/oracle_key
```

This copies the code, the dataset, `.env`, and the ingestion log that the Overview page reads.

## 5. One-time server setup

```bash
ssh -i ~/.ssh/oracle_key ubuntu@129.146.1.2
bash ~/sc_memory_extension/deploy/oracle_setup.sh
exit
```

The script installs Docker and opens ports 80/443 in the host firewall. OCI's Ubuntu images block them by default, even when the security list allows them. You log out so that Docker permissions apply.

## 6. Start it

```bash
ssh -i ~/.ssh/oracle_key ubuntu@129.146.1.2
cd ~/sc_memory_extension
bash deploy/deploy.sh
```

The first build takes about 5–10 minutes: it installs npm packages, builds the UI and installs the Python dependencies. Then open **https://129-146-1-2.sslip.io** (or your domain) and log in with `UI_USER` / `UI_PASSWORD`.

## Day-to-day

| Task | Command (on the server, in `~/sc_memory_extension`) |
|---|---|
| Follow logs | `docker compose logs -f app` |
| Health check | `curl -s https://$DOMAIN/api/health` |
| Restart | `docker compose restart app` |
| Deploy new code | on your Mac: `deploy/sync_to_server.sh ubuntu@IP key`, then on the server: `bash deploy/deploy.sh` |
| Change keys / settings | edit `.env` on the server, then `docker compose up -d` |
| Stop everything | `docker compose down` |
| Run evals on the server | `docker compose exec app python -m eval.run_holdout_scenarios` (results appear on the Results / Overview pages) |

## Notes

- **Persistent data:**
  - `./runtime` on the server holds logged decisions, the retain ledger and the ingestion log.
  - Docker volumes hold the unzipped database and the TLS certificates.
  - Rebuilding the image keeps all of these.
- **Cost protection:**
  - The login prompt keeps strangers from spending your LLM quota.
  - `MAX_CONCURRENT_RUNS` queues model-backed requests. A request that waits longer than `RUN_QUEUE_TIMEOUT_S` gets a "busy, try again" message.
- **Long requests:** a decision takes about 1 minute on free Groq. Caddy's upstream timeouts are set to 10 minutes, so nothing gets cut off.
- **Troubleshooting:**
  - If the site doesn't load, check both firewalls: the VCN security list (step 1.2) *and* the host (`sudo iptables -L INPUT -n | grep -E '80|443'`).
  - If HTTPS fails, make sure `DOMAIN` resolves to the instance: `dig +short $DOMAIN`.
