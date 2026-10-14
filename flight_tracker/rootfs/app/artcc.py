"""Which ARTCC (center) an airport's departures come from, for reading ground-stop scope.

Only airports we're confident about are listed; anything else is "unknown" and the app
shows the scope without saying whether it includes you.
"""

CENTERS = {
    "ZNY": "EWR JFK LGA ISP HPN ABE AVP",
    "ZBW": "BOS PVD BDL MHT PWM BTV ALB ORH BGR ACK MVY HYA",
    "ZDC": "DCA IAD BWI RIC ORF PHF RDU CHO",
    "ZJX": "JAX MCO SFB DAB SAV CHS TLH GNV TPA PIE",
    "ZMA": "MIA FLL PBI RSW EYW",
    "ZTL": "ATL CLT GSP AVL CHA BHM TYS",
    "ZME": "MEM BNA LIT",
    "ZHU": "IAH HOU MSY AUS SAT",
    "ZFW": "DFW DAL OKC",
    "ZAU": "ORD MDW MKE",
    "ZOB": "DTW CLE PIT BUF",
    "ZID": "CMH CVG IND SDF DAY",
    "ZMP": "MSP FAR",
    "ZKC": "MCI STL ICT",
    "ZDV": "DEN COS",
    "ZLA": "LAX SAN LAS BUR ONT SNA PSP LGB",
    "ZOA": "SFO OAK SJC SMF RNO",
    "ZSE": "SEA PDX GEG",
    "ZLC": "SLC BOI",
    "ZAB": "PHX ABQ TUS ELP",
    "ZAN": "ANC FAI",
    "ZHN": "HNL OGG KOA LIH",
}

AIRPORT_CENTER = {apt: c for c, apts in CENTERS.items() for apt in apts.split()}


def center_of(iata):
    return AIRPORT_CENTER.get((iata or "").upper())


def in_scope(item, iata):
    """True / False / None (can't tell) — is a departure from `iata` held by this program?"""
    c = center_of(iata)
    scope = [s.upper() for s in item.get("scope") or []]
    flights = (item.get("flights") or "").upper()
    if "ALL" in flights and "DEP" in flights and not scope:
        return True
    if not scope:
        return None
    if (iata or "").upper() in scope:
        return True
    if c is None:
        return None
    return c in scope
