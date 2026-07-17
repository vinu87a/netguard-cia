# Deploying NetGuard-CIA on Rocky Linux

Tested target: **Rocky Linux 9** (x86_64). Notes for Rocky 8 are called out inline.

> RHEL-family gotchas this guide handles explicitly: SELinux blocking the
> container bind-mount, the `podman-docker` shim shadowing `docker`, Python 3.9
> being the system default (we need 3.11+), and firewalld.

---

## 0. Sizing & prerequisites

| Item | Requirement |
|---|---|
| RAM | **16 GB recommended** (the Batfish engine alone wants ~4 GB free) |
| Disk | **20 GB free** — see the space check below (~4 GB is the bare minimum) |
| Network | Outbound **HTTPS** to your LLM provider (Commotion API / Ollama Cloud) |
| Access | a sudo-capable, **non-root** user to own and run the app |

```bash
sudo dnf update -y
sudo dnf install -y git curl lsof   # lsof: used by run.sh's port check
```

### 0.1 Check disk space FIRST (measured footprint)

Two *different* filesystems matter, and they are usually not the same one:

| What | Lands in | Measured size |
|---|---|---|
| `batfish/allinone` image | `/var/lib/docker` | **2.12 GB** |
| `batfish-mcp-container` image | `/var/lib/docker` | **750 MB** |
| Batfish snapshot volume (`batfish-data`) | `/var/lib/docker` | ~270 MB, **grows with every snapshot** |
| Docker layers/overlay + build cache | `/var/lib/docker` | ~1–2 GB, grows |
| Python venv | `/opt/netguard` | **528 MB** |
| Repo + `.git` | `/opt/netguard` | ~10 MB |

**Bare minimum ≈ 4 GB. Budget 20 GB** so snapshot growth and image updates don't
wedge you later. The heavy consumer is **`/var/lib/docker`**, *not* `/opt`.

Check before you create anything:

```bash
df -h /            /opt            /var/lib/docker    # free space per mount
lsblk                                                 # disk/partition layout
findmnt -no SOURCE,TARGET,FSTYPE /var/lib/docker      # which FS Docker really uses
```

> **Rocky's default LVM layout is the trap:** installers often give `/` only
> ~15–20 GB and hand the bulk to `/home`. `/opt` **and** `/var/lib/docker` both
> sit on `/` — so Docker can fill root while `/home` sits empty. Check `/`
> specifically; don't assume "the disk is 500 GB" means you're fine.

**If `/` is tight, pick one:**

**(a) Grow the root LV** — best if the volume group has free extents:
```bash
sudo vgs                                   # look for VFree > 0
sudo lvextend -r -L +20G /dev/mapper/rl-root   # -r also grows the xfs filesystem
df -h /
```

**(b) Move Docker's data-root to the big filesystem** — best when `/home` (or a
separate disk) has the space. Note **XFS cannot be shrunk**, so you can't simply
take space back from `/home`; relocating Docker is the practical fix.
```bash
sudo systemctl stop docker
sudo mkdir -p /home/docker
sudo rsync -aHAX --info=progress2 /var/lib/docker/ /home/docker/
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{ "data-root": "/home/docker" }
EOF
# SELinux: label the new location or containers won't start
sudo semanage fcontext -a -t container_var_lib_t '/home/docker(/.*)?'
sudo restorecon -Rv /home/docker
sudo systemctl start docker
docker info | grep -i "docker root dir"    # confirm it moved
```
*(`semanage` lives in `policycoreutils-python-utils`: `sudo dnf install -y policycoreutils-python-utils`.)*

**Reclaim space later** (build cache and dangling layers add up fast):
```bash
docker system df       # see what's using space
docker system prune -a # remove unused images/cache (NOT the named data volume)
```

---

## 1. Install Docker CE

Rocky ships Podman, not Docker. **Remove the `podman-docker` shim first** — it
provides a fake `docker` command that will silently intercept everything.

```bash
sudo dnf remove -y podman-docker || true

sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io \
                    docker-buildx-plugin docker-compose-plugin

sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

**Log out and back in** (so the `docker` group applies), then verify:

```bash
docker run --rm hello-world
docker compose version        # must be v2 ("Docker Compose version v2.x")
```

---

## 2. Install Python 3.11

Rocky 9's default `python3` is **3.9** — too old. Install 3.11 alongside it
(don't replace the system python; RHEL tooling depends on it).

```bash
sudo dnf install -y python3.11 python3.11-pip
python3.11 --version          # expect 3.11.x
```

*(Rocky 8: `sudo dnf module enable -y python311 && sudo dnf install -y python311 python311-pip`, then use `python3.11`.)*

---

## 3. Get the code

> Confirm free space first (§0.1) — `df -h /opt /var/lib/docker`. `/opt` needs
> only ~600 MB (venv + repo); `/var/lib/docker` is the one that needs ~4–20 GB.

```bash
sudo mkdir -p /opt/netguard && sudo chown "$USER":"$USER" /opt/netguard
cd /opt/netguard
git clone https://github.com/vinu87a/netguard-cia.git
cd netguard-cia
```

---

## 4. SELinux — label the container bind-mount (REQUIRED)

Rocky runs SELinux **enforcing**. `docker/docker-compose.yml` bind-mounts a
patched file into the MCP container; without an SELinux label the container gets
*permission denied* and the MCP server misbehaves.

Edit `docker/docker-compose.yml` and add `,Z` to that mount:

```yaml
    volumes:
      - ./patches/batfish_failure_impact_tool.py:/app/batfish/tools/batfish_failure_impact_tool.py:ro,Z
```

<details><summary>Alternative (leave compose untouched)</summary>

```bash
sudo chcon -t container_file_t docker/patches/batfish_failure_impact_tool.py
```
Note: `chcon` is not permanent across a full filesystem relabel. For a durable
label use `semanage fcontext -a -t container_file_t '<abs-path>' && restorecon -v '<abs-path>'`.
</details>

> Do **not** disable SELinux to work around this.

---

## 5. Python environment

`run.sh` creates the venv with plain `python3` — which on Rocky is 3.9. **Create
the venv with 3.11 first**; `run.sh` will then reuse it.

```bash
cd /opt/netguard/netguard-cia
python3.11 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python --version    # must be 3.11.x
```

---

## 6. Configure credentials (`.env`)

```bash
cat > /opt/netguard/netguard-cia/.env <<'EOF'
NETGUARD_LLM_PROVIDER=commotion
COMMOTION_URL=https://uat-services.solutions.tatacommunications.com/gateway/aiworker/run
COMMOTION_API_KEY=your-key
COMMOTION_WORKER_ID=your-worker-id
COMMOTION_AUDIENCE_ID=RPA
COMMOTION_ROUTE_SELECTOR=aicoe_workspace
EOF
chmod 600 .env
```

To use Ollama Cloud instead (faster): `NETGUARD_LLM_PROVIDER=ollama` +
`OLLAMA_API_KEY=...`. `.env` is gitignored — never commit it.

---

## 7. Pin the images (production)

`docker-compose.yml` uses `batfish/allinone:latest`. For a stable deployment,
pin it to a digest so a silent upstream change can't break you:

```bash
docker pull batfish/allinone:latest
docker inspect --format='{{index .RepoDigests 0}}' batfish/allinone:latest
# put that image@sha256:... back into docker/docker-compose.yml
```
(The MCP image is already digest-pinned.)

---

## 8. Firewall

**If you expose Streamlit directly** (simplest, no TLS):
```bash
sudo firewall-cmd --permanent --add-port=8501/tcp
sudo firewall-cmd --reload
```

**Recommended for "live":** keep 8501 bound to localhost and put nginx + TLS in
front (see §11) — then open 80/443 instead:
```bash
sudo firewall-cmd --permanent --add-service=http --add-service=https
sudo firewall-cmd --reload
```

---

## 9. First start (foreground smoke test)

```bash
cd /opt/netguard/netguard-cia
./run.sh
```
`run.sh` brings up the Docker stack, waits for the engine healthcheck, and
launches Streamlit. Open `http://<server>:8501`, upload a demo config set from
`scenarios/`, click **Build snapshot**, and ask a scenario. Ctrl-C when happy.

---

## 10. Run as a service (systemd)

The containers already use `restart: unless-stopped`. This unit keeps the app
itself running and starts it after Docker on boot.

```bash
sudo tee /etc/systemd/system/netguard.service >/dev/null <<EOF
[Unit]
Description=NetGuard-CIA (Streamlit)
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=simple
User=$USER
WorkingDirectory=/opt/netguard/netguard-cia
# bind to localhost when behind nginx; use 0.0.0.0 to expose directly
ExecStart=/opt/netguard/netguard-cia/.venv/bin/streamlit run app/streamlit_app.py \
          --server.headless true --server.port 8501 --server.address 127.0.0.1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now netguard
systemctl status netguard
```

The app reads `.env` itself, so no `EnvironmentFile=` is needed.

Bring the Docker stack up on boot (once):
```bash
cd /opt/netguard/netguard-cia && docker compose -f docker/docker-compose.yml up -d
```

---

## 11. nginx + TLS (recommended for live)

Streamlit needs **WebSocket upgrade** proxying or the UI will hang on "Connecting".

```bash
sudo dnf install -y nginx
# SELinux: allow nginx to make outbound proxy connections (else 502)
sudo setsebool -P httpd_can_network_connect 1
```

```nginx
# /etc/nginx/conf.d/netguard.conf
server {
    listen 80;
    server_name netguard.example.com;

    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;      # websockets
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 86400;                    # long-running scenarios
    }
}
```

```bash
sudo systemctl enable --now nginx
# TLS:
sudo dnf install -y certbot python3-certbot-nginx
sudo certbot --nginx -d netguard.example.com
```

> `proxy_read_timeout 86400` matters: a change scenario can take minutes
> (multiple sequential LLM calls). A short timeout will cut the request off.

---

## 12. Verify

```bash
docker ps                                   # both containers up; batfish "(healthy)"
systemctl status netguard                   # app active (running)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8501   # 200
```

---

## 13. Operations

```bash
# app logs
journalctl -u netguard -f

# container logs
docker logs -f netguard-batfish
docker logs -f netguard-batfish-mcp

# restart app after a code change (Streamlit does NOT reload imported modules)
sudo systemctl restart netguard

# update the code
cd /opt/netguard/netguard-cia && git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart netguard

# engine wedged? (scenario hangs on "Running: ...")
docker restart netguard-batfish     # snapshots live in this container — re-upload after
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `docker` behaves oddly / not Docker CE | `podman-docker` shim installed → `sudo dnf remove podman-docker` |
| MCP container errors reading the patch file | SELinux label → add `,Z` to the bind mount (§4) |
| `pip install` fails on syntax / wheels | venv built with Python 3.9 → rebuild with `python3.11 -m venv .venv` (§5) |
| UI stuck "Connecting…" behind nginx | missing WebSocket `Upgrade`/`Connection` headers (§11) |
| nginx 502 to the app | SELinux → `sudo setsebool -P httpd_can_network_connect 1` |
| Engine never healthy | not enough RAM (needs ~4 GB free) → `docker logs netguard-batfish` |
| Scenario cut off mid-run behind nginx | raise `proxy_read_timeout` (§11) |
| Can't reach :8501 from another host | firewalld → open the port (§8) |
| `no space left on device` / image pull fails | `/var/lib/docker` full (usually on a small `/`) → §0.1: grow the root LV or move Docker's data-root; `docker system prune -a` to reclaim |
| Snapshots vanish after `docker compose down -v` | `-v` deletes the `batfish-data` volume → re-upload configs; use plain `down` to keep them |
