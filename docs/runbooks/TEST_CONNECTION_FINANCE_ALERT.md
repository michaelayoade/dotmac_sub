# Activate and verify the native Test Connection Finance workflow

1. Merge the reviewed source through full CI. Rehearse migration 645 → 646 in
   a disposable PostgreSQL database and follow the normal immutable-image
   staging acceptance and production authorization sequence.
2. Confirm the native Test Connection feature from #3429 is available and
   its RADIUS deadline capability is verified. This workflow does not change
   native duration, expiry, security holds, or commercial subscription state.
3. Verify the intended Finance team has active Party-bound staff accounts and
   email addresses. Do not substitute all billing users or an unrelated group.
4. Open Automation Center → Workflows → New workflow. Choose **Network Access
   Control Plane**, **Test Connection created**, and **Test Connections created
   in the preceding 7 days greater than 5**. Choose **Notify Finance of repeated
   Test Connections**, select the approved Finance team, save and review the
   draft, then explicitly activate it. No workflow is seeded by deployment.
5. On an approved staging test customer, create native Test Connections through
   the existing customer subscription form. Let each interval expire before
   repeating on the same subscription. Never alter production clocks, grant
   records, or commercial statuses to accelerate this test.
6. Verify no Finance notice through five creations and a successful review
   action at six. Count across multiple subscriptions for the same account,
   keep another customer's count separate, and verify UTC boundary behavior in
   the disposable automated test lane. Outage compensation must not count.
7. Inspect workflow run/version, source event, native grant, review receipt,
   personal inbox and queued email references. Inspect `notification_deliveries`
   and have intended staging recipients confirm receipt: queued/action-success
   alone is not proof of provider delivery.
8. Retry a failed action and repeat the same source event. Successful staging
   must not duplicate notices or change its saved audience. Delayed processing
   must use the creation-time count, not the time of redrive.
9. Publish in production only after normal deployment and acceptance. Production
   synthetic activations need a designated test account and agreed recipients;
   never run pytest or activate unrelated customers merely to test alerts.

To stop future alerts, pause the workflow through Automation Center. Preserve
source grants, events and review receipts; the notification action cannot undo
access and no downgrade may erase delivered review evidence.
