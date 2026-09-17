# Deployment Guide — GB Grid Telemetry on your VM

Step-by-step walkthrough to get `fetch_grid.py` + `git_push.py` running
automatically (hourly) on your Linux VM, pushing to:

```
https://github.com/epsilonion-liam/UK-Grid-Tracker.git
```

This assumes you are logged into the VM **as root** (as you described), so no
`sudo` is needed — every command below is run directly. `grid-tracker.service`
has already been set to `User=root` / `Group=root` to match.

---

## 0. Do I need to install git on the VM?

**Yes.** `git_push.py` runs `git add` / `git commit` / `git push` via
subprocess, so `git` must be on the VM's `PATH`. You also need `python3` and
`pip`. Install them first:

```bash
apt-get update
apt-get install -y git python3 python3-pip
```

(If your VM uses a different distro, swap `apt-get` for `dnf`/`yum`/etc.)

---

## 1. Copy the project files to the VM

From your local machine (PowerShell), `scp` the whole project directory
across. Replace `your-vm-ip` with your VM's address:

```powershell
scp -r "C:\Users\liamg\Documents\Development\grid dashboard" root@your-vm-ip:/opt/grid-dashboard
```

If `/opt` doesn't exist yet on the VM, create it first (on the VM):
```bash
mkdir -p /opt
```

Once copied, on the VM confirm everything arrived:
```bash
cd /opt/grid-dashboard
ls
```
You should see `fetch_grid.py`, `git_push.py`, `index.html`,
`grid-tracker.service`, `grid-tracker.timer`, `requirements.txt`, `data/`,
`.git/`, etc.

---

## 2. Install Python dependencies

```bash
cd /opt/grid-dashboard
pip3 install -r requirements.txt
```

---

## 3. Connect the VM to your GitHub repo (step by step)

The VM needs its own SSH key so it can push to GitHub non-interactively
(no typing a password/token on every push). `setup_deploy_key.sh` has these
commands pre-written for you to read through — here's the same thing spelled
out step by step:

### 3.1. Generate a dedicated SSH key (as root)
```bash
ssh-keygen -t ed25519 -C "grid-tracker@$(hostname)" -f /root/.ssh/id_ed25519_grid -N ""
```
(The `-N ""` means no passphrase — required since this runs unattended via systemd.)

### 3.2. Print and copy the public key
```bash
cat /root/.ssh/id_ed25519_grid.pub
```
Copy the full output line (starts with `ssh-ed25519 ...`).

### 3.3. Add the public key to GitHub as a Deploy Key
1. Go to `https://github.com/epsilonion-liam/UK-Grid-Tracker/settings/keys`
2. Click **Add deploy key**.
3. Title: `grid-tracker-vm` (anything descriptive).
4. Paste the public key from step 3.2.
5. **Check "Allow write access"** — without this the VM can only pull, not push.
6. Click **Add key**.

### 3.4. Tell SSH to use that key for GitHub only
```bash
cat >> /root/.ssh/config <<'EOF'
Host github.com-grid-tracker
    HostName github.com
    User git
    IdentityFile /root/.ssh/id_ed25519_grid
    IdentitiesOnly yes
EOF
chmod 600 /root/.ssh/config
```

### 3.5. Trust GitHub's host key (avoids an interactive yes/no prompt on first connect)
```bash
ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts
```

### 3.6. Point the repo at the new SSH alias
```bash
cd /opt/grid-dashboard
git remote set-url origin git@github.com-grid-tracker:epsilonion-liam/UK-Grid-Tracker.git
git remote -v   # confirm it now shows the git@github.com-grid-tracker URL
```

### 3.7. Test the connection
```bash
ssh -T git@github.com-grid-tracker
```
Expected output: `Hi epsilonion-liam/UK-Grid-Tracker! You've successfully authenticated...`
(A warning about "shell access" is normal and fine — deploy keys can't open a shell, only do git operations.)

### 3.8. Set a commit identity (git needs this to make commits)
```bash
git -C /opt/grid-dashboard config user.name "grid-tracker-bot"
git -C /opt/grid-dashboard config user.email "grid-tracker-bot@users.noreply.github.com"
```

### 3.9. Test an end-to-end push
```bash
cd /opt/grid-dashboard
python3 fetch_grid.py --export-only
python3 git_push.py
```
Check the output/`update.log` for `Push to origin/main completed successfully.`
If it says "nothing to commit or push", that's fine too — it means the JSON
didn't change since the last commit.

---

## 4. Install the systemd timer + service

```bash
cp /opt/grid-dashboard/grid-tracker.service /etc/systemd/system/
cp /opt/grid-dashboard/grid-tracker.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now grid-tracker.timer
```

`grid-tracker.timer` fires `grid-tracker.service` every hour at :05 past the
hour. Each run executes `fetch_grid.py` (pulls the last 2 days of data) then
`git_push.py` (commits + pushes if anything changed).

---

## 5. Verify it's working

```bash
# Confirm the timer is scheduled and see when it last/next runs
systemctl list-timers grid-tracker.timer

# Check the last run's status
systemctl status grid-tracker.service

# Tail the systemd journal for this service
journalctl -u grid-tracker.service -n 50 --no-pager

# Or tail the script's own logs directly
tail -n 50 /opt/grid-dashboard/fetch_grid.log
tail -n 50 /opt/grid-dashboard/update.log
```

### Trigger a run immediately (don't wait for the next :05)
```bash
systemctl start grid-tracker.service
journalctl -u grid-tracker.service -f   # watch it live, Ctrl+C to stop
```

---

## 6. (Optional) Backfill more history on first run

A fresh database only has whatever `--lookback-days` pulls (default 2 days),
so `week`/`month`/`year` tabs on the dashboard will be mostly empty at first.
Run this once manually to seed more history:

```bash
cd /opt/grid-dashboard
python3 fetch_grid.py --backfill-days 30
python3 git_push.py
```

This makes many chunked API requests and can take a while for large values —
30 days is a reasonable starting point; you can re-run with a larger
`--backfill-days` later if you want a full year.

---

## 7. Publish the dashboard with GitHub Pages

`fetch_grid.py` writes exports to `docs/data/`, and `index.html` lives at
`docs/index.html` — both under `docs/` on purpose, since GitHub Pages can
serve straight from that folder with zero extra configuration, and
`index.html`'s relative fetch paths (`data/day.json`) still resolve correctly
from there.

1. On GitHub, go to your repo: `https://github.com/epsilonion-liam/UK-Grid-Tracker`
2. **Settings** → **Pages** (left sidebar, under "Code and automation").
3. Under **Build and deployment** → **Source**, choose **Deploy from a branch**.
4. Under **Branch**, select `main` and folder **`/docs`**, then **Save**.
5. Wait a minute or two, then refresh the page — GitHub shows the live URL:
   ```
   https://epsilonion-liam.github.io/UK-Grid-Tracker/
   ```
6. That's it — no further action needed. Every time `git_push.py` pushes
   updated JSON to `docs/data/`, GitHub Pages automatically redeploys within
   a minute or so, so the live dashboard stays current on its own.

You can check deployment status/history any time under the repo's **Actions**
tab (GitHub Pages deployments show up there) or **Settings → Pages**.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `git_push.py` logs `CRITICAL ALERT: ... Permission denied (publickey)` | Deploy key isn't set up correctly, or `origin` isn't using the `github.com-grid-tracker` alias. Re-check steps 3.4/3.6, and run `ssh -T git@github.com-grid-tracker` to test. |
| `git_push.py` logs a push rejected / non-fast-forward error | Someone (or something else) pushed to `main` since the VM last pulled. Run `git -C /opt/grid-dashboard pull --rebase origin main` once, manually. |
| `systemctl status grid-tracker.service` shows failed | Run `journalctl -u grid-tracker.service -n 100 --no-pager` for the full error output. |
| Dashboard tabs show "failed to load (HTTP 404)" | That JSON file hasn't been exported yet — run `fetch_grid.py` at least once, or check `data/` on whatever is serving `index.html`. |
| `pip3: command not found` | `python3-pip` wasn't installed — see step 0. |

## A note on running as root

Running the service as `root` works, but it means a bug in `fetch_grid.py` or
`git_push.py` (or a compromised dependency) has full system access rather
than being confined to a low-privilege account. If you ever want to switch to
a dedicated `grid-tracker` service account instead, that just means:
1. `useradd --system --create-home --shell /usr/sbin/nologin grid-tracker`
2. `chown -R grid-tracker:grid-tracker /opt/grid-dashboard`
3. Change `User=root` / `Group=root` back to `User=grid-tracker` / `Group=grid-tracker` in `grid-tracker.service`
4. Redo the SSH key steps above as that user instead of root (its home would be `/home/grid-tracker`).
