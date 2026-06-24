# VPS Deployment Guide

This document covers everything needed to set up, update, and maintain the **AI Model Backend** on a VPS running Ubuntu 26.04 LTS with Python 3.14.

---

## Server Details

| Item | Value |
|------|-------|
| **IP** | `92.113.151.67` |
| **OS** | Ubuntu 26.04 LTS (Resolute Raccoon) |
| **Python** | 3.14.4 |
| **User** | `pantomas` / `sudo` |
| **Install path** | `/opt/ai-backend` |
| **Service name** | `ai-backend` |
| **Logs** | `sudo journalctl -u ai-backend -f` |
| **Firewall** | UFW — SSH + Nginx Full allowed |

---

## Table of Contents

1. [Initial Setup](#initial-setup)
2. [Updating the Software](#updating-the-software)
3. [Service Management](#service-management)
4. [Configuration Reference](#configuration-reference)
5. [Troubleshooting](#troubleshooting)
6. [HTTPS / Domain Setup](#https--domain-setup)
7. [Backup](#backup)
8. [Python 3.14 Compatibility Notes](#python-314-compatibility-notes)

---

## Initial Setup

This was done on a fresh Ubuntu 26.04 LTS VPS. Follow these steps in order.

### 1. Install system packages

```bash
sudo apt update && sudo apt install -y \
    python3.14 \
    python3.14-venv \
    python3.14-dev \
    build-essential \
    libssl-dev \
    libffi-dev \
    git \
    nginx \
    certbot \
    python3-certbot-nginx
```

> **Why `python3.14-dev`, `build-essential`, `libssl-dev`, `libffi-dev`:** Required to build C extensions for `cryptography`, `bcrypt`, and their transitive dependencies.

### 2. Clone the project

```bash
sudo mkdir -p /opt
sudo chown $(whoami):sudo /opt
git clone https://github.com/tomaszkuehn/modelsgate.git /opt/ai-backend
cd /opt/ai-backend
```

> If the repo is private, you'll need a GitHub token or upload the project manually via `scp`/`rsync`.

### 3. Create Python virtual environment

```bash
cd /opt/ai-backend
python3.14 -m venv venv
source venv/bin/activate
pip install --upgrade pip setuptools wheel
```

### 4. Install Python dependencies

```bash
pip install -r requirements.txt
```

> The default `requirements.txt` pins `pydantic>=2.11`, `sqlalchemy>=2.0.36`, and `psutil>=5.9`. These minimum versions are required for Python 3.14 compatibility. See [Python 3.14 Compatibility Notes](#python-314-compatibility-notes) for details.

### 5. Create `.env` from the example

```bash
cp .env.example .env
```

Edit `.env` with your values:

```bash
nano .env
```

**Required settings:**

```ini
# Generate with: openssl rand -hex 32
SESSION_SECRET=<random-64-character-hex-string>

# Admin credentials — CHANGE THESE from defaults
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<strong-password>

# Public URL (used for outbound referer headers, etc.)
PUBLIC_URL=https://modelsgate.eu

# At least one provider API key
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
# etc.

DATA_DIR=/opt/ai-backend/data
MODELS_CONFIG_PATH=/opt/ai-backend/models_config.yaml
```

### 6. Create required files and directories

```bash
mkdir -p /opt/ai-backend/data/keys
echo '# Models configuration — managed via Admin panel' > /opt/ai-backend/models_config.yaml
```

### 7. Test manually

```bash
cd /opt/ai-backend
source venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Visit `http://<server-ip>:8000/` — you should see:
```json
{"status":"ok","service":"AI Model Backend","version":"1.0.0"}
```

Press `Ctrl+C` to stop.

### 8. Set up systemd service

Create the service file:

```bash
sudo nano /etc/systemd/system/ai-backend.service
```

```ini
[Unit]
Description=AI Model Backend
After=network.target

[Service]
Type=simple
User=pantomas
Group=sudo
WorkingDirectory=/opt/ai-backend
Environment=PATH=/opt/ai-backend/venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/opt/ai-backend/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level info
Restart=always
RestartSec=3
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo chown -R pantomas:sudo /opt/ai-backend
sudo systemctl daemon-reload
sudo systemctl enable ai-backend --now
sudo systemctl status ai-backend
```

### 9. Configure Nginx reverse proxy

```bash
sudo nano /etc/nginx/sites-available/ai-backend
```

```nginx
server {
    listen 80;
    server_name modelsgate.eu;

    client_max_body_size 100M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }
}
```

Enable and start:

```bash
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/ai-backend /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
sudo systemctl enable nginx
```

### 10. Configure firewall

```bash
sudo ufw allow 22/tcp          # SSH
sudo ufw allow 80/tcp          # HTTP (nginx)
sudo ufw allow 443/tcp         # HTTPS (nginx + SSL)
sudo ufw allow 8000/tcp        # API direct (if clients hit port 8000)
sudo ufw enable
sudo ufw status
```

> **Why port 8000:** Nginx proxies port 80 → 8000 internally, so web traffic is covered. But if API clients send requests directly to port 8000 (bypassing nginx), that port needs to be open too. Open it if your setup requires it.

### 11. Verify deployment

```bash
# Health check via localhost
curl http://localhost:8000/

# Health check via nginx
curl http://localhost/

# Check service is running
sudo systemctl status ai-backend

# View logs
sudo journalctl -u ai-backend -f

# Test admin login page
curl -s http://localhost/admin/login | head -5
```

Visit `https://modelsgate.eu/admin/login` in a browser and log in with the credentials set in `.env`.

---

## Updating the Software

### Quick update (when repo is public)

```bash
cd /opt/ai-backend
sudo -u pantomas git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart ai-backend
```

### Manual upload update (when repo is private or inaccessible)

**On your local machine:**

```bash
# Create a deploy archive (excluding venv, .git, data)
tar czf /tmp/backend-deploy.tar.gz \
    --exclude='venv' --exclude='.git' --exclude='data' \
    --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='.env' --exclude='*.db' \
    -C /path/to/backend-AI .

# Upload
scp /tmp/backend-deploy.tar.gz pantomas@92.113.151.67:/tmp/
```

**On the VPS:**

```bash
# Stop the service
sudo systemctl stop ai-backend

# Backup current install
sudo cp -r /opt/ai-backend /opt/ai-backend.bak.$(date +%Y%m%d)

# Extract new code (overwriting existing files)
sudo tar xzf /tmp/backend-deploy.tar.gz -C /opt/ai-backend/

# Install any new dependencies
cd /opt/ai-backend
source venv/bin/activate
pip install -r requirements.txt

# Restore ownership and restart
sudo chown -R pantomas:sudo /opt/ai-backend
sudo systemctl start ai-backend
sudo journalctl -u ai-backend -f   # watch logs to confirm clean start
```

### Check what changed before updating

```bash
cd /opt/ai-backend
git fetch origin
git diff HEAD..origin/master --stat    # list changed files
git diff HEAD..origin/master           # view actual diff
```

---

## Service Management

| Action | Command |
|--------|---------|
| **Status** | `sudo systemctl status ai-backend` |
| **Start** | `sudo systemctl start ai-backend` |
| **Stop** | `sudo systemctl stop ai-backend` |
| **Restart** | `sudo systemctl restart ai-backend` |
| **View logs (live)** | `sudo journalctl -u ai-backend -f` |
| **View logs (last 100)** | `sudo journalctl -u ai-backend -n 100 --no-pager` |
| **View logs since last boot** | `sudo journalctl -u ai-backend -b` |
| **Check if enabled on boot** | `sudo systemctl is-enabled ai-backend` |

### Viewing errors only

```bash
sudo journalctl -u ai-backend -p 3 --no-pager   # errors only
```

### Restart Nginx after config changes

```bash
sudo nginx -t                # test config
sudo systemctl reload nginx  # apply without downtime
```

---

## Configuration Reference

### File locations

| File | Location |
|------|----------|
| Environment variables | `/opt/ai-backend/.env` |
| Models config (legacy) | `/opt/ai-backend/models_config.yaml` |
| SQLite database | `/opt/ai-backend/data/app.db` |
| Encryption keys | `/opt/ai-backend/data/keys/` |
| Systemd unit | `/etc/systemd/system/ai-backend.service` |
| Nginx site config | `/etc/nginx/sites-available/ai-backend` |
| Application logs | `journald` (via `journalctl -u ai-backend`) |
| Nginx access log | `/var/log/nginx/access.log` |
| Nginx error log | `/var/log/nginx/error.log` |

### Environment variables (`.env`)

| Variable | Description | Default |
|----------|-------------|---------|
| `HOST` | Bind address | `0.0.0.0` |
| `PORT` | Bind port | `8000` |
| `PUBLIC_URL` | Public-facing URL (used for outbound referer headers) | `http://localhost:8000` |
| `DATA_DIR` | Data directory | `./data` |
| `MODELS_CONFIG_PATH` | Models config path | `./models_config.yaml` |
| `ADMIN_USERNAME` | Admin panel username | `admin` |
| `ADMIN_PASSWORD` | Admin panel password | — |
| `SESSION_SECRET` | Cookie signing key | — |
| `OPENAI_API_KEY` | OpenAI API key | — |
| `ANTHROPIC_API_KEY` | Anthropic API key | — |
| `GEMINI_API_KEY` | Google Gemini API key | — |
| `OPENROUTER_API_KEY` | OpenRouter API key | — |
| `ALIBABA_API_KEY` | Alibaba DashScope key | — |
| `DEEPSEEK_API_KEY` | Deepseek API key | — |
| `OLLAMA_BASE_URL` | Ollama server URL | `http://localhost:11434` |

### Models

Models are managed through the **Admin → Models** page (`/admin/models`), not by editing `models_config.yaml` directly. The YAML file exists for historical/compatibility reasons but the database is the source of truth.

After adding models, you can verify they're loaded:

```bash
sudo journalctl -u ai-backend -n 5 | grep ModelRegistry
# → ModelRegistry initialized with N models
```

---

## Troubleshooting

### App won't start — check the logs

```bash
sudo journalctl -u ai-backend -n 50 --no-pager
```

Common causes:

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `ModuleNotFoundError: No module named 'X'` | Missing pip package | `source venv/bin/activate && pip install X` |
| `ModuleNotFoundError` after update | `requirements.txt` has new deps | Run `pip install -r requirements.txt` |
| `500 Internal Server Error` on login | App crash (check logs) | See logs for traceback |
| `502 Bad Gateway` from nginx | App not running | `sudo systemctl status ai-backend` |
| `504 Gateway Timeout` | Request took too long | Increase `proxy_read_timeout` in nginx config |
| Permission denied on data files | Wrong owner | `sudo chown -R pantomas:sudo /opt/ai-backend/data` |

### Check if the app is actually running

```bash
sudo systemctl status ai-backend
curl http://localhost:8000/
```

### View what changed after an update

```bash
cd /opt/ai-backend
git log --oneline -5
```

### Roll back a bad update

```bash
cd /opt/ai-backend
git log --oneline -10                    # find the last good commit
sudo systemctl stop ai-backend
git checkout <good-commit-hash>
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl start ai-backend
```

---

## HTTPS / Domain Setup

The production server runs at **https://modelsgate.eu** with HTTPS enabled via Let's Encrypt.

### Domain DNS

An A record points `modelsgate.eu` to `92.113.151.67`.

### Nginx config (HTTPS)

Certbot auto-configured SSL. The effective nginx config:

```nginx
server {
    listen 80;
    server_name modelsgate.eu;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl;
    server_name modelsgate.eu;

    ssl_certificate     /etc/letsencrypt/live/modelsgate.eu/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/modelsgate.eu/privkey.pem;

    client_max_body_size 100M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }
}
```

### Certificate renewal

Auto-renewal is handled by a systemd timer (certbot sets this up automatically):

```bash
sudo certbot renew --dry-run   # test renewal
sudo systemctl status certbot.timer
```

### Setting up a new domain

When pointing a new domain to this server:

```bash
# 1. Update nginx server_name
sudo nano /etc/nginx/sites-available/ai-backend

# 2. Get SSL certificate
sudo certbot --nginx -d yourdomain.com

# 3. Reload
sudo nginx -t && sudo systemctl reload nginx
```

---

## Backup

### Quick backup before updates

```bash
# Stop service, back up data + config, restart
sudo systemctl stop ai-backend
sudo cp -r /opt/ai-backend /opt/ai-backend.bak.$(date +%Y%m%d-%H%M)
sudo systemctl start ai-backend
```

This preserves your SQLite database, encryption keys, `.env`, and all code.

### What to back up regularly

| Path | Contains |
|------|----------|
| `/opt/ai-backend/data/` | SQLite DB, encryption keys |
| `/opt/ai-backend/.env` | API keys, credentials |
| `/opt/ai-backend/models_config.yaml` | Legacy config |

### Restore from backup

```bash
sudo systemctl stop ai-backend
sudo cp -r /opt/ai-backend.bak.20260617/data /opt/ai-backend/
sudo cp /opt/ai-backend.bak.20260617/.env /opt/ai-backend/
sudo chown -R pantomas:sudo /opt/ai-backend
sudo systemctl start ai-backend
```

---

## Python 3.14 Compatibility Notes

Ubuntu 26.04 LTS ships Python 3.14.4. The following dependency adjustments were needed:

| Package | Original pin | Compatible version | Issue |
|---------|-------------|-------------------|-------|
| `pydantic` | `==2.7.4` | `>=2.11` | `pydantic-core` 2.18.4 uses PyO3 0.21 which maxes out at Python 3.12 |
| `sqlalchemy` | `==2.0.31` | `>=2.0.36` | Python 3.14 changed `typing.Union.__getitem__` behavior |
| `psutil` | _missing_ | `>=5.9` | Used by `app/stats/memory.py` but was never declared |

The current `requirements.txt` reflects these minimums. When adding new dependencies, always test on Python 3.14 first.

---

## Useful aliases

Add these to `/home/pantomas/.bashrc` on the VPS:

```bash
alias aib-log='sudo journalctl -u ai-backend -f'
alias aib-status='sudo systemctl status ai-backend'
alias aib-restart='sudo systemctl restart ai-backend'
alias aib-update='cd /opt/ai-backend && git pull && source venv/bin/activate && pip install -r requirements.txt && sudo systemctl restart ai-backend'
alias aib-errors='sudo journalctl -u ai-backend -p 3 --no-pager -n 50'
```
