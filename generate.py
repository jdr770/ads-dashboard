#!/usr/bin/env python3
"""Dashboard Leadfy Ads v2 — multi-pages (Global / España / Belgique), recos pilotées par MANDATS.
Pull Meta API -> mandats -> moteur de recos numérotées -> HTML chiffré par page."""
import base64, hashlib, json, os, re, time, urllib.request, urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

FB_TOKEN = os.environ["FB_TOKEN"]
PASSWORD = os.environ["DASH_PASSWORD"]
GRAPH = "https://graph.facebook.com/v25.0/"

CFG = json.load(open("config.json"))
MANDATS = json.load(open("mandats.json"))
NOW = datetime.now(timezone.utc)
PARIS = NOW.astimezone(ZoneInfo("Europe/Paris"))


def api(path, params=None):
    params = dict(params or {})
    params["access_token"] = FB_TOKEN
    url = GRAPH + path + "?" + urllib.parse.urlencode(params)
    for _ in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode())
            except Exception:
                err = {}
            if err.get("error", {}).get("code") in (4, 17, 2, 613):
                time.sleep(70)
                continue
            return err
        except Exception:
            time.sleep(15)
    return {}


def leads_of(actions):
    for a in actions or []:
        if a.get("action_type") == "lead":
            return int(a.get("value", 0))
    return 0


def in_learning(campaign_name):
    for l in CFG["launches"]:
        t0 = datetime.fromisoformat(l["at"].replace("Z", "+00:00"))
        if (NOW - t0).total_seconds() < 72 * 3600:
            for pat in l.get("campaign_match", []):
                if pat.lower() in campaign_name.lower():
                    return True
    return False


def fetch_account(acc):
    out = {"label": acc["label"], "id": acc["id"], "market": acc.get("market", "be"), "campaigns": [], "issues": []}
    info = api(acc["id"], {"fields": "name,account_status"})
    out["account_status"] = info.get("account_status", 0)
    if out["account_status"] != 1:
        if not acc.get("watch_restriction"):
            out["issues"].append(("red", f"⚠️ {acc['label']} : statut compte {out['account_status']} — vérifier"))
        return out
    if acc.get("watch_restriction"):
        out["issues"].append(("green", f"🎉 {acc['label']} : compte repassé ACTIF — restriction levée !"))
    camps = api(acc["id"] + "/campaigns", {
        "fields": "name,effective_status,daily_budget",
        "filtering": json.dumps([{"field": "campaign.effective_status", "operator": "IN", "value": ["ACTIVE"]}]),
        "limit": 30}).get("data", [])
    time.sleep(2)
    ins_today = {i.get("campaign_name"): i for i in api(acc["id"] + "/insights", {
        "level": "campaign", "fields": "campaign_name,spend,actions",
        "date_preset": "today", "limit": 30}).get("data", [])}
    time.sleep(2)
    ins_7d = {i.get("campaign_name"): i for i in api(acc["id"] + "/insights", {
        "level": "campaign", "fields": "campaign_name,spend,actions,frequency,ctr",
        "date_preset": "last_7d", "limit": 30}).get("data", [])}
    time.sleep(2)
    ins_3d = {i.get("campaign_name"): i for i in api(acc["id"] + "/insights", {
        "level": "campaign", "fields": "campaign_name,spend,actions",
        "date_preset": "last_3d", "limit": 30}).get("data", [])}
    time.sleep(2)
    bad = [a for a in api(acc["id"] + "/ads", {
        "fields": "name,effective_status,campaign{effective_status}",
        "filtering": json.dumps([{"field": "ad.effective_status", "operator": "IN",
                                  "value": ["DISAPPROVED", "WITH_ISSUES"]}]),
        "limit": 50}).get("data", [])
        if (a.get("campaign") or {}).get("effective_status") == "ACTIVE"]
    if len(bad) > 4:
        out["issues"].append(("red", f"{len(bad)} ads en anomalie sur {acc['label']} — à inspecter"))
    else:
        for ad in bad:
            out["issues"].append(("red", f"Ad refusée : {ad['name']} ({acc['label']}) — {ad['effective_status']}"))
    time.sleep(2)
    for c in camps:
        t, w, d3 = ins_today.get(c["name"], {}), ins_7d.get(c["name"], {}), ins_3d.get(c["name"], {})
        st, sw, s3 = (float(x.get("spend", 0) or 0) for x in (t, w, d3))
        lt, lw, l3 = leads_of(t.get("actions")), leads_of(w.get("actions")), leads_of(d3.get("actions"))
        target = acc.get("target_cpl", 10.0)
        for pat, val in (acc.get("campaign_targets") or {}).items():
            if pat.lower() in c["name"].lower():
                target = val
        out["campaigns"].append({
            "name": c["name"], "budget": int(c.get("daily_budget", 0) or 0) / 100,
            "spend_t": st, "leads_t": lt, "cpl_t": st / lt if lt else None,
            "spend_w": sw, "leads_w": lw, "cpl_w": sw / lw if lw else None,
            "spend_3": s3, "leads_3": l3, "cpl_3": s3 / l3 if l3 else None,
            "freq": float(w.get("frequency", 0) or 0), "target": target,
            "learning": in_learning(c["name"])})
    return out


def fetch_es_daily(acc_id, days=10):
    since = datetime.fromtimestamp(NOW.timestamp() - days * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
    rows = api(acc_id + "/insights", {
        "fields": "spend,actions", "time_increment": "1",
        "time_range": json.dumps({"since": since, "until": NOW.strftime("%Y-%m-%d")}),
        "limit": str(days + 2)}).get("data", [])
    time.sleep(2)
    return [{"d": r.get("date_start", ""), "spend": float(r.get("spend", 0) or 0),
             "leads": leads_of(r.get("actions"))} for r in rows]


# ---------- moteur de recos (mandats × état live × doctrines) ----------

def scale_state_update(es_campaigns):
    """Suit la date du dernier changement de budget par campagne ES (détecte aussi les changements manuels)."""
    f = "scale_state.json"
    st = json.load(open(f)) if os.path.exists(f) else {}
    today = NOW.strftime("%Y-%m-%d")
    for c in es_campaigns:
        k = c["name"]
        prev = st.get(k)
        if not prev or abs(prev.get("budget", 0) - c["budget"]) > 0.5:
            st[k] = {"budget": c["budget"], "since": today}
    st = {k: v for k, v in st.items() if k in [c["name"] for c in es_campaigns] or True}
    json.dump(st, open(f, "w"), indent=1)
    return st


def build_recos(accounts, es_daily):
    """Retourne (recos_global, recos_es, recos_be) — listes de dicts {lvl, txt, tag}."""
    R = {"es": [], "be": [], "glob": []}
    m_es, m_be = MANDATS["ES"], MANDATS["BE_ISO"]
    es_acc = next((a for a in accounts if a["id"] == m_es["account"]), None)
    palier_days = m_es.get("palier_days", 3)

    for acc in accounts:
        for lvl, msg in acc["issues"]:
            R["glob"].append({"lvl": lvl, "txt": msg, "tag": "COMPTE"})

    # --- ESPAGNE : mode SCALE ×5 ---
    if es_acc:
        st = scale_state_update(es_acc["campaigns"])
        today = NOW.strftime("%Y-%m-%d")
        for c in sorted(es_acc["campaigns"], key=lambda x: -x["spend_w"]):
            since = st.get(c["name"], {}).get("since", today)
            days_stable = (NOW - datetime.fromisoformat(since + "T00:00:00+00:00")).days
            is_test = any(p.lower() in c["name"].lower() for md in ("TER", "LED")
                          for p in MANDATS[md].get("campaign_match", []))
            cpl_ref = c["cpl_3"] if c["leads_3"] >= 8 else c["cpl_w"]
            leads_ref = max(c["leads_3"], c["leads_w"])
            if c["learning"]:
                R["es"].append({"lvl": "blue", "tag": "LEARNING",
                                "txt": f"{c['name']} : en learning — on ne touche pas (lecture à 72h)"})
                continue
            if is_test:
                if cpl_ref and leads_ref >= 10 and cpl_ref > m_es["cpl_max"] * 1.3:
                    R["es"].append({"lvl": "red", "tag": "TEST",
                                    "txt": f"{c['name']} : {cpl_ref:.2f}€ vs {m_es['cpl_max']:.0f}€ max — couper les ads perdantes ou arrêter le test"})
                continue
            if cpl_ref and leads_ref >= 15 and cpl_ref <= m_es["cpl_max"] and days_stable >= palier_days:
                nb = c["budget"] * 1.3
                R["es"].append({"lvl": "green", "tag": "PALIER PRÊT",
                                "txt": f"SCALER {c['name']} : {c['budget']:.0f}€ → {nb:.0f}€ (+30%) — CPL {cpl_ref:.2f}€ ≤ {m_es['cpl_max']:.0f}€, stable depuis {days_stable}j"})
            elif cpl_ref and leads_ref >= 15 and cpl_ref <= m_es["cpl_max"]:
                R["es"].append({"lvl": "blue", "tag": f"PALIER J+{max(palier_days - days_stable, 1)}",
                                "txt": f"{c['name']} : CPL {cpl_ref:.2f}€ OK — prochain palier possible dans {max(palier_days - days_stable, 1)}j (digestion)"})
            elif cpl_ref and leads_ref >= 12 and cpl_ref > m_es["cpl_max"] * 1.3:
                R["es"].append({"lvl": "red", "tag": "FREIN",
                                "txt": f"{c['name']} : {cpl_ref:.2f}€ vs {m_es['cpl_max']:.0f}€ max — pas de palier ; couper les ads faibles ou injecter du stock"})
            if c["freq"] >= 2.0 and not c["learning"]:
                stock = " · ".join(list(m_es["stock_creas"].values())[0][:3])
                R["es"].append({"lvl": "orange", "tag": "FATIGUE",
                                "txt": f"{c['name']} : fréquence {c['freq']:.2f} — préparer l'injection (stock dispo : {stock}…)"})
            if c["spend_t"] == 0 and c["budget"] > 0 and PARIS.hour >= 11:
                R["es"].append({"lvl": "orange", "tag": "DIFFUSION",
                                "txt": f"{c['name']} : 0€ dépensé aujourd'hui — review ou souci de diffusion ?"})
        # trajectoire ×5
        week = [d for d in es_daily[:-1]][-7:]
        avg = sum(d["leads"] for d in week) / max(len(week), 1)
        if avg and avg < m_es["leads_target_daily"]:
            gap = m_es["leads_target_daily"] / max(avg, 1)
            R["es"].append({"lvl": "blue", "tag": "CAP ×5",
                            "txt": f"Rythme actuel {avg:.0f} leads/j — objectif {m_es['leads_target_daily']} (×{gap:.1f} restant). Carburant : paliers + rounds labo tous les {palier_days}-4j."})
        spend_today = sum(c["spend_t"] for c in es_acc["campaigns"])
        if spend_today > m_es["dsl_daily"] * 0.85:
            R["es"].append({"lvl": "blue", "tag": "PLAFOND",
                            "txt": f"Dépense du jour {spend_today:.0f}€ proche du plafond DSL ({m_es['dsl_daily']}€) — bon signe : dépenser au plafond le fait monter."})

    # --- BELGIQUE : mode VOLUME STABLE ---
    for acc in accounts:
        if acc["market"] != "be":
            continue
        for c in acc["campaigns"]:
            cpl_ref = c["cpl_3"] if c["leads_3"] >= 8 else c["cpl_w"]
            if c["learning"]:
                R["be"].append({"lvl": "blue", "tag": "LEARNING", "txt": f"{c['name']} : en learning — ne pas toucher"})
                continue
            if cpl_ref and max(c["leads_3"], c["leads_w"]) >= 10:
                if cpl_ref > c["target"] * 2:
                    R["be"].append({"lvl": "red", "tag": "CPL ×2",
                                    "txt": f"{c['name']} ({acc['label']}) : {cpl_ref:.2f}€ vs cible {c['target']:.0f}€"})
                elif cpl_ref > c["target"] * 1.5:
                    R["be"].append({"lvl": "orange", "tag": "CPL",
                                    "txt": f"{c['name']} : {cpl_ref:.2f}€ vs cible {c['target']:.0f}€ — surveiller"})
            if c["freq"] >= 2.2:
                R["be"].append({"lvl": "orange", "tag": "FATIGUE",
                                "txt": f"{c['name']} : fréquence {c['freq']:.2f} — préparer la relève (stock : cartoon 5 vidéos, Script_1/2 jamais testées)"})
            if c["spend_t"] == 0 and c["budget"] > 0 and PARIS.hour >= 11:
                R["be"].append({"lvl": "orange", "tag": "DIFFUSION",
                                "txt": f"{c['name']} : 0€ aujourd'hui — vérifier"})

    # lectures 72h
    for l in CFG["launches"]:
        t0 = datetime.fromisoformat(l["at"].replace("Z", "+00:00"))
        h = (NOW - t0).total_seconds() / 3600
        dest = "es" if l.get("market") == "es" else "be"
        if 0 <= h < 72:
            R[dest].append({"lvl": "blue", "tag": "72H",
                            "txt": f"{l['name']} : lecture possible dans {72 - h:.0f}h — rien toucher d'ici là"})
        elif 72 <= h < 120:
            R[dest].append({"lvl": "green", "tag": "LECTURE",
                            "txt": f"{l['name']} : 72h passées — lecture et arbitrages possibles (dis « fais le point »)"})

    # numérotation globale (priorité rouge > vert > orange > bleu)
    order = {"red": 0, "green": 1, "orange": 2, "blue": 3}
    n = 1
    for key in ("glob", "es", "be"):
        R[key].sort(key=lambda r: order.get(r["lvl"], 9))
        for r in R[key]:
            r["id"] = f"R{n}"
            n += 1
    return R


def check_token():
    out = []
    me = api("me", {"fields": "id"})
    if me.get("error"):
        out.append({"lvl": "red", "tag": "TOKEN", "id": "R0",
                    "txt": "🔑 TOKEN META INVALIDE — dis à Claude « répare le token dashboard »"})
    return out


# ---------- rendu ----------

def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def pill(cpl, target):
    if cpl is None:
        return '<span class="muted">—</span>'
    cls = "good" if cpl <= target else ("warn" if cpl <= target * 1.5 else "bad")
    return f'<span class="pill {cls}">{cpl:.2f}€</span>'


def reco_cards(recos):
    if not recos:
        return '<div class="empty">Rien à signaler — la machine tourne dans le mandat.</div>'
    h = ""
    for r in recos:
        h += (f'<div class="reco {r["lvl"]}"><div class="rid">{r["id"]}</div>'
              f'<div class="rbody"><span class="rtag">{esc(r["tag"])}</span>'
              f'<span class="rtxt">{esc(r["txt"])}</span></div></div>')
    h += '<div class="gohint">Pour exécuter : dis à Claude « GO R3 » (ou « GO R1 R4 »)</div>'
    return h


def spark(daily, key, color):
    """Mini bar chart SVG 10 jours (marques fines, bouts arrondis, libellés premier/dernier)."""
    pts = daily[-10:]
    if not pts:
        return ""
    mx = max((p[key] for p in pts), default=0) or 1
    W, H, bw = 300, 64, 22
    bars, labels = "", ""
    for i, p in enumerate(pts):
        v = p[key]
        bh = max(round(v / mx * 38), 2)
        x = 8 + i * (bw + 7)
        y = 54 - bh
        op = "1" if i == len(pts) - 1 else "0.55"
        bars += f'<rect x="{x}" y="{y}" width="{bw}" height="{bh}" rx="4" fill="{color}" opacity="{op}"><title>{p["d"][5:]} · {v:.0f}</title></rect>'
        if i in (0, len(pts) - 1):
            lab = f"{v:.0f}"
            labels += f'<text x="{x + bw/2}" y="{max(y - 5, 11)}" text-anchor="middle" class="sv">{lab}</text>'
        if i in (0, len(pts) - 1):
            labels += f'<text x="{x + bw/2}" y="{H - 1}" text-anchor="middle" class="sd">{p["d"][8:]}</text>'
    return f'<svg viewBox="0 0 {W} {H}" class="spark" preserveAspectRatio="xMinYMid meet">{bars}{labels}</svg>'


def camp_rows(accounts, market=None, only_acc=None):
    rows = ""
    for acc in accounts:
        if only_acc and acc["id"] != only_acc:
            continue
        if market and not only_acc and acc["market"] != market:
            continue
        if not acc["campaigns"]:
            continue
        a_s = sum(c["spend_t"] for c in acc["campaigns"])
        a_l = sum(c["leads_t"] for c in acc["campaigns"])
        rows += (f'<div class="acct"><span>{esc(acc["label"])}</span>'
                 f'<span class="asub">{a_s:.0f}€ · {a_l} leads auj.</span></div>')
        for c in sorted(acc["campaigns"], key=lambda x: -x["spend_w"]):
            lrn = ' <span class="chip blue">learning</span>' if c["learning"] else ""
            rows += (f'<div class="crow"><div class="cl1"><span class="cn">{esc(c["name"])}{lrn}</span>'
                     f'{pill(c["cpl_t"], c["target"])}</div>'
                     f'<div class="cl2"><span>bud {c["budget"]:.0f}€</span><span>auj {c["spend_t"]:.0f}€ · {c["leads_t"]}l</span>'
                     f'<span>3j {("%.2f€" % c["cpl_3"]) if c["cpl_3"] else "—"}</span>'
                     f'<span>7j {("%.2f€" % c["cpl_w"]) if c["cpl_w"] else "—"}</span>'
                     f'<span>fq {c["freq"]:.1f}</span></div></div>')
    return rows or '<div class="empty">Aucune campagne active.</div>'


def topnav(active):
    t = [("index.html", "Global"), ("es.html", "🇪🇸 España"), ("be.html", "🇧🇪 Belgique")]
    return '<div class="tabs">' + "".join(
        f'<a href="{h}" class="{"on" if h.startswith(active) else ""}">{lab}</a>' for h, lab in t) + "</div>"


def page_global(accounts, R, google_html, es_daily):
    m_es = MANDATS["ES"]
    spend = {"es": 0.0, "be": 0.0}
    leads = {"es": 0, "be": 0}
    for a in accounts:
        for c in a["campaigns"]:
            spend[a["market"]] = spend.get(a["market"], 0) + c["spend_t"]
            leads[a["market"]] = leads.get(a["market"], 0) + c["leads_t"]
    week = es_daily[-8:-1]
    avg = sum(d["leads"] for d in week) / max(len(week), 1)
    pct = min(avg / m_es["leads_target_daily"] * 100, 100)
    cards = ""
    for mk, label, href in (("es", "🇪🇸 España — SCALE ×5", "es.html"), ("be", "🇧🇪 Belgique — volume stable", "be.html")):
        cpl = f"{spend[mk]/leads[mk]:.2f}€" if leads[mk] else "—"
        nrec = len(R[mk])
        cards += (f'<a class="mcard" href="{href}"><div class="mhead">{label}</div>'
                  f'<div class="mkpis"><div><b>{spend[mk]:.0f}€</b><span>dépense auj.</span></div>'
                  f'<div><b>{leads[mk]}</b><span>leads auj.</span></div>'
                  f'<div><b>{cpl}</b><span>CPL</span></div>'
                  f'<div><b>{nrec}</b><span>recos</span></div></div>'
                  + (f'<div class="mprog"><div class="mbar"><i style="width:{pct:.0f}%"></i></div>'
                     f'<span>{avg:.0f}/{m_es["leads_target_daily"]} leads/j — cap ×5</span></div>' if mk == "es" else "")
                  + "</a>")
    glob = reco_cards(R["glob"]) if R["glob"] else '<div class="empty">Aucune alerte compte.</div>'
    return (f'<section><h2><span class="tick"></span>Marchés</h2><div class="mgrid">{cards}</div></section>'
            f'<section><h2><span class="tick"></span>Alertes comptes</h2>{glob}</section>'
            f'{google_html}')


def page_es(accounts, R, es_daily):
    m = MANDATS["ES"]
    acc_id = m["account"]
    es_acc = next((a for a in accounts if a["id"] == acc_id), None)
    spend_t = sum(c["spend_t"] for c in es_acc["campaigns"]) if es_acc else 0
    leads_t = sum(c["leads_t"] for c in es_acc["campaigns"]) if es_acc else 0
    cpl_t = f"{spend_t/leads_t:.2f}€" if leads_t else "—"
    week = es_daily[-8:-1]
    avg = sum(d["leads"] for d in week) / max(len(week), 1)
    pct = min(avg / m["leads_target_daily"] * 100, 100)
    dsl_pct = min(spend_t / m["dsl_daily"] * 100, 100)
    hero = (f'<div class="hero"><div class="hgrid">'
            f'<div class="hk"><div class="hv">{leads_t}</div><div class="hl">leads aujourd\'hui</div>{spark(es_daily, "leads", "#5EEAD4")}</div>'
            f'<div class="hk"><div class="hv">{spend_t:.0f}<span class="u">€</span></div><div class="hl">dépense aujourd\'hui</div>{spark(es_daily, "spend", "#60A5FA")}</div>'
            f'<div class="hk"><div class="hv">{cpl_t}</div><div class="hl">CPL du jour · max mandat {m["cpl_max"]:.0f}€</div></div></div>'
            f'<div class="obj"><div class="ol"><b>CAP ×5</b> — {avg:.0f} / {m["leads_target_daily"]} leads/j (moyenne 7j)</div>'
            f'<div class="obar"><i style="width:{pct:.1f}%"></i></div></div>'
            f'<div class="obj"><div class="ol"><b>Plafond DSL</b> — {spend_t:.0f} / {m["dsl_daily"]}€ du jour</div>'
            f'<div class="obar dsl"><i style="width:{dsl_pct:.1f}%"></i></div>'
            f'<div class="onote">Dépenser au plafond fait monter le plafond.</div></div>'
            f'<div class="mandat">📜 {esc(m["directive"])}</div></div>')
    stock = ""
    for cat, items in m["stock_creas"].items():
        chips = "".join(f'<span class="chip">{esc(i)}</span>' for i in items)
        stock += f'<div class="scat"><div class="scl">{esc(cat)} <b>{len(items)}</b></div><div class="chips">{chips}</div></div>'
    inter = "".join(f'<span class="chip red">{esc(i)}</span>' for i in m.get("interdites", []))
    return (hero
            + f'<section><h2><span class="tick"></span>Recos du mandat</h2>{reco_cards(R["es"])}</section>'
            + f'<section><h2><span class="tick"></span>Campagnes</h2><div class="clist">{camp_rows(accounts, only_acc=acc_id)}</div></section>'
            + f'<section><h2><span class="tick"></span>Stock créas (le carburant du ×5)</h2>{stock}'
            + (f'<div class="scat"><div class="scl">⛔ Interdites</div><div class="chips">{inter}</div></div>' if inter else "")
            + "</section>")


def page_be(accounts, R, google_html):
    m = MANDATS["BE_ISO"]
    be_accs = [a for a in accounts if a["market"] == "be"]
    spend_t = sum(c["spend_t"] for a in be_accs for c in a["campaigns"])
    leads_t = sum(c["leads_t"] for a in be_accs for c in a["campaigns"])
    cpl_t = f"{spend_t/leads_t:.2f}€" if leads_t else "—"
    hero = (f'<div class="hero"><div class="hgrid">'
            f'<div class="hk"><div class="hv">{leads_t}</div><div class="hl">leads aujourd\'hui</div></div>'
            f'<div class="hk"><div class="hv">{spend_t:.0f}<span class="u">€</span></div><div class="hl">dépense aujourd\'hui</div></div>'
            f'<div class="hk"><div class="hv">{cpl_t}</div><div class="hl">CPL · cible {m["cpl_max"]:.0f}€</div></div></div>'
            f'<div class="mandat">📜 {esc(m["directive"])}</div></div>')
    return (hero
            + f'<section><h2><span class="tick"></span>Recos</h2>{reco_cards(R["be"])}</section>'
            + f'<section><h2><span class="tick"></span>Campagnes</h2><div class="clist">{camp_rows(accounts, market="be")}</div></section>'
            + google_html)


def google_section(limit_market=None):
    grows, gmaj = "", ""
    if os.path.exists("google.json"):
        try:
            gd = json.load(open("google.json"))
            gmaj = f' <span class="asub">maj {esc(gd.get("generated_at", "")[5:16].replace("T", " · "))}</span>'
            for ga in gd.get("accounts", []):
                camps = [c for c in ga.get("campaigns", []) if c["spend_7d"] >= 1 or c["spend_today"] >= 1]
                if not camps:
                    continue
                g_s = sum(c["spend_today"] for c in camps)
                g_c = sum(c["conv_today"] for c in camps)
                grows += (f'<div class="acct"><span>{esc(ga["label"])}</span>'
                          f'<span class="asub">{g_s:.0f}€ · {g_c:.0f} conv auj.</span></div>')
                for c in camps:
                    cpl7 = f"{c['cpl_7d']:.2f}€" if c.get("cpl_7d") else "—"
                    cls = "good" if c.get("cpl_7d") and c["cpl_7d"] < 25 else ("warn" if c.get("cpl_7d") and c["cpl_7d"] < 50 else "bad")
                    grows += (f'<div class="crow"><div class="cl1"><span class="cn">{esc(c["name"])}</span>'
                              f'<span class="pill {cls}">{cpl7}</span></div>'
                              f'<div class="cl2"><span>auj {c["spend_today"]:.0f}€ · {c["conv_today"]:.0f} conv</span>'
                              f'<span>7j {c["spend_7d"]:.0f}€ · {c["conv_7d"]:.0f} conv</span></div></div>')
        except Exception:
            grows = ""
    if not grows:
        return ""
    return f'<section><h2><span class="tick"></span>Google Ads{gmaj}</h2><div class="clist">{grows}</div></section>'


CSS = """:root{--bg:#0B0F16;--s1:#131A26;--s2:#0F1520;--s3:#182131;--line:rgba(148,170,200,.10);--line2:rgba(148,170,200,.18);--tx:#EAF0F7;--tx2:#93A2B6;--tx3:#5D6B7F;--ac:#5EEAD4;--good:#4ADE80;--warn:#FBBF24;--bad:#FB7185;--blue:#60A5FA}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--tx);padding-bottom:calc(30px + env(safe-area-inset-bottom));font-size:15px;-webkit-font-smoothing:antialiased}
header{display:flex;justify-content:space-between;align-items:center;padding:14px 16px 10px;position:sticky;top:0;background:rgba(11,15,22,.85);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);z-index:5;border-bottom:1px solid var(--line)}
.brand{font-size:1em;font-weight:400;letter-spacing:.16em;display:flex;align-items:center;gap:9px}.brand b{font-weight:800}
.maj{font-size:.66em;color:var(--tx3);font-variant-numeric:tabular-nums;letter-spacing:.05em}
.tick{display:inline-block;width:4px;height:15px;border-radius:2px;background:var(--ac)}
.tabs{display:flex;gap:6px;padding:10px 16px 4px;position:sticky;top:49px;z-index:4;background:rgba(11,15,22,.85);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px)}
.tabs a{flex:1;text-align:center;padding:9px 4px;border-radius:10px;font-size:.78em;font-weight:700;letter-spacing:.04em;color:var(--tx2);text-decoration:none;background:var(--s2);border:1px solid var(--line)}
.tabs a.on{color:#06251F;background:var(--ac);border-color:var(--ac)}
section{padding:0 16px}
h2{display:flex;align-items:center;gap:9px;font-size:.76em;font-weight:700;letter-spacing:.18em;text-transform:uppercase;color:var(--tx2);margin:26px 0 10px}
.empty{color:var(--tx3);font-size:.85em;padding:8px 2px}
.hero{margin:14px 16px 0;background:linear-gradient(160deg,#14202E,#101724);border:1px solid var(--line2);border-radius:18px;padding:16px}
.hgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px}
.hk .hv{font-size:2em;font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:-.02em;line-height:1.05}
.hk .hv .u{font-size:.55em;font-weight:600;color:var(--tx2)}
.hk .hl{font-size:.66em;color:var(--tx3);letter-spacing:.1em;text-transform:uppercase;font-weight:600;margin-top:4px}
.spark{width:100%;max-width:300px;height:64px;margin-top:8px;display:block}
.spark .sv{fill:var(--tx2);font-size:11px;font-weight:700;font-variant-numeric:tabular-nums}
.spark .sd{fill:var(--tx3);font-size:9px}
.obj{margin-top:16px}
.ol{font-size:.76em;color:var(--tx2);margin-bottom:6px}.ol b{color:var(--tx);letter-spacing:.06em}
.obar{height:10px;background:var(--s2);border:1px solid var(--line);border-radius:99px;overflow:hidden}
.obar i{display:block;height:100%;background:linear-gradient(90deg,#2DD4BF,#5EEAD4);border-radius:99px;transition:width .9s ease}
.obar.dsl i{background:linear-gradient(90deg,#3B82F6,#60A5FA)}
.onote{font-size:.66em;color:var(--tx3);margin-top:4px}
.mandat{margin-top:16px;font-size:.8em;color:var(--tx2);line-height:1.55;background:var(--s2);border:1px solid var(--line);border-left:3px solid var(--ac);border-radius:10px;padding:10px 12px}
.reco{display:flex;gap:11px;align-items:flex-start;background:var(--s1);border:1px solid var(--line);border-radius:12px;padding:11px 12px;margin-bottom:8px}
.reco .rid{flex:none;font-size:.72em;font-weight:800;font-variant-numeric:tabular-nums;color:#06251F;background:var(--ac);border-radius:8px;padding:4px 8px;letter-spacing:.04em}
.reco.red .rid{background:var(--bad);color:#2b070d}.reco.orange .rid{background:var(--warn);color:#2b1d02}.reco.blue .rid{background:var(--blue);color:#081c33}
.reco .rbody{display:flex;flex-direction:column;gap:3px;min-width:0}
.reco .rtag{font-size:.62em;font-weight:800;letter-spacing:.14em;color:var(--tx3);text-transform:uppercase}
.reco .rtxt{font-size:.85em;line-height:1.45}
.reco.red{background:linear-gradient(180deg,rgba(251,113,133,.08),var(--s1))}
.reco.green{background:linear-gradient(180deg,rgba(74,222,128,.07),var(--s1))}
.gohint{font-size:.7em;color:var(--tx3);padding:6px 2px 0;font-style:italic}
.clist{background:var(--s1);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.acct{display:flex;justify-content:space-between;align-items:center;background:var(--s3);padding:9px 13px;font-size:.7em;font-weight:800;letter-spacing:.08em;text-transform:uppercase}
.asub{color:var(--tx3);font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:0;text-transform:none}
.crow{padding:10px 13px;border-top:1px solid var(--line)}
.cl1{display:flex;justify-content:space-between;align-items:center;gap:10px}
.cn{font-size:.85em;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.cl2{display:flex;gap:11px;font-size:.7em;color:var(--tx3);margin-top:4px;font-variant-numeric:tabular-nums;flex-wrap:wrap}
.pill{font-size:.78em;font-weight:800;font-variant-numeric:tabular-nums;padding:3px 9px;border-radius:99px;white-space:nowrap}
.pill.good{background:rgba(74,222,128,.13);color:var(--good)}
.pill.warn{background:rgba(251,191,36,.13);color:var(--warn)}
.pill.bad{background:rgba(251,113,133,.14);color:var(--bad)}
.muted{color:var(--tx3)}
.chip{display:inline-block;font-size:.68em;font-weight:600;font-variant-numeric:tabular-nums;background:var(--s2);border:1px solid var(--line);color:var(--tx2);border-radius:99px;padding:3px 9px;margin:0 5px 6px 0}
.chip.red{color:var(--bad);border-color:rgba(251,113,133,.3)}
.chip.blue{color:var(--blue);border-color:rgba(96,165,250,.3);font-size:.6em;vertical-align:middle;margin-left:6px}
.scat{margin-bottom:12px}.scl{font-size:.72em;color:var(--tx2);font-weight:700;margin-bottom:6px}.scl b{color:var(--ac)}
.mgrid{display:grid;gap:12px}
.mcard{display:block;background:linear-gradient(160deg,#14202E,#101724);border:1px solid var(--line2);border-radius:16px;padding:15px;text-decoration:none;color:var(--tx)}
.mhead{font-size:.92em;font-weight:800;letter-spacing:.02em}
.mkpis{display:flex;gap:18px;margin-top:12px;flex-wrap:wrap}
.mkpis b{display:block;font-size:1.3em;font-weight:800;font-variant-numeric:tabular-nums}
.mkpis span{font-size:.62em;color:var(--tx3);letter-spacing:.1em;text-transform:uppercase;font-weight:600}
.mprog{margin-top:12px}.mbar{height:8px;background:var(--s2);border:1px solid var(--line);border-radius:99px;overflow:hidden}
.mbar i{display:block;height:100%;background:linear-gradient(90deg,#2DD4BF,#5EEAD4)}
.mprog span{font-size:.66em;color:var(--tx3);display:block;margin-top:5px}
#rfr{background:var(--s3);border:1px solid var(--line);color:var(--ac);font-size:1.05em;border-radius:9px;padding:4px 11px;margin-left:10px}
#lock{position:fixed;inset:0;background:var(--bg);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;z-index:10}
#lock input{background:var(--s1);border:1px solid var(--line);border-radius:11px;padding:13px 18px;color:var(--tx);font-size:1em;text-align:center;letter-spacing:.12em;outline:none}
#lock input:focus{border-color:var(--ac)}
#lock button{background:var(--ac);border:0;border-radius:11px;padding:13px 30px;color:#06251F;font-size:.92em;font-weight:800;letter-spacing:.06em}
@media(min-width:760px){.mgrid{grid-template-columns:1fr 1fr}section,.hero,.tabs{max-width:900px;margin-left:auto;margin-right:auto}.hero{margin-top:14px}}
"""


def encrypt(html):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt, 200000, 32)
    ct = AESGCM(key).encrypt(nonce, html.encode(), None)
    return base64.b64encode(salt).decode(), base64.b64encode(nonce).decode(), base64.b64encode(ct).decode()


def write_page(fname, title, body_html, active):
    upd = PARIS.strftime("%d/%m · %H:%M")
    content = (f'<header><div class="brand"><span class="tick"></span>LEADFY <b>ADS</b></div>'
               f'<span class="maj">{upd}</span></header>{topnav(active)}{body_html}')
    build_ts = int(NOW.timestamp() * 1000)
    salt, nonce, ct = encrypt(content)
    page = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="robots" content="noindex,nofollow">
<meta name="theme-color" content="#0B0F16"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>{title}</title><style>{CSS}</style></head><body>
<div id="lock"><div class="brand"><span class="tick"></span>LEADFY <b>ADS</b></div><input id="pw" type="password" placeholder="Code d'accès" autofocus>
<button onclick="unlock()">Entrer</button><div id="err" style="color:#ef4444"></div></div><div id="app"></div>
<script>
const S="{salt}",N="{nonce}",C="{ct}",BT={build_ts};
const b64=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
async function unlock(){{try{{
const pw=document.getElementById('pw').value;
const km=await crypto.subtle.importKey('raw',new TextEncoder().encode(pw),'PBKDF2',false,['deriveKey']);
const key=await crypto.subtle.deriveKey({{name:'PBKDF2',salt:b64(S),iterations:200000,hash:'SHA-256'}},km,{{name:'AES-GCM',length:256}},false,['decrypt']);
const pt=await crypto.subtle.decrypt({{name:'AES-GCM',iv:b64(N)}},key,b64(C));
document.getElementById('app').innerHTML=new TextDecoder().decode(pt);
document.getElementById('lock').remove();localStorage.setItem('k',pw);initRefresh();
}}catch(e){{document.getElementById('err').textContent='Code incorrect';}}}}
function hardReload(){{location.replace(location.pathname+'?t='+Date.now());}}
function initRefresh(){{
const h=document.querySelector('header');
if(h){{const b=document.createElement('button');b.id='rfr';b.textContent='↻';b.onclick=hardReload;h.appendChild(b);}}
document.addEventListener('visibilitychange',()=>{{
if(document.visibilityState==='visible'&&Date.now()-BT>10*60*1000)hardReload();}});
}}
document.getElementById('pw').addEventListener('keydown',e=>{{if(e.key==='Enter')unlock();}});
if(localStorage.getItem('k')){{document.getElementById('pw').value=localStorage.getItem('k');unlock();}}
</script></body></html>"""
    os.makedirs("site", exist_ok=True)
    open("site/" + fname, "w").write(page)


def main():
    accounts = [fetch_account(a) for a in CFG["accounts"]]
    es_daily = fetch_es_daily(MANDATS["ES"]["account"])
    R = build_recos(accounts, es_daily)
    R["glob"] = check_token() + R["glob"]
    g_be = google_section()
    write_page("index.html", "Leadfy Ads", page_global(accounts, R, g_be, es_daily), "index")
    write_page("es.html", "Leadfy · España", page_es(accounts, R, es_daily), "es")
    write_page("be.html", "Leadfy · Belgique", page_be(accounts, R, g_be), "be")
    n = sum(len(v) for v in R.values())
    print(f"OK — {sum(len(a['campaigns']) for a in accounts)} campagnes · {n} recos · 3 pages")


if __name__ == "__main__":
    main()
