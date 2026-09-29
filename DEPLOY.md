# Deploying on an Oracle Cloud (OCI) instance

What runs on the server:

```
Internet ──443/80──▶ Caddy (automatic HTTPS) ──▶ app container :8000
                                                  ├─ FastAPI  (api/server.py)  /api/*
                                                  └─ React UI (web/dist)       /
app container ──▶ Hindsight Cloud (memory)   ──▶ Groq or Anthropic (LLM)
```

Everything is in `docker-compose.yml`. The code travels through git. The only file you copy by hand is `.env`, because it holds your keys and must never be committed.

Example values below use the server `138.2.91.14`, with site address **`138-2-91-14.sslip.io`**. That's a free wildcard DNS name that resolves to the IP, so Caddy can get a real HTTPS certificate without you owning a domain.

---

## 1. Open ports 80 and 443 in Oracle's network (OCI console, once)

*Instance → Primary VNIC → Subnet → Security List → Add Ingress Rules*. Add two rules, both with source `0.0.0.0/0`, protocol TCP:
- destination port **80**
- destination port **443**

## 2. Push the code (on your Mac)

```bash
cd /Users/mallikarjunadepu/Desktop/HWH/sc_memory_extension
git push -u origin feat/decision-agent
```

## 3. Add the deployment settings to your local `.env` (on your Mac)

```bash
DOMAIN=138-2-91-14.sslip.io
UI_USER=demo
UI_PASSWORD=choose-a-long-password
MAX_CONCURRENT_RUNS=1
```

## 4. Clone on the server

```bash
ssh ubuntu@138.2.91.14
git clone -b feat/decision-agent https://github.com/Bhargavvz/SCAM.git ~/SCAM
exit
```

If the repository is private, GitHub will ask for a username and a **personal access token**, not your password. You can create one under GitHub → Settings → Developer settings → Tokens.

## 5. Copy `.env` to the server (on your Mac)

```bash
scp /Users/mallikarjunadepu/Desktop/HWH/sc_memory_extension/.env ubuntu@138.2.91.14:~/SCAM/.env
```

## 6. One-time server setup

```bash
ssh ubuntu@138.2.91.14
bash ~/SCAM/deploy/oracle_setup.sh
exit
```

The script installs Docker only if it's missing, adds you to the `docker` group, and opens 80/443 in the host firewall. Oracle's Ubuntu images block those ports with iptables even when `ufw` shows *inactive*. You log out so the group change applies.

## 7. Start it

```bash
ssh ubuntu@138.2.91.14
cd ~/SCAM
bash deploy/deploy.sh
```

The first build takes about 5–10 minutes: npm packages, the UI build, then Python dependencies. When `docker compose ps` shows both containers `Up`, open **https://138-2-91-14.sslip.io** and log in with `UI_USER` / `UI_PASSWORD`.

## Updating after you change code

On your Mac:

```bash
git add -A && git commit -m "..." && git push
```

Then on the server:

```bash
cd ~/SCAM && bash deploy/update.sh
```

`update.sh` pulls the branch and rebuilds. Your `.env`, logged decisions and certificates are kept.

## Day-to-day

| Task | Command (on the server, in `~/SCAM`) |
|---|---|
| Follow logs | `docker compose logs -f app` |
| Health check | `curl -s https://138-2-91-14.sslip.io/api/health` |
| Restart | `docker compose restart app` |
| Change keys / settings | edit `.env`, then `docker compose up -d` |
| Stop everything | `docker compose down` |
| Run holdout eval on the server | `docker compose exec app python -m eval.run_holdout_scenarios` |

## Troubleshooting

- **Site doesn't load.** Check both firewalls: the OCI security list (step 1) *and* the host (`sudo iptables -L INPUT -n --line-numbers | grep -E 'dpt:(80|443)'` should show ACCEPT rules above the REJECT rule). You can re-run `deploy/oracle_setup.sh` safely.
- **HTTPS certificate error.** `DOMAIN` must resolve to the server: `dig +short 138-2-91-14.sslip.io` should print `138.2.91.14`. Check `docker compose logs caddy`.
- **"permission denied … docker.sock".** You haven't logged out and back in since `oracle_setup.sh` ran. `deploy.sh` falls back to `sudo docker` automatically.
- **"The agent is busy".** Another request is running. With `MAX_CONCURRENT_RUNS=1`, model runs are queued one at a time to stay inside the free Groq rate limit.
