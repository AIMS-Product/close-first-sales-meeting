# Original source and reactivation attribution

Original Source is the acquisition field (`cf_74IE1CpFKVCifR0ifK18wsOidWhQQ3s7UxiwczUTtyg`). Booked By remains the separate existing booking-category field. Reactivation Setter Name/User and Scraper Funnel continue to carry their existing booking/setter attribution. This change does not activate the separately prepared Booked By updater or change its rules.

`update_field.py` no longer writes Reactivation Scrapers into Funnel Name DEAL, including when the field is blank. When a lead is otherwise checked, a nonblank legacy origin can fill a blank Original Source; a nonblank Original Source is never overwritten. VSL retains its existing blank-only inference. The updater does not perform a full origin backfill.

The 13 audited recoveries restore the retained pre-overwrite source to both legacy Funnel Name DEAL and Original Source, with an immediate readback. Current fields must match the reviewed precondition; changed records are held rather than overwritten. Existing Booked By and setter attribution are preserved.
