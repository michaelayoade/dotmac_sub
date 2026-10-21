# sub-staging-ci: Increment 1 grants exactly one leaf read (Gate-0 canary).
# Widened only by a later reviewed increment; never add wildcards here.
path "secret/data/dotmac/sub/staging/gate0-canary" {
  capabilities = ["read"]
}
