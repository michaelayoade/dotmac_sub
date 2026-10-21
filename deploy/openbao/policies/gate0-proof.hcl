# gate0-proof: proof-1 positive job only. Reads the staging canary; the
# production canary is deliberately absent so its read must return 403.
path "secret/data/dotmac/sub/staging/gate0-canary" {
  capabilities = ["read"]
}
