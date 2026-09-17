#!/usr/bin/env bash
# setup_deploy_key.sh
# Reference/runbook for provisioning a GitHub deploy key on a headless Ubuntu/Debian VM
# so grid-tracker.service can `git push` without interactive password prompts.
#
# Run these steps as the service account (e.g. `grid-tracker`), not root.
# Review each command before running; this script is meant to be read and
# executed step-by-step, not blindly piped to bash.
set -euo pipefail

REPO_DIR="/opt/grid-dashboard"
SSH_KEY="$HOME/.ssh/id_ed25519_grid"

# 1. Create the service account (run once, as root/sudo) -------------------
#   sudo useradd --system --create-home --shell /usr/sbin/nologin grid-tracker
#   sudo mkdir -p "$REPO_DIR"
#   sudo chown grid-tracker:grid-tracker "$REPO_DIR"

# 2. Generate a dedicated ed25519 deploy key (no passphrase, for unattended use)
ssh-keygen -t ed25519 -C "grid-tracker@$(hostname)" -f "$SSH_KEY" -N ""

# 3. Print the public key to paste into GitHub ------------------------------
echo "Add the following PUBLIC key to GitHub -> Repo -> Settings -> Deploy keys"
echo "(check 'Allow write access' so the VM can push):"
cat "$SSH_KEY.pub"

# 4. Restrict this key to github.com only, avoiding changes to global ssh config
cat >> "$HOME/.ssh/config" <<EOF
Host github.com-grid-tracker
    HostName github.com
    User git
    IdentityFile $SSH_KEY
    IdentitiesOnly yes
EOF
chmod 600 "$HOME/.ssh/config"

# 5. Pre-trust GitHub's host key to avoid an interactive "yes/no" prompt
ssh-keyscan -t ed25519 github.com >> "$HOME/.ssh/known_hosts"

# 6. Point the repo's origin remote at the aliased host -----------------
#    (run inside the repo directory, e.g. /opt/grid-dashboard)
#    git remote set-url origin git@github.com-grid-tracker:<owner>/<repo>.git

# 7. Verify authentication non-interactively --------------------------------
#    ssh -T git@github.com-grid-tracker
#    (expect: "Hi <owner>/<repo>! You've successfully authenticated ...")

# 8. Configure committer identity for the service account -------------------
#    git -C "$REPO_DIR" config user.name "grid-tracker-bot"
#    git -C "$REPO_DIR" config user.email "grid-tracker-bot@users.noreply.github.com"

# 9. Test an end-to-end push -------------------------------------------------
#    git -C "$REPO_DIR" push origin main

echo "Deploy key setup steps printed above still require the manual GitHub"
echo "step (adding the public key) and the commented git remote/push checks."
