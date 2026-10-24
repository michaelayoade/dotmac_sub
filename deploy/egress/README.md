# Host egress candidate

`render.py config.example.json <new-output-directory>` renders files only. It
does not install a resolver, reload a firewall, change DNS, or enable enforcement.
The default records unmatched output and permits it. Enforcing output is a
separate reviewed host operation after the live Gate 0 rotation proof.

This corrects an unproven assumption in the original dnsmasq design. The
[dnsmasq manual](https://thekelleys.org.uk/dnsmasq/docs/dnsmasq-man.html) documents
set insertion, without promising per-answer TTL expiry. The candidate uses
[SmartDNS's native nftset TTL option](https://pymumu.github.io/smartdns/en/config/ipset-nftset/)
instead of introducing a custom DNS controller. `nftset-timeout yes` is explicit;
expired-answer serving, prefetch and persistent caching are disabled. No TTL
floor is configured. This is source preparation, not acceptance of a specific
resolver/kernel build.

Provisioning must pin and verify the resolver binary, prove IPv4/IPv6 answer
insertion, refresh and actual timeout expiry on the dedicated test server,
then repeat rotation on staging. Local fixtures must use a real upstream answer;
static hosts-file mappings do not establish upstream answer handling. A failed
TTL/rotation test blocks enforcement. Never describe a fixed set timeout as a
DNS TTL proof.

The CP and OpenBao hostnames are reviewed root-owned host configuration. A signed
deployment document cannot change them. The exact base suffixes include their
subdomains per the resolver's domain matching; that scope must be reviewed during
provisioning. No GitHub address snapshot or DNS-equality gate exists.

The table is dedicated and governs host output only, including daemon pulls.
It does not govern Docker forwarded traffic. Resolver listens on loopback only;
container DNS/forwarding remains a later cutover. Existing private-network,
established, loopback, DNS and NTP exceptions must be checked against the real
host census before enforcement. Load the table before starting a resolver that
populates it. Never flush the host ruleset or replace another owner's table.

Rollback is scoped to this table and resolver configuration after capturing the
current host plan. No rollback command is executed by the renderer.

## container_forward mode

`render.py` also renders a default-deny table for traffic forwarded from
container bridges, for hosts where the container runtime owns the forwarding
rules. Input has `"mode": "container_forward"` and exactly
`scope_ifnames`, `internal_cidrs`, `exempt_src_cidrs`, `allow`,
`hostnet_uids` and `enforce` (see `container-forward.profile-input.example.json`
for the profile form). With `--profile seabone` the interface scope and
host-network uids are fixed by the profile and the remaining keys are input;
`--allowlist FILE` supplies `allow` from a separate measured list.

- Table `inet dotmac_staging_egress`, one atomic `nft -f` transaction, no
  `flush ruleset`. Base chains use `policy accept` and only add drops, so the
  runtime's own chains are unaffected and a runtime restart does not touch it.
- New traffic from the listed bridges is denied unless the destination is an
  explicit internal network or an exact `{ip, proto, port}` allowlist entry.
  There is no private-range acceptance in this mode. Network lists reject
  anything broader than a /16 and the three private blocks. Allowlist entries
  are single addresses; ranges are refused.
- A second chain scopes host-network processes by uid (`hostnet_uids`) on host
  output. Other host users are untouched.
- Dropped tuples are recorded in a timed set and rate-limit logged.
- `enforce=false` records and logs only. `enforce=true` refuses
  documentation-range placeholder allow entries.
- Persistence files are rendered too: a oneshot unit with no `ExecStop` (stopping
  it never opens egress) and a runtime drop-in that refuses to start the runtime
  without the table. The runtime is deliberately not given `Requires=`.
- Disable any packaged ruleset service whose configuration begins with
  `flush ruleset`; it would remove this table and the runtime's rules.

## host_output fixes

The blanket private-range accept and the unrestricted `udp dport 123` accept are
gone. Use optional `local_cidrs` (explicit networks) and `ntp_servers` (named
addresses). Empty means no exception.

Not covered by the unit tests: kernel behaviour. `nft -c` syntax checks run only
where `nft` is installed.
