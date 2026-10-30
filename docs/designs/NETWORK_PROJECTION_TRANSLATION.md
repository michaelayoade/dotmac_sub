# Disarmed network projection translation

`network_projection_plan` translates the incumbent planner's work item into
`network.access_projection.v1`; it does not decide access policy. Unsupported
items raise `UntranslatableWorkItem` before a projection-secret authorization is
opened. A caller may quarantine an unsupported item and commit other plans
without leaving an authorization for that item.

The connector's v1 route vocabulary represents only `Framed-Route +=` with a
CIDR, gateway `0.0.0.0`, and metric `1`. Other operators, gateways, metrics, or
malformed route tuples are named refusals, rather than silent projection loss.
Supporting those routes requires an explicitly governed contract/connector
successor; this translator does not expand the published vocabulary.

Command identity hashes the complete canonical step input. Retrying an exact
plan keeps its command ID; opening a fresh authorization changes the plan and
therefore gets a different command ID. Rename and policy fields participate in
that identity even when the desired-row fingerprint is unchanged. Retries must
retain the exact submitted plan, including its authorization reference.

The outbound client checks document string keys and values for supplied
forbidden credential material before JSON serialization and transport. JSON
escaping must not turn quoted, backslash, newline, or Unicode material into a
guard bypass. This guard supplements the translator's credential-reference-only
payload; it does not authorize sending secrets or replace the owner fetch port.

Both network flags remain off. Before wiring live ingress, composition must
validate observation shape, allowed outcomes, and timezone-aware observation
times, and complete the separately gated binding, effective-target, shadow,
TEST_RADIUS retirement, and single-writer work. Source acceptance activates none
of those paths.
