# Apex Equipment Services: account handling rules

*Provided by the customer's operations lead at kickoff. Lightly edited for formatting only. This is the document the mapping step interprets; several rules conflict with the data, and one is genuinely ambiguous. That is the point.*

## Accounts

1. Every account has exactly one primary contact. Where the CRM has no contact flagged as primary, the `primary_contact_email` on the account row is authoritative and should be used to create or flag the primary contact.
2. Accounts with no recorded activity in the last 18 months are dormant. Dormant accounts should be treated as **Inactive** regardless of what the CRM status says.
3. `customer_tier` is a sales classification: Gold, Silver and Bronze reflect annual spend. **Strategic** is an executive designation assigned by the VP of Sales and is not a spend band. Platinum was retired in 2022 and any remaining Platinum accounts are Gold.
4. Strategic accounts always carry an Enterprise subscription. If the billing system shows something else, billing is wrong, not the designation.
5. In the billing system, country `CA` means Canada. In CRM rows created before 2021, `CA` in the country column sometimes meant California because the field was free text at the time. Use the state column to disambiguate.
6. `On Hold` accounts are active accounts with a temporary billing pause. `Closed` accounts are churned.

## Subscriptions

7. `Legacy Gold` is the old name for the Enterprise plan, unless the subscription has fewer than 10 seats, in which case it should be migrated as Professional.
8. `Pro`, `Professional` and `Professional Plus` are all the Professional plan. `Basic` is Starter. `ENT` and `Enterprise v2` are Enterprise.
9. Quarterly billing (`Q`) was discontinued in 2023. Remaining quarterly subscriptions should be converted to monthly at migration.
10. `paused` subscriptions should migrate as `past_due`. `expired` subscriptions should migrate as `cancelled`.
11. Trial subscriptions older than 90 days are dead and should not be migrated at all.
12. Amounts in the billing export are monthly regardless of billing frequency. CAD amounts should be migrated as-is in USD (finance will true up after go-live).

## Contacts

13. Contact roles are free text in the CRM. `AP`, `Billing` and `billing contact` are billing contacts. `CTO`, `IT` and `Technical` are technical contacts. `CEO`, `Owner` and `Primary` are the primary contact when no explicit primary flag exists.
14. Contacts who have opted out (`opt_out = Y`) must still be migrated; opt-out is a marketing preference, not a data deletion request.
