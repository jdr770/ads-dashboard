#!/usr/bin/env python3
"""Génère le dashboard Leadfy Ads : pull Meta API -> règles -> HTML chiffré -> Telegram."""
import base64, hashlib, json, os, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone

FB_TOKEN = os.environ["FB_TOKEN"]
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
PASSWORD = os.environ["DASH_PASSWORD"]
GRAPH = "https://graph.facebook.com/v21.0/"

CFG = json.load(open("config.json"))
NOW = datetime.now(timezone.utc)


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
            if c["spend_t"] == 0 and c["budget"] > 0 and NOW.hour >= 10:
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


def fmt_cpl(cpl, target):
    if cpl is None:
        return '<span class="muted">—</span>'
    cls = "good" if cpl <= target else ("warn" if cpl <= target * 1.5 else "bad")
    return f'<span class="{cls}">{cpl:.2f}€</span>'


def render(accounts, recos, alerts):
    upd = NOW.strftime("%d/%m %H:%M UTC")
    groups = {"perso": "🏠 Perso", "certicasa": "🇪🇸 Certicasa (géré)"}
    kpi_blocks = ""
    for g, glabel in groups.items():
        gs = sum(c["spend_t"] for a in accounts if a.get("group") == g for c in a["campaigns"])
        gl = sum(c["leads_t"] for a in accounts if a.get("group") == g for c in a["campaigns"])
        gc = f"{gs / gl:.2f}€" if gl else "—"
        kpi_blocks += (f'<div class="glabel">{glabel}</div><div class="kpis">'
                       f'<div class="kpi"><div class="v">{gs:.0f}€</div><div class="l">Dépense auj.</div></div>'
                       f'<div class="kpi"><div class="v">{gl}</div><div class="l">Leads auj.</div></div>'
                       f'<div class="kpi"><div class="v">{gc}</div><div class="l">CPL</div></div></div>')
    rows = ""
    for acc in accounts:
        if not acc["campaigns"]:
            continue
        rows += f'<tr class="acct"><td colspan="6">{esc(acc["label"])}</td></tr>'
        for c in sorted(acc["campaigns"], key=lambda x: -x["spend_w"]):
            rows += (f'<tr><td class="cname">{esc(c["name"])}</td>'
                     f'<td>{c["spend_t"]:.0f}€</td><td>{c["leads_t"]}</td>'
                     f'<td>{fmt_cpl(c["cpl_t"], c["target"])}</td>'
                     f'<td>{fmt_cpl(c["cpl_w"], c["target"])}</td>'
                     f'<td>{c["freq"]:.1f}</td></tr>')
    alert_cards = [(lvl, m) for lvl, _k, m in alerts]
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
                  f'<div class="ps muted2">7j : {cpl_w}</div></div>')
    import re as _re
    def card_key(m):
        return _re.sub(r"[\d.,€%]+", "", m)[:80]
    reco_html = "".join(
        f'<div class="card {lvl}" data-key="{esc(card_key(m))}">{esc(m)}</div>'
        for lvl, m in (alert_cards + recos)) or '<div class="card green">Rien à signaler ✅</div>'
    backlog = "".join(f'<div class="card blue"><b>{esc(b["market"])}</b> · {b["count"]} vidéos<br><span class="small">{esc(b["note"])}</span></div>' for b in CFG["video_backlog"])
    return f"""<div class="head"><h1>📊 Leadfy Ads</h1><div class="upd">MAJ {upd}</div>
{kpi_blocks}</div>
<section id="campagnes"><h2>📈 Campagnes</h2><div class="twrap"><table>
<tr><th>Campagne</th><th>€ auj.</th><th>Leads</th><th>CPL auj.</th><th>CPL 7j</th><th>Fréq.</th></tr>
{rows}</table></div></section>
<section id="produits"><h2>💶 CPL par produit</h2><div class="pgrid">{prods}</div></section>
<section id="cerveau"><h2>🧠 Cerveau</h2>{reco_html}</section>
<section id="videos"><h2>🎬 Vidéos à lancer</h2>{backlog}</section>
<nav><a href="#campagnes">📈</a><a href="#produits">💶</a><a href="#cerveau">🧠</a><a href="#videos">🎬</a></nav>"""


CSS = """*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1420;color:#e8ecf4;padding-bottom:70px}
.head{padding:18px 16px 8px}h1{font-size:1.4em}h2{font-size:1.05em;margin:18px 0 10px}.upd{color:#8b94a8;font-size:.8em;margin:2px 0 12px}
.glabel{font-size:.8em;color:#8b94a8;font-weight:700;margin:10px 0 6px;text-transform:uppercase;letter-spacing:.5px}.kpis{display:flex;gap:10px;margin-bottom:4px}.kpi{flex:1;background:#1a2233;border-radius:14px;padding:12px;text-align:center}.kpi .v{font-size:1.3em;font-weight:700}.kpi .l{font-size:.72em;color:#8b94a8;margin-top:2px}
section{padding:0 16px}.card{background:#1a2233;border-radius:12px;padding:12px 14px;margin-bottom:8px;font-size:.9em;border-left:4px solid #3b82f6}
.card[data-key]{cursor:pointer;position:relative;padding-right:52px}.card .new{position:absolute;top:10px;right:10px;background:#3b82f6;color:#fff;font-size:.62em;font-weight:800;padding:3px 7px;border-radius:20px;letter-spacing:.5px}.card.read{opacity:.45}
.card.red{border-color:#ef4444}.card.orange{border-color:#f59e0b}.card.green{border-color:#22c55e}.card.blue{border-color:#3b82f6}.small{color:#8b94a8;font-size:.85em}
.twrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:.82em}th{text-align:left;color:#8b94a8;font-weight:600;padding:6px 8px;border-bottom:1px solid #2a3550}
td{padding:7px 8px;border-bottom:1px solid #1e2740;white-space:nowrap}tr.acct td{background:#151c2c;font-weight:700;padding-top:12px}.cname{max-width:180px;overflow:hidden;text-overflow:ellipsis}
.good{color:#22c55e;font-weight:700}.warn{color:#f59e0b;font-weight:700}.bad{color:#ef4444;font-weight:700}.muted{color:#4b5568}
nav{position:fixed;bottom:0;left:0;right:0;background:#151c2c;display:flex;border-top:1px solid #2a3550}nav a{flex:1;text-align:center;padding:14px;font-size:1.3em;text-decoration:none}
.pgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.pcard{background:#1a2233;border-radius:14px;padding:12px}.pl{font-size:.78em;color:#8b94a8;font-weight:700}.pv{font-size:1.35em;font-weight:800;margin:4px 0 2px}.ps{font-size:.75em;color:#c3cad9}.muted2{color:#5d6880}
#lock{position:fixed;inset:0;background:#0f1420;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;z-index:10}
#lock input{background:#1a2233;border:1px solid #2a3550;border-radius:10px;padding:12px 16px;color:#fff;font-size:1em;text-align:center}#lock button{background:#3b82f6;border:0;border-radius:10px;padding:12px 26px;color:#fff;font-size:1em}"""


def encrypt(html):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt, 200000, 32)
    ct = AESGCM(key).encrypt(nonce, html.encode(), None)
    return base64.b64encode(salt).decode(), base64.b64encode(nonce).decode(), base64.b64encode(ct).decode()


def write_site(content_html):
    salt, nonce, ct = encrypt(content_html)
    page = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>Leadfy Ads</title><style>{CSS}</style></head><body>
<div id="lock"><div style="font-size:2em">🔒</div><input id="pw" type="password" placeholder="Code d'accès" autofocus>
<button onclick="unlock()">Entrer</button><div id="err" style="color:#ef4444"></div></div><div id="app"></div>
<script>
const S="{salt}",N="{nonce}",C="{ct}";
const b64=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
async function unlock(){{try{{
const pw=document.getElementById('pw').value;
const km=await crypto.subtle.importKey('raw',new TextEncoder().encode(pw),'PBKDF2',false,['deriveKey']);
const key=await crypto.subtle.deriveKey({{name:'PBKDF2',salt:b64(S),iterations:200000,hash:'SHA-256'}},km,{{name:'AES-GCM',length:256}},false,['decrypt']);
const pt=await crypto.subtle.decrypt({{name:'AES-GCM',iv:b64(N)}},key,b64(C));
document.getElementById('app').innerHTML=new TextDecoder().decode(pt);
document.getElementById('lock').remove();localStorage.setItem('k',pw);initCards();
}}catch(e){{document.getElementById('err').textContent='Code incorrect';}}}}
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


def main():
    accounts = [fetch_account(a) for a in CFG["accounts"]]
    recos, alerts = build_recos(accounts)
    test_ads = fetch_test_ads(accounts)
    recos = senior_recos(accounts, test_ads) + recos
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
    # daily scan du matin (run de ~05:17 UTC = 07:17 Paris)
    if 5 <= NOW.hour < 7:
        lines = [f"☀️ DAILY LEADFY — {NOW.strftime('%d/%m')}"]
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
