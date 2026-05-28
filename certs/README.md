# Cloudflare Origin Certificates

Drop the four files generated in the Cloudflare dashboard
(SSL/TLS → Origin Server → Create Certificate) here:

```
certs/
├── sentinelcam.crt
├── sentinelcam.key
├── aarestadt.crt
└── aarestadt.key
```

The Traefik container mounts this directory read-only at
`/etc/traefik/certs/` and picks up the certs via
`traefik/dynamic/tls.yml`.

These files are gitignored — keep them out of the repo.

Cloudflare Origin Certs only work if the affected hostnames are proxied
through Cloudflare ("orange cloud"). For `vpn.sentinelcam.ch` (used by
WireGuard) keep the DNS record as "DNS only" because Cloudflare does not
proxy UDP.
