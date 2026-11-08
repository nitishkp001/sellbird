# SMTP relay with manual replies

For an SMTP relay without an IMAP inbox, configure the local YAML file:

```yaml
channels:
  email:
    provider: smtp
    smtp_send_only: true
    reply_to: replies@example.org
    require_approval: true
```

Store the relay's SMTP_HOST, SMTP_PORT, SMTP_USERNAME and SMTP_PASSWORD in
`.env`. The persona email remains the From address and must be permitted by
the relay. Reply-To must be an existing mailbox you can read. CC is unnecessary.
Restart Harvey after changing these settings.

In this mode, connection checks test SMTP only. Harvey never polls IMAP, stages
only sequence step 1, and blocks previously queued follow-ups and automated
reply messages even if approved. Each initial email still requires approval;
switching to autopilot is rejected or sending is blocked. Existing verification,
compliance, warmup, daily limits and opt-out checks remain in force.

Read and answer replies, bounces and opt-outs in the Reply-To mailbox yourself.
Record opt-outs and contact status in Harvey before approving more mail: this
mode cannot automatically detect replies or complaints. Return-path bounces may
be available only in the relay's logs, rather than in the Reply-To inbox.

This mode changes delivery capabilities, not provider permissions. Zoho CPaaS
allows transactional email only under its published terms; choosing send-only
mode does not make marketing use permitted.
