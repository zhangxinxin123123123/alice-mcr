import importlib.abc
import importlib.machinery
import os
import smtplib
import sys
import threading
import time
from email.mime.text import MIMEText
from email.utils import formataddr

BOSS_EMAIL = os.environ.get("ALICE_BOSS_EMAIL", "xinxinzhang330@gmail.com")

_JS = r'''
(function(){
  if(window.__aliceSettlePatch) return;
  window.__aliceSettlePatch = true;
  var bossDefault = "xinxinzhang330@gmail.com";
  var drafts = {}, opened = {}, selected = new Set();
  function D(){ try { return data; } catch(e) { window.data = window.data || {}; return window.data; } }
  function H(){ try { return authHeaders(); } catch(e) { return {}; } }
  function today(){ return new Date().toISOString().slice(0,10); }
  function money(n){ try { return yen(n); } catch(e) { return "¥" + Math.round(Number(n||0)).toLocaleString(); } }
  function clean(v){ return String(v == null ? "" : v).trim(); }
  function esc(v){ return clean(v).replace(/[&<>"']/g, function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]; }); }
  function rdate(){ var e=document.getElementById("settleReportDate"); return (e && e.value) || today(); }
  function girlEmail(name){ var g=(D().girls||[]).find(function(x){ return clean(x.name)===clean(name); }); return (g && g.email) || ""; }
  function reportOf(date,girl){ return (D().settlement_reports||[]).find(function(x){ return clean(x.report_date)===clean(date) && clean(x.girl_name)===clean(girl); }); }
  function key(date,girl){ return date + "||" + girl; }
  function formulaDefault(total, nonCash){ return Number(nonCash||0) ? "理论 " + money(total) + " - 非现金 " + money(nonCash) + " = " + money(Number(total||0)-Number(nonCash||0)) : "理论 " + money(total); }
  function val(row, field, fallback){ var k=key(row.date,row.girl); if(drafts[k] && drafts[k][field] !== undefined) return drafts[k][field]; var r=reportOf(row.date,row.girl); if(r && r[field] !== undefined && r[field] !== null && String(r[field]) !== "") return r[field]; return fallback; }
  function actual(row){ return Number(val(row,"actual_settlement",Number(row.total||0)-Number(row.nonCash||0))||0); }
  function formula(row){ return String(val(row,"formula_text",formulaDefault(row.total,row.nonCash))||""); }
  async function post(url, body){
    if(typeof api === "function") return api(url, body);
    var r=await fetch(url,{method:"POST",headers:Object.assign({"Content-Type":"application/json"},H()),body:JSON.stringify(body||{})});
    var j=await r.json().catch(function(){return {ok:false,error:"服务器返回不是JSON"};});
    if(!r.ok || j.ok===false) throw new Error(j.error||"保存失败");
    return j;
  }
  async function loadReports(){
    try{
      var r=await fetch("/api/settlements?v="+Date.now(),{cache:"no-store",headers:H()});
      var j=await r.json();
      D().settlement_reports=j.settlements||[];
      D().settlement_boss_email=j.boss_email||bossDefault;
    }catch(e){ D().settlement_reports=D().settlement_reports||[]; D().settlement_boss_email=bossDefault; }
  }
  window.loadSettlementReports = loadReports;
  function ensureStyle(){
    if(document.getElementById("aliceSettleStyle")) return;
    var s=document.createElement("style"); s.id="aliceSettleStyle";
    s.textContent=".settle-email-grid{display:grid;grid-template-columns:1fr 1.5fr auto;gap:8px;align-items:center}.settle-input{min-width:120px}@media(max-width:760px){.settle-email-grid{grid-template-columns:1fr}}";
    document.head.appendChild(s);
  }
  function ensureSettlement(){
    var sec=document.getElementById("settlement"); if(!sec || document.getElementById("settlePatchRoot")) return;
    sec.innerHTML='<div class="card" id="settlePatchRoot"><h2>金额结算</h2><p class="note">按通报日期汇总未结算订单。结算公式和实际结算都可以编辑保存；实际结算就是今天真实给老板的钱。女孩邮箱没填会自动跳过。</p><div class="form"><input id="settleReportDate" type="date"><input id="settleGirlQ" placeholder="筛选女孩"><button class="primary" onclick="saveSettlementReports()">保存实际结算</button><button class="primary" onclick="sendDailySettlementReport()">当日结算通报</button><button class="soft" onclick="clearSettlementPick()">清空勾选</button><button class="primary" onclick="bulkSettleSettlement(\'已结算\')">勾选改已结算</button><button class="soft" onclick="bulkSettleSettlement(\'未结算\')">勾选改未结算</button></div><div id="settlementTable"></div></div>';
    var d=document.getElementById("settleReportDate"); if(d&&!d.value)d.value=today(); if(d)d.onchange=renderSettlement;
    var q=document.getElementById("settleGirlQ"); if(q)q.oninput=renderSettlement;
  }
  function ensureEmailPanel(){
    var sec=document.getElementById("girls"); if(!sec || document.getElementById("girlEmailManager")) return;
    var c=document.createElement("div"); c.className="card"; c.id="girlEmailManager";
    c.innerHTML='<h2>女孩邮箱</h2><p class="note">用于“当日结算通报”。没有邮箱的女孩自动跳过。</p><div id="girlEmailRows"></div>';
    sec.insertBefore(c, sec.children[1] || null);
  }
  function renderEmails(){
    ensureEmailPanel(); var box=document.getElementById("girlEmailRows"); if(!box)return;
    var rows=(D().girls||[]).slice().sort(function(a,b){return clean(a.name).localeCompare(clean(b.name));});
    box.innerHTML=rows.map(function(g){return '<div class="settle-email-grid"><b>'+esc(g.name)+'</b><input class="settle-input" id="girlEmail_'+Number(g.id)+'" value="'+esc(g.email||"")+'" placeholder="邮箱"><button class="soft save-girl-email" data-id="'+Number(g.id)+'" data-name="'+esc(g.name)+'">保存</button></div>';}).join("") || '<p class="note">还没有女孩资料。</p>';
    box.querySelectorAll(".save-girl-email").forEach(function(b){ b.onclick=async function(){ var id=b.getAttribute("data-id"), email=(document.getElementById("girlEmail_"+id)||{}).value||""; await post("/api/girls/email",{id:id,name:b.getAttribute("data-name"),email:email}); var g=(D().girls||[]).find(function(x){return Number(x.id)===Number(id);}); if(g)g.email=email; await loadReports(); renderEmails(); if(document.getElementById("settlement")&&document.getElementById("settlement").classList.contains("on"))renderSettlement(); alert("女孩邮箱已保存"); }; });
  }
  window.renderGirlEmailManager = renderEmails;
  function groups(){
    var date=rdate(), q=clean((document.getElementById("settleGirlQ")||{}).value).toLowerCase(), map={};
    (D().orders||[]).filter(function(o){ return (o.settlement_status||"未结算")!=="已结算" && (!date || o.order_date===date) && (!q || clean(o.girl_name).toLowerCase().indexOf(q)>=0); }).forEach(function(o){
      var girl=o.girl_name||"未填写女孩", day=o.order_date||date, amt=Number(o.store_profit||0), pm=clean(o.payment_method||"现金"), non=pm&&pm!=="现金"?amt:0;
      map[girl]=map[girl]||{girl:girl,total:0,nonCash:0,count:0,ids:[],dates:{}};
      map[girl].total+=amt; map[girl].nonCash+=non; map[girl].count++; map[girl].ids.push(Number(o.id));
      map[girl].dates[day]=map[girl].dates[day]||{date:day,girl:girl,total:0,nonCash:0,count:0,ids:[]};
      map[girl].dates[day].total+=amt; map[girl].dates[day].nonCash+=non; map[girl].dates[day].count++; map[girl].dates[day].ids.push(Number(o.id));
    });
    return Object.values(map).sort(function(a,b){return b.total-a.total;});
  }
  function rowsForSave(){ var out=[]; groups().forEach(function(g){ Object.values(g.dates).forEach(function(r){ out.push({girl_name:g.girl,theoretical_amount:Number(r.total||0),actual_settlement:actual(r),formula_text:formula(r),order_ids:r.ids,girl_email:girlEmail(g.girl)}); }); }); return out; }
  window.setSettlementDraft=function(date,girl,field,value){ var k=key(date,girl); drafts[k]=drafts[k]||{}; drafts[k][field]=field==="actual_settlement"?Number(value||0):value; };
  window.toggleSettlementIds=function(ids,checked){ ids.forEach(function(id){ checked?selected.add(Number(id)):selected.delete(Number(id)); }); renderSettlement(); };
  window.clearSettlementPick=function(){ selected.clear(); renderSettlement(); };
  window.renderSettlement=function(){
    ensureStyle(); ensureSettlement(); var box=document.getElementById("settlementTable"); if(!box)return;
    var gs=groups(), theory=0, real=0, out='<div class="table"><table><thead><tr><th>展开</th><th>女孩</th><th>未结算单数</th><th>理论结算</th><th>结算公式</th><th>实际结算</th><th>邮箱</th><th>选择</th></tr></thead><tbody>';
    gs.forEach(function(g){ var k=encodeURIComponent(g.girl), dr=Object.values(g.dates).sort(function(a,b){return clean(b.date).localeCompare(clean(a.date));}), ga=dr.reduce(function(s,r){return s+actual(r);},0), all=g.ids.length&&g.ids.every(function(id){return selected.has(Number(id));}); theory+=Number(g.total||0); real+=ga;
      out+='<tr><td><button class="soft open-settle" data-open="'+esc(k)+'">'+(opened[k]?"收起":"展开")+'</button></td><td>'+esc(g.girl)+'</td><td>'+g.count+'</td><td><b>'+money(g.total)+'</b></td><td><small>'+esc(dr.map(formula).join("；"))+'</small></td><td><b>'+money(ga)+'</b></td><td>'+esc(girlEmail(g.girl)||"未填写")+'</td><td><input class="check-settle" type="checkbox" '+(all?"checked":"")+' data-ids="'+g.ids.join(",")+'"> 全选该女孩</td></tr>';
      if(opened[k]) dr.forEach(function(r){ var ck=r.ids.length&&r.ids.every(function(id){return selected.has(Number(id));}); out+='<tr><td></td><td style="padding-left:28px">'+esc(g.girl)+'</td><td>'+esc(r.date)+'｜'+r.count+'单</td><td>'+money(r.total)+'</td><td><input class="edit-settle" data-date="'+esc(r.date)+'" data-girl="'+esc(g.girl)+'" data-field="formula_text" style="min-width:220px" value="'+esc(formula(r))+'"></td><td><input class="edit-settle" data-date="'+esc(r.date)+'" data-girl="'+esc(g.girl)+'" data-field="actual_settlement" style="width:120px" value="'+actual(r)+'"></td><td>'+esc(girlEmail(g.girl)||"未填写")+'</td><td><input class="check-settle" type="checkbox" '+(ck?"checked":"")+' data-ids="'+r.ids.join(",")+'"> 选择这一天</td></tr>'; });
    });
    if(!gs.length) out+='<tr><td colspan="8">暂无未结算金额</td></tr>';
    out+='<tr><td colspan="3"><b>'+esc(rdate())+' 当日合计</b></td><td><b>'+money(theory)+'</b></td><td></td><td><b>'+money(real)+'</b></td><td colspan="2">老板邮箱：'+esc(D().settlement_boss_email||bossDefault)+'</td></tr></tbody></table></div>'; box.innerHTML=out;
    box.querySelectorAll(".open-settle").forEach(function(b){ b.onclick=function(){ var k=b.getAttribute("data-open"); opened[k]=!opened[k]; renderSettlement(); }; });
    box.querySelectorAll(".check-settle").forEach(function(c){ c.onchange=function(){ toggleSettlementIds(clean(c.getAttribute("data-ids")).split(",").filter(Boolean), c.checked); }; });
    box.querySelectorAll(".edit-settle").forEach(function(e){ e.onchange=function(){ setSettlementDraft(e.getAttribute("data-date"),e.getAttribute("data-girl"),e.getAttribute("data-field"),e.value); if(e.getAttribute("data-field")==="actual_settlement")renderSettlement(); }; });
  };
  window.saveSettlementReports=async function(silent){ var items=rowsForSave(); if(!items.length){alert("当天没有可保存的未结算金额"); return null;} var r=await post("/api/settlements/save",{date:rdate(),items:items}); await loadReports(); renderSettlement(); if(!silent)alert("已保存实际结算："+(r.saved||items.length)+" 条"); return r; };
  window.sendDailySettlementReport=async function(){ var saved=await saveSettlementReports(true); if(!saved)return; if(!confirm("确认发送 "+rdate()+" 当日结算通报？\n老板邮箱："+(D().settlement_boss_email||bossDefault)+"\n女孩邮箱没填会自动跳过。"))return; var r=await post("/api/settlements/notify",{date:rdate()}); await loadReports(); renderSettlement(); var sg=(r.girls||[]).filter(function(x){return x.sent;}).length, sk=(r.girls||[]).length-sg, bt=r.boss&&r.boss.sent?"老板已发送":"老板邮件未发送："+((r.boss&&r.boss.reason)||"未知原因"); alert(bt+"\n女孩已发送："+sg+"\n女孩跳过/未发："+sk); };
  window.bulkSettleSettlement=async function(status){ var ids=[].slice.call(selected); if(!ids.length){alert("请先勾选要修改的日期或女孩");return;} await post("/api/orders/bulk_settle",{ids:ids,settlement_status:status}); selected.clear(); if(typeof loadAll==="function")await loadAll(); else renderSettlement(); };
  var oldRender=window.render; if(typeof oldRender==="function"&&!oldRender.__settleWrapped){ var wrapped=function(){ var r=oldRender.apply(this,arguments); setTimeout(function(){ loadReports().then(function(){ ensureSettlement(); renderEmails(); if(document.getElementById("settlement")&&document.getElementById("settlement").classList.contains("on"))renderSettlement(); }); },0); return r; }; wrapped.__settleWrapped=true; window.render=wrapped; }
  document.addEventListener("DOMContentLoaded",function(){ ensureStyle(); ensureSettlement(); ensureEmailPanel(); loadReports().then(function(){ renderEmails(); if(document.getElementById("settlement")&&document.getElementById("settlement").classList.contains("on"))renderSettlement(); }); });
})();
'''

def _smtp_value(name, default=""):
    return os.environ.get("ALICE_" + name) or os.environ.get(name) or default

def _yen(value):
    try:
        return "¥" + f"{int(round(float(value or 0))):,}"
    except Exception:
        return "¥0"

def _send_email(to_addr, subject, body):
    to_addr = (to_addr or "").strip()
    if not to_addr:
        return {"sent": False, "reason": "missing recipient"}
    host = _smtp_value("SMTP_HOST")
    user = _smtp_value("SMTP_USER")
    password = _smtp_value("SMTP_PASSWORD")
    from_addr = _smtp_value("SMTP_FROM", user)
    port = int(_smtp_value("SMTP_PORT", "587") or 587)
    if not host or not from_addr or not password:
        return {"sent": False, "reason": "SMTP not configured", "to": to_addr, "subject": subject, "preview": body}
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Alice MCR", from_addr))
    msg["To"] = to_addr
    cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    with cls(host, port, timeout=20) as smtp:
        if port != 465:
            smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.sendmail(from_addr, [to_addr], msg.as_string())
    return {"sent": True, "to": to_addr, "subject": subject}

def _install(module):
    app = getattr(module, "app", None)
    conn = getattr(module, "conn", None)
    if not app or not conn or getattr(app, "_actual_settle_patch", False):
        return
    app._actual_settle_patch = True
    from flask import Response, jsonify, request

    def schema():
        with conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS settlement_reports(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT NOT NULL,
                girl_name TEXT NOT NULL,
                theoretical_amount INTEGER DEFAULT 0,
                actual_settlement INTEGER DEFAULT 0,
                formula_text TEXT DEFAULT '',
                order_ids TEXT DEFAULT '',
                boss_email TEXT DEFAULT '',
                girl_email TEXT DEFAULT '',
                sent_to_boss_at TEXT DEFAULT '',
                sent_to_girl_at TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(report_date,girl_name)
            )""")
            cols = [r[1] for r in c.execute("PRAGMA table_info(girls)").fetchall()]
            if cols and "email" not in cols:
                c.execute("ALTER TABLE girls ADD COLUMN email TEXT DEFAULT ''")

    old_init = getattr(module, "init_db", None)
    if callable(old_init):
        def patched_init_db(*args, **kwargs):
            result = old_init(*args, **kwargs)
            schema()
            return result
        module.init_db = patched_init_db

    def rows(result):
        return [dict(r) for r in result]

    def girl_email(c, name):
        try:
            r = c.execute("SELECT email FROM girls WHERE name=?", (name,)).fetchone()
            return (r["email"] if r else "") or ""
        except Exception:
            return ""

    @app.route("/api/settlements", methods=["GET"])
    def _settlements_get():
        schema()
        with conn() as c:
            data = rows(c.execute("SELECT * FROM settlement_reports ORDER BY report_date DESC,girl_name ASC").fetchall())
        return jsonify(ok=True, settlements=data, boss_email=BOSS_EMAIL)

    @app.route("/api/settlements/save", methods=["POST"])
    def _settlements_save():
        schema()
        d = request.json or {}
        report_date = str(d.get("date") or d.get("report_date") or "").strip()
        if not report_date:
            return jsonify(ok=False, error="请先选择通报日期"), 400
        saved = 0
        with conn() as c:
            for item in d.get("items") or []:
                girl = str(item.get("girl_name") or item.get("girl") or "").strip()
                if not girl:
                    continue
                theory = int(float(item.get("theoretical_amount") or 0))
                actual = int(float(item.get("actual_settlement") or 0))
                formula = str(item.get("formula_text") or "")
                order_ids = ",".join(str(x) for x in (item.get("order_ids") or []))
                g_email = str(item.get("girl_email") or girl_email(c, girl) or "")
                old = c.execute("SELECT id FROM settlement_reports WHERE report_date=? AND girl_name=?", (report_date, girl)).fetchone()
                if old:
                    c.execute("""UPDATE settlement_reports SET theoretical_amount=?,actual_settlement=?,formula_text=?,order_ids=?,boss_email=?,girl_email=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (theory, actual, formula, order_ids, BOSS_EMAIL, g_email, old["id"]))
                else:
                    c.execute("""INSERT INTO settlement_reports(report_date,girl_name,theoretical_amount,actual_settlement,formula_text,order_ids,boss_email,girl_email) VALUES(?,?,?,?,?,?,?,?)""", (report_date, girl, theory, actual, formula, order_ids, BOSS_EMAIL, g_email))
                saved += 1
        return jsonify(ok=True, saved=saved)

    @app.route("/api/settlements/notify", methods=["POST"])
    def _settlements_notify():
        schema()
        d = request.json or {}
        report_date = str(d.get("date") or d.get("report_date") or "").strip()
        if not report_date:
            return jsonify(ok=False, error="请先选择通报日期"), 400
        with conn() as c:
            rs = rows(c.execute("SELECT * FROM settlement_reports WHERE report_date=? ORDER BY girl_name ASC", (report_date,)).fetchall())
            for r in rs:
                if not r.get("girl_email"):
                    r["girl_email"] = girl_email(c, r["girl_name"])
        if not rs:
            return jsonify(ok=False, error="请先保存实际结算"), 400
        total_theory = sum(int(r.get("theoretical_amount") or 0) for r in rs)
        total_actual = sum(int(r.get("actual_settlement") or 0) for r in rs)
        lines = [f"当日结算通报 {report_date}", f"今日理论 {_yen(total_theory)}，实给 {_yen(total_actual)}。", ""]
        for r in rs:
            lines.append(f"{r['girl_name']}：今日理论 {_yen(r['theoretical_amount'])}，实给 {_yen(r['actual_settlement'])}。公式：{r.get('formula_text') or '未填写'}")
        boss = _send_email(BOSS_EMAIL, f"当日结算通报 {report_date}", "\n".join(lines))
        girls = []
        with conn() as c:
            for r in rs:
                to_addr = (r.get("girl_email") or "").strip()
                if not to_addr:
                    girls.append({"girl": r["girl_name"], "sent": False, "reason": "no email"})
                    continue
                body = f"{r['girl_name']}，今日家教费：理论 {_yen(r['theoretical_amount'])}，实给 {_yen(r['actual_settlement'])}。"
                result = _send_email(to_addr, f"家教费结算 {report_date}", body)
                result["girl"] = r["girl_name"]
                girls.append(result)
                if result.get("sent"):
                    c.execute("UPDATE settlement_reports SET sent_to_girl_at=CURRENT_TIMESTAMP WHERE id=?", (r["id"],))
            if boss.get("sent"):
                c.execute("UPDATE settlement_reports SET sent_to_boss_at=CURRENT_TIMESTAMP WHERE report_date=?", (report_date,))
        return jsonify(ok=True, boss=boss, girls=girls)

    @app.route("/api/girls/email", methods=["POST"])
    def _girls_email_save():
        schema()
        d = request.json or {}
        email = str(d.get("email") or "").strip()
        gid = int(d.get("id") or 0)
        name = str(d.get("name") or "").strip()
        with conn() as c:
            if gid:
                c.execute("UPDATE girls SET email=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (email, gid))
            elif name:
                c.execute("UPDATE girls SET email=?,updated_at=CURRENT_TIMESTAMP WHERE name=?", (email, name))
            else:
                return jsonify(ok=False, error="缺少女孩"), 400
        return jsonify(ok=True)

    @app.route("/alice_settlement_patch.js", methods=["GET"])
    def _settlement_js():
        return Response(_JS, mimetype="application/javascript")

    @app.after_request
    def _inject_js(response):
        if request.method == "GET" and request.path in ("/", "/static/index.html") and "text/html" in (response.content_type or ""):
            try:
                response.direct_passthrough = False
                body = response.get_data(as_text=True)
                if "alice_settlement_patch.js" not in body and "</body>" in body:
                    body = body.replace("</body>", '<script src="/alice_settlement_patch.js?v=20260629"></script></body>')
                    response.set_data(body)
                    response.headers["Cache-Control"] = "no-store"
            except Exception:
                pass
        return response

    try:
        schema()
    except Exception:
        pass

class _Loader(importlib.abc.Loader):
    def __init__(self, inner):
        self.inner = inner
    def create_module(self, spec):
        return self.inner.create_module(spec) if hasattr(self.inner, "create_module") else None
    def exec_module(self, module):
        self.inner.exec_module(module)
        _install(module)

class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "app":
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and spec.loader and not isinstance(spec.loader, _Loader):
            spec.loader = _Loader(spec.loader)
        return spec

def _watch_main():
    for _ in range(300):
        module = sys.modules.get("__main__")
        if module is not None and getattr(module, "app", None) is not None and getattr(module, "conn", None) is not None:
            _install(module)
            return
        time.sleep(0.05)

if not any(isinstance(f, _Finder) for f in sys.meta_path):
    sys.meta_path.insert(0, _Finder())
threading.Thread(target=_watch_main, daemon=True).start()
