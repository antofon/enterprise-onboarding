# Customer implementation plan: Apex Equipment Services to Meridian

*Fictional customer, fictional platform, synthetic data. This is the plan an implementation engineer would send the customer after the first rehearsal, written from what the tool measured. Every number in it comes from the committed sample and the stored readiness report.*

**Status today: BLOCKED.** The first full rehearsal moved 7,163 of 9,184 in-scope records into Meridian staging with nothing refused by the platform and every record accounted for. 2,021 records cannot migrate as they stand. All of them have a known reason, and most of the reasons are the customer's to resolve: seven decisions, thirteen data corrections, four sign-offs.

## 1. Objectives

1. Move Apex's accounts, contacts and subscriptions into Meridian before go-live, with activity history if the core entities are clean in time.
2. Lose nothing silently: every source row ends up migrated, excluded with a stated reason, or set aside by one of Apex's own rules.
3. Make every judgment call visible and owned: what a field means, what a legacy value becomes, which records are accepted with known imperfections.
4. Go live on evidence: a readiness report that says READY, or READY WITH CONDITIONS with each condition signed off.

## 2. Systems involved

| system | role | owner at Apex | how we reach it |
|---|---|---|---|
| SalesTrack 2011 (legacy CRM, on-prem) | accounts and contacts | ops lead | one-off CSV exports (`organizations.csv`, `contacts.csv`) |
| CRM activity module | activity history | ops lead | one-off CSV export (`activity.csv`) |
| LegacyBill 4.2 | subscriptions | finance systems admin | read-only REST API: bearer token, paginated, rate-limited |
| Account handling rules | how Apex classifies accounts | ops lead | `business_rules.md`, 14 numbered rules |
| Meridian | the target platform | Meridian platform team | write API with staging namespaces; a rehearsal never touches live data |
| Implementation workbench | profiling, mapping review, rehearsal, reconciliation, readiness | implementation engineer | this tool |

## 3. Dependencies

- **A corrected export from Apex** for the thirteen data corrections in section 9. The rehearsal cannot clear its blockers without it.
- **Answers to the seven questions** in section 9, from someone at Apex with authority to decide (ops lead for account rules, finance for plans and billing).
- **The billing token stays valid** through the rehearsals and the cutover, and the feed's rate limit is known (the loader already waits out a 429).
- **Meridian staging stays available** for repeat rehearsals; each rehearsal writes into a namespace of its own and can be purged in one call.
- **A go-live date fixed in writing**, because two of Apex's rules are measured from a date (dormancy at 18 months, dead trials at 90 days).

## 4. Assumptions

- Meridian's `organization_id` is the CRM account number, so a re-run sends the same identifier and is a no-op rather than a duplicate.
- Apex's 14 rules in `business_rules.md` are current. Where the data contradicts a rule, the rule wins unless Apex says otherwise; every such case is listed, never fixed quietly.
- Values the rules do not cover are not guessed. `Call` as an activity kind, `Trial` as a plan and `Actve` as a status stay behind until Apex says what they become.
- Contacts who opted out still migrate (rule 14).
- Nothing is written to live Meridian data until the readiness report and the acceptance criteria in section 10 are met.

## 5. Milestones

| milestone | done when | owner |
|---|---|---|
| M1. Sources profiled | all four sources attached and measured; quality issues counted with examples | implementation engineer (done) |
| M2. Mappings decided | every one of the 41 source fields approved, ignored or rejected by a person; no open question | implementation engineer with Apex ops lead |
| M3. First full rehearsal | a dry run of every valid record into staging, reconciled | implementation engineer (done: BLOCKED) |
| M4. Customer actions closed | the seven decisions answered and recorded as rules or mapping decisions; the corrected export received | Apex ops lead and finance |
| M5. Clean rehearsal | re-profile, re-rehearse, readiness READY or READY WITH CONDITIONS | implementation engineer |
| M6. Sign-off | each remaining condition accepted in writing by Apex | Apex project sponsor |
| M7. Cutover | the live migration in the agreed window, reconciled against the source | implementation engineer with the Meridian platform team |
| M8. Close-out | staging namespaces purged, the record of decisions handed over | implementation engineer |

The go-live target was set three weeks from kickoff. M4 is the critical path: everything after it takes one rehearsal (about a minute for this customer's sample, about ten minutes at ten times the volume, measured).

## 6. Migration sequence

1. **Organizations first.** Every child record needs its organization to exist. An organization Meridian refuses blocks its contacts, subscriptions and activities, which are recorded as blocked with the reason, not sent.
2. **Contacts**, with one primary per organization (rule 1; Meridian refuses a second).
3. **Subscriptions**, after the dormancy rule has decided each account's status (rule 2), because Meridian refuses a live subscription under an inactive account.
4. **Activities** last, and only if the core entities are clean (kickoff assumption).
5. **Reconcile.** Every source row followed through to what Meridian holds, identifier by identifier.

Writes go one record per request, with the customer's identifier on every write, so a retry after a timeout cannot double-write. A Meridian outage stops the run after ten consecutive failures instead of retrying into a dead endpoint, and the run says so.

## 7. Validation

Every record is checked three times before it is sent, in this order:

1. **Meridian's own contract:** types, required fields, allowed values, formats (country codes, E.164 phones, identifier shapes).
2. **Meridian's cross-record rules:** unique identifiers, every child's organization present, one primary contact and one email per organization, no live subscription under an inactive account, no dates after the migration date.
3. **Apex's own cross-checks:** a Strategic account carries an Enterprise subscription (rule 4); conflicts are flagged, not changed.

Then the platform itself is the fourth check: in the first rehearsal it refused nothing, which says the first three caught everything it would have refused. Reconciliation is the fifth: 28 of 28 checks balanced, nothing missing in Meridian and nothing unexpected.

## 8. Rollback considerations

- **Rehearsals need no rollback.** Each one writes into its own staging namespace, isolated from live data and from every other rehearsal, and a namespace is purged in one call. Live data refuses to be purged.
- **The cutover is reversible by design, not by hope.** Every accepted write carries the customer's identifier, a hash of its content and the request id that wrote it. Together those are the ledger a compensating delete would work from, batch by batch, organizations last.
- **Point-in-time restore of Meridian** is the last resort and has to be confirmed with the Meridian platform team before the cutover window, not during it.
- **Go/no-go at the window:** if the cutover reconciliation shows any discrepancy, writes stop and the window is used to explain it. A count that does not add up is never shipped.

## 9. Open questions and actions for Apex

**Decisions** (only Apex can make these; each one releases records):

| # | question | records waiting |
|---|---|---|
| 1 | `customer_tier`: should it map to account priority, to the subscription plan, or to neither? Your rule 3 says Strategic is a designation and Gold, Silver and Bronze are spend bands, so it is neither a plan nor exactly a priority. | the field itself |
| 2 | 311 subscriptions are live in billing, but the account each belongs to has had no activity in 18 months and migrates as inactive (rule 2), and Meridian allows no live subscription under an inactive account. For each account: should it stay active in Meridian, or should the subscription end before migration? | 311 |
| 3 | Which Meridian activity kind should `Call` become? | 215 |
| 4 | Which plan should `Trial` become? | 27 |
| 5 | 22 subscriptions sit on accounts whose tier calls for a different plan (rule 4). Is billing right, or should these accounts move plan? | 22 |
| 6 | Is `Actve` in the account status a typo for Active? | 16 |
| 7 | Which country are these 7 accounts in? The export says `CA` with no state to decide between Canada and California (rule 5). | 7 |
| 8 | Which plan should this subscription get? `Legacy Gold` depends on the seat count (rule 7), and the export has none. | 1 |

**Data corrections** (lists with source rows are exported from the rehearsal, one per problem): 106 contacts with an invalid email, 97 accounts missing billing country or status, 88 contacts with a phone number that is too short, 70 contacts and 55 activities whose account is not in the export, 61 subscriptions missing seats or dates, 45 contacts repeating an email already used on the same account, 43 contacts marked primary on accounts that already have one, 19 contacts missing a required value, 18 subscriptions whose account is not in the export, 15 duplicate account numbers, 13 accounts with no account number, 9 subscriptions with a value Meridian does not allow.

**Released automatically:** up to 839 more records (519 activities, 231 contacts, 89 subscriptions) migrate once the accounts they belong to are fixed.

**For sign-off:** 308 records will migrate carrying a warning. The warnings, counted per record and so overlapping: 152 accounts with no contact flagged as primary, 127 accounts whose named primary contact is not in the contact export, 16 accounts with no primary contact named, 3 dates in the future, plus the plan and country questions above. Each is a known imperfection Apex accepts or corrects.

## 10. Acceptance criteria

Go-live proceeds when all of these hold:

1. Every one of the 41 source fields has a decision recorded by a person, with no open question.
2. The latest full rehearsal ran against today's mappings and configuration (a rehearsal made before a change counts as stale).
3. Reconciliation balanced: every check passed, no record missing in Meridian, none unexpected.
4. Meridian refused or failed nothing in that rehearsal.
5. At least 95% of each entity's in-scope records landed (organizations are at 86.3%, contacts 74.7%, subscriptions 44.6%, activities 84.1% today).
6. Every remaining condition is signed off in writing by Apex's sponsor.
7. The configuration's reference date is moved to the go-live date and the rehearsal repeated with it.
8. A cutover window and the rollback path in section 8 are agreed with the Meridian platform team.

The readiness report checks criteria 1 to 5 itself, prints what it found for each, and says READY only when they all pass.
