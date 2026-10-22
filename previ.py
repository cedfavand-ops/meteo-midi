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
import argparse, json, math, random, sys, time, urllib.parse, urllib.request
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
def fetch(d, stations=None, batch_size=5, retries=4, timeout=90):
    """Interroge Open-Meteo par petits lots de communes, sur 2 jours seulement
    (18h UTC de la veille -> fin du jour d), avec nouvelles tentatives en cas d'échec."""
    start, end = d - timedelta(days=1), d
    points = stations or STATIONS
    payloads = []
    for i in range(0, len(points), batch_size):
        lot = points[i:i + batch_size]
        params = {
            "latitude": ",".join(str(s[1]) for s in lot),
            "longitude": ",".join(str(s[2]) for s in lot),
            "hourly": ",".join(HOURLY),
            "models": ",".join(MODELS),
            "timezone": "UTC",
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "wind_speed_unit": "kmh",
        }
        url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Meteo-Midi/1.0 (GitHub Actions)"})
        for essai in range(1, retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.load(r)
                break
            except Exception as e:
                print(f"Lot {i // batch_size + 1} : tentative {essai}/{retries} échouée ({e})", file=sys.stderr)
                if essai == retries:
                    raise
                time.sleep(5 * essai ** 2)          # 5 s, 20 s, 45 s
        payloads.extend(data if isinstance(data, list) else [data])
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
            "gust": rnd(gmax), "dir": st.direction(t),
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
    res["gmax"] = rnd(max(gs)) if gs else None
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
               f"({liste(wet_st)}), surtout {MOMENTS[i_max]}, jusqu'à {str(round(rr_max, 1)).replace('.', ',')} mm "
               f"à {rr_max_n} sur 24 h.")
    elif rr_max >= 0.2:
        txt = (f"quelques gouttes possibles localement (max {str(round(rr_max, 1)).replace('.', ',')} mm "
               f"à {rr_max_n}).")
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

# ------------------------------------------------------------ CARTE -------
TABS = ('<nav class="tabs"><button data-v="tableau" class="on">Tableau</button>'
        '<button data-v="carte">Carte</button></nav>')

MAP_TEMPLATE = r"""<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
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
.mk{width:100%;height:100%;box-sizing:border-box;display:flex;flex-direction:column;align-items:center;justify-content:center;
  background:rgba(9,25,52,.92);border:1px solid #2f5aa0;border-radius:10px;color:#e8f0ff;line-height:1.15;
  text-align:center;position:relative;box-shadow:0 2px 8px rgba(0,0,0,.55);padding:2px 3px;overflow:visible}
.mk::after{content:"";position:absolute;left:50%;bottom:-7px;margin-left:-6px;border:6px solid transparent;
  border-bottom:0;border-top:7px solid #2f5aa0}
.mk .mn{font-size:10px;color:#9db8e6;max-width:66px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mk .mi{height:32px;display:flex;align-items:center}
.mk .pic{height:30px}.mk .emo{font-size:24px}
.mk .mt b{font-size:19px;font-weight:700}.mk .mt.tx b{font-size:20px;font-weight:800;color:#ff1f1f}
.mk .mt.tn b{font-size:14px;font-weight:700;color:#4dc3ff}
.mk .mr{font-size:11px;color:#9ad0ff}
.mk .mg{font-size:10px;color:#cfe0ff;white-space:nowrap}.mk .mg b{font-size:12px}
.mk .w1 b{color:#ffe082}.mk .w2 b{color:#ffab40}.mk .w3 b{color:#ff6e6e}
#map.fb{display:flex;justify-content:center;
  background:radial-gradient(circle at 50% 40%,#12305f,#0a1c3b 75%)}
.fbx{height:100%;position:relative}
.fbm{position:absolute;transform:translate(-50%,-100%);width:72px;z-index:2}
.fbl{position:absolute;left:0;top:0;pointer-events:none}
.fbm .mk{height:auto;padding:3px}
.fbnote{position:absolute;left:10px;bottom:6px;font-size:11px;color:var(--mut)}
.maphelp{color:var(--mut);font-size:12px;margin-top:6px}
</style>
<section id="vue-carte" hidden>
  <div class="mapbar">
    <button data-m="0" class="on">8 h</button><button data-m="1">12 h</button>
    <button data-m="2">16 h</button><button data-m="3">Synthèse 24 h</button>
  </div>
  <div id="map"></div>
  <div class="maphelp">Molette ou doigts pour zoomer. Synthèse : Tx en rouge, Tn en bleu, cumul de pluie, rafale max et pictogramme de midi.</div>
</section>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
(function(){
  var D = __DATA__;
  var mode = 0, map = null, inited = false, mk = [], lines = [], FB = null;
  var SZ = [80, 80, 80, 122], W = 72;
  function $(s){return document.querySelector(s);}
  function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');}
  function gcls(g){return g==null?'':(g>=80?'w3':g>=60?'w2':g>=40?'w1':'');}
  function T(v){return v==null?'–':'<b>'+v+'<span class="u">°C</span></b>';}
  function card(c){
    var name = '<div class="mn">'+esc(c.name)+'</div>';
    if (mode < 3) {
      var e = c.ech[mode];
      return '<div class="mk">'+name+'<div class="mi">'+(D.icons[e.p]||'')+'</div><div class="mt">'+T(e.t)+'</div>'
        + '<div class="mg '+gcls(e.g)+'">'+(e.g==null?'':'<b>'+e.g+'</b> '+e.d)+'</div></div>';
    }
    var rr = (c.rr!=null && c.rr>=0.2) ? '<div class="mr">'+c.rr.toFixed(1).replace('.',',')+' mm</div>' : '';
    return '<div class="mk">'+name+'<div class="mi">'+(D.icons[c.ech[1].p]||'')+'</div>'
      + '<div class="mt tx">'+T(c.tx)+'</div><div class="mt tn">'+T(c.tn)+'</div>'+rr
      + '<div class="mg '+gcls(c.gmax)+'">'+(c.gmax==null?'':'<b>'+c.gmax+'</b> '+c.gd)+'</div></div>';
  }
  // Écarte les cartes qui se chevauchent. P = positions réelles (px) ; renvoie le bas-centre de chaque carte.
  function declutter(P, w, h, bnd){
    var gap = 4, n = P.length, C = P.map(function(p){return {x:p.x, y:p.y-7};});
    for (var it = 0; it < 400; it++) {
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
        C[i].x += (P[i].x - C[i].x) * 0.02; C[i].y += ((P[i].y-7) - C[i].y) * 0.02;
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
      P = D.communes.map(function(c){return {
        x:(0.1+0.8*(c.lon-FB.x0)/(FB.x1-FB.x0))*Wb, y:(0.1+0.8*(FB.b-c.lat)/(FB.b-FB.a))*Hb};});
      C = declutter(P, W, h, {x0:W/2+2, x1:Wb-W/2-2, y0:h+8, y1:Hb-4});
      var svg = '';
      D.communes.forEach(function(c,i){
        mk[i].style.left = C[i].x+'px'; mk[i].style.top = C[i].y+'px';
        svg += '<line x1="'+C[i].x+'" y1="'+(C[i].y+7)+'" x2="'+P[i].x+'" y2="'+P[i].y+'" stroke="#4fc3f7" stroke-opacity=".7"/>'
             + '<circle cx="'+P[i].x+'" cy="'+P[i].y+'" r="3" fill="#4fc3f7"/>';
      });
      var s = $('.fbl'); s.setAttribute('width', Wb); s.setAttribute('height', Hb); s.innerHTML = svg;
    }
  }
  function init(){
    if (inited) return;
    var el = $('#map');
    if (window.L) {
      map = L.map(el, {minZoom:8, maxZoom:14, zoomSnap:0.25, zoomDelta:0.5});
      L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png',
        {maxZoom:14, subdomains:'abcd', attribution:'© OpenStreetMap · © CARTO'}).addTo(map);
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
      var ar = ((FB.x1-FB.x0)*Math.cos((FB.a+FB.b)/2*Math.PI/180))/(FB.b-FB.a);
      var box = document.createElement('div'); box.className='fbx'; box.style.aspectRatio = String(ar);
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
  document.querySelectorAll('.mapbar button').forEach(function(b){
    b.onclick = function(){
      mode = +b.dataset.m;
      document.querySelectorAll('.mapbar button').forEach(function(x){x.classList.toggle('on', x===b);});
      refresh();
    };
  });
  window.addEventListener('resize', function(){ if (inited && !map) layout(); });
  view(location.hash === '#carte' ? 'carte' : 'tableau');
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
                     "p": e["picto"]} for e in r["ech"]],
            "tn": r["tn"], "tx": r["tx"], "rr": round(r["rr"], 1), "gmax": r["gmax"],
            "gd": compass(r["gmax_dir"]) if r["gmax_dir"] is not None else ""})
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
        rr = f'{r["rr"]:.1f}'.replace(".", ",")
        body.append(
            f'<tr><th class="n">{name}{mark}</th>{cells}'
            f'<td class="x tn">{temp_cell(r["tn"])}</td><td class="x tx">{temp_cell(r["tx"])}</td>'
            f'<td class="x rr">{rr} <small>mm</small></td>'
            f'<td class="x g">{wind_cell(r["gmax"], r["gmax_dir"])}</td></tr>')
    bul = "\n".join(f"<p>{l}</p>" for l in bulletin(rows))
    tabs = TABS
    carte = MAP_TEMPLATE.replace("__DATA__", map_payload(rows))
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
Fond de carte © OpenStreetMap · © CARTO · Pictogrammes © <a style="color:var(--acc)" href="https://content.meteoblue.com/en/research-education/specifications/standards/symbols-and-pictograms">meteoblue</a> · Prévisions brutes de modèles, non corrigées.</footer>
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
