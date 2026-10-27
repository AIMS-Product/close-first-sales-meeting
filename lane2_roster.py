"""Canonical Lane 2 rep, offer, readiness, and meeting-title configuration."""

import re


OFFER_VP = "Vendingpreneurs (VP)"
OFFER_TLG = "The Land Geek (TLG)"
OFFER_PPA = "Publishing Profits Academy (PPA)"
OFFER_AA = "Acquisition Ace (AA)"


# Missing user IDs are resolved by exact name from Close's organization user
# directory when assignment or state reconciliation starts.
LANE2_ROSTER = [
    {
        "rep": "Tony Parton", "user_id": "user_vw2xwmG2n6zLu3pjkuZHS7ZPNlg1dyImRUguk60YCbN", "offer": OFFER_PPA,
        "ready_for_leads": False,
        "title_patterns": [r"ppa\s+growth\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Ashley Atencio", "user_id": "user_3VLmKDrh5OfMOmxRCBcxdYV6ltOhvXaB0tJSdV9LKS4", "offer": OFFER_VP,
        "ready_for_leads": False,
        "title_patterns": [r"vendingpren[eu]+rs?\s+enterprise\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Ibraheem Khan", "user_id": "user_bILw2ouCfAZbR607XP0TmUaSLEoFPbbE1eas85DanoI", "offer": OFFER_VP,
        "ready_for_leads": False,
        "title_patterns": [r"vendingpren[eu]+rs?\s+velocity\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Monde", "user_id": "user_uH23xPzFSVeiPL8Vg5xW21R5Vrp9BxOlo0O2gPZIZuC", "offer": OFFER_VP,
        "ready_for_leads": False,
        "title_patterns": [r"vendingpren[eu]+rs?\s+pinnacle\s*-\s*next\s+steps"],
    },
    {
        "rep": "Finley Eastlake", "user_id": "user_VFwRELQ3t8yDDI65IBHweYD4zlvxxSgNejm4oNPZTYd", "offer": OFFER_VP,
        "ready_for_leads": False,
        "title_patterns": [r"vendingpren[eu]+rs?\s+executive\s*-\s*next\s+steps"],
    },
    {
        "rep": "Charlie Ingram", "user_id": "user_yZWJTiMjUBfJt8pUPQG6hS7QfKUxwt322aYEABSUrQb",
        "offer": OFFER_VP, "ready_for_leads": True,
        "title_patterns": [r"vendingpren[eu]+rs?\s+-\s+next\s+steps\s+call"],
    },
    {
        "rep": "Vince Bartolini", "user_id": "user_dQi0iL0igjCKtEXPSsv8ALDZMAz9orJxL60O7Q921jy",
        "offer": OFFER_VP, "ready_for_leads": True,
        "title_patterns": [r"vendingpren[eu]+rs?\s+next\s+steps\s+call"],
    },
    {
        "rep": "William Nowak", "user_id": "user_ZNKG1S9eI71qxhSozBK4jskTVtJqXzfNCPWqmADRR9F",
        "offer": OFFER_VP, "ready_for_leads": True,
        "title_patterns": [r"vendingpren[eu]+r\s+next\s+steps"],
    },
    {
        "rep": "Spencer Reynolds", "user_id": "user_4sfuKGMbv0LQZ4hpS8ipASv406kKTSNP5Xx79jOwSqM",
        "offer": OFFER_VP, "ready_for_leads": False,
        "title_patterns": [r"vending\s+discovery\s+-\s+next\s+steps"],
    },
    {
        "rep": "Pearl Sathekge", "user_id": "user_0SuNg0OWd2reYMeyuDVqiVvjiGcRiFheKKOXXZpyaPZ",
        "offer": OFFER_VP, "ready_for_leads": True,
        "title_patterns": [r"vendingpren[eu]+rs?\s+next\s+steps\s+session"],
    },
    {
        "rep": "August Young", "user_id": "user_wH5PGq1Wm84UW6KrKCt6YCioWocmlffYkbadH6rN43H",
        "offer": OFFER_VP, "ready_for_leads": True,
        "title_patterns": [r"vending\s+discovery\s+call\s+-\s+next\s+steps"],
    },
    {
        "rep": "Cassie Caraballo", "user_id": "user_Hoijs8g8hxab7NN7tMVvC4dpzwHcxSgkIuHeBRphyUL",
        "offer": OFFER_VP, "ready_for_leads": True,
        "title_patterns": [r"vending\s+opportunity\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Jessica Zatkin", "user_id": "user_WmBJj4uIsE9WRLKMn5Y1i8MinIDJG5GjOHPeX2sUJCp",
        "offer": OFFER_VP, "ready_for_leads": True,
        "title_patterns": [r"vendingpren[eu]+rs?\s+connect\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Connor George", "user_id": "user_YlAbrpKa9iKWFt351Dk1BC4Cmr4SXHKDsSDMG4hnVHi",
        "offer": OFFER_VP, "ready_for_leads": True,
        "title_patterns": [r"vendingpren[eu]+rs?\s+momentum\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Naria Torres", "user_id": "user_PBMfAYkPSkMaYK58gXuG70Vu1SLd2bsG3Mvys6RZNgY",
        "offer": OFFER_VP, "ready_for_leads": True,
        "title_patterns": [
            r"vendingpren[eu]+rs?\s+pathway\s*-?\s*next\s+steps",
            r"vendingpren[eu]+rs?\s+navigator\s*-\s*next\s+steps",
        ],
    },
    {
        "rep": "Melia King", "user_id": "user_32021LR58tWOSl2MX2nFVMSP8PoaGV1DjZEF0v0yGXs",
        "offer": OFFER_VP, "ready_for_leads": True,
        "title_patterns": [r"vendingpren[eu]+rs?\s+blueprint\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Jacob Hepner", "user_id": "user_IeWR2TlhpjqoXy3K6jX7u9C8c83iBnHXSIvFZpotF3z",
        "offer": OFFER_VP, "ready_for_leads": False,
        "title_patterns": [r"vendingpren[eu]+rs?\s+call\s+-\s+next\s+steps"],
    },
    {
        "rep": "Jacob Herbig", "user_id": "user_p2y1gLbIgUb9xognGTvuXoRpzp4Ro8QkO20ltgF1CvJ",
        "offer": OFFER_VP, "ready_for_leads": False,
        "title_patterns": [r"vendingpren[eu]+rs?\s+-\s+next\s+steps(?!\s+call)"],
    },
    {
        "rep": "Kelly Schrader", "user_id": "user_WquWudQN7dghZsAPiNY80eJUmg1EadQg2UCQdvgbif7",
        "offer": OFFER_VP, "ready_for_leads": False,
        "title_patterns": [r"vendingpren[eu]+rs?\s+discovery\s+-\s+next\s+steps"],
    },
    {
        "rep": "Amy Mulch", "user_id": "user_rGhjlxgkAA0mXchgw6zggbWXqHSYpfpzCvO6UgkqVnm",
        "offer": OFFER_VP, "ready_for_leads": False,
        "title_patterns": [r"vendingpren[eu]+rs?\s+strategy\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Abigail Garza", "user_id": "user_O9qFgDidrldSA1zU3pKPpz5zUbCcNpoEBTCrtAolDUi",
        "offer": OFFER_VP, "ready_for_leads": False,
        "title_patterns": [r"vending\s+success\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Dana Lesiuk", "user_id": "user_OsKxvuqk3YYRh22NXqonG3PbfFoTC39bn4vGFRKBdMZ",
        "offer": OFFER_VP, "ready_for_leads": False,
        "title_patterns": [r"vendingpren[eu]+rs?\s+launch\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Kaylane Nunes", "user_id": "user_8SCBGk2kKZfioenvHC9dUNv0reDS5d6HkZQ1idDaf4j", "offer": OFFER_TLG,
        "ready_for_leads": True,
        "title_patterns": [r"tlg\s+pinnacle\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Josh Stoffel", "user_id": "user_WdDkCcFt7B6F7nPJJiWGi6vl5g5jeqbluErfy6Syrfs",
        "offer": OFFER_TLG, "ready_for_leads": True,
        "title_patterns": [r"tlg\s+compass\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Catalina", "user_id": "user_SstZur4m512IhMhhIWPIehvCpkkSndAKZiWPhQ13jaQ",
        "offer": OFFER_TLG, "ready_for_leads": True,
        "title_patterns": [r"tlg\s+elevate\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Connor Mason", "user_id": "user_RXqPV5zrF4dPBwGJtxs8W77fNgCXir4RrT0K0TGNaoh",
        "offer": OFFER_TLG, "ready_for_leads": True,
        "title_patterns": [r"tlg\s+vanguard\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Jonathan Quinn", "user_id": "user_8cNzhmBZ12lhwqIrBPcVFheGSWdyw6ZDva2S1gysf3u", "offer": OFFER_TLG,
        "ready_for_leads": True,
        "title_patterns": [r"tlg\s+northstar\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Owen Hart", "user_id": "user_8UDC7WAhyYNxEyhrh3flGsYUqmXh9UVkn7Z1LOlY6Lm",
        "offer": OFFER_VP, "ready_for_leads": True,
        "title_patterns": [r"vendingpren[eu]+rs?\s+ascent\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Brad Savage", "user_id": "user_iYReXOSE5Mx1NlCsyASyZMBP5orKZjVFSvKonrtQRhF",
        "offer": OFFER_VP, "ready_for_leads": True,
        "title_patterns": [r"vendingpren[eu]+rs?\s+keystone\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Rob Maxfield", "user_id": "user_eX4cSEZl12Xns7Bvce6738CEXt9f81MiPkSPkYx1tgO",
        "offer": OFFER_VP, "ready_for_leads": True,
        "title_patterns": [r"vendingpren[eu]+rs?\s+summit\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Igor Trojanowski", "user_id": "user_m2KuqSrAVKIfcCb8IqSUNW64P2koKqxdSHq53zWvruZ", "offer": OFFER_VP,
        "ready_for_leads": True,
        "title_patterns": [r"vendingpren[eu]+rs?\s+growth\s*-?\s*next\s+steps"],
    },
    # Existing assignment seats omitted from the ready list are held as false
    # until they are explicitly certified. They remain in their historical
    # owner-team configuration, but receive no new lists.
    {
        "rep": "Raiya", "user_id": "user_JM69zsjxDPj2MVQKI3kCcfToub01d0eoKP9sxFwUEQY",
        "offer": OFFER_PPA, "ready_for_leads": False,
        "title_patterns": [r"ppa\s+catalyst\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Ariella Irvine", "user_id": "user_BaN2TstWtyF34eaQSSLG11j6DhKKm67Y6JltbIYCafO",
        "offer": OFFER_VP, "ready_for_leads": False, "title_patterns": [],
    },
    {
        "rep": "Beatrice Braescu Cojocaru", "user_id": "user_stO6qgPWPprrhNp8wBxHFzkYLrG8JAN9wBiWXru1xvJ",
        "offer": OFFER_TLG, "ready_for_leads": False,
        "title_patterns": [r"tlg\s+horizon\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Luna", "user_id": "user_bT94lFhgyA2c4inUFezZLSmGp9pRpfvc9LG7Io8luN7",
        "offer": OFFER_TLG, "ready_for_leads": False,
        "title_patterns": [r"tlg\s+clarity\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Jessica Hernandez", "user_id": "user_EegZMdY31eOlhpDRCp0IeRPrszKSnvSURlKlESoeEyb",
        "offer": OFFER_PPA, "ready_for_leads": False,
        "title_patterns": [r"ppa\s+pinnacle\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Jason Aaron", "user_id": "user_MrBLkl5wCqTm7QxHxPo2ydNV5KxMllg6YZDVc12Aqzj",
        "offer": OFFER_VP, "ready_for_leads": False, "title_patterns": [],
    },
    {
        "rep": "Eduardo Santos", "email": "eduardo.santos@modern-amenities.com",
        "start_date": "2026-10-19",
        "user_id": "", "offer": OFFER_AA, "ready_for_leads": False,
        "title_patterns": [r"acquisition\s+ace\s+launch\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Sam Norris", "email": "sam.norris@modern-amenities.com",
        "start_date": "2026-10-19",
        "user_id": "", "offer": OFFER_AA, "ready_for_leads": False,
        "title_patterns": [r"acquisition\s+ace\s+growth\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Jennifer Murray", "email": "jennifer.murray@modern-amenities.com",
        "start_date": "2026-10-19",
        "user_id": "", "offer": OFFER_VP, "ready_for_leads": False,
        "title_patterns": [r"vendingpren[eu]+rs?\s+elevate\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Osheana Mitchell", "email": "osheana@modern-amenities.com",
        "start_date": "2026-10-19",
        "user_id": "", "offer": OFFER_VP, "ready_for_leads": False,
        "title_patterns": [r"vendingpren[eu]+rs?\s+accelerate\s*-?\s*next\s+steps"],
    },
    {
        "rep": "Tara Hall", "email": "tara.hall@modern-amenities.com",
        "start_date": "2026-10-19",
        "user_id": "", "offer": OFFER_VP, "ready_for_leads": False,
        "title_patterns": [
            r"vendingpren[eu]+rs?\s+pioneer\s*-?\s*next\s+steps",
            r"vendingpren[eu]+rs?\s+visionary\s*-?\s*next\s+steps",
        ],
    },
    {
        "rep": "Antonio Garcia", "email": "antonio.garcia@modern-amenities.com",
        "start_date": "2026-10-19",
        "user_id": "", "offer": OFFER_VP, "ready_for_leads": False,
        "title_patterns": [r"vendingpren[eu]+rs?\s+legacy\s*-?\s*next\s+steps"],
    },
]

# Owner Team role is still distinct from offer and certification. Keep its role
# membership here too so the reconciler and assignment scripts share identities.
OWNER_GROUPS = {
    "SETTERS": {
        "William Nowak", "Spencer Reynolds", "Eduardo Santos", "Sam Norris",
        "Jennifer Murray", "Osheana Mitchell", "Tara Hall", "Antonio Garcia",
    },
    "HYBRID_SETTERS": {"Ariella Irvine"},
    "SCRAPERS": {
        "Vince Bartolini", "Charlie Ingram", "Pearl Sathekge", "August Young",
        "Cassie Caraballo", "Jessica Zatkin", "Connor George", "Naria Torres",
        "Melia King", "Raiya", "Owen Hart", "Brad Savage", "Rob Maxfield",
        "Jason Aaron", "Jacob Hepner", "Jacob Herbig", "Kelly Schrader",
        "Amy Mulch", "Abigail Garza", "Dana Lesiuk", "Beatrice Braescu Cojocaru",
        "Catalina", "Luna",
    },
    "TLG_SETTERS": {
        "Beatrice Braescu Cojocaru", "Josh Stoffel", "Catalina", "Luna",
        "Connor Mason", "Kaylane Nunes", "Jonathan Quinn",
    },
}

for rep in LANE2_ROSTER:
    rep["owner_groups"] = [
        group for group, names in OWNER_GROUPS.items() if rep["rep"] in names
    ]


SCRAPER_TITLE_MAP = [
    {
        **rep,
        "pattern": re.compile(pattern, re.IGNORECASE),
    }
    for rep in LANE2_ROSTER
    for pattern in rep["title_patterns"]
]

# Historical reporting rules are kept next to the roster so every title map has
# one source of truth. These are intentionally workflow-specific: the two older
# reports have different historical attributions for some identical titles.
MEETING_COUNT_TITLE_MAP = [
    (re.compile(r"ppa\s+growth\s*-?\s*next\s+steps", re.IGNORECASE), "Tony Parton"),
    (re.compile(r"vendingpren[eu]+rs?\s+enterprise\s*-?\s*next\s+steps", re.IGNORECASE), "Ashley Atencio"),
    (re.compile(r"vendingpren[eu]+rs?\s+velocity\s*-?\s*next\s+steps", re.IGNORECASE), "Ibraheem Khan"),
    (re.compile(r"vendingpren[eu]+rs?\s+pinnacle\s*-\s*next\s+steps", re.IGNORECASE), "Monde"),
    (re.compile(r"vendingpren[eu]+rs?\s+executive\s*-\s*next\s+steps", re.IGNORECASE), "Finley Eastlake"),
    (re.compile(r"vendingpren[eu]+rs?\s+-\s+next\s+steps\s+call", re.IGNORECASE), "Charlie Ingram"),
    (re.compile(r"vendingpren[eu]+rs?\s+call\s+-\s+next\s+steps", re.IGNORECASE), "Jacob Hepner"),
    (re.compile(r"vendingpren[eu]+rs?\s+next\s+steps\s+call", re.IGNORECASE), "Vince Bartolini"),
    (re.compile(r"vendingpren[eu]+rs?\s+next\s+steps\s+session", re.IGNORECASE), "Pearl Sathekge"),
    (re.compile(r"vendingpren[eu]+rs?\s+discovery\s+-\s+next\s+steps", re.IGNORECASE), "Kelly Schrader"),
    (re.compile(r"vendingpren[eu]+rs?\s+-\s+next\s+steps(?!\s+call)", re.IGNORECASE), "Jacob Herbig"),
    (re.compile(r"vendingpren[eu]+r\s+next\s+steps", re.IGNORECASE), "William Nowak"),
    (re.compile(r"vending\s+consult\s+call", re.IGNORECASE), "William Nowak"),
    (re.compile(r"vending\s+discovery\s+call\s+-\s+next\s+steps", re.IGNORECASE), "August Young"),
    (re.compile(r"vending\s+discovery\s+-\s+next\s+steps", re.IGNORECASE), "Spencer Reynolds"),
    (re.compile(r"vendingpren[eu]+rs?\s+strategy\s*-?\s*next\s+steps", re.IGNORECASE), "Amy Mulch"),
    (re.compile(r"vending\s+opportunity\s*-?\s*next\s+steps", re.IGNORECASE), "Cassie Caraballo"),
    (re.compile(r"vendingpren[eu]+rs?\s+connect\s*-?\s*next\s+steps", re.IGNORECASE), "Jessica Zatkin"),
    (re.compile(r"vending\s+success\s*-?\s*next\s+steps", re.IGNORECASE), "Abigail Garza"),
    (re.compile(r"vendingpren[eu]+rs?\s+momentum\s*-?\s*next\s+steps", re.IGNORECASE), "Connor George"),
    (re.compile(r"vendingpren[eu]+rs?\s+launch\s*-?\s*next\s+steps", re.IGNORECASE), "Dana Lesiuk"),
    (re.compile(r"vendingpren[eu]+rs?\s+pathway\s*-?\s*next\s+steps", re.IGNORECASE), "Naria Torres"),
    (re.compile(r"vendingpren[eu]+rs?\s+navigator\s*-\s*next\s+steps", re.IGNORECASE), "Naria Torres"),
    (re.compile(r"vendingpren[eu]+rs?\s+blueprint\s*-?\s*next\s+steps", re.IGNORECASE), "Melia King"),
    (re.compile(r"vendingpren[eu]+rs?\s+compass\s*-?\s*next\s+steps", re.IGNORECASE), "Josh Stoffel"),
    (re.compile(r"vendingpren[eu]+rs?\s+horizon\s*-?\s*next\s+steps", re.IGNORECASE), "Beatrice Braescu Cojocaru"),
    (re.compile(r"tlg\s+pathfinder\s*-?\s*next\s+steps", re.IGNORECASE), "Kaylane Nunes"),
    (re.compile(r"tlg\s+pinnacle\s*-?\s*next\s+steps", re.IGNORECASE), "Kaylane Nunes"),
    (re.compile(r"tlg\s+elevate\s*-?\s*next\s+steps", re.IGNORECASE), "Catalina"),
    (re.compile(r"vendingpren[eu]+rs?\s+catalyst\s*-?\s*next\s+steps", re.IGNORECASE), "Raiya"),
    (re.compile(r"tlg\s+clarity\s*-?\s*next\s+steps", re.IGNORECASE), "Luna"),
    (re.compile(r"tlg\s+vanguard\s*-?\s*next\s+steps", re.IGNORECASE), "Connor Mason"),
    (re.compile(r"tlg\s+northstar\s*-?\s*next\s+steps", re.IGNORECASE), "Jonathan Quinn"),
    (re.compile(r"vendingpren[eu]+rs?\s+ascent\s*-?\s*next\s+steps", re.IGNORECASE), "Owen Hart"),
    (re.compile(r"vendingpren[eu]+rs?\s+keystone\s*-?\s*next\s+steps", re.IGNORECASE), "Brad Savage"),
    (re.compile(r"vendingpren[eu]+rs?\s+growth\s*-?\s*next\s+steps", re.IGNORECASE), "Igor Trojanowski"),
    (re.compile(r"acquisition\s+ace\s+launch\s*-?\s*next\s+steps", re.IGNORECASE), "Eduardo Santos"),
    (re.compile(r"acquisition\s+ace\s+growth\s*-?\s*next\s+steps", re.IGNORECASE), "Sam Norris"),
    (re.compile(r"vendingpren[eu]+rs?\s+elevate\s*-?\s*next\s+steps", re.IGNORECASE), "Jennifer Murray"),
    (re.compile(r"vendingpren[eu]+rs?\s+accelerate\s*-?\s*next\s+steps", re.IGNORECASE), "Osheana Mitchell"),
    (re.compile(r"vendingpren[eu]+rs?\s+pioneer\s*-?\s*next\s+steps", re.IGNORECASE), "Tara Hall"),
    (re.compile(r"vendingpren[eu]+rs?\s+visionary\s*-?\s*next\s+steps", re.IGNORECASE), "Tara Hall"),
    (re.compile(r"vendingpren[eu]+rs?\s+legacy\s*-?\s*next\s+steps", re.IGNORECASE), "Antonio Garcia"),
]

MISSING_LEAD_RECONCILIATION_TITLE_MAP = [
    (re.compile(r"acquisition\s+ace\s+launch\s*-?\s*next\s+steps", re.IGNORECASE), "Eduardo Santos"),
    (re.compile(r"acquisition\s+ace\s+growth\s*-?\s*next\s+steps", re.IGNORECASE), "Sam Norris"),
    (re.compile(r"vendingpren[eu]+rs?\s+elevate\s*-?\s*next\s+steps", re.IGNORECASE), "Jennifer Murray"),
    (re.compile(r"vendingpren[eu]+rs?\s+accelerate\s*-?\s*next\s+steps", re.IGNORECASE), "Osheana Mitchell"),
    (re.compile(r"vendingpren[eu]+rs?\s+pioneer\s*-?\s*next\s+steps", re.IGNORECASE), "Tara Hall"),
    (re.compile(r"vendingpren[eu]+rs?\s+visionary\s*-?\s*next\s+steps", re.IGNORECASE), "Tara Hall"),
    (re.compile(r"vendingpren[eu]+rs?\s+legacy\s*-?\s*next\s+steps", re.IGNORECASE), "Antonio Garcia"),
    (re.compile(r"ppa\s+growth\s*-?\s*next\s+steps", re.IGNORECASE), "Tony Parton"),
    (re.compile(r"vendingpren[eu]+rs?\s+enterprise\s*-?\s*next\s+steps", re.IGNORECASE), "Ashley Atencio"),
    (re.compile(r"vendingpren[eu]+rs?\s+velocity\s*-?\s*next\s+steps", re.IGNORECASE), "Ibraheem Khan"),
    (re.compile(r"vendingpren[eu]+rs?\s+navigator\s*-\s*next\s+steps", re.IGNORECASE), "Naria Torres"),
    (re.compile(r"tlg\s+pathfinder\s*-?\s*next\s+steps", re.IGNORECASE), "Kaylane Nunes"),
    (re.compile(r"vendingpren[eu]+rs?\s+pinnacle\s*-\s*next\s+steps", re.IGNORECASE), "Monde"),
    (re.compile(r"vendingpren[eu]+rs?\s+executive\s*-\s*next\s+steps", re.IGNORECASE), "Finley Eastlake"),
    (re.compile(r"vendingpren[eu]+rs?\s+-\s+next\s+steps\s+call", re.IGNORECASE), "Kristin Nelson"),
    (re.compile(r"vendingpren[eu]+rs?\s+-\s+next\s+steps(?!\s+call)", re.IGNORECASE), "Spencer Reynolds"),
    (re.compile(r"vendingpren[eu]+r\s+next\s+steps", re.IGNORECASE), "Mallory Kent"),
]


def assignment_roster(offer):
    """Return ready reps with known Close IDs and the unresolved rep names."""
    eligible = [
        rep for rep in LANE2_ROSTER
        if rep["offer"] == offer and rep["ready_for_leads"]
    ]
    roster = {rep["user_id"]: rep["rep"] for rep in eligible if rep["user_id"]}
    missing_ids = [rep["rep"] for rep in eligible if not rep["user_id"]]
    return roster, missing_ids


def owner_team_rosters():
    """Return the existing user-ID maps used to stamp Lane 2 Owner Team."""
    return {
        group: {
            rep["user_id"]: rep["rep"]
            for rep in LANE2_ROSTER
            if group in rep["owner_groups"] and rep["user_id"]
        }
        for group in OWNER_GROUPS
    }


def names_requiring_user_id():
    return [
        rep["rep"] for rep in LANE2_ROSTER
        if not rep["user_id"]
        and (rep["ready_for_leads"] or rep["owner_groups"])
    ]


def resolve_missing_user_ids(users):
    """Fill missing IDs from exact full-name matches in Close's user directory."""
    by_name = {}
    ambiguous = set()
    for user in users:
        name = " ".join(
            f"{user.get('first_name', '')} {user.get('last_name', '')}".split()
        ).casefold()
        if not name or not user.get("id"):
            continue
        if name in by_name and by_name[name] != user["id"]:
            ambiguous.add(name)
        else:
            by_name[name] = user["id"]

    unresolved = []
    for rep in LANE2_ROSTER:
        if rep["user_id"] or not (rep["ready_for_leads"] or rep["owner_groups"]):
            continue
        name = " ".join(rep["rep"].split()).casefold()
        user_id = by_name.get(name)
        if user_id and name not in ambiguous:
            rep["user_id"] = user_id
        else:
            unresolved.append(rep["rep"])
    return unresolved
