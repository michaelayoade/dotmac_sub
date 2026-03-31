#!/bin/bash
# Setup script for nginx configuration
# Domain: selfcare.dotmac.io

set -e

DOMAIN="selfcare.dotmac.io"
APP_DIR="/opt/dotmac_sub"
NGINX_CONF="/etc/nginx/sites-available/${DOMAIN}"
NGINX_ENABLED="/etc/nginx/sites-enabled/${DOMAIN}"

echo "=== DotMac Sub Nginx Setup ==="
echo "Domain: ${DOMAIN}"
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo ./setup-nginx.sh)"
    exit 1
fi

# Check if nginx is installed
if ! command -v nginx &> /dev/null; then
    echo "Installing nginx..."
    apt update
    apt install -y nginx
fi

# Create certbot webroot directory
echo "Creating certbot webroot directory..."
mkdir -p /var/www/certbot

# Copy nginx configuration
echo "Installing nginx configuration..."
cp "${APP_DIR}/nginx/selfcare.dotmac.io.conf" "${NGINX_CONF}"

# Create symlink to enable site
echo "Enabling site..."
ln -sf "${NGINX_CONF}" "${NGINX_ENABLED}"

# Test nginx configuration (will fail if SSL certs don't exist yet)
echo ""
echo "Testing nginx configuration..."
if nginx -t 2>&1 | grep -q "ssl_certificate"; then
    echo ""
    echo "SSL certificates not found. Running initial setup without SSL..."

    # Create temporary HTTP-only config for certbot
    cat > "${NGINX_CONF}" << 'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name selfcare.dotmac.io;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 503;
    }
}
EOF

    nginx -t && systemctl reload nginx

    echo ""
    echo "=== Next Steps ==="
    echo ""
    echo "1. Ensure DNS is configured:"
    echo "   ${DOMAIN} -> your server IP"
    echo ""
    echo "2. Obtain SSL certificate with certbot:"
    echo "   sudo certbot certonly --webroot -w /var/www/certbot -d ${DOMAIN}"
    echo ""
    echo "3. Re-run this script to install full configuration:"
    echo "   sudo ${APP_DIR}/nginx/setup-nginx.sh"
    echo ""
else
    nginx -t && systemctl reload nginx
    echo ""
    echo "=== Setup Complete ==="
    echo ""
    echo "Nginx is configured and running for ${DOMAIN}"
    echo ""
    echo "To renew SSL certificates automatically, add to crontab:"
    echo "  0 0 1 * * certbot renew --quiet && systemctl reload nginx"
    echo ""
fi
