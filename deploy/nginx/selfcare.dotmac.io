server {
    server_name selfcare.dotmac.io;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    # Static files
    location /static/ {
        alias /root/projects/dotmac_sub/static/;
        expires 30d;
        add_header Cache-Control "public, immutable";
        access_log off;
    }

    # Uploads
    location /uploads/ {
        alias /root/projects/dotmac_sub/uploads/;
        expires 1d;
        access_log off;
    }

    # WebSocket
    location /ws {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }

    # All other requests → app
    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    # Upload limit
    client_max_body_size 10M;

    # Deny hidden files
    location ~ /\. {
        deny all;
        access_log off;
        log_not_found off;
    }

    listen [::]:443 ssl; # managed by Certbot
    listen 443 ssl; # managed by Certbot
    ssl_certificate /etc/letsencrypt/live/selfcare.dotmac.io/fullchain.pem; # managed by Certbot
    ssl_certificate_key /etc/letsencrypt/live/selfcare.dotmac.io/privkey.pem; # managed by Certbot
    include /etc/letsencrypt/options-ssl-nginx.conf; # managed by Certbot
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem; # managed by Certbot
}

server {
    if ($host = selfcare.dotmac.io) {
        return 301 https://$host$request_uri;
    } # managed by Certbot

    listen 80;
    listen [::]:80;
    server_name selfcare.dotmac.io;
    return 404; # managed by Certbot
}
