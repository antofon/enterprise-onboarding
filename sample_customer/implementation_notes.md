# Apex Equipment Services: implementation kickoff notes

*Fictional customer. These are the implementation engineer's notes from the kickoff call, the kind of document that exists at the start of every real onboarding.*

## Engagement

- Customer: Apex Equipment Services (industrial equipment rental and service, US and Canada)
- Contract signed; go-live target is three weeks out
- Scope: migrate accounts, contacts and subscriptions into Meridian before go-live; activity history is nice-to-have

## Source systems

| system | what we get | how | owner on their side |
|---|---|---|---|
| Legacy CRM ("SalesTrack 2011", on-prem) | `organizations.csv`, `contacts.csv` | one-off export, UTF-8, comma separated | ops lead |
| Billing ("LegacyBill 4.2") | subscription records | REST API, paginated, read-only token | finance systems admin |
| Activity export | `activity.csv` | one-off export from the CRM's activity module | ops lead |
| Account handling rules | `business_rules.md` | word doc from the ops lead, converted to markdown | ops lead |

*For this build the billing API is simulated by the application itself under `/mock/billing/v1` (bearer token, paginated, read-only), serving the same records as `data/subscriptions.json`.*

## Known issues the customer told us about

- Accounts were merged by hand for years; there are duplicates under different account numbers
- The CRM status field was free text until 2019
- Nobody is sure whether `customer_tier` should become the plan or the account priority in Meridian
- The billing system still has quarterly subscriptions even though the plan was discontinued
- Country was free text in old rows; "CA" is sometimes California

## Open questions for the customer

Tracked properly in the tool once mapping starts. Initial list:

1. Should `customer_tier` map to `account_priority`, to `subscription.plan`, or to neither?
2. For accounts where the CRM and billing disagree on status, which system wins?
3. Are `On Hold` accounts active or inactive for go-live purposes?
4. Which records with a missing account number should be dropped versus assigned a new id?

## Assumptions

- Meridian `organization_id` will be the CRM account number, so re-runs are idempotent
- Activity history is migrated only if the core entities are clean by the dry run
- Nothing is written to production until the readiness report says READY or READY WITH CONDITIONS with sign-off
