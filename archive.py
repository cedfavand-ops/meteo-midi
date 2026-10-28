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
import argparse, json, os, sys, time, urllib.error, urllib.parse, urllib.request
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
STATUT = {"infos": [], "erreurs": []}
IC_BLOQUE = {"raison": None}      # renseigné dès que les appels sont inutiles : on arrête d'insister
IC_ECHECS = {"n": 0}

def note(msg, erreur=True):
    """Affiche dans le journal et garde une trace pour data/statut.json."""
    print(("  " if erreur else "") + msg, file=sys.stderr if erreur else sys.stdout)
    STATUT["erreurs" if erreur else "infos"].append(msg)

def ic_get(ids, start, end, token, timeout=60):
    """Appelle l'API OpenData v2. Deux écritures de l'adresse sont tentées (crochets de stations[] en clair,
    puis encodés). En cas de refus, le message exact du serveur est journalisé ; la clé n'apparaît jamais."""
    if IC_BLOQUE["raison"]:
        return None
    masque = lambda txt: txt.replace(token, "***") if token else txt
    base = [("version", "2"), ("method", "get"), ("format", "json")]
    base += [("stations[]", i) for i in ids]
    base += [("start", start.isoformat()), ("end", end.isoformat()), ("token", token)]
    variantes = [("crochets en clair", urllib.parse.urlencode(base, safe="[]")),
                 ("crochets encodés", urllib.parse.urlencode(base))]
    for nom, qs in variantes:
        req = urllib.request.Request(API + "?" + qs, headers={"User-Agent": "Meteo-Midi/1.0 (usage non commercial)"})
        for essai in range(1, 4):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.load(r)
                if data.get("status") != "OK":
                    note(masque(f"Infoclimat {ids} [{nom}] : statut={data.get('status')} erreurs={data.get('errors')}"))
                    break                                   # réponse d'erreur de l'API : inutile de répéter
                if data.get("errors"):
                    note(masque(f"Infoclimat {ids} : avertissements {data['errors']}"))
                IC_ECHECS["n"] = 0
                return data
            except urllib.error.HTTPError as e:
                corps = e.read()[:300].decode("utf-8", "replace")
                note(masque(f"Infoclimat {ids} [{nom}] : HTTP {e.code} {e.reason} ; réponse du serveur : {corps!r}"))
                if "wrong ip" in corps.lower():
                    IC_BLOQUE["raison"] = "adresse IP non autorisée pour cette clé (« Wrong IP address »)"
                    note("Clé Infoclimat refusée : « Wrong IP address ». Cette clé n'est acceptée que depuis une adresse IP précise, "
                         "or les serveurs GitHub changent d'adresse à chaque exécution. Les appels suivants sont suspendus "
                         "pour ne pas surcharger Infoclimat.")
                    return None
                if e.code < 500:
                    break                                   # erreur de requête : on passe à l'autre écriture
                time.sleep(5 * essai)
            except Exception as e:
                note(masque(f"Infoclimat {ids} [{nom}] : tentative {essai}/3 échouée ({type(e).__name__} {e})"))
                if essai < 3:
                    time.sleep(5 * essai)
    IC_ECHECS["n"] += 1
    if IC_ECHECS["n"] >= 4:
        IC_BLOQUE["raison"] = "4 échecs consécutifs"
        note("Infoclimat : 4 échecs consécutifs, les appels suivants sont suspendus.")
    return None

def ensure_meta(ids, token, meta):
    """Coordonnées/altitude/licence des stations (lues dans l'API, mises en cache)."""
    today = datetime.now(PARIS).date()
    for i in ids:
        if i in meta:
            continue
        data = ic_get([i], today - timedelta(days=1), today, token)
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
    for label, h, (wa, wb) in ECHEANCES:
        t = loc_to_utc(d, h)
        m[f"t{label}"] = r2(st.mean("temperature_2m", t))
        # picto choisi + nébulosité (totale, basse, moyenne, haute) : sert à régler les seuils
        m[f"p{label}"] = previ.picto(st, loc_to_utc(d, wa), loc_to_utc(d, wb))
        m[f"c{label}"] = [r2(x) for x in previ.sky_stats(st, loc_to_utc(d, wa), loc_to_utc(d, wb))]
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
        if meta:                                             # jamais de fichier vide
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

def run_obs(today, token, lookback=3, exiger_prevision=True):
    """Télécharge les observations des `lookback` derniers jours. En mode normal, seuls les jours dont la prévision
    est archivée sont traités ; en mode autonome (exiger_prevision=False), tous les jours le sont."""
    for k in range(1, lookback + 1):
        if IC_BLOQUE["raison"]:
            note(f"Observations ignorées : {IC_BLOQUE['raison']}.")
            return
        d = today - timedelta(days=k)
        ffile = DATA / "forecasts" / f"{d}.json"
        if exiger_prevision and not ffile.exists():
            continue
        ofile = DATA / "obs" / f"{d}.json"
        obs = jload(ofile, {})
        todo = [i for i in REF_IDS
                if not any(v is not None for key, v in (obs.get(i) or {}).items() if key != "n")]
        for i in todo:
            data = ic_get([i], d - timedelta(days=1), d + timedelta(days=1), token)
            if data is None:
                if IC_BLOQUE["raison"]:
                    break
                continue
            obs[i] = obs_metrics(parse_rows(data.get("hourly", {}).get(i, [])), d)
            time.sleep(1)
        if obs:                                              # jamais de fichier vide
            jdump(ofile, obs)
            note(f"Observations {d} : {len(obs)}/{len(REF_IDS)} stations enregistrées.", erreur=False)
            if ffile.exists():
                compare(d)
        elif not IC_BLOQUE["raison"]:
            note(f"Observations {d} : aucune station récupérée.")

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
def ecrire_statut(d, token):
    obs_dir = DATA / "obs"
    jdump(DATA / "statut.json", {
        "genere_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "jour_prevu": d.isoformat(),
        "cle_infoclimat_presente": bool(token), "stations_connues": len(jload(DATA / "stations.json", {})),
        "fichiers_observations": sorted(f.name for f in obs_dir.glob("*.json")) if obs_dir.exists() else [],
        "infoclimat_bloque": IC_BLOQUE["raison"],
        "infos": STATUT["infos"][-20:], "erreurs": STATUT["erreurs"][-30:]})

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="jour prévu AAAA-MM-JJ (défaut : demain)")
    ap.add_argument("--obs-seulement", action="store_true",
                    help="ne télécharge que les observations Infoclimat (à lancer depuis l'ordinateur dont l'adresse IP est autorisée)")
    ap.add_argument("--jours", type=int, default=7, help="avec --obs-seulement : nombre de jours passés à récupérer (défaut 7)")
    a = ap.parse_args()
    brut = os.environ.get("INFOCLIMAT_TOKEN", "")
    token = brut.strip().strip("\"'").strip()               # tolère espaces, retour à la ligne, guillemets collés
    today = datetime.now(PARIS).date()
    d = datetime.strptime(a.date, "%Y-%m-%d").date() if a.date else today + timedelta(days=1)
    if token:
        note(f"Clé Infoclimat reçue : {len(token)} caractères"
             + (f" ({len(brut) - len(token)} caractère(s) superflu(s) retiré(s))" if len(brut) != len(token) else ""), erreur=False)
    if a.obs_seulement:
        if not token:
            sys.exit("INFOCLIMAT_TOKEN absent : définissez la variable d'environnement avec votre clé avant de lancer.")
        run_obs(today, token, lookback=max(1, a.jours), exiger_prevision=False)
        return
    run_forecast(d, token)
    if token:
        run_obs(today, token)
    else:
        note("INFOCLIMAT_TOKEN absent : observations ignorées.")
    ecrire_statut(d, token)

if __name__ == "__main__":
    main()
