#!/usr/bin/env python3
"""
Météo-Midi : archivage quotidien (étape 1 du mode correctif).

 1. Prévisions J+1 (moyenne AROME HD / ICON-CH1) :
      - pour les 19 communes ;
      - pour l'emplacement exact de chaque station de référence (erreur propre au modèle).
    -> data/forecasts/AAAA-MM-JJ.json
 2. Observations Infoclimat des 3 derniers jours, sur les MÊMES fenêtres que la prévision.
    -> data/obs/AAAA-MM-JJ.json
 3. Affiche dans le journal un comparatif prévu / observé.

La clé d'API est lue dans la variable d'environnement INFOCLIMAT_TOKEN (secret GitHub).
Données Infoclimat : licence CC BY-NC (usage non commercial, mention de la source).
"""
import argparse, json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import previ
from previ import ECHEANCES, PARIS, STATIONS, Station, frange, loc_to_utc, utc_dt

DATA = Path("data")
API = "https://www.infoclimat.fr/opendata/"

# Commune -> station de référence Infoclimat
COMMUNE_REF = {
    "Laudun": "000ZB", "Bagnols/Cèze": "000ZB",
    "Nîmes": "07645", "Orange": "07579", "Avignon": "07563", "Uzès": "000OW",
    "Pont-St-Esprit": "MF30202002", "Pierrelatte": "000FU", "Montjoyer": "STATIC0098",
    "Donzère": "MF26116002", "Lussan": "000ZW", "Barjac": "MF07033001",
    "Alès": "MF30101001", "Sabran": "STATIC0451",
}
REF_IDS = sorted(set(COMMUNE_REF.values()))


# ------------------------------------------------------------- utilitaires --
def jload(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default

def jdump(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

def r2(x):
    return None if x is None else round(x, 2)

def fnum(x):
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------- API Infoclimat --
def ic_get(ids, start, end, token, retries=3, timeout=60):
    """Appelle l'API OpenData v2. Ne journalise jamais la clé."""
    params = [("version", "2"), ("method", "get"), ("format", "json")]
    params += [("stations[]", i) for i in ids]
    params += [("start", start.isoformat()), ("end", end.isoformat()), ("token", token)]
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": "Meteo-Midi/1.0 (usage non commercial)"})
    for essai in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
            if data.get("status") != "OK":
                raise RuntimeError(f"statut={data.get('status')} erreurs={data.get('errors')}")
            if data.get("errors"):
                print(f"  Infoclimat {ids} : avertissements {data['errors']}", file=sys.stderr)
            return data
        except Exception as e:
            msg = str(e).replace(token, "***")
            print(f"  Infoclimat {ids} : tentative {essai}/{retries} échouée ({type(e).__name__} {msg})", file=sys.stderr)
            if essai == retries:
                return None
            time.sleep(5 * essai)

def ensure_meta(ids, token, meta):
    """Coordonnées/altitude/licence des stations (lues dans l'API, mises en cache)."""
    today = datetime.now(PARIS).date()
    for i in ids:
        if i in meta:
            continue
        data = ic_get([i], today, today, token)
        if not data:
            continue
        for s in data.get("stations", []):
            meta[s["id"]] = {"name": s.get("name"), "latitude": s.get("latitude"),
                             "longitude": s.get("longitude"), "elevation": s.get("elevation"),
                             "type": s.get("type"), "license": (s.get("license") or {}).get("license")}
        time.sleep(1)
    return meta


# ------------------------------------------------------------ prévisions ----
def fc_metrics(st, d):
    """Grandeurs prévues (non arrondies) sur les mêmes fenêtres que le tableau."""
    m = {}
    for label, h, _ in ECHEANCES:
        t = loc_to_utc(d, h)
        m[f"t{label}"] = r2(st.mean("temperature_2m", t))
        g = [x for k in (-1, 0, 1) if (x := st.mean("wind_gusts_10m", t + timedelta(hours=k))) is not None]
        m[f"g{label}"] = r2(max(g)) if g else None
    s, e = utc_dt(d - timedelta(days=1), 18), utc_dt(d, 18)
    temps = [x for t in frange(s, e) if (x := st.mean("temperature_2m", t)) is not None]
    tx = [x for t in frange(utc_dt(d, 6), e) if (x := st.mean("temperature_2m", t)) is not None]
    h1 = timedelta(hours=1)
    rr = [x for t in frange(s + h1, e) if (x := st.mean("precipitation", t)) is not None]
    gs = [x for t in frange(s + h1, e) if (x := st.mean("wind_gusts_10m", t)) is not None]
    m["tn"], m["tx"] = r2(min(temps)) if temps else None, r2(max(tx)) if tx else None
    m["rr"], m["gmax"] = r2(sum(rr)) if rr else None, r2(max(gs)) if gs else None
    m["models"] = len(st.models_used)
    return m

def run_forecast(d, token):
    meta = jload(DATA / "stations.json", {})
    if token:
        meta = ensure_meta(REF_IDS, token, meta)
        jdump(DATA / "stations.json", meta)
    pts = [(f"ST:{i}", meta[i]["latitude"], meta[i]["longitude"]) for i in REF_IDS
           if i in meta and meta[i].get("latitude") is not None]
    locs = list(STATIONS) + pts
    payloads = previ.fetch(d, stations=locs)
    out = {"date": d.isoformat(), "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
           "models": list(previ.MODELS), "communes": {}, "points": {}}
    for (name, _, _), payload in zip(locs, payloads):
        m = fc_metrics(Station(name, payload), d)
        if name.startswith("ST:"):
            out["points"][name[3:]] = m
        else:
            out["communes"][name] = {**m, "station": COMMUNE_REF.get(name)}
    jdump(DATA / "forecasts" / f"{d}.json", out)
    manq = [i for i in REF_IDS if i not in out["points"]]
    print(f"Prévisions {d} archivées : {len(out['communes'])} communes, {len(out['points'])} points station"
          + (f" (points manquants : {', '.join(manq)})" if manq else ""))


# ----------------------------------------------------------- observations ---
def parse_rows(rows):
    out = []
    for r in rows:
        try:
            out.append((datetime.strptime(r["dh_utc"], "%Y-%m-%d %H:%M:%S"), r))
        except Exception:
            continue
    out.sort(key=lambda x: x[0])
    return out

def nearest(rows, key, t, tol_min=20):
    best = None
    for ts, r in rows:
        v = fnum(r.get(key))
        if v is None:
            continue
        dt = abs((ts - t).total_seconds()) / 60
        if dt <= tol_min and (best is None or dt < best[0]):
            best = (dt, v)
    return None if best is None else best[1]

def obs_metrics(rows, d):
    """Mêmes définitions que la prévision : Tn 18hUTC(J-1)->18hUTC(J), Tx 6->18hUTC,
    pluie et rafale max 18hUTC->18hUTC, T et rafales aux échéances locales."""
    s, e = utc_dt(d - timedelta(days=1), 18), utc_dt(d, 18)
    win = [(t, r) for t, r in rows if s <= t <= e]
    m = {}
    for label, h, _ in ECHEANCES:
        t = loc_to_utc(d, h)
        m[f"t{label}"] = nearest(rows, "temperature", t)
        g = [x for ts, r in rows if abs((ts - t).total_seconds()) <= 3600
             for k in ("vent_rafales", "vent_rafales_10min") if (x := fnum(r.get(k))) is not None]
        m[f"g{label}"] = max(g) if g else None
    tn = [x for t, r in win for k in ("temperature", "temperature_min") if (x := fnum(r.get(k))) is not None]
    tx = [x for t, r in win if t >= utc_dt(d, 6)
          for k in ("temperature", "temperature_max") if (x := fnum(r.get(k))) is not None]
    gm = [x for t, r in win for k in ("vent_rafales", "vent_rafales_10min") if (x := fnum(r.get(k))) is not None]
    m["tn"], m["tx"] = (min(tn) if tn else None), (max(tx) if tx else None)
    m["gmax"] = max(gm) if gm else None
    hrs = [x for t, r in win if t.minute == 0 and t > s if (x := fnum(r.get("pluie_1h"))) is not None]
    rr = None
    if len(hrs) >= 20:                       # au moins 20 h sur 24 renseignées
        rr = sum(hrs)
    else:                                    # repli : cumul 24 h relevé à 18h UTC
        r24 = [fnum(r.get("pluie_24h")) for t, r in rows if t == e]
        rr = r24[0] if r24 else None
    m["rr"] = r2(rr)
    m = {k: (r2(v) if isinstance(v, float) else v) for k, v in m.items()}
    m["n"] = len(win)
    return m

def run_obs(today, token, lookback=3):
    for k in range(1, lookback + 1):
        d = today - timedelta(days=k)
        if not (DATA / "forecasts" / f"{d}.json").exists():
            continue
        ofile = DATA / "obs" / f"{d}.json"
        obs = jload(ofile, {})
        todo = [i for i in REF_IDS
                if not any(v is not None for key, v in (obs.get(i) or {}).items() if key != "n")]
        for i in todo:
            data = ic_get([i], d - timedelta(days=1), d + timedelta(days=1), token)
            if data is None:
                continue
            obs[i] = obs_metrics(parse_rows(data.get("hourly", {}).get(i, [])), d)
            time.sleep(1)
        jdump(ofile, obs)
        compare(d)

def compare(d):
    f, o = jload(DATA / "forecasts" / f"{d}.json", {}), jload(DATA / "obs" / f"{d}.json", {})
    meta = jload(DATA / "stations.json", {})
    print(f"Comparatif {d} (prévu / observé) :")
    for sid, om in sorted(o.items()):
        fm = f.get("points", {}).get(sid)
        if not fm:
            continue
        def fmt(key):
            a, b = fm.get(key), om.get(key)
            return f"{key} {a:.1f}/{b:.1f}" if a is not None and b is not None else f"{key} –"
        nom = (meta.get(sid) or {}).get("name") or sid
        print(f"  {nom[:26]:26s} " + "  ".join(fmt(k) for k in ("tn", "tx", "gmax", "rr")))


# ------------------------------------------------------------------ main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="jour prévu AAAA-MM-JJ (défaut : demain)")
    a = ap.parse_args()
    token = os.environ.get("INFOCLIMAT_TOKEN", "").strip()
    today = datetime.now(PARIS).date()
    d = datetime.strptime(a.date, "%Y-%m-%d").date() if a.date else today + timedelta(days=1)
    run_forecast(d, token)
    if token:
        run_obs(today, token)
    else:
        print("INFOCLIMAT_TOKEN absent : observations ignorées.")

if __name__ == "__main__":
    main()
