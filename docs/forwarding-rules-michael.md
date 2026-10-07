# Getting tender alerts into the triage tool

**For:** Michael Liebmann, Alister Leong (cc Philip, Kavan)
**Goal:** every portal alert you receive also lands in the tender triage dashboard automatically, tagged ✉ alert, so nothing depends on someone forwarding it by hand.

## The end state (Alex, 6 Oct)

Portal and panel logins move to one shared mailbox, **tenders@visioni.com.au**. Alerts then go straight there instead of to your personal inboxes, and the triage tool reads that mailbox every hour.

**Nothing changes on any portal until we agree it together.** You both hold the state logins today, so the cut-over is a joint session, one portal at a time, with you making each change. Until then, the forwarding rule below gets the alerts flowing without touching any registration.

## Step 1 — forwarding rule in Outlook (5 minutes, do this now)

Use **Redirect**, not Forward. Redirect keeps the portal as the sender, which is how the tool recognises the format.

1. Outlook on the web → **Settings** (gear) → **Mail** → **Rules** → **+ Add new rule**.
   (Desktop Outlook: **File → Manage Rules & Alerts → New Rule → Apply rule on messages I receive**.)
2. Name it `Tender alerts → tenders@`.
3. **Condition:** *From* → add each of these (type the address or domain and press Enter):

   | Portal | Add to "From" |
   |---|---|
   | QTenders (QLD) | `qtenders.epw.qld.gov.au` |
   | buy.nsw / eTendering NSW | `buy.nsw.gov.au`, `tenders.nsw.gov.au` |
   | Tenders WA | `tenders.wa.gov.au` |
   | Tenders VIC / Buying for Victoria | `tenders.vic.gov.au`, `buyingfor.vic.gov.au` |
   | Tenders ACT | `tenders.act.gov.au` |
   | SA Tenders | `tenders.sa.gov.au` |
   | Tenders TAS | `tenders.tas.gov.au` |
   | NT Tenders | `tendersonline.nt.gov.au` |
   | AusTender | `tenders.gov.au` |
   | ICN Gateway | `icn.org.au` |
   | VendorPanel | `vendorpanel.com.au` |
   | Local Buy | `localbuy.com.au` |
   | GETS (NZ) | `gets.govt.nz` |
   | SEN tender newsletter | `sen.news` |

   If a portal sends from a different address, open one of its alerts, copy the sender, and add it — the rule only catches what is listed.
4. **Action:** *Redirect to* → `tenders@visioni.com.au`.
5. Leave **Stop processing more rules** unticked so the alert stays in your inbox as well.
6. **Save**.

**Check it worked:** after the next alert arrives, open the dashboard's **Upload / add tender** page → *Recent alert emails*. It should be listed within the hour, and its tenders appear in **New this week** with an ✉ alert badge.

### If you use Gmail for any portal

Settings → **Filters and Blocked Addresses** → **Create a new filter** → *From*: `qtenders.epw.qld.gov.au OR buy.nsw.gov.au OR tenders.wa.gov.au OR vendorpanel.com.au` (and the rest of the list above) → **Create filter** → **Forward it to** `tenders@visioni.com.au` (Gmail will ask you to confirm the forwarding address once).

## Step 2 — anything else worth flagging

For anything that isn't a portal alert — an SEN article, a tip from a customer, something a contractor mentioned:

- **Forward the email to tenders@visioni.com.au**, or
- paste it into **Upload / add tender → Paste an alert** on the dashboard, or
- add it by hand under **Add a tender manually**.

Even if it's only "should we have known about this?", send it — the weekly coverage report counts every tender that reached us by email but not by John's weekly scrape, and that list is how we tune the scrape.

## Step 3 — the cut-over to tenders@ (together, later)

For each portal, in one sitting with Michael or Alister logged in:

1. Add tenders@visioni.com.au as the notification email (or as an additional contact, where the portal allows more than one).
2. Re-save the saved searches / UNSPSC categories so alerts follow the new address.
3. Confirm the next alert arrives in tenders@ and shows up in the dashboard.
4. Only then remove the personal address, if at all.

Passwords stay where they are today (your password manager); nothing about logins is ever stored in the triage tool or its code.
