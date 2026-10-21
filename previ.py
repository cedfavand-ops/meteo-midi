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
          "precipitation", "cloud_cover", "cloud_cover_low", "cloud_cover_high", "weather_code"]

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
def picto(st, t0, t1):
    """Code meteoblue (1-35, jeu "day") pour la fenêtre [t0 ; t1].
    Heuristique maison (nébulosité totale/basse/haute, cumul de pluie, codes météo des modèles)."""
    hrs = list(frange(t0, t1))

    def avg(v, default):
        xs = [x for t in hrs if (x := st.mean(v, t)) is not None]
        return sum(xs) / len(xs) if xs else default

    cc, cl, ch = avg("cloud_cover", 50), avg("cloud_cover_low", 0), avg("cloud_cover_high", 0)
    p = sum(x for t in hrs[1:] if (x := st.mean("precipitation", t)) is not None)
    temps = [x for t in hrs if (x := st.mean("temperature_2m", t)) is not None]
    codes = [c for t in hrs for c in st.codes(t)]

    def cir(a, b):                       # 0 = rien, 1 = quelques cirrus, 2 = cirrus
        return 0 if ch < a else 1 if ch < b else 2

    thunder = any(c in (95, 96, 99) for c in codes)
    snow_code = any(c in (71, 73, 75, 77, 85, 86) for c in codes)
    rain_code = any(c in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82) for c in codes)
    wet = p >= 0.3
    snow = snow_code or (wet and temps and max(temps) < 1.0)
    shower = any(c in (80, 81, 82, 85, 86) for c in codes) or (wet and cc < 85)
    fog = bool(codes) and sum(c in (45, 48) for c in codes) > len(codes) / 2

    # Précipitations / orages (prioritaires)
    if thunder and wet:
        return 29 if snow else 30 if p >= 8 else 27 if p >= 1.5 else 28
    if thunder:
        return 10 + cir(10, 30)
    if wet and snow:
        if snow_code and rain_code and p >= 0.5: return 35
        if shower: return 32
        return 26 if p >= 6 else 24 if p >= 1 else 34
    if wet:
        if shower: return 31
        return 25 if p >= 10 else 23 if p >= 1.5 else 33
    # Brouillard
    if fog:
        return 16 + cir(5, 15)
    # Ciel
    if cc < 20:
        base = {(0, 0): 1, (0, 1): 2, (0, 2): 3, (1, 0): 4, (1, 1): 5, (1, 2): 6}
        return base[(1 if cl >= 5 else 0, cir(5, 12))]
    if cc < 55:
        return 7 + cir(10, 30)
    if cc < 85:
        return 19 + cir(10, 30)
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
<footer>Échéances en heure locale · Tn : 18 h UTC veille → 18 h UTC · Tx : 6 h → 18 h UTC ·
Pluie et rafale max : 18 h UTC veille → 18 h UTC · Rafales : <span class="w1"><b>≥40</b></span> <span class="w2"><b>≥60</b></span> <span class="w3"><b>≥80</b></span> km/h ·
Pictogrammes © <a style="color:var(--acc)" href="https://content.meteoblue.com/en/research-education/specifications/standards/symbols-and-pictograms">meteoblue</a> · Prévisions brutes de modèles, non corrigées.</footer>
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
