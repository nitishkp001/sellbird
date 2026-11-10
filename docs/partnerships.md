# Agency partnerships

Open **Partnerships** in the dashboard. This workflow shares the existing
companies, contacts, Outbox and reply handling; it does not start a second
outreach process.

## Import and edit

Import a CSV or JSON list, inspect the merge preview, then apply it. CSV
requires `id`, `company` and an HTTP(S) `website`. Optional fields include
`region`, `city`, `target_person`, `target_role`, `target_profile`,
`direct_email`, `business_inbox`, `partnership_angle`, `research_sources`,
`research_notes`, `next_action`, `followup_date`, `email_subject`, and
`email_body`. Research source URLs use ` | ` as the separator.

Imports match normalized company domains and preserve external tracker IDs.
Existing account edits, contact verification, suppression and activity history
survive a repeat import. A SQLite snapshot is taken before an import through
the dashboard. Conflicting identities are refused rather than silently merged.

Imported contacts have a separate `partnership` status. They are not fed into
the general writer's new-contact queue. A published address stays a guess
until checked; it is not marked deliverable just because it appeared in a CSV.
General company inboxes remain separate from named direct contacts.

Open an account to edit its research and next action. Use **Check with
configured email verifier** when a named direct address is ready to check.
This invokes the existing verifier and can consume configured provider credits.
An inconclusive result remains unverified. Changing the address resets its
verification.

## Moving a company in from the pipeline

On **Companies**, open a company and press **Move to Partnerships**. It becomes
an account and keeps its place in Contacts: the best-scored named person is
linked as the decision maker, using the contact record that already exists, so
an email verification carries over. The account starts with:

- the fit score and reasons, the company's notes and the writer's notes on the
  contact, as research notes;
- the writer's "... Angle:" line, if it has one, as the partnership angle;
- the company's website and the page the contact was found on, as sources.

The observation, proposed component and first engagement stay blank: those are
yours to write from what you actually saw. A company with only a general inbox
gets that inbox as its company inbox and no named person. If the pipeline
already emailed the company, the account opens at **Review prior outreach**.
A company where someone replied or opted out is refused: handle it from
Conversations. Moving the same company twice opens the existing account.

## One company, one track

A company with a partnership account is never written to by the cold-email
pipeline, whoever else works there:

- the writer skips every contact there, and the heartbeat doesn't count them as
  waiting to be written;
- cold emails already drafted or approved for the company are cancelled when the
  account is created, imported or moved, and swept again on every sender cycle;
- a cold sequence email to anyone there is cancelled just before sending, as a
  last check. Replies to someone who wrote to you are not cold and still go
  through their own review. Emails already sent are left alone.

Companies shows these as **Partnership**, with the reason, instead of waiting for
the writer. There is no button to send a company back to the pipeline. To leave
a company alone in both tracks, set its account's working stage to Paused.

## One-month pilot

The pilot begins in **draft**. Set the start and end dates and activate it
from the dashboard when ready. It runs in Asia/Kolkata, for a window of
28–31 days. Paused, future and expired pilots cannot dispatch partnership
emails or record new requests through the active workflow.

The initial channels are email and LinkedIn connection requests. There is
no automatic LinkedIn invitation sender or DM sender in this feature. Open
the person's researched LinkedIn profile, complete the request there, and
record the request afterward. Record acceptance separately. Imported
historical activity does not imply acceptance.

Emails and requests are not counted from queued tasks. Actual email sends
come from Outbox results. LinkedIn activity is labeled as operator-recorded.
An email queued after a recorded request or sent email is scheduled at least
two working days later. Replies and do-not-contact events stop cold outreach.
Inbound conversations can continue after the pilot expires.

## Briefs and email

Save brand settings once, including an optional PNG logo and relevant team
background. On an account, write a sourced observation, the proposed
component and a first engagement. **Generate PDF** creates a new
immutable version. Preview it before approving it.

Material changes require a new brief version before a new email can attach
it. An already queued email keeps its exact reviewed version, even if newer
versions are generated. Missing or unapproved bound assets hold the email;
they are never silently dropped.

Edit the email yourself, or use **Draft with Harvey + Humanizer**, which
uses the configured writing model and the installed Humanizer skill. No draft
action sends an email. The result still needs review.

Queue email-only or select an approved PDF version. The email enters the
existing **Outbox** as pending review. Partnership emails require a human
Outbox approval and a verified direct email, even if other outreach uses
autopilot. The normal compliance, sending limits, reply, suppression and
uncertain-send protections remain in place. PDF attachments are supported by
the native Gmail, SMTP and Microsoft 365 providers. This workflow does not
deploy PDF campaigns to Instantly.

The first-month limit is four actual emails per account. Rejected first
drafts can be revised without being misclassified as follow-ups. A send
whose result is uncertain is not automatically retried.

## Brief kit

By default a brief is the built-in two-page PDF (a kit sets its own page count). To print briefs in your own
design, point `partnerships.brief_kit` at a **kit**: a folder holding your HTML
template, your fonts and your logo. Put the setting in `sellbird.local.yaml`, which
git ignores, so your design and its path stay out of the repository.

```yaml
partnerships:
  brief_kit: /path/to/brief-kit
```

The folder holds `kit.json`, the template, and the files it names:

```json
{
  "name": "My agency brief",
  "brand": "My Agency",
  "template": "template.html",
  "fonts": [{"family": "Inter", "weight": 400, "file": "assets/Inter-400.ttf"}],
  "logo": "assets/logo.png"
}
```

The template is ordinary HTML. Write your slides or pages as usual and put these
fields where the text belongs:

| Field | Filled with |
|---|---|
| `{{COMPANY}}` | The company name |
| `{{OBSERVATION}}`, `{{COMPONENT}}`, `{{FIRST_ENGAGEMENT}}` | The text you wrote on the account, one paragraph per line |
| `{{SOURCES}}` | The research source links |
| `{{FONTS}}` | Your fonts, embedded (place it in your `<style>`) |
| `{{LOGO}}` | Your logo as an `<img class="brand-logo">` |

A template must use the first five. Everything typed into an account is escaped,
so it cannot add markup, and a typed `{{...}}` is never read as a field. Page numbers
in `<span class="index">01 / 08</span>` follow the real page count. The template's own
`@page` rule sets the page size, so a 16:9 deck prints as 16:9.

Fonts are embedded in the page, never looked up by name, so a brief prints in the
same fonts on every machine instead of whatever that machine has. Harvey refuses to
export a brief whose font failed to load, or whose text is set in a font the kit
doesn't embed, and one whose text doesn't fit its page. It names the page to shorten.

The template is previewed in the dashboard, so it may not contain scripts, event
handlers, frames, `@import` or links to other sites. Printing is offline, so a link
to a web font or image would silently fall back; Harvey refuses it instead. Every file
must be inside the kit folder, with a font or logo no larger than 3 MB.

### Varying the brief by company type

A kit can pick its proof pages by what kind of company the brief is for. List the types in
`kit.json`, each with keywords, and name the one to use when nothing matches:

```json
{
  "segments": [
    {"id": "commerce", "label": "Commerce studios", "keywords": ["shopify", "ecommerce", "erp"]},
    {"id": "app", "label": "App companies", "keywords": ["mobile app", "ios", "android"]},
    {"id": "general", "label": "Other or not sure", "keywords": []}
  ],
  "default_segment": "general"
}
```

In the template, wrap a page, or a row inside a page, that only suits some types:

```html
<!-- only: commerce, fintech -->
  <section class="slide">…payments proof…</section>
<!-- /only -->
```

Anything outside such a wrapper is in every brief, so the shared pages stay shared. The wrappers
can't nest, and every type must still carry all five account fields, so what you write for a company
can never be dropped. Harvey checks both when the kit loads, and refuses a wrapper that names a type
`kit.json` doesn't declare. The `<!-- only -->` comments are invisible, so the raw template still opens
in a browser showing everything.

On the account, **Company type** picks the type. Left on *Suggest from my notes*, Harvey counts each
type's keywords (whole words, case-insensitive) in the account's **research notes** and **observation**,
the notes that describe the company. It doesn't read the proposed component: a studio you offer a
payments component to is not a payments company. The type with the most matches wins, the one listed
first wins a tie, and no match gives the default. Agencies often describe several kinds of work, so the
account says when the top two were within one keyword (a close call), and then it is worth choosing by
hand. The page shows which type will be used and why. Each type is a different version of the brief,
so changing the type needs the new version reviewed and approved.

Each version is stamped with a fingerprint of the kit. Changing the template, a font
or the logo makes the next brief a new version that needs another review, and a brief
you already approved keeps the exact version you reviewed. If `brief_kit` is set but
the kit can't be used, generating a brief fails with the reason rather than quietly
printing the built-in design.

## Files on disk

Every brief is built in two steps: Harvey fills the company's own text into an HTML page,
then prints that page to PDF. Each time you generate a brief, both are also saved as files
named for the company, so you can open, review and attach them without the dashboard:

```text
data/partnership-briefs/
  S50-example-studio/
    S50-example-studio-brief-v1.html
    S50-example-studio-brief-v1.pdf
```

The folder is the account's tracker ID and company name. Each new version adds a pair
(`-v2`, `-v3`) and leaves the earlier ones alone. The HTML is exactly the page that was
printed, with its fonts embedded, so it opens on its own in any browser and prints to the same
PDF. The files are private to your user, because they hold your research on the company.

They are outputs. To change a brief, edit the account and generate a new version: an HTML file
you change by hand is not the version you reviewed and approved, and the email binds the approved
PDF held in the database. Renaming the company in the account starts a new folder from then on.

By default the folder is `partnership-briefs` beside the database. To save them elsewhere, set
`partnerships.output_dir`:

```yaml
partnerships:
  output_dir: /path/to/briefs
```

If the files can't be saved (no permission, a full disk), the brief is still generated and the
dashboard says why; generate it again once that is fixed.

## Optional video

Generate a 60–90 second recording plan from the reviewed observation and
proposed component. Record the walkthrough and save its actual video URL.
Script preparation does not fabricate a Loom recording or upload a video.

## Data

Partnership tables are added by the existing schema migration mechanism in
the same database. Assets and their PDF bytes stay in that database. CSV
export is available in the dashboard, including source fields and recorded
activity. The database is authoritative after import; exported spreadsheets
do not synchronize edits back automatically.

No emails or invitations are sent by importing, editing, generating a brief,
approving a brief or activating the pilot alone. Emails leave only through
the existing Outbox/heartbeat process after its checks and approval.
