# QA completion notifications

The existing QA Railway worker can notify the configured Slack channel after a
successful completed run. A notification counts only `verified_qa` admissions:
actual new assignments to Naria with successful Close readback. Backlog clock
resets, tier returns, attempted writes, holds, observe runs and zero-addition
runs do not produce a message.

The message mentions Naria, reports the new-lead count, links to her existing QA
list and shows the completion time in Pacific time. It contains no customer
names or lead IDs. No historical runs are backfilled when this is enabled.

## Configuration and delivery

Use the existing Slack notification bot after its membership in the destination
private channel is approved and verified. Keep its token in Railway variables,
never in source, the deployment bundle or logs:

- `QA_SLACK_NOTIFICATIONS_ENABLED=1` enables completion recording and delivery.
- `QA_SLACK_CHANNEL_ID` selects the approved destination.
- `QA_SLACK_BOT_TOKEN` holds the bot token with `chat:write` permission.

The existing `workflow_meta` store retains each completed positive run and its
notification state. This requires no new table, scheduled job or change to QA
eligibility, timers, ownership, attribution or write limits. The worker sends
after both its QA pass and existing closer producer finish successfully; pending
completions survive restart and are checked at subsequent successful runs.

Each message is claimed durably before its single Slack POST. A successful
response must return the configured channel and a message timestamp before it
is marked sent. Confirmed Slack rejection or throttling can be retried later;
timeouts, unknown provider responses and interrupted sends remain uncertain for
review rather than risking duplicate notifications. Delivery problems do not
replay CRM changes or fail the routing worker. No notification is sent when
the worker runs in observe mode.

To stop future notification capture and delivery, set
`QA_SLACK_NOTIFICATIONS_ENABLED=0` and deploy the variable change. Retain the
saved delivery records. Before re-enabling after a long pause, review pending
messages so old completions are not presented as fresh additions. A saved
notification is never silently redirected if the configured channel changes.

## Activation status

Implemented and covered by synthetic acceptance tests. Private-channel bot
membership requires the user's action-time confirmation before activation.
No live notification has been sent by this change yet.
