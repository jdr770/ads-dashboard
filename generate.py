#!/usr/bin/env python3
"""Génère le dashboard Leadfy Ads : pull Meta API -> règles -> HTML chiffré -> Telegram."""
import base64, hashlib, json, os, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

FB_TOKEN = os.environ["FB_TOKEN"]
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
PASSWORD = os.environ["DASH_PASSWORD"]
GRAPH = "https://graph.facebook.com/v21.0/"

CFG = json.load(open("config.json"))
NOW = datetime.now(timezone.utc)
PARIS = NOW.astimezone(ZoneInfo("Europe/Paris"))


def api(path, params=None):
    params = dict(params or {})
    params["access_token"] = FB_TOKEN
    url = GRAPH + path + "?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            err = json.loads(e.read().decode())
            code = err.get("error", {}).get("code")
            if code in (4, 17, 2):
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


def fetch_account(acc):
    out = {"label": acc["label"], "id": acc["id"], "group": acc.get("group", "perso"), "campaigns": [], "issues": []}
    info = api(acc["id"], {"fields": "name,account_status"})
    out["account_status"] = info.get("account_status", 0)
    if out["account_status"] != 1:
        if not acc.get("watch_restriction"):
            out["issues"].append(("red", f"⚠️ NOUVELLE restriction : {acc['label']} vient de passer en statut {out['account_status']}"))
    elif acc.get("watch_restriction"):
        out["issues"].append(("green", f"🎉 {acc['label']} : le compte est repassé ACTIF (statut 1) — la restriction est levée !"))
    camps = api(acc["id"] + "/campaigns", {
        "fields": "name,effective_status,daily_budget",
        "filtering": json.dumps([{"field": "campaign.effective_status", "operator": "IN", "value": ["ACTIVE"]}]),
        "limit": 25}).get("data", [])
    time.sleep(2)
    ins_today = {i.get("campaign_name"): i for i in api(acc["id"] + "/insights", {
        "level": "campaign", "fields": "campaign_name,spend,actions",
        "date_preset": "today", "limit": 30}).get("data", [])}
    time.sleep(2)
    ins_7d = {i.get("campaign_name"): i for i in api(acc["id"] + "/insights", {
        "level": "campaign", "fields": "campaign_name,spend,actions,frequency,ctr",
        "date_preset": "last_7d", "limit": 30}).get("data", [])}
    time.sleep(2)
    if out["account_status"] == 1:
        bad_ads = [a for a in api(acc["id"] + "/ads", {
            "fields": "name,effective_status,campaign{effective_status}",
            "filtering": json.dumps([{"field": "ad.effective_status", "operator": "IN",
                                      "value": ["DISAPPROVED", "WITH_ISSUES"]}]),
            "limit": 50}).get("data", [])
            if (a.get("campaign") or {}).get("effective_status") == "ACTIVE"]
        if len(bad_ads) > 4:
            out["issues"].append(("red", f"{len(bad_ads)} ads en anomalie sur {acc['label']} (DISAPPROVED/WITH_ISSUES) — à inspecter"))
        else:
            for ad in bad_ads:
                out["issues"].append(("red", f"Ad {ad['name']} ({acc['label']}) : {ad['effective_status']}"))
    for c in (camps if out["account_status"] == 1 else []):
        t = ins_today.get(c["name"], {})
        w = ins_7d.get(c["name"], {})
        spend_t, spend_w = float(t.get("spend", 0) or 0), float(w.get("spend", 0) or 0)
        leads_t, leads_w = leads_of(t.get("actions")), leads_of(w.get("actions"))
        cpl_t = spend_t / leads_t if leads_t else None
        cpl_w = spend_w / leads_w if leads_w else None
        target = acc.get("target_cpl", 8.0)
        for pat, val in (acc.get("campaign_targets") or {}).items():
            if pat.lower() in c["name"].lower():
                target = val
        freq = float(w.get("frequency", 0) or 0)
        out["campaigns"].append({
            "name": c["name"], "budget": int(c.get("daily_budget", 0) or 0) / 100,
            "spend_t": spend_t, "leads_t": leads_t, "cpl_t": cpl_t,
            "spend_w": spend_w, "leads_w": leads_w, "cpl_w": cpl_w,
            "freq": freq, "ctr": float(w.get("ctr", 0) or 0), "target": target})
    return out


def fetch_test_ads(accounts):
    """Perf par ad depuis le lancement, pour les campagnes des launches."""
    results = {}
    pats = [p for l in CFG["launches"] for p in l.get("campaign_match", [])]
    earliest = min(l["at"] for l in CFG["launches"])[:10]
    for acc in accounts:
        if acc["account_status"] != 1:
            continue
        if not any(any(p.lower() in c["name"].lower() for p in pats) for c in acc["campaigns"]):
            continue
        rows = api(acc["id"] + "/insights", {
            "level": "ad", "fields": "ad_name,campaign_name,spend,actions",
            "time_range": json.dumps({"since": earliest, "until": NOW.strftime("%Y-%m-%d")}),
            "limit": 100}).get("data", [])
        time.sleep(2)
        for r in rows:
            cname = r.get("campaign_name", "")
            if not any(p.lower() in cname.lower() for p in pats):
                continue
            results.setdefault(cname, []).append({
                "ad": r.get("ad_name", "?"), "spend": float(r.get("spend", 0) or 0),
                "leads": leads_of(r.get("actions"))})
    return results


def senior_recos(accounts, test_ads):
    out = []
    # verdicts post-72h par campagne de test
    for l in CFG["launches"]:
        t0 = datetime.fromisoformat(l["at"].replace("Z", "+00:00"))
        if (NOW - t0).total_seconds() < 72 * 3600:
            continue
        for cname, ads in test_ads.items():
            if not any(p.lower() in cname.lower() for p in l.get("campaign_match", [])):
                continue
            # duel Corps A vs B (ads suffixées _A / _B)
            grp = {"A": [0.0, 0], "B": [0.0, 0]}
            for a in ads:
                suf = a["ad"].rstrip().rsplit("_", 1)[-1]
                if suf in grp:
                    grp[suf][0] += a["spend"]; grp[suf][1] += a["leads"]
            if grp["A"][1] + grp["B"][1] >= 10 and grp["A"][0] + grp["B"][0] > 50:
                cpl_a = grp["A"][0] / grp["A"][1] if grp["A"][1] else 9999
                cpl_b = grp["B"][0] / grp["B"][1] if grp["B"][1] else 9999
                win, lose = ("A", "B") if cpl_a <= cpl_b else ("B", "A")
                cw = min(cpl_a, cpl_b); cl = max(cpl_a, cpl_b)
                fl = f"{cl:.2f}€" if cl < 9999 else "aucun lead"
                out.append(("green", f"🏆 {cname} : Corps {win} gagne ({cw:.2f}€ vs {fl}) → lance la vague suivante en corps {win} uniquement"))
            # coupes : ads qui ont dépensé sans convertir ou trop cher
            cuts = []
            tgt = None
            for acc in accounts:
                for c in acc["campaigns"]:
                    if c["name"] == cname:
                        tgt = c["target"]
            for a in sorted(ads, key=lambda x: -x["spend"]):
                if tgt and a["spend"] >= tgt * 2.5 and (a["leads"] == 0 or a["spend"] / a["leads"] > tgt * 2):
                    cpl_txt = f"{a['spend']/a['leads']:.0f}€" if a["leads"] else "0 lead"
                    cuts.append(f"{a['ad']} ({a['spend']:.0f}€, {cpl_txt})")
            if cuts:
                out.append(("red", f"✂️ {cname} : coupe " + " · ".join(cuts[:4])))
    # scaling : campagnes stables largement sous leur cible
    for acc in accounts:
        for c in acc["campaigns"]:
            if in_learning(c["name"]):
                continue
            if c["cpl_w"] and c["leads_w"] >= 30 and c["cpl_w"] < c["target"] * 0.7 and c["budget"] > 0:
                out.append(("green", f"📈 Scaling possible : {c['name']} ({acc['label']}) à {c['cpl_w']:.2f}€ vs cible {c['target']:.0f}€ → +20-30% de budget (palier 3-4 jours, ne pas toucher pendant)"))
    return out


def in_learning(campaign_name):
    for l in CFG["launches"]:
        t0 = datetime.fromisoformat(l["at"].replace("Z", "+00:00"))
        if (NOW - t0).total_seconds() < 72 * 3600:
            for pat in l.get("campaign_match", []):
                if pat.lower() in campaign_name.lower():
                    return True
    return False


def build_recos(accounts):
    recos, alerts = [], []
    R = CFG["rules"]
    for acc in accounts:
        for lvl, msg in acc["issues"]:
            (alerts if lvl in ("red", "green") else recos).append((lvl, msg[:40], msg) if lvl in ("red", "green") else (lvl, msg))
        for c in acc["campaigns"]:
            ref_cpl = c["cpl_t"] if (c["spend_t"] >= R["min_spend_for_alert"] and c["leads_t"]) else c["cpl_w"]
            learning = in_learning(c["name"])
            if ref_cpl and c["spend_w"] >= R["min_spend_for_alert"] and not learning:
                if ref_cpl >= c["target"] * R["cpl_alert_ratio"]:
                    alerts.append(("red", f"cplx2|{c['name']}", f"CPL x2 : {c['name']} ({acc['label']}) à {ref_cpl:.2f}€ vs cible {c['target']:.0f}€"))
                elif ref_cpl >= c["target"] * R["cpl_warn_ratio"]:
                    recos.append(("orange", f"CPL élevé : {c['name']} ({acc['label']}) à {ref_cpl:.2f}€ vs cible {c['target']:.0f}€ — à surveiller, juger à 72h"))
            if c["freq"] >= R["freq_alert"]:
                recos.append(("red", f"Fatigue créa : {c['name']} ({acc['label']}) fréquence 7j = {c['freq']:.2f} — prévoir refresh/vague suivante"))
            elif c["freq"] >= R["freq_warn"]:
                recos.append(("orange", f"Fréquence qui monte : {c['name']} ({acc['label']}) = {c['freq']:.2f}"))
            if c["spend_t"] == 0 and c["budget"] > 0 and PARIS.hour >= 11:
                recos.append(("orange", f"Zéro dépense aujourd'hui : {c['name']} ({acc['label']}) — review en cours ou problème de diffusion ?"))
    for l in CFG["launches"]:
        t0 = datetime.fromisoformat(l["at"].replace("Z", "+00:00"))
        h = (NOW - t0).total_seconds() / 3600
        if 0 <= h < 72:
            recos.append(("blue", f"⏳ {l['name']} : lecture 72h possible dans {72 - h:.0f}h — ne rien toucher d'ici là"))
        elif 72 <= h < 120:
            recos.append(("green", f"✅ {l['name']} : les 72h sont passées — lecture et arbitrage possibles (demande « fais le point »)"))
    return recos, alerts


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_cpl(cpl, target, pill=False):
    if cpl is None:
        return '<span class="muted">—</span>'
    cls = "good" if cpl <= target else ("warn" if cpl <= target * 1.5 else "bad")
    if pill:
        return f'<span class="pill {cls}">{cpl:.2f}€</span>'
    return f'<span class="{cls}">{cpl:.2f}€</span>'


def render(accounts, recos, alerts):
    upd = PARIS.strftime("%d/%m · %H:%M")
    groups = {"perso": "Perso", "certicasa": "Certicasa · géré"}
    kpi_blocks = ""
    for g, glabel in groups.items():
        gs = sum(c["spend_t"] for a in accounts if a.get("group") == g for c in a["campaigns"])
        gl = sum(c["leads_t"] for a in accounts if a.get("group") == g for c in a["campaigns"])
        gc = f"{gs / gl:.2f}€" if gl else "—"
        kpi_blocks += (f'<div class="eyebrow">{glabel}</div><div class="kpis">'
                       f'<div class="kpi"><div class="v">{gs:.0f}<span class="u">€</span></div><div class="l">dépense</div></div>'
                       f'<div class="kpi"><div class="v">{gl}</div><div class="l">leads</div></div>'
                       f'<div class="kpi"><div class="v">{gc}</div><div class="l">CPL</div></div></div>')

    import re as _re
    def card_key(m):
        return _re.sub(r"[\d.,€%]+", "", m)[:80]
    def cards(items):
        return "".join(f'<div class="card {lvl}" data-key="{esc(card_key(m))}"><span class="dot"></span><span class="ctext">{esc(m)}</span></div>' for lvl, m in items)

    important = [(lvl, m) for lvl, _k, m in alerts] + [(l, m) for l, m in recos if l in ("red", "green")]
    veille = [(l, m) for l, m in recos if l not in ("red", "green")]
    imp_html = cards(important) or '<div class="empty">Rien d\'urgent — le système tourne.</div>'
    veille_html = cards(veille) or '<div class="empty">Rien à signaler.</div>'

    rows = ""
    for acc in accounts:
        if not acc["campaigns"]:
            continue
        a_s = sum(c["spend_t"] for c in acc["campaigns"])
        a_l = sum(c["leads_t"] for c in acc["campaigns"])
        rows += (f'<div class="acct"><span>{esc(acc["label"])}</span>'
                 f'<span class="asub">{a_s:.0f}€ · {a_l} leads</span></div>')
        for c in sorted(acc["campaigns"], key=lambda x: -x["spend_w"]):
            rows += (f'<div class="crow"><div class="cl1"><span class="cn">{esc(c["name"])}</span>'
                     f'{fmt_cpl(c["cpl_t"], c["target"], pill=True)}</div>'
                     f'<div class="cl2"><span>{c["spend_t"]:.0f}€</span><span>{c["leads_t"]} leads</span>'
                     f'<span>7j {fmt_cpl(c["cpl_w"], c["target"])}</span><span>fq {c["freq"]:.1f}</span></div></div>')

    prods = ""
    for p in CFG.get("products", []):
        st = lt = sw = lw = 0.0
        for a in accounts:
            for c in a["campaigns"]:
                hit = a["id"] in p.get("accounts", []) or any(
                    pat.lower() in c["name"].lower() for pat in p.get("name_contains", []))
                if hit:
                    st += c["spend_t"]; lt += c["leads_t"]; sw += c["spend_w"]; lw += c["leads_w"]
        cpl_t = f"{st/lt:.2f}€" if lt else "—"
        cpl_w = f"{sw/lw:.2f}€ · {lw:.0f} leads" if lw else "—"
        prods += (f'<div class="pcard"><div class="pl">{esc(p["label"])}</div>'
                  f'<div class="pv">{cpl_t}</div><div class="ps">{lt:.0f} leads auj.</div>'
                  f'<div class="ps dim">7j · {cpl_w}</div></div>')

    backlog = "".join(f'<div class="vcard"><div class="vhead"><b>{esc(b["market"])}</b><span class="vcount">{b["count"]}</span></div><div class="vnote">{esc(b["note"])}</div></div>' for b in CFG["video_backlog"])
    return f"""<header><div class="brand"><span class="tick"></span>LEADFY <b>ADS</b></div><span class="maj">{upd}</span></header>
<div class="wrap">{kpi_blocks}</div>
<section id="important"><h2><span class="tick"></span>Actions</h2>{imp_html}</section>
<section id="campagnes"><h2><span class="tick"></span>Campagnes</h2><div class="clist">{rows}</div></section>
<section id="produits"><h2><span class="tick"></span>CPL par produit</h2><div class="pgrid">{prods}</div></section>
<section id="cerveau"><h2><span class="tick"></span>Veille</h2>{veille_html}</section>
<section id="videos"><h2><span class="tick"></span>Vidéos à lancer</h2>{backlog}</section>
<nav><a href="#important"><i>🚨</i>Actions</a><a href="#campagnes"><i>📈</i>Camp.</a><a href="#produits"><i>💶</i>CPL</a><a href="#cerveau"><i>🧠</i>Veille</a><a href="#videos"><i>🎬</i>Vidéos</a></nav>"""


CSS = """:root{--bg:#0c1118;--s1:#141b26;--s2:#1a2331;--line:rgba(148,170,200,.10);--tx:#e9eef5;--tx2:#93a1b7;--tx3:#62708a;--ac:#5eead4;--good:#4ade80;--warn:#fbbf24;--bad:#fb7185}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--tx);padding-bottom:calc(74px + env(safe-area-inset-bottom))}
header{display:flex;justify-content:space-between;align-items:center;padding:16px;position:sticky;top:0;background:rgba(12,17,24,.82);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);z-index:5;border-bottom:1px solid var(--line)}
.brand{font-size:1.02em;font-weight:400;letter-spacing:.16em;display:flex;align-items:center;gap:9px}.brand b{font-weight:800}
.maj{font-size:.68em;color:var(--tx3);font-variant-numeric:tabular-nums;letter-spacing:.05em}
.tick{display:inline-block;width:4px;height:15px;border-radius:2px;background:var(--ac)}
.wrap{padding:14px 16px 0}
.eyebrow{font-size:.66em;color:var(--tx3);font-weight:700;letter-spacing:.18em;text-transform:uppercase;margin:12px 2px 7px}
.kpis{display:flex;gap:8px}
.kpi{flex:1;background:linear-gradient(180deg,var(--s2),var(--s1));border:1px solid var(--line);border-radius:12px;padding:12px 10px;text-align:center}
.kpi .v{font-size:1.42em;font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:-.01em}.kpi .v .u{font-size:.6em;font-weight:600;color:var(--tx2);margin-left:1px}
.kpi .l{font-size:.62em;color:var(--tx3);margin-top:3px;letter-spacing:.14em;text-transform:uppercase;font-weight:600}
section{padding:0 16px}
h2{display:flex;align-items:center;gap:9px;font-size:.78em;font-weight:700;letter-spacing:.18em;text-transform:uppercase;color:var(--tx2);margin:26px 0 10px;scroll-margin-top:64px}
.card{display:flex;gap:10px;align-items:flex-start;background:var(--s1);border:1px solid var(--line);border-radius:11px;padding:11px 13px;margin-bottom:7px;font-size:.86em;line-height:1.45}
.card[data-key]{cursor:pointer;position:relative;padding-right:54px}
.card .dot{flex:none;width:8px;height:8px;border-radius:50%;margin-top:5px}
.card.red .dot{background:var(--bad);box-shadow:0 0 8px rgba(251,113,133,.55)}
.card.orange .dot{background:var(--warn)}.card.green .dot{background:var(--good)}.card.blue .dot{background:#60a5fa}
.card.red{background:linear-gradient(180deg,rgba(251,113,133,.07),var(--s1))}
.card.green{background:linear-gradient(180deg,rgba(74,222,128,.06),var(--s1))}
.card .new{position:absolute;top:9px;right:10px;background:var(--ac);color:#062b25;font-size:.6em;font-weight:800;padding:3px 7px;border-radius:99px;letter-spacing:.1em}
.card.read{opacity:.38}
.empty{color:var(--tx3);font-size:.85em;padding:10px 2px}
.clist{background:var(--s1);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.acct{display:flex;justify-content:space-between;align-items:center;background:var(--s2);padding:9px 13px;font-size:.72em;font-weight:800;letter-spacing:.08em;text-transform:uppercase}
.asub{color:var(--tx3);font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:0;text-transform:none}
.crow{padding:10px 13px;border-top:1px solid var(--line)}
.cl1{display:flex;justify-content:space-between;align-items:center;gap:10px}
.cn{font-size:.86em;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.cl2{display:flex;gap:12px;font-size:.72em;color:var(--tx3);margin-top:4px;font-variant-numeric:tabular-nums}
.pill{font-size:.8em;font-weight:800;font-variant-numeric:tabular-nums;padding:3px 9px;border-radius:99px;white-space:nowrap}
.pill.good{background:rgba(74,222,128,.13);color:var(--good)}
.pill.warn{background:rgba(251,191,36,.13);color:var(--warn)}
.pill.bad{background:rgba(251,113,133,.14);color:var(--bad)}
.good{color:var(--good);font-weight:700}.warn{color:var(--warn);font-weight:700}.bad{color:var(--bad);font-weight:700}.muted{color:var(--tx3)}
.pgrid{display:grid;grid-template-columns:1fr 1fr;gap:9px}
.pcard{background:linear-gradient(180deg,var(--s2),var(--s1));border:1px solid var(--line);border-radius:13px;padding:12px}
.pl{font-size:.72em;color:var(--tx2);font-weight:700}
.pv{font-size:1.5em;font-weight:800;margin:5px 0 3px;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.ps{font-size:.7em;color:var(--tx2);font-variant-numeric:tabular-nums}.dim{color:var(--tx3)}
.vcard{background:var(--s1);border:1px solid var(--line);border-radius:11px;padding:12px 13px;margin-bottom:7px}
.vhead{display:flex;justify-content:space-between;align-items:center;font-size:.86em}
.vcount{background:rgba(94,234,212,.12);color:var(--ac);font-weight:800;font-size:.78em;padding:2px 9px;border-radius:99px;font-variant-numeric:tabular-nums}
.vnote{font-size:.76em;color:var(--tx2);margin-top:6px;line-height:1.5}
nav{position:fixed;bottom:0;left:0;right:0;background:rgba(18,24,34,.88);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);display:flex;border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom)}
nav a{flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;padding:9px 0 7px;font-size:.6em;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--tx3);text-decoration:none}
nav a i{font-style:normal;font-size:1.55em}
nav a:active{color:var(--ac)}
#rfr{background:var(--s2);border:1px solid var(--line);color:var(--ac);font-size:1.05em;border-radius:9px;padding:4px 11px;margin-left:10px}
#lock{position:fixed;inset:0;background:var(--bg);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;z-index:10}
#lock .brand{font-size:1.15em}
#lock input{background:var(--s1);border:1px solid var(--line);border-radius:11px;padding:13px 18px;color:var(--tx);font-size:1em;text-align:center;letter-spacing:.12em;outline:none}
#lock input:focus{border-color:var(--ac)}
#lock button{background:var(--ac);border:0;border-radius:11px;padding:13px 30px;color:#062b25;font-size:.92em;font-weight:800;letter-spacing:.06em}
@media(prefers-reduced-motion:no-preference){.card,.pcard,.kpi{transition:opacity .18s ease}}
"""


def encrypt(html):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt, 200000, 32)
    ct = AESGCM(key).encrypt(nonce, html.encode(), None)
    return base64.b64encode(salt).decode(), base64.b64encode(nonce).decode(), base64.b64encode(ct).decode()


def write_site(content_html):
    build_ts = int(NOW.timestamp() * 1000)
    salt, nonce, ct = encrypt(content_html)
    page = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="robots" content="noindex,nofollow">
<meta name="theme-color" content="#0c1118"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Leadfy Ads</title><style>{CSS}</style></head><body>
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
document.getElementById('lock').remove();localStorage.setItem('k',pw);initCards();initRefresh();
}}catch(e){{document.getElementById('err').textContent='Code incorrect';}}}}
function hardReload(){{location.replace(location.pathname+'?t='+Date.now());}}
function initRefresh(){{
const h=document.querySelector('header');
if(h){{const b=document.createElement('button');b.id='rfr';b.textContent='↻';b.onclick=hardReload;h.appendChild(b);}}
document.addEventListener('visibilitychange',()=>{{
if(document.visibilityState==='visible'&&Date.now()-BT>10*60*1000)hardReload();}});
}}
function initCards(){{
const read=new Set(JSON.parse(localStorage.getItem('readCards')||'[]'));
document.querySelectorAll('.card[data-key]').forEach(c=>{{
const k=c.dataset.key;
if(read.has(k)){{c.classList.add('read');}}else{{const b=document.createElement('span');b.className='new';b.textContent='NEW';c.appendChild(b);}}
c.addEventListener('click',()=>{{
if(c.classList.contains('read')){{c.classList.remove('read');read.delete(k);}}
else{{c.classList.add('read');const b=c.querySelector('.new');if(b)b.remove();read.add(k);}}
localStorage.setItem('readCards',JSON.stringify([...read].slice(-200)));}});
}});}}
document.getElementById('pw').addEventListener('keydown',e=>{{if(e.key==='Enter')unlock();}});
if(localStorage.getItem('k')){{document.getElementById('pw').value=localStorage.getItem('k');unlock();}}
</script></body></html>"""
    os.makedirs("site", exist_ok=True)
    open("site/index.html", "w").write(page)


def telegram(text):
    if not TG_TOKEN:
        return
    try:
        urllib.request.urlopen("https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage?" +
                               urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}), timeout=30)
    except Exception as e:
        print("telegram fail:", e)


def check_token():
    out = []
    me = api("me", {"fields": "id"})
    if me.get("error"):
        out.append(("red", "token_dead",
                    "🔑 TOKEN META INVALIDE : le dashboard ne peut plus lire les comptes. Dis à Claude « répare le token dashboard »."))
        return out
    dbg = api("debug_token", {"input_token": FB_TOKEN}).get("data", {})
    for label, ts in (("expiration", dbg.get("expires_at") or 0),
                      ("expiration accès données", dbg.get("data_access_expires_at") or 0)):
        if ts:
            days = (ts - NOW.timestamp()) / 86400
            if days < 10:
                lvl = "red" if days < 3 else "orange"
                out.append((lvl, f"token_{label}",
                            f"🔑 Token Meta : {label} dans {max(days,0):.0f} jour(s) — régénérer le token et mettre à jour le secret FB_TOKEN (demander à Claude)"))
    return out


def main():
    accounts = [fetch_account(a) for a in CFG["accounts"]]
    recos, alerts = build_recos(accounts)
    test_ads = fetch_test_ads(accounts)
    recos = senior_recos(accounts, test_ads) + recos
    alerts = check_token() + alerts
    write_site(render(accounts, recos, alerts))
    # alertes : n'envoyer que les nouvelles (état commité dans le repo)
    state_f = "alerts_state.json"
    today = NOW.strftime("%Y-%m-%d")
    prev = json.load(open(state_f)) if os.path.exists(state_f) else {}
    if not isinstance(prev, dict):
        prev = {}
    new_msgs = []
    for _lvl, key, msg in alerts:
        if prev.get(key) != today:
            new_msgs.append(msg)
        prev[key] = today
    if new_msgs:
        telegram("🚨 LEADFY ADS — ALERTES\n\n" + "\n".join("• " + m for m in sorted(new_msgs)))
    json.dump({k: v for k, v in prev.items() if v == today}, open(state_f, "w"))
    # daily scan du matin (verrou : un seul envoi par jour)
    if 5 <= NOW.hour < 7 and prev.get("__daily__") != today:
        prev["__daily__"] = today
        json.dump({k: v for k, v in prev.items() if v == today}, open(state_f, "w"))
        lines = [f"☀️ DAILY LEADFY — {PARIS.strftime('%d/%m')}"]
        for g, glabel in (("perso", "🏠 PERSO"), ("certicasa", "🇪🇸 CERTICASA (géré)")):
            gs = sum(c["spend_w"] for a in accounts if a.get("group") == g for c in a["campaigns"])
            gl = sum(c["leads_w"] for a in accounts if a.get("group") == g for c in a["campaigns"])
            gc = f"CPL {gs/gl:.2f}€" if gl else "pas de leads"
            lines.append(f"{glabel} · 7j : {gs:.0f}€ · {gl} leads · {gc}")
        lines.append("")
        for a in accounts:
            for c in a["campaigns"]:
                cpl = f"{c['cpl_w']:.2f}€" if c["cpl_w"] else "—"
                lines.append(f"{a['label'][:4]} {c['name'][:28]} · {c['spend_w']:.0f}€/7j · CPL {cpl}")
        lines.append("")
        lines += ["• " + m for m in ([m for _l, _k, m in alerts] + [m for _l, m in recos])[:8]]
        telegram("\n".join(lines))
    print(f"OK — {sum(len(a['campaigns']) for a in accounts)} campagnes, {len(alerts)} alertes, {len(recos)} recos")


if __name__ == "__main__":
    main()
