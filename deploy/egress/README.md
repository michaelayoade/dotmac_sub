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
