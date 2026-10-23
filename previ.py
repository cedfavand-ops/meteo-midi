#!/usr/bin/env python3
"""
Prévisions J+1 Gard / Basse Vallée du Rhône
Moyenne 50/50 AROME HD + ICON-CH1 (via Open-Meteo) -> page HTML unique.

Usage :
    python3 previ.py                 # demain (date locale), écrit previ.html
    python3 previ.py --date 2026-10-22
    python3 previ.py --demo          # données synthétiques (test de mise en page)

Cron conseillé (17h45 locale) :
    45 17 * * *  cd /chemin/previ_gard && python3 previ.py
"""
import argparse, json, math, random, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------- CONFIG --
PARIS, UTC = ZoneInfo("Europe/Paris"), timezone.utc

MODELS = {                       # nom Open-Meteo : poids
    "meteofrance_arome_france_hd": 0.5,
    "meteoswiss_icon_ch1": 0.5,
}

# Échéances : (libellé, heure locale, fenêtre picto en heures locales)
ECHEANCES = [("8h", 8, (6, 10)), ("12h", 12, (10, 14)), ("16h", 16, (14, 18))]

# Fenêtres "climato" en UTC, jour J = [18h UTC J-1 ; 18h UTC J]
TN_START_UTC, TN_END_UTC = 18, 18      # veille 18h -> jour 18h
TX_START_UTC, TX_END_UTC = 6, 18       # jour J 6h -> 18h

# (nom, latitude, longitude) -- coordonnées approximatives, à affiner
STATIONS = [
    ("Montjoyer",        44.4970, 4.7640),
    ("Donzère",          44.4425, 4.7058),
    ("Pierrelatte",      44.3775, 4.6985),
    ("Pont-St-Esprit",   44.2564, 4.6444),
    ("Bagnols/Cèze",     44.1618, 4.6194),
    ("Orange",           44.1383, 4.8089),
    ("Laudun",           44.1000, 4.6800),
    ("Sabran",           44.1700, 4.5200),
    ("Roquemaure",       44.0500, 4.7750),
    ("Pouzilhac",        44.0200, 4.5400),
    ("Remoulins",        43.9378, 4.5636),
    ("Avignon",          43.9493, 4.8055),
    ("Beaucaire",        43.8070, 4.6430),
    ("Nîmes",            43.8367, 4.3601),
    ("Uzès",             44.0120, 4.4196),
    ("Verfeuil",         44.1900, 4.4500),
    ("Lussan",           44.2200, 4.2800),
    ("Barjac",           44.3050, 4.3570),
    ("Alès",             44.1250, 4.0830),
]

# Mode correctif (phase 2) : biais à retrancher/ajouter par station (°C), à alimenter
# plus tard avec Météo-France / Infoclimat. Ex : {"Orange": {"t": -0.5}}
CORRECTIONS = {}

# Pictos meteoblue (jeu "day", 35 codes) : fichiers NN_day*.png dans ce dossier.
ICON_DIR = Path("icons")
PICTO_FR = {
    1: "Ciel dégagé", 2: "Dégagé, quelques cirrus", 3: "Dégagé avec cirrus",
    4: "Dégagé, quelques nuages bas", 5: "Dégagé, nuages bas et quelques cirrus",
    6: "Dégagé, nuages bas et cirrus", 7: "Partiellement nuageux",
    8: "Partiellement nuageux, quelques cirrus", 9: "Partiellement nuageux avec cirrus",
    10: "Mitigé, risque orageux", 11: "Mitigé, quelques cirrus, risque orageux",
    12: "Mitigé avec cirrus, risque orageux", 13: "Dégagé mais brumeux",
    14: "Brumeux, quelques cirrus", 15: "Brumeux avec cirrus",
    16: "Brouillard / stratus bas", 17: "Brouillard, quelques cirrus", 18: "Brouillard avec cirrus",
    19: "Très nuageux", 20: "Très nuageux, quelques cirrus", 21: "Très nuageux avec cirrus",
    22: "Couvert", 23: "Couvert, pluie", 24: "Couvert, neige", 25: "Couvert, forte pluie",
    26: "Couvert, forte neige", 27: "Pluie, orages probables", 28: "Pluie faible, orages probables",
    29: "Orage, forte neige", 30: "Forte pluie, orages probables", 31: "Mitigé, averses",
    32: "Mitigé, averses de neige", 33: "Couvert, pluie faible", 34: "Couvert, neige faible",
    35: "Couvert, pluie et neige mêlées",
}

HOURLY = ["temperature_2m", "wind_gusts_10m", "wind_speed_10m", "wind_direction_10m",
          "precipitation", "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
          "weather_code"]

# ------------------------------------------------------------ OUTILS ------
def r10(x):                      # rafales : dizaine la plus proche
    return None if x is None else int(math.floor(x / 10 + 0.5)) * 10

PLUIE_CLASSES = [(1, "0,1 à 1"), (2, "1 à 2"), (5, "2 à 5"), (10, "5 à 10"), (20, "10 à 20"),
                 (50, "20 à 50"), (100, "50 à 100"), (150, "100 à 150"), (200, "150 à 200")]

def classe_pluie(v):
    """Tranche de cumul en mm : « 0 », « 0,1 à 1 », « 1 à 2 », … « > 200 » (bornes hautes incluses)."""
    if v is None:
        return "–"
    if v < 0.1:
        return "0"
    v = round(v, 1)
    for borne, label in PLUIE_CLASSES:
        if v <= borne:
            return label
    return "> 200"

def rnd(x):                      # arrondi "commercial" (0,5 -> haut)
    return None if x is None else int(math.floor(x + 0.5))

def compass(deg):
    pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO"]
    return pts[int((deg % 360) / 22.5 + 0.5) % 16]

def loc_to_utc(d, hour):
    return datetime(d.year, d.month, d.day, hour, tzinfo=PARIS).astimezone(UTC).replace(tzinfo=None)

def utc_dt(d, hour):
    return datetime(d.year, d.month, d.day, hour)

def frange(a, b):                # heures de a à b inclus, pas 1h
    t = a
    while t <= b:
        yield t
        t += timedelta(hours=1)

# ------------------------------------------------------------ DONNÉES -----
def _get_json(req, timeout):
    """Télécharge et décode du JSON ; en cas de problème, l'erreur dit ce que le serveur a renvoyé."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            statut, ctype = getattr(r, "status", 200), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        corps = e.read()[:300].decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code} : {corps}") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise RuntimeError(f"réponse non JSON (statut {statut}, type {ctype or '?'}, {len(raw)} octets) : "
                           f"{raw[:200].decode('utf-8', 'replace')!r}") from None
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"erreur API : {data.get('reason')}")
    return data

def fetch(d, stations=None, batch_size=5, retries=4, timeout=90):
    """Interroge Open-Meteo par petits lots de communes, sur 2 jours seulement
    (18h UTC de la veille -> fin du jour d), avec nouvelles tentatives en cas d'échec.
    Si un lot échoue, repli automatique : sans les nuages moyens, puis avec AROME seul."""
    start, end = d - timedelta(days=1), d
    points = stations or STATIONS
    modeles = list(MODELS)
    sans_mid = [v for v in HOURLY if v != "cloud_cover_mid"]
    configs = [("complète", HOURLY, modeles, retries),
               ("sans nuages moyens", sans_mid, modeles, 2),
               ("AROME seul", sans_mid, modeles[:1], 2)]
    payloads = []
    for i in range(0, len(points), batch_size):
        lot = points[i:i + batch_size]
        data = None
        for nom, variables, mods, essais in configs:
            params = {
                "latitude": ",".join(str(s[1]) for s in lot),
                "longitude": ",".join(str(s[2]) for s in lot),
                "hourly": ",".join(variables),
                "models": ",".join(mods),
                "timezone": "UTC",
                "start_date": start.isoformat(), "end_date": end.isoformat(),
                "wind_speed_unit": "kmh",
            }
            url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={"User-Agent": "Meteo-Midi/1.0 (GitHub Actions)"})
            for essai in range(1, essais + 1):
                try:
                    data = _get_json(req, timeout)
                    break
                except Exception as e:
                    print(f"Lot {i // batch_size + 1} [{nom}] : tentative {essai}/{essais} échouée ({e})", file=sys.stderr)
                    if essai < essais:
                        time.sleep(5 * essai ** 2)          # 5 s, 20 s, 45 s
            if data is not None:
                if nom != "complète":
                    print(f"Lot {i // batch_size + 1} : repli utilisé ({nom})", file=sys.stderr)
                break
        if data is None:
            raise RuntimeError(f"Open-Meteo injoignable pour le lot {i // batch_size + 1} (voir les messages ci-dessus)")
        lot_data = data if isinstance(data, list) else [data]
        if len(mods) == 1:                                  # modèle unique : Open-Meteo ne suffixe plus les noms
            for item in lot_data:
                h = item.get("hourly", {})
                for v in variables:
                    if v in h and f"{v}_{mods[0]}" not in h:
                        h[f"{v}_{mods[0]}"] = h[v]
        payloads.extend(lot_data)
        time.sleep(1)
    return payloads

def demo_data():
    out, base = [], datetime.now(UTC).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    times = [(base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(24 * 5)]
    for _ in STATIONS:
        h = {"time": times}
        for m in MODELS:
            ph = random.uniform(0, 6)
            h[f"temperature_2m_{m}"] = [12 + 7 * math.sin((i - 9) / 24 * 2 * math.pi) + random.uniform(-1, 1) for i in range(len(times))]
            h[f"wind_gusts_10m_{m}"] = [random.uniform(15, 70) for _ in times]
            h[f"wind_speed_10m_{m}"] = [random.uniform(5, 40) for _ in times]
            h[f"wind_direction_10m_{m}"] = [(300 + random.uniform(-30, 30)) % 360 for _ in times]
            h[f"precipitation_{m}"] = [max(0, random.uniform(-1.5, 1.5)) if math.sin(i / 7 + ph) > 0.6 else 0 for i in range(len(times))]
            h[f"cloud_cover_{m}"] = [min(100, max(0, 50 + 50 * math.sin(i / 9 + ph))) for i in range(len(times))]
            h[f"cloud_cover_low_{m}"] = [random.uniform(0, 40) for _ in times]
            h[f"cloud_cover_mid_{m}"] = [random.uniform(0, 40) for _ in times]
            h[f"cloud_cover_high_{m}"] = [random.uniform(0, 60) for _ in times]
            h[f"weather_code_{m}"] = [random.choice([0, 1, 2, 3, 61, 80, 95]) for _ in times]
        out.append({"hourly": h})
    return out

class Station:
    """Séries horaires (UTC) par modèle + fonctions de fusion pondérée."""
    def __init__(self, name, payload):
        self.name = name
        h = payload["hourly"]
        times = [datetime.strptime(t, "%Y-%m-%dT%H:%M") for t in h["time"]]
        self.series = {}
        for v in HOURLY:
            for m in MODELS:
                arr = h.get(f"{v}_{m}")
                if arr is None and len(MODELS) == 1:
                    arr = h.get(v)
                if arr:
                    self.series[(v, m)] = {t: x for t, x in zip(times, arr) if x is not None}
        self.models_used = sorted({m for (v, m), s in self.series.items()
                                   if v == "temperature_2m" and s})

    def raw(self, v, m, t):
        return self.series.get((v, m), {}).get(t)

    def mean(self, v, t):
        num = den = 0.0
        for m, w in MODELS.items():
            x = self.raw(v, m, t)
            if x is not None:
                num += w * x; den += w
        return num / den if den else None

    def direction(self, t):
        """Moyenne vectorielle pondérée par vitesse et par poids de modèle."""
        u = v = 0.0; ok = False
        for m, w in MODELS.items():
            d, s = self.raw("wind_direction_10m", m, t), self.raw("wind_speed_10m", m, t)
            if d is None:
                continue
            s = s if s else 1.0
            u += w * s * math.sin(math.radians(d)); v += w * s * math.cos(math.radians(d)); ok = True
        return (math.degrees(math.atan2(u, v)) % 360) if ok else None

    def codes(self, t):
        return [c for m in MODELS if (c := self.raw("weather_code", m, t)) is not None]

# ------------------------------------------------------------ CALCULS -----
def sky_stats(st, t0, t1):
    """Nébulosité moyenne sur [t0 ; t1] : (totale, basse, moyenne, haute), None si absente."""
    hrs = list(frange(t0, t1))
    def avg(v):
        xs = [x for t in hrs if (x := st.mean(v, t)) is not None]
        return sum(xs) / len(xs) if xs else None
    return avg("cloud_cover"), avg("cloud_cover_low"), avg("cloud_cover_mid"), avg("cloud_cover_high")

def opaque_cover(cc, cl, cm, ch):
    """Couverture par les nuages opaques (bas + moyens), hors cirrus fins.
    Recouvrement aléatoire entre couches ; si les nuages moyens manquent, on les déduit du total."""
    cc = 50.0 if cc is None else cc
    cl, ch = cl or 0.0, ch or 0.0
    if cm is None:
        d = (1 - cl / 100) * (1 - ch / 100)
        cm = max(0.0, min(100.0, 100 * (1 - (1 - cc / 100) / d))) if d > 0.02 else 0.0
    return 100 * (1 - (1 - cl / 100) * (1 - cm / 100))

def picto(st, t0, t1):
    """Code meteoblue (1-35, jeu "day") pour la fenêtre [t0 ; t1].
    Heuristique maison : le ciel est classé d'après les nuages OPAQUES (bas + moyens) ;
    les nuages hauts (cirrus) n'ajoutent qu'un voile (pictos « avec cirrus »)."""
    hrs = list(frange(t0, t1))
    cc, cl, cm, ch = sky_stats(st, t0, t1)
    ch = ch or 0.0
    op = opaque_cover(cc, cl, cm, ch)
    p = sum(x for t in hrs[1:] if (x := st.mean("precipitation", t)) is not None)
    temps = [x for t in hrs if (x := st.mean("temperature_2m", t)) is not None]
    codes = [c for t in hrs for c in st.codes(t)]

    def cir():                           # 0 = rien, 1 = quelques cirrus, 2 = cirrus
        return 0 if ch < 10 else 1 if ch < 40 else 2

    thunder = any(c in (95, 96, 99) for c in codes)
    snow_code = any(c in (71, 73, 75, 77, 85, 86) for c in codes)
    rain_code = any(c in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82) for c in codes)
    wet = p >= 0.3
    snow = snow_code or (wet and temps and max(temps) < 1.0)
    shower = any(c in (80, 81, 82, 85, 86) for c in codes) or (wet and op < 85)
    fog = bool(codes) and sum(c in (45, 48) for c in codes) > len(codes) / 2

    # Précipitations / orages (prioritaires)
    if thunder and wet:
        return 29 if snow else 30 if p >= 8 else 27 if p >= 1.5 else 28
    if thunder:
        return 10 + cir()
    if wet and snow:
        if snow_code and rain_code and p >= 0.5: return 35
        if shower: return 32
        return 26 if p >= 6 else 24 if p >= 1 else 34
    if wet:
        if shower: return 31
        return 25 if p >= 10 else 23 if p >= 1.5 else 33
    if fog:
        return 16 + cir()
    # Ciel (nuages opaques) + voile de cirrus
    if op < 20:
        low = (cl or 0.0) >= 5 or op >= 5
        base = {(0, 0): 1, (0, 1): 2, (0, 2): 3, (1, 0): 4, (1, 1): 5, (1, 2): 6}
        return base[(1 if low else 0, cir())]
    if op < 55:
        return 7 + cir()
    if op < 85:
        return 19 + cir()
    return 22

def compute(st, d):
    corr = CORRECTIONS.get(st.name, {})
    dt = corr.get("t", 0.0)
    res = {"ech": []}
    for label, hloc, (a, b) in ECHEANCES:
        t = loc_to_utc(d, hloc)
        temp = st.mean("temperature_2m", t)
        gust = st.mean("wind_gusts_10m", t)
        # rafale/dir sur la tranche +-1h pour ne pas rater un pic ponctuel
        gmax = max([g for k in (-1, 0, 1) if (g := st.mean("wind_gusts_10m", t + timedelta(hours=k))) is not None], default=gust)
        res["ech"].append({
            "label": label,
            "t": rnd(temp + dt) if temp is not None else None,
            "gust": r10(gmax), "dir": st.direction(t),
            "picto": picto(st, loc_to_utc(d, a), loc_to_utc(d, b)),
        })
    s_tn, e_tn = utc_dt(d - timedelta(days=1), TN_START_UTC), utc_dt(d, TN_END_UTC)
    temps = [x for t in frange(s_tn, e_tn) if (x := st.mean("temperature_2m", t)) is not None]
    tx_t = [x for t in frange(utc_dt(d, TX_START_UTC), utc_dt(d, TX_END_UTC)) if (x := st.mean("temperature_2m", t)) is not None]
    res["tn"] = rnd(min(temps) + dt) if temps else None
    res["tx"] = rnd(max(tx_t) + dt) if tx_t else None
    # précip : l'échéance horaire h cumule (h-1 ; h]
    res["rr"] = sum(p for t in frange(s_tn + timedelta(hours=1), e_tn) if (p := st.mean("precipitation", t)) is not None)
    gs = [g for t in frange(s_tn + timedelta(hours=1), e_tn) if (g := st.mean("wind_gusts_10m", t)) is not None]
    res["gmax"] = r10(max(gs)) if gs else None
    res["gmax_dir"] = None
    if gs:
        tmax = max((t for t in frange(s_tn + timedelta(hours=1), e_tn) if st.mean("wind_gusts_10m", t) is not None),
                   key=lambda t: st.mean("wind_gusts_10m", t))
        res["gmax_dir"] = st.direction(tmax)
    res["models"] = len(st.models_used)
    return res

# ------------------------------------------------------------ BULLETIN ----
DIR_FR = {"N": "nord", "NNE": "nord-nord-est", "NE": "nord-est", "ENE": "est-nord-est",
          "E": "est", "ESE": "est-sud-est", "SE": "sud-est", "SSE": "sud-sud-est",
          "S": "sud", "SSO": "sud-sud-ouest", "SO": "sud-ouest", "OSO": "ouest-sud-ouest",
          "O": "ouest", "ONO": "ouest-nord-ouest", "NO": "nord-ouest", "NNO": "nord-nord-ouest"}
SKY_OF = {**{c: 0 for c in (1, 2, 3)}, **{c: 1 for c in (4, 5, 6, 13, 14, 15)},
          **{c: 2 for c in (7, 8, 9, 10, 11, 12)}, 19: 3, 20: 3, 21: 3, 22: 4}
FOG = {16, 17, 18}
THUNDER = {27, 28, 29, 30}
THUNDER_RISK = {10, 11, 12}
SNOW = {24, 26, 29, 32, 34, 35}
SKY_TXT = ["ensoleillé", "peu nuageux", "partiellement nuageux", "très nuageux", "couvert"]
WET = set(range(23, 36))
MOMENTS = ["le matin", "à midi", "l'après-midi"]

def liste(noms, n=4):
    noms = list(noms)
    if len(noms) > n:
        return ", ".join(noms[:n]) + "…"
    return noms[0] if len(noms) == 1 else ", ".join(noms[:-1]) + " et " + noms[-1]

def vec_dir(pairs):
    """Direction moyenne (vecteur) de couples (degrés, poids)."""
    u = sum(w * math.sin(math.radians(d)) for d, w in pairs if d is not None)
    v = sum(w * math.cos(math.radians(d)) for d, w in pairs if d is not None)
    return math.degrees(math.atan2(u, v)) % 360 if (u or v) else None

def bulletin(rows):
    """Quelques phrases de synthèse construites à partir des valeurs du tableau."""
    R = [(n, r) for n, r, _ in rows]
    N = len(R)
    out = []

    # --- Ciel
    desc = []
    for i in range(len(ECHEANCES)):
        cats = [r["ech"][i]["picto"] for _, r in R]
        sec = [SKY_OF[c] for c in cats if c in SKY_OF]
        wet = sum(c in WET for c in cats)
        fog = sum(c in FOG for c in cats)
        if wet >= N / 2:
            desc.append("pluvieux")
        elif fog >= N / 3:
            desc.append("brumeux")
        elif sec:
            desc.append(SKY_TXT[min(4, int(sum(sec) / len(sec) + 0.5))])
        else:
            desc.append("variable")
    if len(set(desc)) == 1:
        out.append(f"<b>Ciel :</b> {desc[0]} toute la journée.")
    else:
        out.append("<b>Ciel :</b> " + ", ".join(f"{d} {m}" for d, m in zip(desc, MOMENTS)) + ".")

    # --- Précipitations
    rr_max_n, rr_max = max(((n, r["rr"]) for n, r in R), key=lambda x: x[1])
    wet_st = [n for n, r in R if r["rr"] >= 1.0]
    orage_st = [n for n, r in R if any(e["picto"] in THUNDER | THUNDER_RISK for e in r["ech"])]
    neige_st = [n for n, r in R if any(e["picto"] in SNOW for e in r["ech"])]
    if wet_st:
        par_ech = [sum(r["ech"][i]["picto"] in WET for _, r in R) for i in range(len(ECHEANCES))]
        i_max = par_ech.index(max(par_ech))
        txt = (f"pluie sur {len(wet_st)} commune{'s' if len(wet_st) > 1 else ''} "
               f"({liste(wet_st)}), surtout {MOMENTS[i_max]}, avec des cumuls de {classe_pluie(rr_max)} mm "
               f"à {rr_max_n} sur 24 h.")
    elif rr_max >= 0.2:
        txt = f"quelques gouttes possibles localement (moins de 1 mm)."
    else:
        txt = "pas de précipitation significative."
    if orage_st:
        txt += f" Risque d'orage : {liste(orage_st)}."
    if neige_st:
        txt += f" Neige possible : {liste(neige_st)}."
    out.append("<b>Précipitations :</b> " + txt)

    # --- Températures
    tn = [(r["tn"], n) for n, r in R if r["tn"] is not None]
    tx = [(r["tx"], n) for n, r in R if r["tx"] is not None]
    if tn and tx:
        tn_lo, tn_hi = min(tn), max(tn)
        tx_lo, tx_hi = min(tx), max(tx)
        moy = rnd(sum(t for t, _ in tx) / len(tx))
        out.append(f"<b>Températures :</b> minimales de {tn_lo[0]} °C ({tn_lo[1]}) à {tn_hi[0]} °C ({tn_hi[1]}) ; "
                   f"maximales de {tx_lo[0]} °C ({tx_lo[1]}) à {tx_hi[0]} °C ({tx_hi[1]}), "
                   f"{moy} °C en moyenne.")

    # --- Vent
    g = [(r["gmax"], n, r["gmax_dir"]) for n, r in R if r["gmax"] is not None]
    if g:
        gm, gn, _ = max(g)
        med = sorted(x[0] for x in g)[len(g) // 2]
        if gm < 30:
            out.append("<b>Vent :</b> faible, rafales inférieures à 30 km/h.")
        else:
            d = vec_dir([(x[2], x[0]) for x in g])
            nom = DIR_FR[compass(d)] if d is not None else ""
            mistral = d is not None and (d >= 315 or d <= 22.5) and med >= 50
            de = "d'" if nom[:1] in "eo" else "de "
            txt = f"vent {de}{nom}{' (mistral)' if mistral else ''}, rafales courantes de {med} km/h, jusqu'à {gm} km/h à {gn}."
            fortes = [n for v, n, _ in g if v >= 60]
            if fortes and len(fortes) < N:
                txt += f" Rafales ≥ 60 km/h sur {liste(fortes)}."
            elif len(fortes) == N:
                txt += " Rafales ≥ 60 km/h sur l'ensemble des communes."
            out.append("<b>Vent :</b> " + txt)
    return out

# --------------------------------------------------- BULLETIN LITTÉRAIRE ----
NB = "\u00a0"                      # espace insécable
ZONES = {
    "la vallée du Rhône": ["Montjoyer", "Donzère", "Pierrelatte", "Pont-St-Esprit", "Bagnols/Cèze",
                           "Laudun", "Roquemaure", "Orange", "Avignon", "Beaucaire"],
    "les garrigues": ["Nîmes", "Uzès", "Remoulins", "Pouzilhac", "Verfeuil", "Sabran"],
    "le piémont cévenol": ["Lussan", "Barjac", "Alès"],
}
CIRRUS = {2, 3, 5, 6, 8, 9, 11, 12, 14, 15, 17, 18, 20, 21}

def deg(v):
    return f"{rnd(v)}{NB}°C"

def plage(lo, hi):
    """« de 3 °C à 7 °C », ou « de 3 °C » si les deux valeurs sont identiques."""
    return f"de {deg(lo)}" if rnd(lo) == rnd(hi) else f"de {deg(lo)} à {deg(hi)}"

def et(noms):
    noms = list(noms)
    return noms[0] if len(noms) == 1 else ", ".join(noms[:-1]) + " et " + noms[-1]

def cap(s):
    return s[:1].upper() + s[1:]

def pluriel(zone):
    return zone.startswith("les ")

def lieu(noms):
    """« sur la vallée du Rhône », « du côté de Nîmes et Uzès », « sur l’ensemble du secteur »."""
    noms = list(dict.fromkeys(noms))
    n, tot = len(noms), sum(len(v) for v in ZONES.values())
    if n >= 0.8 * tot:
        return "sur l’ensemble du secteur"
    pleines = [z for z, mem in ZONES.items() if sum(m in noms for m in mem) >= 0.75 * len(mem)]
    couvert = {m for z in pleines for m in ZONES[z]} & set(noms)
    if pleines and len(couvert) >= 0.8 * n:
        reste = [m for m in noms if m not in couvert]
        txt = "sur " + et(pleines)
        return txt + (f", ainsi qu’à {et(reste)}" if reste else "")
    if n > 5:
        return f"sur de nombreuses communes, dont {et(noms[:3])}"
    return f"à {et(noms)}"

def cls(code):
    if code in (27, 28, 29, 30): return "orage"
    if code in SNOW: return "neige"
    if code in (23, 25, 31, 33): return "pluie"
    if code in FOG: return "brume"
    if code in (10, 11, 12): return "menace"
    return "sec"

def stats_ech(R, i):
    st = {"wet": [], "orage": [], "menace": [], "neige": [], "brume": []}
    idx, cir = [], 0
    for name, r in R:
        c = r["ech"][i]["picto"]; k = cls(c)
        if k in ("pluie", "orage", "neige"): st["wet"].append(name)
        if k in ("orage", "menace", "neige", "brume"): st[k].append(name)
        if k in ("sec", "menace") and c in SKY_OF:
            idx.append(SKY_OF[c]); cir += c in CIRRUS
    st["idx"] = sum(idx) / len(idx) if idx else 3.5
    st["cir"] = cir / len(idx) if idx else 0.0
    return st

def vent_ech(R, i):
    g = [(r["ech"][i]["gust"], r["ech"][i]["dir"], n) for n, r in R if r["ech"][i]["gust"] is not None]
    if not g:
        return None
    return {"mean": sum(x[0] for x in g) / len(g),
            "dir": vec_dir([(x[1], x[0]) for x in g if x[1] is not None]),
            "max": max(g, key=lambda x: x[0])}

def de_nom(d):
    nom = DIR_FR[compass(d)]
    return ("d’" if nom[0] in "eo" else "de ") + nom

def is_mistral(d, g):
    return d is not None and (d >= 315 or d <= 22.5) and g >= 45

def vent_nom(d, g):
    if g < 15:
        return "des brises légères"
    if is_mistral(d, g):
        return "un mistral modéré" if g < 55 else "un mistral soutenu" if g < 75 else "un mistral violent"
    fort = ("un vent faible" if g < 25 else "un vent modéré" if g < 40 else "un vent assez soutenu" if g < 55
            else "un vent soutenu" if g < 70 else "un vent fort" if g < 90 else "un vent violent")
    return fort + (" " + de_nom(d) if d is not None else "")

def ciel(st, rng):
    """Groupe nominal « un ciel … » (après « sous », « offrir »…)."""
    i, cir = st["idx"], st["cir"] >= 0.4
    if i < 0.6:
        return rng.choice(["un ciel d’un bleu presque limpide", "un ciel largement dégagé"]) if not cir else \
               rng.choice(["un ciel bleu, simplement voilé de cirrus", "un ciel dégagé, strié çà et là de nuages élevés"])
    if i < 1.4:
        return rng.choice(["un ciel peu nuageux", "un ciel où ne flottent que quelques nuages"]) if not cir else \
               "un ciel peu nuageux, légèrement voilé en altitude"
    if i < 2.4:
        return rng.choice(["un ciel partagé entre nuages et éclaircies",
                           "un ciel changeant, où les éclaircies alternent avec les passages nuageux"])
    if i < 3.4:
        return rng.choice(["un ciel très nuageux, qui ne laisse filtrer que de timides éclaircies",
                           "un ciel lourdement chargé"])
    return rng.choice(["un ciel couvert", "un plafond nuageux compact et uniforme"])

def niveau_tx(m):
    return ("une chaleur écrasante" if m >= 35 else "une chaleur marquée" if m >= 30
            else "des valeurs estivales" if m >= 25 else "une douceur agréable" if m >= 20
            else "des valeurs modérées" if m >= 15 else "une fraîcheur marquée" if m >= 10
            else "un temps froid" if m >= 5 else "un froid vif")

def lit_situation(R, S, V, rng):
    vs = [v for v in V if v]
    zone = "le Gard et la basse vallée du Rhône"
    out = []
    if vs:
        g = sum(v["mean"] for v in vs) / len(vs)
        d = vec_dir([(v["dir"], v["mean"]) for v in vs if v["dir"] is not None])
        if g < 20:
            out.append(rng.choice([f"Le vent s’efface et laisse {zone} sous l’emprise d’une circulation très faible.",
                                   f"Une atmosphère calme s’installe sur {zone}, presque sans souffle."]))
        elif is_mistral(d, g):
            out.append(rng.choice([f"Un flux de nord-ouest bien établi s’engouffre dans le couloir rhodanien : le mistral s’invite sur {zone}.",
                                   f"Le mistral dévale le couloir rhodanien et balaie {zone}."]))
        elif d is not None and 100 <= d <= 200 and g >= 25:
            out.append(f"Un flux de sud-est, le « marin », s’établit sur {zone} en apportant l’humidité de la Méditerranée.")
        else:
            q = "faible" if g < 25 else "modéré" if g < 40 else "assez soutenu" if g < 55 else "soutenu" if g < 70 else "fort"
            out.append(f"Un flux {q} {de_nom(d) if d is not None else 'variable'} s’établit sur {zone}.")
    N = len(R)
    wet_any = {n for s in S for n in s["wet"]}
    rrmax = max([r["rr"] for _, r in R if r["rr"] is not None] or [0])
    idx = sum(s["idx"] for s in S) / len(S)
    orage = {n for s in S for n in s["orage"]}
    i0, i2 = S[0]["idx"], S[2]["idx"]
    if not wet_any and rrmax < 0.5:
        if i0 - i2 >= 1.2:
            out.append("Le temps sera sec, avec un début de journée gris avant de larges éclaircies.")
        elif i2 - i0 >= 1.2:
            out.append("Le temps sera sec, mais le soleil du matin cédera peu à peu la place aux nuages.")
        else:
            out.append("Le temps sera sec et " + ("largement ensoleillé." if idx < 0.8 else
                       "alternera éclaircies et nuages." if idx < 1.8 else "gris, le soleil devant composer avec des nuages nombreux."))
    elif len(wet_any) < 0.3 * N:
        out.append("Le temps reste globalement sec, mais quelques ondées viendront perturber la journée" +
                   (", parfois orageuses." if orage else "."))
    else:
        out.append("Le temps se montre franchement instable" + (", avec des orages sur le secteur." if orage else ", avec de la pluie sur une bonne partie du secteur."))
    return " ".join(out)

def lit_matin(R, S, V, rng):
    s, out = S[0], []
    share = len(s["wet"]) / len(R)
    if share >= 0.6:
        out.append(f"Le jour se lève sous un ciel bas et pluvieux, la pluie s’installant {lieu(s['wet'])}.")
    else:
        t = rng.choice(["Le jour se lève sous {c}", "L’aube se dessine sous {c}",
                        "Les premières lueurs du jour éclairent {c}"]).format(c=ciel(s, rng))
        if s["brume"]:
            t += f", avec des brumes ou des brouillards {lieu(s['brume'])}"
        out.append(t + ".")
        if s["wet"]:
            out.append(("Quelques gouttes ou ondées isolées" if share < 0.15 else "Des ondées")
                       + f" se manifestent {lieu(s['wet'])}.")
        if s["orage"]:
            out.append(f"Le ciel gronde déjà {lieu(s['orage'])}.")
    T = [(r["ech"][0]["t"], n) for n, r in R if r["ech"][0]["t"] is not None]
    if T:
        lo, hi, m = min(T), max(T), sum(t for t, _ in T) / len(T)
        q = ("un matin glacial, avec du gel presque partout" if m < 0 else "un matin froid, propice à la gelée blanche dans les points bas" if m < 3
             else "un matin frais" if m < 8 else "un matin doux" if m < 13 else "un matin tiède" if m < 19
             else "un matin déjà chaud" if m < 25 else "un matin d’une chaleur déjà lourde")
        if lo[0] == hi[0]:
            out.append(f"À 8{NB}h, le thermomètre affiche {deg(lo[0])} partout{NB}: {q}.")
        else:
            out.append(f"À 8{NB}h, le thermomètre affiche de {deg(lo[0])} ({lo[1]}) à {deg(hi[0])} ({hi[1]}), "
                       f"soit {deg(rnd(m))} en moyenne{NB}: {q}.")
    tn = [(r["tn"], n) for n, r in R if r["tn"] is not None]
    if tn:
        gel = [n for v, n in tn if v <= 0]
        trop = [n for v, n in tn if v >= 20]
        if gel:
            mn = min(tn)
            mx = max(v for v, _ in tn if v <= 0)
            out.append(f"En fin de nuit, le gel a pu s’installer {lieu(gel)}, avec des minimales "
                       + (f"voisines de {deg(mn[0])}." if rnd(mn[0]) == rnd(mx) else f"descendant jusqu’à {deg(mn[0])} à {mn[1]}."))
        elif trop:
            out.append(f"La nuit aura été tropicale {lieu(trop)}, sans que les minimales ne passent sous 20{NB}°C.")
    v = V[0]
    if v:
        if v["mean"] < 15:
            out.append("Le vent est quasi nul, tout au plus quelques brises légères.")
        else:
            out.append(f"{cap(vent_nom(v['dir'], v['mean']))} " + rng.choice(["souffle dès le lever", "se fait déjà sentir au lever"])
                       + f", avec des rafales d’environ {r10(v['mean'])}{NB}km/h en moyenne.")
    return " ".join(out)

def lit_apres_midi(R, S, V, rng):
    out = []
    d_idx = S[2]["idx"] - S[0]["idx"]
    wet12, wet16 = S[1]["wet"], S[2]["wet"]
    union = list(dict.fromkeys(wet12 + wet16))
    if len(S[2]["wet"]) / len(R) >= 0.6:
        out.append("L’après-midi s’écoule sous un ciel bas et pluvieux.")
    elif d_idx >= 0.8:
        out.append(rng.choice(["Les nuages gagnent peu à peu du terrain", "Le ciel se charge progressivement"])
                   + f", pour offrir en fin d’après-midi {ciel(S[2], rng)}.")
    elif d_idx <= -0.8:
        out.append(rng.choice(["Les nuages s’effilochent et laissent place à de belles éclaircies",
                               "Le ciel se déchire peu à peu"]) + f", si bien que la journée s’achève sous {ciel(S[2], rng)}.")
    else:
        calme = S[2]["idx"] < 1.8 and not union
        out.append(rng.choice([f"L’après-midi s’écoule sous {ciel(S[2], rng)}, dans la continuité de la matinée.",
                               f"Rien ne vient troubler la quiétude du ciel : l’après-midi se déroule sous {ciel(S[2], rng)}."]
                              if calme else [f"L’après-midi s’écoule sous {ciel(S[2], rng)}, dans la continuité de la matinée."]))
    if union and len(S[2]["wet"]) / len(R) < 0.6:
        quand = ("dès la mi-journée, avant de se calmer" if len(wet12) > len(wet16) + 1 else
                 "en cours d’après-midi" if len(wet16) > len(wet12) + 1 else "tout au long de l’après-midi")
        out.append(("Quelques ondées isolées" if len(union) <= 3 else "Des ondées")
                   + f" se développent {lieu(union)} {quand}.")
    orage = list(dict.fromkeys(S[1]["orage"] + S[2]["orage"]))
    menace = [n for n in dict.fromkeys(S[1]["menace"] + S[2]["menace"]) if n not in orage]
    if orage:
        out.append(f"Des orages éclatent {lieu(orage)}, accompagnés de pluies parfois soutenues.")
    elif menace:
        out.append(f"Des nuages à développement orageux ne sont pas exclus {lieu(menace)}.")
    TX = [(r["tx"], n) for n, r in R if r["tx"] is not None]
    T8 = [r["ech"][0]["t"] for _, r in R if r["ech"][0]["t"] is not None]
    if TX:
        lo, hi, m = min(TX), max(TX), sum(t for t, _ in TX) / len(TX)
        delta = rnd(m - sum(T8) / len(T8)) if T8 else None
        if rnd(lo[0]) == rnd(hi[0]):
            t = f"Les maximales plafonnent à {deg(lo[0])}, {niveau_tx(m)}"
        else:
            t = rng.choice([f"Les maximales s’échelonnent de {deg(lo[0])} ({lo[1]}) à {deg(hi[0])} ({hi[1]})",
                            f"Le thermomètre culmine entre {deg(lo[0])} ({lo[1]}) et {deg(hi[0])} ({hi[1]})"])
            t += f", soit {deg(m)} en moyenne, {niveau_tx(m)}"
        if delta is not None:
            t += (f". Le mercure gagne {delta} degrés depuis le lever, une belle amplitude diurne" if delta >= 10 else
                  f". Le mercure ne progresse que de {max(delta, 0)} degrés depuis le lever, une journée peu contrastée" if delta <= 3 else
                  f". Le mercure progresse de {delta} degrés depuis le lever")
        out.append(t + ".")
        zm = {z: sum(r["tx"] for n, r in R if n in mem and r["tx"] is not None) /
                 max(1, sum(1 for n, r in R if n in mem and r["tx"] is not None)) for z, mem in ZONES.items()}
        hot, cold = max(zm, key=zm.get), min(zm, key=zm.get)
        if zm[hot] - zm[cold] >= 1.5:
            out.append(f"C’est {hot} qui affiche les valeurs les plus élevées, tandis que {cold} "
                       f"{'restent' if pluriel(cold) else 'reste'} en retrait.")
    v1, v2 = V[1], V[2]
    if V[0] and (v1 or v2):
        vv = v2 or v1
        g0, g1 = V[0]["mean"], vv["mean"]
        pic = vv["max"]
        if g1 >= 15 or g0 >= 15:
            pointe = f", avec des pointes à {r10(pic[0])}{NB}km/h à {pic[2]}" if pic[0] >= g1 + 8 else ""
            if g1 >= 1.2 * g0 and g1 >= 25:
                t = (f"Le vent se renforce{NB}: {vent_nom(vv['dir'], g1)}, avec des rafales d’environ "
                     f"{r10(g1)}{NB}km/h en moyenne" + (f" et des pointes à {r10(pic[0])}{NB}km/h à {pic[2]}" if pointe else ""))
            elif g1 <= 0.8 * g0:
                t = f"Le vent mollit progressivement, les rafales ne dépassant guère {r10(g1)}{NB}km/h en moyenne en fin de journée"
            else:
                t = f"Le vent se maintient{NB}: {vent_nom(vv['dir'], g1)}{pointe}"
            if V[0]["dir"] is not None and vv["dir"] is not None and min(abs(V[0]["dir"] - vv["dir"]), 360 - abs(V[0]["dir"] - vv["dir"])) >= 60 and g1 >= 20:
                t += f", en tournant au secteur {DIR_FR[compass(vv['dir'])]}"
            out.append(t + ".")
    return " ".join(out)

def lit_resume(R, S, V, rng):
    out = []
    tn = [r["tn"] for _, r in R if r["tn"] is not None]
    tx = [r["tx"] for _, r in R if r["tx"] is not None]
    rr = [(r["rr"], n) for n, r in R if r["rr"] is not None]
    gm = [(r["gmax"], n) for n, r in R if r["gmax"] is not None]
    bits = []
    if tn and tx:
        bits.append(f"des minimales {plage(min(tn), max(tn))} et des maximales {plage(min(tx), max(tx))}")
    rrmax = max(rr) if rr else (0, "")
    cl = classe_pluie(rrmax[0])
    bits.append((f"des cumuls de pluie sur 24{NB}h supérieurs à 200{NB}mm à {rrmax[1]}" if cl == "> 200"
                 else f"des cumuls de pluie sur 24{NB}h de {cl}{NB}mm à {rrmax[1]}") if rrmax[0] > 1 else
                "quelques gouttes sans conséquence" if rrmax[0] >= 0.1 else "aucune pluie")
    if gm:
        g = max(gm)
        bits.append(f"des rafales maximales de {g[0]}{NB}km/h à {g[1]}")
    out.append("La journée apporte " + ", ".join(bits[:-1]) + " et " + bits[-1] + "." if len(bits) > 1 else "")
    orage = any(s["orage"] for s in S)
    gmax = max(gm)[0] if gm else 0
    idx = sum(s["idx"] for s in S) / len(S)
    wet_any = {n for s in S for n in s["wet"]}
    if gmax >= 90:
        out.append("Ces rafales justifient la plus grande prudence : arbres, chantiers et routes exposées sont à surveiller.")
    elif orage or rrmax[0] >= 30:
        out.append("Les orages et les fortes pluies appellent à la vigilance, notamment près des cours d’eau sujets aux crues rapides.")
    elif gmax >= 70:
        out.append("Ces rafales appellent à la prudence sur les routes exposées et pour tout ce qui est mal arrimé.")
    elif tx and sum(tx) / len(tx) >= 35:
        out.append("Cette chaleur appelle à la prudence : hydratation, ombre et repos aux heures les plus chaudes seront de mise.")
    elif tx and sum(tx) / len(tx) >= 30 and not wet_any:
        out.append("Une journée chaude et sèche : pensez à vous hydrater et à limiter l’effort en milieu d’après-midi.")
    elif tx and sum(tx) / len(tx) < 5:
        out.append("Un froid vif s’installe : mieux vaut bien se couvrir, surtout aux heures matinales.")
    elif wet_any:
        out.append("Le parapluie sera de sortie par moments : les ondées resteront toutefois de courte durée.")
    elif S[0]["idx"] - S[2]["idx"] >= 1.2:
        out.append("Un matin voilé, puis un après-midi lumineux : la patience sera récompensée.")
    elif S[2]["idx"] - S[0]["idx"] >= 1.2:
        out.append("Mieux vaut profiter du soleil du matin : l’après-midi sera plus gris.")
    elif idx < 0.8 and gmax >= 55:
        out.append("Une belle journée lumineuse, mais où le vent rappellera qu’il est maître des lieux.")
    elif idx < 0.8:
        out.append("Une journée lumineuse, à passer dehors sans hésiter.")
    elif idx < 1.8:
        out.append("Une journée agréable, où le soleil saura se faire désirer sans jamais disparaître.")
    else:
        out.append("Une journée grise, mais clémente.")
    return " ".join(x for x in out if x)

def bulletin_litteraire(rows, d):
    """Bulletin rédigé (situation, matin, après-midi, résumé), varié d’un jour à l’autre."""
    try:
        R = [(n, r) for n, r, _ in rows]
        rng = random.Random(d.toordinal())
        S = [stats_ech(R, i) for i in range(3)]
        V = [vent_ech(R, i) for i in range(3)]
        parts = [("Situation.", lit_situation(R, S, V, rng)), ("Le matin.", lit_matin(R, S, V, rng)),
                 ("L’après-midi.", lit_apres_midi(R, S, V, rng)), ("En résumé.", lit_resume(R, S, V, rng))]
        titre = f"{cap(JOURS[d.weekday()])} {d.day} {MOIS[d.month-1]}"
        corps = "".join(f'<p><span class="lh">{t}</span> {x}</p>' for t, x in parts if x)
        return (f'<article class="lit"><h2>Bulletin météo · {titre}</h2>{corps}'
                '<p class="note">Bulletin rédigé automatiquement à partir de la moyenne des modèles AROME HD et ICON-CH1. '
                'Il ne remplace ni les prévisions ni la vigilance de Météo-France.</p></article>')
    except Exception as e:                                  # ne jamais bloquer la page
        print(f"Bulletin littéraire ignoré : {type(e).__name__} {e}", file=sys.stderr)
        return ""

# ------------------------------------------------------------ CARTE -------
TABS = ('<nav class="tabs"><button data-v="tableau" class="on">Tableau</button>'
        '<button data-v="carte">Carte</button></nav>')

MAP_TEMPLATE = r"""<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css">
<style>
.tabs{display:flex;gap:8px;margin:0 0 10px}
.tabs button,.mapbar button{background:#0f2a55;color:var(--mut);border:1px solid var(--line);border-radius:999px;
  padding:6px 18px;font-size:13px;cursor:pointer;font-family:inherit}
.tabs button.on,.mapbar button.on{background:var(--acc);color:#06203a;border-color:var(--acc);font-weight:600}
.mapbar{display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap}
#map{height:calc(100vh - 150px);min-height:540px;border-radius:10px;border:1px solid var(--line);
  background:#0b1d3a;position:relative;overflow:hidden}
#map.leaflet-container{background:#0b1d3a;font-family:inherit}
.mkw{background:none;border:0}
.tuiles-sombres{filter:url(#fond-sombre)}
.leaflet-control-layers,.leaflet-bar a{background:#0f2a55;color:#e8f0ff;border-color:#2f5aa0}
.leaflet-control-layers{border:1px solid #2f5aa0;font-family:inherit;font-size:12px}
.leaflet-control-attribution{background:rgba(9,25,52,.75)!important;color:#9db8e6!important}
.leaflet-control-attribution a{color:#4fc3f7!important}
.mk{position:absolute;left:0;right:0;bottom:0;box-sizing:border-box;display:flex;flex-direction:column;align-items:center;
  background:rgba(9,25,52,.94);border:1px solid #2f5aa0;border-radius:11px;color:#e8f0ff;line-height:1.15;
  text-align:center;box-shadow:0 2px 9px rgba(0,0,0,.6);padding:5px 5px 6px}
.mk::after{content:"";position:absolute;left:50%;bottom:-7px;margin-left:-6px;border:6px solid transparent;
  border-bottom:0;border-top:7px solid #2f5aa0}
.mk .mn{font-size:12.5px;font-weight:600;line-height:16px;color:#d6e5ff;max-width:100%;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.mk .mi{height:46px;margin:3px 0 2px;display:flex;align-items:center;justify-content:center}
.mk .pic{height:44px;width:auto}.mk .emo{font-size:36px;line-height:44px}
.mk .mt b{font-size:22px;font-weight:700}.mk .mt.tx b{font-size:24px;font-weight:800;color:#ff1f1f}
.mk .mt.tn b{font-size:15px;font-weight:700;color:#4dc3ff}
.mk .mr{font-size:11px;color:#9ad0ff;white-space:nowrap;margin-top:1px}
.mk .mg{display:flex;align-items:center;justify-content:center;gap:4px;font-size:11px;color:#cfe0ff;white-space:nowrap;margin-top:2px}
.mk .mg b{font-size:14px;color:#fff}.mk .mg i{font-style:normal;color:#8fd3ff}
.mk .ar{flex:none;display:block}
.mk .wv{text-align:left;line-height:1.2}
#map.fb{background:radial-gradient(circle at 50% 40%,#12305f,#0a1c3b 75%)}
.fbx{position:absolute;left:0;top:0;right:0;bottom:0}
.fbm{position:absolute;transform:translate(-50%,-100%);width:104px;z-index:2}
.fbl{position:absolute;left:0;top:0;pointer-events:none}
.fbm .mk{position:relative}
.fbnote{position:absolute;left:10px;bottom:6px;font-size:11px;color:var(--mut)}
.maphelp{color:var(--mut);font-size:12px;margin-top:6px}
#bulpage .lit{max-width:960px;margin:8px auto 0;padding:24px 34px 14px}
#bulpage .lit h2{font-size:22px;margin-bottom:12px}
#bulpage .lit p{font-size:17.5px;line-height:1.75}
.lit{margin-top:16px;background:linear-gradient(180deg,#112b55,#0d2147);border:1px solid var(--line);
  border-left:4px solid var(--acc);border-radius:10px;padding:16px 24px 10px;
  font-family:Georgia,"Times New Roman",serif}
.lit h2{margin:0 0 6px;font-size:18px;font-weight:600;color:#fff;letter-spacing:.3px}
.lit p{margin:9px 0;font-size:15.5px;line-height:1.65;color:#e3ecff;text-align:justify;hyphens:auto}
.lit .lh{color:var(--acc);font-variant:small-caps;font-weight:700;letter-spacing:.6px;margin-right:4px}
.lit p.note{font-size:11.5px;color:var(--mut);font-style:italic;text-align:left;margin-top:12px;
  font-family:"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
</style>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><filter id="fond-sombre" color-interpolation-filters="sRGB"><feColorMatrix type="matrix" values="-0.2551 -0.8582 -0.0866 0.0000 1.2392 -0.2750 -0.9250 -0.0934 0.0000 1.3953 -0.2778 -0.9345 -0.0943 0.0000 1.5184 0.0000 0.0000 0.0000 1.0000 0.0000"/></filter></svg>
<section id="vue-carte" hidden>
  <div class="mapbar">
    <button data-m="0" class="on">8 h</button><button data-m="1">12 h</button>
    <button data-m="2">16 h</button><button data-m="3">Synthèse 24 h</button><button data-m="4">Bulletin</button>
  </div>
  <div id="mapwrap">
    <div id="map"></div>
    <div class="maphelp">Molette ou doigts pour zoomer. Vent : rafales en km/h arrondies à la dizaine ; la flèche indique le sens vers lequel
      souffle le vent, <b style="color:#ffd21f">jaune</b> dès 60, <b style="color:#ff9a1f">orange</b> dès 80, <b style="color:#ff3b3b">rouge</b> dès 100 km/h.
      Synthèse : Tx en rouge, Tn en bleu, pluie, rafale max et pictogramme de midi.</div>
  </div>
  <div id="bulpage" hidden>
  __BULLETIN__
  </div>
</section>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
(function(){
  var D = __DATA__;
  var mode = 0, map = null, inited = false, mk = [], lines = [], FB = null;
  var SZ = [142, 142, 142, 178], W = 104;
  function $(s){return document.querySelector(s);}
  function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');}
  function gcls(g){return g==null?'':(g>=80?'w3':g>=60?'w2':g>=40?'w1':'');}
  function T(v){return v==null?'–':'<b>'+v+'<span class="u">°C</span></b>';}
  function arrow(dd, g){                       // flèche épaisse, orientée vers où souffle le vent
    if (g == null || dd == null || g < 60) return '';
    var col = g > 99 ? '#ff3b3b' : g > 79 ? '#ff9a1f' : '#ffd21f';
    return '<svg class="ar" viewBox="-12 -12 24 24" width="24" height="24" aria-hidden="true"><g transform="rotate('+Math.round((dd+180)%360)+')">'
      + '<path d="M0,-11 L9.5,-1 L3.8,-1 L3.8,11 L-3.8,11 L-3.8,-1 L-9.5,-1 Z" fill="'+col+'" stroke="#06142b" stroke-width="1.3" stroke-linejoin="round"/></g></svg>';
  }
  function wind(g, d, dd){
    if (g == null) return '<div class="mg">–</div>';
    var ar = arrow(dd, g);                       // avec flèche : la direction passe sous la vitesse
    return '<div class="mg">'+ar+(ar ? '<span class="wv"><b>'+g+'</b> km/h'+(d ? '<br><i>'+d+'</i>' : '')+'</span>'
                                  : '<span><b>'+g+'</b> km/h'+(d ? ' <i>'+d+'</i>' : '')+'</span>')+'</div>';
  }
  function card(c){
    var name = '<div class="mn">'+esc(c.name)+'</div>';
    if (mode < 3) {
      var e = c.ech[mode];
      return '<div class="mk">'+name+'<div class="mi">'+(D.icons[e.p]||'')+'</div><div class="mt">'+T(e.t)+'</div>'
        + wind(e.g, e.d, e.dd)+'</div>';
    }
    var rr = (c.rr!=null && c.rr>=0.1) ? '<div class="mr">'+c.rc+' mm</div>' : '';
    return '<div class="mk">'+name+'<div class="mi">'+(D.icons[c.ech[1].p]||'')+'</div>'
      + '<div class="mt tx">'+T(c.tx)+'</div><div class="mt tn">'+T(c.tn)+'</div>'+rr
      + wind(c.gmax, c.gd, c.gdd)+'</div>';
  }
  // Écarte les cartes qui se chevauchent. P = positions réelles (px) ; renvoie le bas-centre de chaque carte.
  function declutter(P, w, h, bnd){
    var gap = 6, n = P.length, C = P.map(function(p){return {x:p.x, y:p.y-7};});
    for (var it = 0; it < 1500; it++) {
      var moved = false;
      for (var i = 0; i < n; i++) for (var j = i+1; j < n; j++) {
        var dx = C[j].x - C[i].x, dy = C[j].y - C[i].y;
        var ox = (w+gap) - Math.abs(dx), oy = (h+gap) - Math.abs(dy);
        if (ox > 0 && oy > 0) {
          moved = true;
          if (ox < oy) { var sx = (dx >= 0 ? 1 : -1) * ox/2; C[i].x -= sx; C[j].x += sx; }
          else { var sy = (dy >= 0 ? 1 : -1) * oy/2; C[i].y -= sy; C[j].y += sy; }
        }
      }
      for (i = 0; i < n; i++) {
        C[i].x += (P[i].x - C[i].x) * 0.006; C[i].y += ((P[i].y-7) - C[i].y) * 0.006;
        if (bnd) { C[i].x = Math.min(Math.max(C[i].x, bnd.x0), bnd.x1); C[i].y = Math.min(Math.max(C[i].y, bnd.y0), bnd.y1); }
      }
      if (!moved) break;
    }
    return C;
  }
  function icon(c){
    return L.divIcon({className:'mkw', html:card(c), iconSize:[W,SZ[mode]], iconAnchor:[W/2,SZ[mode]+7]});
  }
  function layout(){
    if (!inited) return;
    var h = SZ[mode], C, P;
    if (map) {
      P = D.communes.map(function(c){var q = map.latLngToLayerPoint([c.lat,c.lon]); return {x:q.x, y:q.y};});
      C = declutter(P, W, h, null);
      D.communes.forEach(function(c,i){
        var ll = map.layerPointToLatLng(L.point(C[i].x, C[i].y+7));
        mk[i].setLatLng(ll);
        var far = Math.abs(C[i].x-P[i].x) + Math.abs(C[i].y+7-P[i].y) > 10;
        lines[i].setLatLngs(far ? [[c.lat,c.lon], ll] : [[c.lat,c.lon],[c.lat,c.lon]]);
      });
    } else {
      var box = $('.fbx'), Wb = box.clientWidth, Hb = box.clientHeight;
      var latc = (FB.a+FB.b)/2, lonc = (FB.x0+FB.x1)/2, kx = 111.32*Math.cos(latc*Math.PI/180), ky = 110.57;
      var sc = Math.min((Wb-2*(W/2+40))/((FB.x1-FB.x0)*kx), (Hb-h-60)/((FB.b-FB.a)*ky));
      P = D.communes.map(function(c){return {
        x:Wb/2+(c.lon-lonc)*kx*sc, y:(Hb+h)/2-(c.lat-latc)*ky*sc};});
      C = declutter(P, W, h, {x0:W/2+4, x1:Wb-W/2-4, y0:h+10, y1:Hb-8});
      var svg = '';
      D.communes.forEach(function(c,i){
        mk[i].style.left = C[i].x+'px'; mk[i].style.top = C[i].y+'px';
        svg += '<line x1="'+C[i].x+'" y1="'+(C[i].y+7)+'" x2="'+P[i].x+'" y2="'+P[i].y+'" stroke="#4fc3f7" stroke-opacity=".7"/>'
             + '<circle cx="'+P[i].x+'" cy="'+P[i].y+'" r="3" fill="#4fc3f7"/>';
      });
      var s = $('.fbl'); s.setAttribute('width', Wb); s.setAttribute('height', Hb); s.innerHTML = svg;
    }
  }
  var started = false;
  function withLeaflet(cb){                    // Leaflet : cdnjs d'abord, jsDelivr en secours
    if (window.L) return cb();
    var sc = document.createElement('script');
    sc.src = 'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js';
    sc.onload = cb; sc.onerror = cb; document.head.appendChild(sc);
  }
  function init(){
    if (started) return; started = true; withLeaflet(build);
  }
  function build(){
    var el = $('#map');
    if (window.L) {
      map = L.map(el, {minZoom:8, maxZoom:14, zoomSnap:0.25, zoomDelta:0.5});
      var fonds = {
        'Sombre': L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
          {maxZoom:14, className:'tuiles-sombres', attribution:'© OpenStreetMap'}),
        'Plan': L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
          {maxZoom:14, attribution:'© OpenStreetMap'}),
        'Relief': L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
          {maxZoom:14, subdomains:'abc', attribution:'© OpenStreetMap, SRTM · © OpenTopoMap'})
      };
      fonds['Sombre'].addTo(map);
      L.control.layers(fonds, null, {position:'topright', collapsed:true}).addTo(map);
      var okT = 0, koT = 0;                    // si le fond sombre ne charge rien, bascule sur « Relief »
      fonds['Sombre'].on('tileload', function(){ okT++; });
      fonds['Sombre'].on('tileerror', function(){
        if (++koT >= 4 && okT === 0 && map.hasLayer(fonds['Sombre'])) { map.removeLayer(fonds['Sombre']); fonds['Relief'].addTo(map); }
      });
      D.communes.forEach(function(c){
        lines.push(L.polyline([[c.lat,c.lon],[c.lat,c.lon]], {color:'#4fc3f7', weight:1, opacity:.75, interactive:false}).addTo(map));
        L.circleMarker([c.lat,c.lon], {radius:3, color:'#4fc3f7', weight:1, fillColor:'#4fc3f7', fillOpacity:1, interactive:false}).addTo(map);
      });
      D.communes.forEach(function(c){
        mk.push(L.marker([c.lat,c.lon], {icon:icon(c), keyboard:false}).addTo(map));
      });
      map.fitBounds(L.latLngBounds(D.communes.map(function(c){return [c.lat,c.lon];})), {padding:[90,90]});
      inited = true;
      map.on('zoomend', layout);
      layout(); setTimeout(layout, 400);
    } else {                                  // repli sans fond de carte (Leaflet indisponible)
      el.classList.add('fb');
      var la = D.communes.map(function(c){return c.lat;}), lo = D.communes.map(function(c){return c.lon;});
      FB = {a:Math.min.apply(null,la), b:Math.max.apply(null,la), x0:Math.min.apply(null,lo), x1:Math.max.apply(null,lo)};
      var box = document.createElement('div'); box.className='fbx';
      el.appendChild(box);
      box.insertAdjacentHTML('beforeend', '<svg class="fbl"></svg>');
      D.communes.forEach(function(c){
        var d = document.createElement('div'); d.className='fbm'; d.innerHTML = card(c); box.appendChild(d); mk.push(d);
      });
      var n = document.createElement('div'); n.className='fbnote';
      n.textContent = 'Carte simplifiée : fond de carte indisponible'; el.appendChild(n);
      inited = true; layout();
    }
  }
  function refresh(){
    if (!inited) return;
    mk.forEach(function(m,i){
      if (map) m.setIcon(icon(D.communes[i])); else m.innerHTML = card(D.communes[i]);
    });
    layout();
  }
  function view(v){
    $('#vue-tableau').hidden = (v !== 'tableau');
    $('#vue-carte').hidden = (v !== 'carte');
    document.querySelectorAll('.tabs button').forEach(function(b){b.classList.toggle('on', b.dataset.v===v);});
    if (v === 'carte') { init(); if (map) map.invalidateSize(); }
    try { history.replaceState(null, '', v==='carte' ? '#carte' : location.pathname+location.search); } catch(e) {}
  }
  document.querySelectorAll('.tabs button').forEach(function(b){b.onclick=function(){view(b.dataset.v);};});
  function showBul(on){                          // page « Bulletin » : remplace la carte
    $('#mapwrap').hidden = on; $('#bulpage').hidden = !on;
    if (!on && map) { map.invalidateSize(); layout(); }
    try { history.replaceState(null, '', on ? '#bulletin' : '#carte'); } catch(e) {}
  }
  document.querySelectorAll('.mapbar button').forEach(function(b){
    b.onclick = function(){
      var m = +b.dataset.m;
      document.querySelectorAll('.mapbar button').forEach(function(x){x.classList.toggle('on', x===b);});
      if (m === 4) { showBul(true); return; }
      showBul(false); mode = m; refresh();
    };
  });
  window.addEventListener('resize', function(){ if (inited && !map) layout(); });
  var h0 = location.hash;
  view(h0 === '#carte' || h0 === '#bulletin' ? 'carte' : 'tableau');
  if (h0 === '#bulletin') { var bb = $('.mapbar button[data-m="4"]'); if (bb) bb.click(); }
})();
</script>
"""

def map_payload(rows):
    """Données des communes pour la carte (JSON injecté dans la page)."""
    coords = {n: (la, lo) for n, la, lo in STATIONS}
    com = []
    for name, r, _ in rows:
        la, lo = coords[name]
        com.append({
            "name": name, "lat": la, "lon": lo,
            "ech": [{"t": e["t"], "g": e["gust"], "d": compass(e["dir"]) if e["dir"] is not None else "",
                     "dd": round(e["dir"]) if e["dir"] is not None else None,
                     "p": e["picto"]} for e in r["ech"]],
            "tn": r["tn"], "tx": r["tx"], "rr": round(r["rr"], 1), "rc": classe_pluie(r["rr"]), "gmax": r["gmax"],
            "gd": compass(r["gmax_dir"]) if r["gmax_dir"] is not None else "",
            "gdd": round(r["gmax_dir"]) if r["gmax_dir"] is not None else None})
    codes = sorted({e["p"] for c in com for e in c["ech"]})
    data = {"communes": com, "icons": {str(k): icon_html(k) for k in codes}}
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

# ------------------------------------------------------------ RENDU -------
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]

def icon_html(code):
    lab = PICTO_FR.get(code, "")
    f = next(iter(sorted(ICON_DIR.glob(f"{code:02d}_day*.png"))), None) if ICON_DIR.exists() else None
    if f:
        return f'<img src="{ICON_DIR.name}/{f.name}" alt="{lab}" title="{lab}" class="pic">'
    emo = ("🌫️" if code in FOG else "⛈️" if code in THUNDER else "🌨️" if code in SNOW
           else "🌧️" if code in WET and code != 31 else "🌦️" if code == 31
           else "☀️" if SKY_OF.get(code, 2) == 0 else "⛅" if SKY_OF.get(code, 2) == 2
           else "🌥️" if SKY_OF.get(code) == 3 else "☁️" if code == 22 else "🌤️")
    return f'<span class="emo" title="{lab}">{emo}</span>'

def wind_cell(g, d):
    if g is None: return "–"
    cls = "w3" if g >= 80 else "w2" if g >= 60 else "w1" if g >= 40 else ""
    dr = compass(d) if d is not None else ""
    return f'<span class="{cls}"><b>{g}</b> <small>km/h</small> <i>{dr}</i></span>'

def temp_cell(t):
    return "–" if t is None else f'<b>{t}<span class="u">°C</span></b>'

def render(rows, d, generated):
    head = "".join(f'<th colspan="3" class="grp">{e[0]}</th>' for e in ECHEANCES)
    sub = "".join('<th class="s">Temps</th><th class="s">T°</th><th class="s">Rafales</th>' for _ in ECHEANCES)
    body = []
    for name, r, partial in rows:
        cells = "".join(f'<td class="c">{icon_html(e["picto"])}</td><td class="t">{temp_cell(e["t"])}</td>'
                        f'<td class="g">{wind_cell(e["gust"], e["dir"])}</td>' for e in r["ech"])
        mark = ' <sup title="Un seul modèle disponible">*</sup>' if partial else ""
        rr = classe_pluie(r["rr"])
        body.append(
            f'<tr><th class="n">{name}{mark}</th>{cells}'
            f'<td class="x tn">{temp_cell(r["tn"])}</td><td class="x tx">{temp_cell(r["tx"])}</td>'
            f'<td class="x rr">{rr} <small>mm</small></td>'
            f'<td class="x g">{wind_cell(r["gmax"], r["gmax_dir"])}</td></tr>')
    bul = "\n".join(f"<p>{l}</p>" for l in bulletin(rows))
    tabs = TABS
    carte = MAP_TEMPLATE.replace("__DATA__", map_payload(rows)).replace("__BULLETIN__", bulletin_litteraire(rows, d))
    titre = f"{JOURS[d.weekday()].capitalize()} {d.day} {MOIS[d.month-1]} {d.year}"
    return f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Météo-Midi · {titre}</title>
<style>
:root{{--bg:#0b1d3a;--card:#102a52;--row:#143262;--line:#24467d;--txt:#e8f0ff;--mut:#8fa9d6;--acc:#4fc3f7}}
*{{box-sizing:border-box}}
body{{margin:0;background:radial-gradient(circle at 20% 0,#14305e,#081530 70%);color:var(--txt);
font-family:"Segoe UI",Roboto,Helvetica,Arial,sans-serif;padding:14px 18px}}
header{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px}}
h1{{font-size:20px;margin:0;letter-spacing:.5px}} h1 span{{color:var(--acc)}}
.sub{{color:var(--mut);font-size:12px}}
.wrap{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:14px;background:var(--card);border-radius:8px;overflow:hidden}}
th,td{{padding:3px 6px;text-align:center;white-space:nowrap;border-bottom:1px solid var(--line)}}
thead th{{background:#0a1b38;color:var(--acc);font-weight:600;font-size:13px}}
thead th.s{{color:var(--mut);font-weight:400;font-size:11px;padding:2px 4px}}
th.grp{{border-left:2px solid var(--line);font-size:15px}}
th.n{{text-align:left;font-weight:600;padding-left:10px;background:var(--row);position:sticky;left:0}}
tbody tr:nth-child(even) td{{background:rgba(255,255,255,.035)}}
tbody tr:hover td{{background:rgba(79,195,247,.12)}}
td.c{{border-left:2px solid var(--line)}}
td.x{{background:rgba(0,0,0,.18)}} td.tn{{border-left:2px solid var(--acc);color:#4dc3ff}} td.tx{{color:#ff2020}}
td.t b{{font-size:22px;font-weight:700}}
td.tn b,td.tx b{{font-size:26px;font-weight:800}}
td.tx b{{color:#ff1f1f;text-shadow:0 0 8px rgba(255,31,31,.45)}}
td.tn b{{color:#4dc3ff}}
.u{{font-size:.55em;font-weight:600;margin-left:1px;opacity:.85}}
td.rr{{color:#9ad0ff}}
.pic{{height:34px;border-radius:6px;vertical-align:middle}} .emo{{font-size:24px;line-height:1;vertical-align:middle}}
td b{{font-size:15px}} small{{color:var(--mut);font-size:10px}} i{{font-style:normal;color:var(--acc);font-size:12px;margin-left:2px}}
.w1 b{{color:#ffe082}} .w2 b{{color:#ffab40}} .w3 b{{color:#ff6e6e}}
td.g{{text-align:left;min-width:92px}}
.bul{{margin-top:12px;background:var(--card);border-left:4px solid var(--acc);border-radius:8px;padding:10px 16px}}.bul h2{{margin:0 0 6px;font-size:14px;color:var(--acc);letter-spacing:.5px;text-transform:uppercase}}.bul p{{margin:3px 0;font-size:14px;line-height:1.45}} .bul b{{color:#fff}}
footer{{color:var(--mut);font-size:11px;margin-top:8px;line-height:1.5}}
@media (max-width:900px){{table{{font-size:12px}} .pic{{height:24px}}}}
</style></head><body>
<header><h1>Météo-Midi <span>· Prévisions {titre}</span></h1>
<div class="sub">Moyenne AROME HD / ICON-CH1 (50/50) · édité le {generated}</div></header>
{tabs}
<div id="vue-tableau">
<div class="wrap"><table>
<thead>
<tr><th rowspan="2" class="n">Commune</th>{head}
<th colspan="4" class="grp">Synthèse 24 h</th></tr>
<tr>{sub}<th class="s">Tn</th><th class="s">Tx</th><th class="s">Pluie</th><th class="s">Rafale max</th></tr>
</thead>
<tbody>
{chr(10).join(body)}
</tbody></table></div>
<section class="bul"><h2>Bulletin</h2>
{bul}
</section>
</div>
{carte}
<footer>Échéances en heure locale · Tn : 18 h UTC veille → 18 h UTC · Tx : 6 h → 18 h UTC ·
Pluie et rafale max : 18 h UTC veille → 18 h UTC · Rafales : <span class="w1"><b>≥40</b></span> <span class="w2"><b>≥60</b></span> <span class="w3"><b>≥80</b></span> km/h ·
Fond de carte © OpenStreetMap contributors · © OpenTopoMap · Pictogrammes © <a style="color:var(--acc)" href="https://content.meteoblue.com/en/research-education/specifications/standards/symbols-and-pictograms">meteoblue</a> · Prévisions brutes de modèles, non corrigées.</footer>
</body></html>"""

# ------------------------------------------------------------ MAIN --------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="jour prévu AAAA-MM-JJ (défaut : demain)")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--out", default="previ.html")
    a = ap.parse_args()
    d = date.fromisoformat(a.date) if a.date else (datetime.now(PARIS) + timedelta(days=1)).date()
    payloads = demo_data() if a.demo else fetch(d)
    rows = []
    for (name, _, _), p in zip(STATIONS, payloads):
        st = Station(name, p)
        r = compute(st, d)
        rows.append((name, r, r["models"] < len(MODELS)))
    html = render(rows, d, datetime.now(PARIS).strftime("%d/%m/%Y %H:%M"))
    Path(a.out).write_text(html, encoding="utf-8")
    manquants = [n for n, r, partial in rows if partial]
    print(f"OK -> {a.out}" + (f" | un seul modèle pour : {', '.join(manquants)}" if manquants else ""))

if __name__ == "__main__":
    main()
