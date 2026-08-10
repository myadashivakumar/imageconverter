#!/usr/bin/env bash
# Sets up this app on a fresh Ubuntu 24.04 EC2 instance: Python venv, systemd
# service, nginx reverse proxy. Run as the `ubuntu` user via sudo, from
# inside the cloned repo directory (i.e. `bash deploy/setup-ec2.sh`).
#
# Optional: pass the GitHub Actions CI deploy public key as $1 to register it
# for passwordless SSH, so .github/workflows/deploy.yml can auto-deploy on
# push: `bash deploy/setup-ec2.sh "ssh-ed25519 AAAA... github-actions-deploy"`
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="$(whoami)"
SERVICE_NAME="imagetools"
CI_PUBLIC_KEY="${1:-}"

if [ -n "$CI_PUBLIC_KEY" ]; then
  echo "==> Registering CI deploy key for passwordless SSH"
  mkdir -p ~/.ssh
  chmod 700 ~/.ssh
  touch ~/.ssh/authorized_keys
  grep -qxF "$CI_PUBLIC_KEY" ~/.ssh/authorized_keys || echo "$CI_PUBLIC_KEY" >> ~/.ssh/authorized_keys
  chmod 600 ~/.ssh/authorized_keys
fi

echo "==> Installing system packages"
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip nginx

echo "==> Adding a 1GB swapfile (t3.micro only has 1GB RAM; pip installs of numpy/opencv/pymupdf are safer with headroom)"
if [ ! -f /swapfile ]; then
  sudo fallocate -l 1G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

echo "==> Creating virtualenv and installing dependencies"
cd "$APP_DIR"
python3.12 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "==> Writing systemd service"
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<EOF
[Unit]
Description=Image & Document Tools (FastAPI/uvicorn)
After=network.target

[Service]
User=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "==> Writing nginx reverse-proxy config"
sudo tee "/etc/nginx/sites-available/${SERVICE_NAME}" > /dev/null <<'EOF'
server {
    listen 80;
    server_name _;
    client_max_body_size 50M;  # allow image/PDF/docx uploads larger than nginx's 1M default

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF
sudo ln -sf "/etc/nginx/sites-available/${SERVICE_NAME}" "/etc/nginx/sites-enabled/${SERVICE_NAME}"
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t

echo "==> Starting services"
sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_NAME}"
sudo systemctl restart nginx

PUBLIC_IP="$(curl -s -m 3 http://169.254.169.254/latest/meta-data/public-ipv4 || echo '<your-ec2-public-ip>')"
echo ""
echo "==> Done. Visit: http://${PUBLIC_IP}/"
echo "    Check app logs with: sudo journalctl -u ${SERVICE_NAME} -f"
if [ -n "$CI_PUBLIC_KEY" ]; then
  echo "    CI deploy key registered - set the EC2_HOST GitHub secret to: ${PUBLIC_IP}"
  echo "    (consider allocating an Elastic IP so this address doesn't change on reboot)"
fi
