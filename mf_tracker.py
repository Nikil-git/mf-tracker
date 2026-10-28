#!/usr/bin/env python3
import bisect, calendar, json, os, socket, sys, threading, time, uuid, webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mf_data.json")
NAV_API = "https://api.mfapi.in/mf"
CACHE_SECONDS = 1800
STAMP_DUTY = 0.00005          # 0.005% stamp duty taken from every purchase
lock = threading.Lock()
_funds = {}


# ---------- NAV data ----------

def get_json(url):
    req = Request(url, headers={"User-Agent": "mf-tracker/2.0"})
    with urlopen(req, timeout=20) as r:
        return json.load(r)


def get_fund(code):
    hit = _funds.get(code)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    raw = get_json(f"{NAV_API}/{quote(code)}")
    pairs = []
    for r in raw.get("data") or []:
        try:
            pairs.append((datetime.strptime(r["date"], "%d-%m-%Y").date(), float(r["nav"])))
        except (KeyError, ValueError):
            continue
    if not pairs:
        raise LookupError(f"No NAV data for scheme code {code}")
    pairs.sort()
    fund = {"name": raw.get("meta", {}).get("scheme_name", code),
            "dates": [p[0] for p in pairs], "navs": [p[1] for p in pairs]}
    _funds[code] = (time.time(), fund)
    return fund


# ---------- saved entries ----------

def load_data():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"investments": []}


def save_data(data):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DATA_FILE)


def add_investment(b):
    today = date.today()
    code = str(b.get("scheme_code", "")).strip()
    if not code.isdigit():
        raise ValueError("Pick a fund from the search results.")
    kind = b.get("type")
    if kind not in ("lump", "sip", "redeem"):
        raise ValueError("Choose Lump sum, SIP or Redeem.")
    amount = float(b.get("amount"))
    if amount <= 0:
        raise ValueError("Amount must be more than 0.")
    start = date.fromisoformat(b.get("start"))
    if start > today:
        raise ValueError("The date is in the future.")
    end = b.get("end") or None
    if kind != "sip":
        end = None
    elif end and date.fromisoformat(end) < start:
        raise ValueError("SIP end date is before its start date.")
    units = b.get("units")
    units = float(units) if units not in (None, "") and kind != "sip" else None
    if units is not None and units <= 0:
        raise ValueError("Units must be more than 0.")
    fund = get_fund(code)    # also proves the code is real
    entry = {"id": uuid.uuid4().hex[:10], "scheme_code": code, "name": fund["name"], "type": kind,
             "amount": amount, "start": start.isoformat(), "end": end, "units": units}
    with lock:
        data = load_data()
        data["investments"].append(entry)
        save_data(data)
    return entry


def delete_investment(inv_id):
    with lock:
        data = load_data()
        keep = [i for i in data["investments"] if i["id"] != inv_id]
        if len(keep) == len(data["investments"]):
            raise LookupError("Entry not found.")
        data["investments"] = keep
        save_data(data)


# ---------- maths ----------

def xirr(flows):
    flows = [(d, a) for d, a in flows if a]
    if len(flows) < 2 or not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None
    d0 = min(d for d, _ in flows)
    if (max(d for d, _ in flows) - d0).days < 30:
        return None                      # too short to annualise sensibly

    def npv(r):
        return sum(a / (1 + r) ** ((d - d0).days / 365.0) for d, a in flows)

    lo, hi = -0.9999, 100.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def events_for(inv, fund, today):
    """Turn one entry into dated (date, amount, units) rows. Returns (rows, warning)."""
    start = date.fromisoformat(inv["start"])
    if inv["type"] == "sip":
        end = date.fromisoformat(inv["end"]) if inv.get("end") else today
        end = min(end, today)
        days, y, m = [], start.year, start.month
        while (y, m) <= (end.year, end.month):
            d = date(y, m, min(start.day, calendar.monthrange(y, m)[1]))
            if start <= d <= end:
                days.append(d)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    else:
        days = [start]
    sign = -1 if inv["type"] == "redeem" else 1
    rows, early = [], False
    for d in days:
        i = bisect.bisect_left(fund["dates"], d)       # NAV of that day, or the next business day
        if i == len(fund["dates"]):
            continue
        if d < fund["dates"][0] - timedelta(days=7):
            early = True
            continue
        amount = sign * inv["amount"]
        if inv.get("units"):
            units = sign * inv["units"]
        else:
            units = amount * ((1 - STAMP_DUTY) if sign > 0 else 1) / fund["navs"][i]
        rows.append((d, amount, units))
    warn = f"{fund['name']}: some dates are before the fund started, so they were skipped." if early else None
    return rows, warn


def portfolio():
    today = date.today()
    invs = load_data()["investments"]
    groups, warnings, parts = {}, [], []
    for inv in invs:
        groups.setdefault(inv["scheme_code"], []).append(inv)
    for code, items in groups.items():
        try:
            fund = get_fund(code)
        except (URLError, OSError, LookupError, ValueError) as e:
            warnings.append(f"{items[0]['name']}: could not load NAV ({e})")
            continue
        ev = []
        for inv in items:
            rows, warn = events_for(inv, fund, today)
            ev += rows
            if warn and warn not in warnings:
                warnings.append(warn)
        ev.sort()
        if ev:
            parts.append((code, fund, ev))

    funds_out, flows_all = [], []
    tot = {"invested": 0.0, "redeemed": 0.0, "value": 0.0, "day": 0.0}
    for code, fund, ev in parts:
        nav, nav_date = fund["navs"][-1], fund["dates"][-1]
        units = sum(e[2] for e in ev)
        invested = sum(e[1] for e in ev if e[1] > 0)
        redeemed = -sum(e[1] for e in ev if e[1] < 0)
        value = max(units, 0) * nav
        gain = value + redeemed - invested
        day = units * (nav - fund["navs"][-2]) if len(fund["navs"]) > 1 and units > 0 else 0.0
        flows = [(e[0], -e[1]) for e in ev] + [(nav_date, value)]
        flows_all += flows
        x = xirr(flows)
        funds_out.append({"scheme_code": code, "name": fund["name"], "units": round(units, 3),
                          "nav": nav, "nav_date": nav_date.isoformat(), "invested": round(invested, 2),
                          "value": round(value, 2), "gain": round(gain, 2),
                          "gain_pct": round(gain / invested * 100, 2) if invested else None,
                          "xirr": None if x is None else round(x * 100, 2), "day_change": round(day, 2)})
        tot["invested"] += invested
        tot["redeemed"] += redeemed
        tot["value"] += value
        tot["day"] += day

    series = []
    if parts:
        first = min(ev[0][0] for _, _, ev in parts)
        last = max(f["dates"][-1] for _, f, _ in parts)
        grid, d = [], first
        while d < last:
            grid.append(d)
            d += timedelta(days=7)
        grid.append(last)
        for g in grid:
            val = net = 0.0
            for _, f, ev in parts:
                u = n = 0.0
                for e in ev:
                    if e[0] > g:
                        break
                    u += e[2]
                    n += e[1]
                j = bisect.bisect_right(f["dates"], g) - 1
                if j >= 0:
                    val += max(u, 0) * f["navs"][j]
                net += n
            series.append({"d": g.isoformat(), "v": round(val, 2), "i": round(net, 2)})

    gain = tot["value"] + tot["redeemed"] - tot["invested"]
    funds_out.sort(key=lambda f: f["value"], reverse=True)
    x = xirr(flows_all)
    return {"invested": round(tot["invested"], 2), "redeemed": round(tot["redeemed"], 2),
            "value": round(tot["value"], 2), "gain": round(gain, 2),
            "gain_pct": round(gain / tot["invested"] * 100, 2) if tot["invested"] else None,
            "xirr": None if x is None else round(x * 100, 2), "day_change": round(tot["day"], 2),
            "as_of": max((f["nav_date"] for f in funds_out), default=None),
            "funds": funds_out, "series": series, "investments": invs, "warnings": warnings}


# ---------- web page ----------

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>MF Tracker</title>
<style>
:root{--bg:#0e1116;--card:#171b22;--line:#262c36;--tx:#e8ebf0;--mu:#8892a6;--up:#2ecc71;--dn:#ff5a5f;--ac:#4c8dff}
@media (prefers-color-scheme:light){:root{--bg:#f4f6f9;--card:#fff;--line:#dde2ea;--tx:#161a21;--mu:#5d6778}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.4 system-ui,sans-serif;padding:20px;max-width:1100px;margin:auto}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:26px 0 10px}.mu{color:var(--mu)}.up{color:var(--up)}.down{color:var(--dn)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:14px}
.card,.box{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.card .l{color:var(--mu);font-size:12px}.card .v{font-size:21px;font-weight:600;margin-top:2px}.card .s{font-size:12px}
.box{overflow-x:auto}canvas{width:100%;height:280px;display:block}#tip{position:fixed;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 9px;font-size:12px;display:none}
table{border-collapse:collapse;width:100%;min-width:640px}th,td{text-align:right;padding:8px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mu);font-weight:500;font-size:12px}th:first-child,td:first-child{text-align:left;white-space:normal}
input,select,button{font:inherit;color:var(--tx);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px}
button{cursor:pointer;background:var(--ac);border-color:var(--ac);color:#fff}button.x{background:none;border-color:var(--line);color:var(--mu);padding:3px 9px}
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;align-items:end}.form label{font-size:12px;color:var(--mu);display:block;margin-bottom:3px}
.form input,.form select{width:100%}.wide{grid-column:1/-1}#res button{display:block;width:100%;text-align:left;background:var(--bg);color:var(--tx);border-color:var(--line);margin-top:4px}
#msg{margin-top:8px;min-height:18px}.warn{color:#e6a700;font-size:13px;margin-top:6px}
</style></head><body>
<h1>MF Tracker</h1><div class="mu" id="asof">Loading&rdquo;¦</div>
<div class="cards" id="cards"></div><div class="warn" id="warns"></div>
<h2>Growth</h2><div class="box"><canvas id="chart"></canvas></div><div id="tip"></div>
<h2>Funds</h2><div class="box"><table id="funds"></table></div>
<h2>Add an investment</h2><div class="box"><div class="form">
<div class="wide"><label>Search the fund (pick the same plan as in Groww, usually Ã¢â‚¬Å&ldquo;Direct Plan &#8377; Growth&rdquo;)</label>
<input id="q" placeholder="e.g. parag parikh flexi cap direct growth" autocomplete="off"><div id="res"></div><div id="sel" class="mu" style="margin-top:4px"></div></div>
<div><label>Type</label><select id="type"><option value="lump">Lump sum</option><option value="sip">Monthly SIP</option><option value="redeem">Redeem (sold)</option></select></div>
<div><label>Amount (&#8377;)</label><input id="amount" type="number" min="1" step="any"></div>
<div><label id="dl">Date</label><input id="start" type="date"></div>
<div id="endw" style="display:none"><label>SIP end date (blank = still running)</label><input id="end" type="date"></div>
<div id="unitw"><label>Units (optional, else worked out from NAV)</label><input id="units" type="number" min="0" step="any"></div>
<div><button id="add" style="width:100%">Add</button></div></div><div id="msg"></div></div>
<h2>My entries</h2><div class="box"><table id="entries"></table></div>
<p class="mu" style="font-size:12px"></p>
<script>
const $=s=>document.querySelector(s);
const inr=n=>n==null?"&#8377;":(n<0?"-":"")+"&#8377;"+Math.abs(Math.round(n)).toLocaleString("en-IN");
const pc=n=>n==null?"&#8377;":(n>0?"+":"")+n.toFixed(2)+"%";
const cl=n=>n>0?"up":n<0?"down":"";
const short=n=>{const a=Math.abs(n);return "&#8377;"+(a>=1e7?(a/1e7).toFixed(1)+"Cr":a>=1e5?(a/1e5).toFixed(1)+"L":a>=1e3?(a/1e3).toFixed(0)+"K":Math.round(a))};
function el(t,x,c){const e=document.createElement(t);if(x!=null)e.textContent=x;if(c)e.className=c;return e}
async function api(p,o){const r=await fetch(p,o);const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
let pts=[],selected=null;
function card(l,v,s,c){const d=el("div",null,"card");d.append(el("div",l,"l"),el("div",v,"v "+(c||"")));if(s)d.append(el("div",s,"s "+(c||"")));return d}
function row(tbl,cells,head){const tr=el("tr");cells.forEach(c=>{const td=el(head?"th":"td");if(c instanceof Node)td.append(c);else{td.textContent=c.t;if(c.c)td.className=c.c}tr.append(td)});tbl.append(tr)}
function draw(s){
 const c=$("#chart"),dpr=window.devicePixelRatio||1,W=c.clientWidth,H=c.clientHeight;c.width=W*dpr;c.height=H*dpr;
 const g=c.getContext("2d");g.scale(dpr,dpr);g.clearRect(0,0,W,H);pts=[];
 const mu=getComputedStyle(document.body).getPropertyValue("--mu"),ln=getComputedStyle(document.body).getPropertyValue("--line");
 g.font="11px system-ui";g.fillStyle=mu;
 if(s.length<2){g.fillText("Add an investment to see your growth curve.",12,24);return}
 const P={l:56,r:10,t:10,b:22},mx=Math.max(...s.flatMap(p=>[p.v,p.i]),1)*1.08,t0=Date.parse(s[0].d),t1=Date.parse(s[s.length-1].d);
 const X=t=>P.l+(t-t0)/(t1-t0||1)*(W-P.l-P.r),Y=v=>H-P.b-v/mx*(H-P.t-P.b);
 g.strokeStyle=ln;g.textAlign="right";
 for(let k=0;k<=4;k++){const v=mx*k/4,y=Y(v);g.beginPath();g.moveTo(P.l,y);g.lineTo(W-P.r,y);g.stroke();g.fillText(short(v),P.l-6,y+4)}
 g.textAlign="left";g.fillText(s[0].d,P.l,H-6);g.textAlign="right";g.fillText(s[s.length-1].d,W-P.r,H-6);
 const path=k=>{g.beginPath();s.forEach((p,i)=>{const x=X(Date.parse(p.d)),y=Y(p[k]);i?g.lineTo(x,y):g.moveTo(x,y)})};
 path("v");g.lineTo(X(t1),Y(0));g.lineTo(X(t0),Y(0));g.fillStyle="rgba(46,204,113,.12)";g.fill();
 path("i");g.strokeStyle="#8892a6";g.lineWidth=1.5;g.setLineDash([5,4]);g.stroke();g.setLineDash([]);
 path("v");g.strokeStyle="#2ecc71";g.lineWidth=2;g.stroke();
 pts=s.map(p=>({x:X(Date.parse(p.d)),p}));
}
$("#chart").addEventListener("mousemove",e=>{
 const tip=$("#tip");if(!pts.length){tip.style.display="none";return}
 const r=e.target.getBoundingClientRect(),mx=e.clientX-r.left;let b=pts[0];pts.forEach(q=>{if(Math.abs(q.x-mx)<Math.abs(b.x-mx))b=q});
 tip.textContent=b.p.d+"  Value "+inr(b.p.v)+"  Invested "+inr(b.p.i);tip.style.display="block";tip.style.left=Math.min(e.clientX+12,innerWidth-260)+"px";tip.style.top=(e.clientY+12)+"px"});
$("#chart").addEventListener("mouseleave",()=>$("#tip").style.display="none");
async function load(){
 try{
  const d=await api("/api/portfolio");
  $("#asof").textContent=d.as_of?"":"No investments yet";
  const C=$("#cards");C.replaceChildren(
   card("Current value",inr(d.value)),card("Invested",inr(d.invested),d.redeemed?"Redeemed "+inr(d.redeemed):""),
   card("Total gain",inr(d.gain),pc(d.gain_pct),cl(d.gain)),card("XIRR (per year)",d.xirr==null?"&#8377;":pc(d.xirr),d.xirr==null?"needs 30+ days":"",cl(d.xirr)),
   card("Today",inr(d.day_change),"",cl(d.day_change)));
  $("#warns").replaceChildren(...d.warnings.map(w=>el("div",w)));
  draw(d.series);
  const F=$("#funds");F.replaceChildren();row(F,["Fund","Units","NAV","Invested","Value","Gain","XIRR"].map(x=>({t:x})),true);
  d.funds.forEach(f=>row(F,[{t:f.name},{t:f.units.toLocaleString("en-IN")},{t:"&#8377;"+f.nav.toFixed(2)},{t:inr(f.invested)},{t:inr(f.value)},
   {t:inr(f.gain)+" ("+pc(f.gain_pct)+")",c:cl(f.gain)},{t:f.xirr==null?"&#8377;":pc(f.xirr),c:cl(f.xirr)}]));
  const E=$("#entries");E.replaceChildren();row(E,["Fund","Type","Details",""].map(x=>({t:x})),true);
  d.investments.forEach(i=>{const b=el("button","Delete","x");b.onclick=async()=>{if(!confirm("Delete this entry?"))return;try{await api("/api/investments/"+i.id,{method:"DELETE"});load()}catch(e){$("#msg").textContent=e.message}};
   const det=i.type==="sip"?inr(i.amount)+" every month from "+i.start+(i.end?" to "+i.end:", ongoing"):inr(i.amount)+" on "+i.start+(i.units?" ("+i.units+" units)":"");
   row(E,[{t:i.name},{t:{lump:"Lump sum",sip:"SIP",redeem:"Redeem"}[i.type]},{t:det},b])});
 }catch(e){$("#asof").textContent="Error: "+e.message}
}
let timer;$("#q").addEventListener("input",()=>{clearTimeout(timer);const q=$("#q").value.trim(),R=$("#res");if(q.length<3){R.replaceChildren();return}
 timer=setTimeout(async()=>{try{const L=await api("/api/search?q="+encodeURIComponent(q));R.replaceChildren(...L.slice(0,10).map(f=>{const b=el("button",f.schemeName);
  b.onclick=()=>{selected={code:String(f.schemeCode),name:f.schemeName};$("#sel").textContent="Selected: "+f.schemeName;R.replaceChildren();$("#q").value=""};return b}))}catch(e){R.replaceChildren(el("div",e.message,"mu"))}},350)});
function mode(){const t=$("#type").value;$("#endw").style.display=t==="sip"?"":"none";$("#unitw").style.display=t==="sip"?"none":"";$("#dl").textContent=t==="sip"?"SIP start date":t==="redeem"?"Date sold":"Date invested"}
$("#type").onchange=mode;mode();
$("#add").onclick=async()=>{const m=$("#msg");m.className="";
 try{if(!selected)throw new Error("Search and pick a fund first.");
  await api("/api/investments",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({scheme_code:selected.code,type:$("#type").value,amount:$("#amount").value,start:$("#start").value,end:$("#end").value,units:$("#units").value})});
  m.textContent="Added.";m.className="up";selected=null;$("#sel").textContent="";["amount","start","end","units"].forEach(i=>$("#"+i).value="");load()}
 catch(e){m.textContent=e.message;m.className="down"}};
window.addEventListener("resize",load);load();setInterval(load,300000);
</script></body></html>"""


# ---------- server ----------

class Handler(BaseHTTPRequestHandler):
    def send(self, status, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else (body if ctype != "application/json" else json.dumps(body)).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if ctype == "text/html" else ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def run(self, fn):
        try:
            return self.send(200, fn())
        except LookupError as e:
            return self.send(404, {"error": str(e)})
        except (ValueError, KeyError, TypeError) as e:
            return self.send(400, {"error": str(e)})
        except (URLError, TimeoutError, OSError) as e:
            return self.send(502, {"error": f"Could not reach the NAV service. Check your internet. ({e})"})

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            return self.send(200, PAGE, "text/html")
        if url.path == "/api/portfolio":
            return self.run(portfolio)
        if url.path == "/api/search":
            q = parse_qs(url.query).get("q", [""])[0].strip()
            return self.run(lambda: get_json(f"{NAV_API}/search?q={quote(q)}") if len(q) >= 3 else [])
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/api/investments":
            return self.send(404, {"error": "not found"})
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self.send(415, {"error": "JSON only"})
        size = int(self.headers.get("Content-Length") or 0)
        if size > 10000:
            return self.send(413, {"error": "too large"})
        body = json.loads(self.rfile.read(size) or b"{}")
        self.run(lambda: add_investment(body))

    def do_DELETE(self):
        p = urlparse(self.path).path
        if not p.startswith("/api/investments/"):
            return self.send(404, {"error": "not found"})
        self.run(lambda: (delete_investment(p.rsplit("/", 1)[1]), {"ok": True})[1])

    def log_message(self, *a):
        pass


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "your-pc-ip"


def main():
    args = sys.argv[1:]
    port = int(os.environ.get("PORT", 8000))
    host = "0.0.0.0"
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"MF Tracker running: http://localhost:{port}")
    if host == "0.0.0.0":
        print(f"On your phone (same Wi-Fi): http://{lan_ip()}:{port}  (anyone on this Wi-Fi can open it)")
    print("Press Ctrl+C to stop.")
    if "--no-browser" not in args:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
