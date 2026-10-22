# Email

The only outbound-only channel in v1, and the only one where you choose the
transport: your own SMTP server, Resend, or Amazon SES. The sending domain, the
reputation and the bill stay yours.

Specification: `docs/SPEC.md` §6.7 (the channel) and §11.10 (the `send_email`
node). Implementation: `apps/channels/providers/email.py`, with the three
transports behind `apps/channels/providers/email_backends.py`.

> **This page is about the email channel**, which sends flows, broadcasts and
> inbox replies to your contacts. It is set up in the app, per workspace.
>
> The mail BrightBean Chat sends to *its own users* (signup verification,
> password resets, invitations, notifications) is a different thing, set once
> per deployment with the `EMAIL_*` variables in `.env`. See
> [Account email](../../README.md#account-email). The two share no settings.

## Setup at a glance

1. Publish [SPF, DKIM and DMARC](#before-you-connect-anything-spf-dkim-and-dmarc)
   for the domain you will send from.
2. [Connect the channel](#connecting): pick SMTP, Resend or SES, enter a
   from-address on that domain and the provider's credentials.
3. Click **Send a test email** on the connection's page.
4. Optional, Resend or SES only: turn on bounce handling
   ([Resend](#resend), [SES](#amazon-ses)).

Nothing goes in `.env` for any of it. You can send after step 3. Step 4 is what
lets hard bounces and spam complaints suppress an address automatically.

The channel only sends. A contact who replies is writing to your from-address's
mailbox, and the reply never reaches the BrightBean Chat inbox.

## Before you connect anything: SPF, DKIM and DMARC

Set all three up on the domain you are going to send from, before you send
anything. This is not a nicety and not a "later" item:

* **SPF** publishes which servers may send as your domain. Without it, anything
  you send is a message claiming to be from you that nothing can corroborate.
* **DKIM** signs each message so a receiver can verify it was not altered and
  really came from you. Your provider gives you the record to publish; SES and
  Resend both generate one per domain, and an SMTP provider will too.
* **DMARC** tells receivers what to do when the first two fail, and — the part
  people miss — is what makes them *checked* rather than merely present. Start
  at `p=none` with a reporting address, read the reports for a week, then move to
  `p=quarantine` and eventually `p=reject`.

Since February 2024, Google and Yahoo **reject** bulk mail from domains without
them. A deployment that skips this step does not get "slightly worse delivery";
it gets a channel that silently does not work for a large share of recipients.

Your provider publishes the exact records. What BrightBean Chat can tell you is
that all three belong to the domain in your from-address, which is also the
domain that becomes this channel's identity — see below.

## The sending domain is the channel's identity

`external_id` is the domain of your from-address (SPEC §5), and the unique
constraint on it is **deployment-wide**: two workspaces on one deployment cannot
both claim `example.com`.

That is deliberate, and email is the channel where it matters most. SPF, DKIM
and DMARC are properties of a domain, one set of DNS records governs it, and a
domain's sending reputation is shared by everything that sends as it. Two tenants
sending as one domain would be sharing a reputation neither of them controls, and
one of them getting reported for spam would take the other down with it.

Use a subdomain per workspace if you need to separate them —
`mail.team-a.example.com` and `mail.team-b.example.com` — which is also the
practice that keeps marketing mail from damaging the reputation of your
transactional mail.

## Connecting

Open *Settings → Channels*, find **Email** under *Available to connect*, and
click **Set up** (`/w/<workspace>/settings/channels/email/connect/`). Pick a
provider at the top of the form, and the fields below it change to match.

Every provider asks for the same three things first:

| Field | Notes |
|---|---|
| From address | What recipients see, e.g. `hello@mail.example.com`. Its domain becomes the channel's identity and must be the domain you set up SPF, DKIM and DMARC for |
| From name | Optional. The name shown next to the address, e.g. "Acme Support" |
| Channel name | Optional. What the channel is called inside BrightBean Chat. Defaults to the domain |

Then the provider's own fields, below. Whichever provider you choose, the
credentials are checked against it **before anything is written**, so a
mistyped password leaves no half-configured channel behind. They are stored in
an encrypted column and never shown again; changing them means entering them
again.

Once it saves, click the new connection in the channels list and press **Send a
test email**. It sends one real message through the connection to the address
you log in with, carrying the same unsubscribe headers and footer as every other
send. If it arrives, the channel works.

The channels list will keep showing *Connected, but nothing has arrived yet* for
an SMTP connection, and for a Resend or SES one until bounce handling is on and
the provider's first notification arrives. That line counts inbound webhook
deliveries, and provider notifications are the only thing an email connection
ever receives. It does not mean sending is broken.

### Your own SMTP server

| Field | Notes |
|---|---|
| Host | Your provider's SMTP hostname |
| Port | 587 for STARTTLS, 465 for SSL/TLS |
| Encryption | STARTTLS, SSL/TLS, or none — pick "none" only for a relay on your own private network |
| Username / password | Whatever your provider issued. Many issue an app-specific password rather than your account one; use that |

BrightBean Chat opens a connection and authenticates when you connect, so a
wrong password is reported on the spot rather than at the first send.

**Relaying through localhost or your own network needs `EMAIL_SMTP_ALLOW_INTERNAL=true`.**
By default the host you give is resolved and refused if it points at loopback, a
private range, or the cloud metadata service — on a multi-tenant deployment
`manage_channels` is a *workspace* permission, so an unguarded SMTP host field
is an internal port scanner with a form in front of it. A single-tenant install
relaying through a local postfix or a sidecar sets the flag and the check steps
aside. It is a separate flag from `EXTERNAL_REQUEST_ALLOW_PRIVATE`, which
governs the HTTP guard and never opens loopback for anything.

SMTP has **no delivery callback**, so a connection on this transport records a
message as `sent` when the relay accepts it and learns nothing more. Bounces
arrive as email to your return-path address, and reading them is out of scope in
v1 — if you need bounce handling, use Resend or SES.

### Resend

First add your sending domain under *Domains* in the Resend dashboard and wait
for it to show as verified. The DNS records Resend gives you there are the SPF
and DKIM from the section above. Resend refuses to send from a domain it has not
verified, and the connect step checks only the API key, so an unverified domain
first shows up as a failed test email.

Then paste an API key from the Resend dashboard. Optionally paste a **webhook
signing secret** as well; you need it for bounce handling and not for sending,
so you can add it later.

To turn on bounce handling:

1. Connect the channel. Its page then shows a webhook URL of the form
   `https://<your-deployment>/webhooks/email/resend/<connection id>/`.
2. In Resend, create a webhook endpoint pointing at that URL, subscribed to
   `email.bounced`, `email.complained` and `email.delivered`.
3. Copy the signing secret Resend shows you (`whsec_…`) and paste it into the
   **Bounce handling** box on the channel's page.

Step 3 is on the channel's page rather than the connect form because of the
order these have to happen in: the webhook URL contains the connection's id, so
the connection has to exist before you can create the Resend endpoint, and the
signing secret only appears when you do. The SES topic ARN is the same shape and
the same box. Until the secret is set, Resend's deliveries are rejected with a
403 — the signature cannot be checked, so they are not trusted.

Every delivery is verified as a Svix signature — an HMAC over the raw body,
checked before the body is parsed, with a five-minute timestamp tolerance. A
delivery that does not verify gets a 403, exactly like one naming a connection
that does not exist.

### Amazon SES

First create a **domain identity** for your sending domain in the SES console
(*Identities → Create identity*), in the region you will send from, and publish
the DKIM records it gives you. SES sends only from verified identities.

Then create an IAM user for BrightBean Chat with an access key, and attach this
policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["ses:GetAccount", "ses:SendEmail", "ses:SendRawEmail"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": "sns:ConfirmSubscription",
      "Resource": "*"
    }
  ]
}
```

`ses:GetAccount` is the one people leave out. The connect step and the test
button both call it to check the key, so without it the form says AWS did not
accept the credentials even though the key itself is fine. The second statement
is only for bounce handling; narrow its `Resource` to your topic's ARN once you
have one.

| Field | Notes |
|---|---|
| Access key ID / secret | The IAM user's access key, from above |
| Region | The SES region your domain is verified in, e.g. `eu-west-1` |
| Bounce topic ARN | Optional, and easier to add afterwards. See below |

New SES accounts are in the **sandbox**, where you may only send to addresses you
have verified and are capped at a low daily rate. Request production access
before you rely on it; the connect step will succeed either way, because it
checks the credentials rather than trying a send.

To turn on bounce handling, do these **in this order**:

1. **Create an SNS topic** in the same region as SES, and copy its ARN.
2. **Save the ARN in BrightBean Chat** first: paste it into *SES bounce topic
   ARN* under *Bounce handling* on the channel's page, and save.
3. **Subscribe the topic to the channel.** In SNS, create a subscription on the
   topic with protocol **HTTPS** and the channel's webhook URL as the endpoint:
   `https://<your-deployment>/webhooks/email/ses/<connection id>/`. Within a
   minute the subscription's status should change from *Pending confirmation* to
   *Confirmed*.
4. **Point SES at the topic.** Open your domain under *Identities*, then the
   *Notifications* tab, and edit *Feedback notifications*: set Bounce and
   Complaint (and Delivery, if you want them) to the topic.

The order matters because confirmation is only accepted from the topic saved in
step 2. Subscribe first and SNS's confirmation request arrives before the app
knows the topic, gets ignored, and the subscription sits at *Pending
confirmation* forever.

Confirmation also needs the worker running and `sns:ConfirmSubscription` on the
key. When SNS posts its `SubscriptionConfirmation`, BrightBean Chat queues a job
that confirms it by **calling the AWS API** with the credentials this connection
already holds. It never fetches the `SubscribeURL` in the payload, because that
URL is supplied by whoever sent the request and fetching it would be a
server-side request forgery for no gain.

A subscription stuck at *Pending confirmation* is missing one of those three:
the saved ARN, the permission, or the worker. Fix it, then select the
subscription in the SNS console and click **Request confirmation**.

Every SNS delivery's RSA signature is verified against AWS's signing
certificate. The certificate URL in the payload has to match
`https://sns.<region>.amazonaws.com/SimpleNotificationService-*.pem` exactly and
is fetched through the deployment's SSRF guard, so a forged notification naming
a certificate you control is refused rather than trusted.

#### Which topic is yours

A valid signature proves **AWS** sent a notification. It does not prove *your*
topic did — anyone can create a topic in their own AWS account, publish a
message whose body is a bounce notification naming somebody else's address, and
have AWS sign it for them. So notifications are also checked against the topic
this channel listens to.

So **bounce handling is off until you set the ARN**, either when connecting or
afterwards on the channel's page. That is deliberate rather than strict: trusting
the first topic seen would not help, because nobody needs SNS to *deliver*
anything to reach this route — an attacker can publish to a topic they own,
capture the payload AWS signed for them, and post it here themselves. Until an
expected ARN is recorded there is no way to tell that apart from a real bounce,
so notifications are ignored and logged rather than acted on.

## Unsubscribe is not optional

**Every** email BrightBean Chat sends carries:

* a `List-Unsubscribe` header pointing at a hosted `/u/<token>` page;
* a `List-Unsubscribe-Post: List-Unsubscribe=One-Click` header, which is what
  makes Gmail and Apple Mail show the one-click unsubscribe button next to the
  sender's name (RFC 8058);
* an unsubscribe link in the footer of the body, in both the HTML and the plain
  text.

The link keeps working after the channel it was sent from is disconnected. That
costs one thing worth knowing: the token carries the mailbox, so anyone holding
the URL can read the address out of it — which is already true of anyone who can
read the message it was delivered in.

There is no setting that turns any of them off, and no way for a flow to omit
them: they are added by the adapter, downstream of every path that can produce
an email — the `send_email` node, a broadcast, an inbox reply, the API. That is
SPEC §6.7's "compliance in-core", and it is also what Google and Yahoo require
of bulk senders.

The link never expires. An unsubscribe link sits in an inbox for years, and one
that stopped working would be a compliance problem rather than a broken link.

**Opening the link does not unsubscribe anybody** — it shows a page with one
button, and pressing that button does. That is deliberate: corporate mail
gateways, Outlook's Safe Links and antivirus plugins fetch every URL in a message
before a human sees it, so a link that acted on being *opened* would unsubscribe
a share of every list on delivery. The one-click header path is a POST from the
mail client itself and is honoured with no page at all.

## Suppression

Unsubscribing does two things: it records the opt-out on the contact's email
identity, and it adds the address to the workspace's **suppression list**.

The second one is what survives everything else. `apps/contacts/imports.py`
never fabricates a channel identity for an imported contact — a spreadsheet
column is not consent — and deleting a contact leaves a tombstone that a
re-import does not match, so a contact that goes away and comes back is a new
row with no identity and no memory of the opt-out. A bounce is not a fact about a
contact record; it is a fact about a mailbox, so that is what the list is keyed
on.

| Event | Message status | Suppressed? |
|---|---|---|
| Unsubscribe (footer link or one-click) | — | Yes |
| Hard bounce (SES `Permanent`, Resend `hard`) | `failed` | Yes |
| Soft bounce (SES `Transient`, Resend `soft`) | `failed` | No |
| Spam complaint | `failed` | Yes |
| Delivery | `delivered` | No |

A soft bounce is a full mailbox or a server having a bad day; it may clear, and
suppressing on one would lose you recipients permanently over a transient fault.
A hard bounce is the receiving server saying the mailbox does not exist, and a
complaint is a person pressing "spam" — both mean stop, and continuing after
either is what gets a sending domain blocklisted.

Suppression is **per workspace**, not per deployment: two workspaces send from
different domains, and one tenant's bounce is not evidence about another's
relationship with that mailbox.

There is deliberately no way to un-suppress an address from the product. Opt-out
is a chokepoint precisely so it cannot be bypassed (SPEC §19), and a toggle that
un-said it would be a bypass with a friendlier name. If somebody wants to
re-subscribe, they have to say so themselves — through a form your flow collects
with `data_collection`, which records the consent with its own audit trail.

## What goes out

The flow engine renders one abstract message and
`apps.channels.downgrade.downgrade` adapts it before the adapter sees it, so
these limits are enforced in one place rather than per platform.

| Limit | Value | Where it is applied |
|---|---|---|
| Subject | 300 characters | `send_email`'s schema; truncated rather than refused at send |
| Body | 100 000 characters | `send_email`'s schema and `Capabilities.max_text_len` |
| Buttons | none | Email has no button widget — see below |
| Images | inline | `<img>` in the body; audio, video and files become links |
| Attachments | none | Attachments are a deliverability liability; media is linked |

**Buttons become links.** A URL button is inlined into the text as
`label: url` by the shared downgrade and then turned back into a real anchor
when the body is built, because for email a "button" is a hyperlink. A *postback*
button becomes a numbered option, which is honest but not useful — an email
cannot receive a reply, so do not put postback buttons in one.

**A from-override must stay on the sending domain.** SPEC §11.10's optional
per-message From address is there for `billing@` rather than `hello@`, and that
is as far as it goes: node config is written by anyone with `edit_flows`, while
what this channel sends *as* is `manage_channels`, which is admin-only. An
override on another domain is ignored and the connection's own address is used.

**Only the `send_email` body is HTML.** Text from anywhere else — a
`send_message` block, an inbox reply, an API send — is plain text and is escaped
on the way into the email, so a contact whose name is `<img src="…">` arrives as
words rather than as a tracking pixel nobody configured.

**The body is normalised when it is saved, not only when it is sent.** It is the
one field in the product that stores markup, and the flow builder renders it as
markup while you edit — so a body saved by one member is markup another member's
browser runs when they open the node. `save_draft` puts it through the same
allowlist the email uses, on every write path including the API, so what is
stored is already what will be sent. Anything outside the allowlist is dropped
rather than the save being refused; reopening the node shows you what survived.

**A message with no subject of its own gets one.** `send_email` always has a
subject; an inbox reply has no field for one, because on every other platform a
subject is meaningless. Those are sent as "Message from &lt;your from-name&gt;",
or a configured `default_subject` if the connection has one.

**The plain-text alternative is generated**, from the same HTML the recipient
gets. You do not write it twice, and the two cannot drift apart: links keep their
destination as `text <url>` and lists become bullets.

**Placeholders in the body are HTML-escaped.** A contact whose first name is
`<script>alert(1)</script>` arrives as text, while the markup *you* wrote stays
markup (SPEC §19, SECURITY-BASELINE §3). The subject is a header rather than an
HTML context, so it is substituted as plain text.

## Rate limits

The default is 10 sends per second per connection, from the platform's policy
row (`apps/channels/policy.py`), enforced by the connection's token bucket. Raise
or lower it for a deployment with `DEFAULT_SEND_RATE_OVERRIDES`.

There is no throttle inside the adapter, and that is deliberate: the global limit
is the token bucket, and the per-recipient limit is satisfied by the shape of the
system — SPEC §9.6 serialises everything one contact does behind one advisory
lock, so a second timer in the adapter would be a sleep held *inside* that lock.

Note that your provider's limit is usually the binding one, and it is often much
lower than 10/s on a new account. SES sandbox accounts start at 1/s.

## Deliverability, briefly

Beyond SPF, DKIM and DMARC:

* **Warm the domain up.** A brand-new domain sending ten thousand messages on
  its first day looks exactly like a compromised one. Start small and grow over
  a couple of weeks.
* **Send to people who asked.** The suppression list handles the aftermath;
  it cannot repair a reputation built on a purchased list.
* **Keep a separate subdomain for bulk mail**, so a bad campaign cannot take
  your password-reset emails down with it.
* **Watch the bounce rate.** Above about 5% and providers start throttling you;
  SES will pause your account outright.

## Out of scope in v1

Inbound email — replies never reach the inbox, and the webhook route carries
delivery notifications only. Open and click tracking, which belongs to
[#26](https://github.com/brightbeanxyz/brightbean-chat/issues/26) and covers
every channel at once. A drag-and-drop email builder: the body is a rich-text
field, per SPEC §6.7. Warmup tooling, per-recipient analytics beyond
`message.status`, and reading bounces out of an SMTP return-path mailbox.
