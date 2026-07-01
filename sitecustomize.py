import importlib.abc
import importlib.machinery
import os
import smtplib
import sys
import threading
import time
from datetime import datetime, timedelta
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
  function js(v){ return JSON.stringify(clean(v)); }
  function isBoss(){ return !!(auth && auth.role === "boss"); }
  function roleName(role){ return role==="boss" ? "老板" : (role==="admin" ? "管理员" : "普通用户"); }
  function rdate(){ var e=document.getElementById("settleReportDate"); return (e && e.value) || today(); }
  function girlEmail(name){ var g=(D().girls||[]).find(function(x){ return clean(x.name)===clean(name); }); return (g && g.email) || ""; }
  function reportOf(date,girl){ return (D().settlement_reports||[]).find(function(x){ return clean(x.report_date)===clean(date) && clean(x.girl_name)===clean(girl); }); }
  function key(date,girl){ return date + "||" + girl; }
  function formulaDefault(total, nonCash){ return Number(nonCash||0) ? "理论 " + money(total) + " - 非现金 " + money(nonCash) + " = " + money(Number(total||0)-Number(nonCash||0)) : "理论 " + money(total); }
  function idKey(ids){ return (Array.isArray(ids)?ids:String(ids||"").split(",")).map(Number).filter(Boolean).sort(function(a,b){return a-b;}).join(","); }
  function reportMatchesRow(r,row){ return !!(r && idKey(r.order_ids) && idKey(r.order_ids)===idKey(row.ids)); }
  function val(row, field, fallback){ var k=key(row.date,row.girl); if(drafts[k] && drafts[k][field] !== undefined) return drafts[k][field]; var r=reportOf(row.date,row.girl); if(reportMatchesRow(r,row) && r[field] !== undefined && r[field] !== null && String(r[field]) !== "") return r[field]; return fallback; }
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
    s.textContent=".settle-email-grid{display:grid;grid-template-columns:1fr 1.5fr auto;gap:8px;align-items:center}.settle-input{min-width:120px}.settle-picked{background:rgba(148,163,184,.28)!important;color:#6b7280!important}.settle-picked td{color:#6b7280!important}.settle-picked input,.settle-picked button{filter:grayscale(1);opacity:.78}.settle-state{display:inline-block;margin-left:6px;border:1px solid #9ca3af;border-radius:999px;padding:2px 8px;font-size:12px;color:#4b5563;background:#e5e7eb}.settle-summary-head{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between;margin:14px 0 8px}.settle-summary-head h3{margin:0}.settle-pick-hint{margin:8px 0;color:#6b7280;font-weight:700}.login-alert-row{background:#fff1f2!important;color:#9f1239!important}.login-alert-badge{display:inline-block;border:1px solid #fb7185;border-radius:999px;background:#ffe4e6;color:#be123c;padding:2px 8px;font-weight:900}.user-admin-form{display:grid;grid-template-columns:1fr 120px 1fr auto;gap:8px;align-items:center;margin:12px 0}.managed-username,.managed-pass{min-width:130px}.user-offline{color:#6b7280}.user-online{color:#15803d;font-weight:900}@media(max-width:760px){.settle-email-grid,.user-admin-form{grid-template-columns:1fr}}";
    document.head.appendChild(s);
  }
  function ensureSettlement(){
    var sec=document.getElementById("settlement"); if(!sec || document.getElementById("settlePatchRoot")) return;
    sec.innerHTML='<div class="card" id="settlePatchRoot"><h2>金额结算</h2><p class="note">按通报日期汇总未结算订单。结算公式和实际结算都可以编辑保存；实际结算就是今天真实给老板的钱。已结算订单不会再计入实际结算。</p><div class="form"><input id="settleReportDate" type="date"><input id="settleGirlQ" placeholder="筛选女孩"><button class="primary" onclick="window.saveSettlementReports()">保存实际结算</button><button class="primary" onclick="window.sendDailySettlementReport()">当日结算通报</button><button class="soft" onclick="window.clearSettlementPick()">清空勾选</button><button class="primary" onclick="window.bulkSettleSettlement(\'已结算\')">勾选改已结算</button><button class="soft" onclick="window.bulkSettleSettlement(\'未结算\')">勾选改未结算</button></div><div id="settlementTable"></div><div id="settleAllSummary"></div></div>';
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
  function isSettledOrder(o){ return clean((o||{}).settlement_status||"未结算")==="已结算"; }
  function selectedAll(ids){ ids=(ids||[]).map(Number).filter(Boolean); return ids.length&&ids.every(function(id){return selected.has(Number(id));}); }
  function addSettlementOrder(map,o){
    var girl=o.girl_name||"未填写女孩", day=o.order_date||"", amt=Number(o.store_profit||0), pm=clean(o.payment_method||"现金"), non=pm&&pm!=="现金"?amt:0;
    map[girl]=map[girl]||{girl:girl,total:0,nonCash:0,count:0,ids:[],dates:{}};
    map[girl].total+=amt; map[girl].nonCash+=non; map[girl].count++; map[girl].ids.push(Number(o.id));
    map[girl].dates[day]=map[girl].dates[day]||{date:day,girl:girl,total:0,nonCash:0,count:0,ids:[]};
    map[girl].dates[day].total+=amt; map[girl].dates[day].nonCash+=non; map[girl].dates[day].count++; map[girl].dates[day].ids.push(Number(o.id));
  }
  function groups(){
    var date=rdate(), q=clean((document.getElementById("settleGirlQ")||{}).value).toLowerCase(), map={};
    (D().orders||[]).filter(function(o){ return !isSettledOrder(o) && (!date || o.order_date===date) && (!q || clean(o.girl_name).toLowerCase().indexOf(q)>=0); }).forEach(function(o){ addSettlementOrder(map,o); });
    return Object.values(map).sort(function(a,b){return b.total-a.total;});
  }
  function allUnsettledGroups(){
    var q=clean((document.getElementById("settleGirlQ")||{}).value).toLowerCase(), map={};
    (D().orders||[]).filter(function(o){ return !isSettledOrder(o) && (!q || clean(o.girl_name).toLowerCase().indexOf(q)>=0); }).forEach(function(o){ addSettlementOrder(map,o); });
    return Object.values(map).sort(function(a,b){return b.total-a.total;});
  }
  function rangeLabel(dates){
    var ds=Object.keys(dates||{}).filter(Boolean).sort();
    if(!ds.length) return "";
    return ds[0] === ds[ds.length-1] ? ds[0] : ds[0]+" 至 "+ds[ds.length-1];
  }
  function rowsForSave(){ var out=[]; groups().forEach(function(g){ Object.values(g.dates).forEach(function(r){ out.push({girl_name:g.girl,theoretical_amount:Number(r.total||0),actual_settlement:actual(r),formula_text:formula(r),order_ids:r.ids,girl_email:girlEmail(g.girl)}); }); }); return out; }
  window.setSettlementDraft=function(date,girl,field,value){ var k=key(date,girl); drafts[k]=drafts[k]||{}; drafts[k][field]=field==="actual_settlement"?Number(value||0):value; };
  window.toggleSettlementIds=function(ids,checked){ ids.map(Number).filter(Boolean).forEach(function(id){ checked?selected.add(id):selected.delete(id); }); renderSettlement(); };
  window.clearSettlementPick=function(){ selected.clear(); renderSettlement(); };
  function checkedSettlementIds(){
    var ids=new Set([].slice.call(selected).map(Number).filter(Boolean));
    document.querySelectorAll("#settlePatchRoot .check-settle:checked").forEach(function(c){
      clean(c.getAttribute("data-ids")).split(",").map(Number).filter(Boolean).forEach(function(id){ ids.add(id); });
    });
    return [].slice.call(ids);
  }
  function renderAllUnsettledSummary(){
    var box=document.getElementById("settleAllSummary"); if(!box)return;
    var rows=allUnsettledGroups(), total=0, actualTotal=0, count=0;
    var html='<div class="settle-summary-head"><h3>女孩全部未结算总额</h3><span class="note">不按通报日期过滤，已结算订单不会计入。</span></div><div class="table"><table><thead><tr><th>女孩</th><th>未结算总额</th><th>实际应结</th><th>未结算单数</th><th>日期数</th><th>日期范围</th><th>选择</th></tr></thead><tbody>';
    rows.forEach(function(g){
      var real=Number(g.total||0)-Number(g.nonCash||0), all=selectedAll(g.ids);
      total+=Number(g.total||0); actualTotal+=real; count+=Number(g.count||0);
      html+='<tr class="'+(all?'settle-picked':'')+'"><td>'+esc(g.girl)+(all?'<span class="settle-state">已勾选</span>':'')+'</td><td><b>'+money(g.total)+'</b></td><td><b>'+money(real)+'</b></td><td>'+g.count+'</td><td>'+Object.keys(g.dates||{}).length+'</td><td>'+esc(rangeLabel(g.dates))+'</td><td><input class="check-settle" type="checkbox" '+(all?'checked':'')+' data-ids="'+g.ids.join(",")+'"> 全选全部未结算</td></tr>';
    });
    if(!rows.length) html+='<tr><td colspan="7">暂无全部未结算金额</td></tr>';
    html+='<tr><td><b>全部女孩合计</b></td><td><b>'+money(total)+'</b></td><td><b>'+money(actualTotal)+'</b></td><td>'+count+'</td><td colspan="3"></td></tr></tbody></table></div>';
    box.innerHTML=html;
  }
  window.renderSettlement=function(){
    ensureStyle(); ensureSettlement(); var box=document.getElementById("settlementTable"); if(!box)return;
    var gs=groups(), theory=0, real=0, out='<div class="settle-pick-hint">已勾选 '+selected.size+' 单；勾选行会变灰，点“勾选改已结算”后会从未结算统计里移除。</div><div class="table"><table><thead><tr><th>展开</th><th>女孩</th><th>未结算单数</th><th>理论结算</th><th>结算公式</th><th>实际结算</th><th>邮箱</th><th>选择</th></tr></thead><tbody>';
    gs.forEach(function(g){ var k=encodeURIComponent(g.girl), dr=Object.values(g.dates).sort(function(a,b){return clean(b.date).localeCompare(clean(a.date));}), ga=dr.reduce(function(s,r){return s+actual(r);},0), all=selectedAll(g.ids); theory+=Number(g.total||0); real+=ga;
      out+='<tr class="'+(all?'settle-picked':'')+'"><td><button class="soft open-settle" data-open="'+esc(k)+'">'+(opened[k]?"收起":"展开")+'</button></td><td>'+esc(g.girl)+(all?'<span class="settle-state">已勾选</span>':'')+'</td><td>'+g.count+'</td><td><b>'+money(g.total)+'</b></td><td><small>'+esc(dr.map(formula).join("；"))+'</small></td><td><b>'+money(ga)+'</b></td><td>'+esc(girlEmail(g.girl)||"未填写")+'</td><td><input class="check-settle" type="checkbox" '+(all?"checked":"")+' data-ids="'+g.ids.join(",")+'"> 全选该女孩</td></tr>';
      if(opened[k]) dr.forEach(function(r){ var ck=selectedAll(r.ids); out+='<tr class="'+(ck?'settle-picked':'')+'"><td></td><td style="padding-left:28px">'+esc(g.girl)+(ck?'<span class="settle-state">已勾选</span>':'')+'</td><td>'+esc(r.date)+'｜'+r.count+'单</td><td>'+money(r.total)+'</td><td><input class="edit-settle" data-date="'+esc(r.date)+'" data-girl="'+esc(g.girl)+'" data-field="formula_text" style="min-width:220px" value="'+esc(formula(r))+'"></td><td><input class="edit-settle" data-date="'+esc(r.date)+'" data-girl="'+esc(g.girl)+'" data-field="actual_settlement" style="width:120px" value="'+actual(r)+'"></td><td>'+esc(girlEmail(g.girl)||"未填写")+'</td><td><input class="check-settle" type="checkbox" '+(ck?"checked":"")+' data-ids="'+r.ids.join(",")+'"> 选择这一天</td></tr>'; });
    });
    if(!gs.length) out+='<tr><td colspan="8">暂无未结算金额</td></tr>';
    out+='<tr><td colspan="3"><b>'+esc(rdate())+' 当日合计</b></td><td><b>'+money(theory)+'</b></td><td></td><td><b>'+money(real)+'</b></td><td colspan="2">老板邮箱：'+esc(D().settlement_boss_email||bossDefault)+'</td></tr></tbody></table></div>'; box.innerHTML=out;
    renderAllUnsettledSummary();
    var root=document.getElementById("settlePatchRoot")||box;
    root.querySelectorAll(".open-settle").forEach(function(b){ b.onclick=function(){ var k=b.getAttribute("data-open"); opened[k]=!opened[k]; renderSettlement(); }; });
    root.querySelectorAll(".check-settle").forEach(function(c){ c.onchange=function(){ toggleSettlementIds(clean(c.getAttribute("data-ids")).split(",").filter(Boolean), c.checked); }; });
    root.querySelectorAll(".edit-settle").forEach(function(e){ e.onchange=function(){ setSettlementDraft(e.getAttribute("data-date"),e.getAttribute("data-girl"),e.getAttribute("data-field"),e.value); if(e.getAttribute("data-field")==="actual_settlement")renderSettlement(); }; });
  };
  window.saveSettlementReports=async function(silent){ var items=rowsForSave(); if(!items.length){alert("当天没有可保存的未结算金额"); return null;} var r=await post("/api/settlements/save",{date:rdate(),items:items}); await loadReports(); renderSettlement(); if(!silent)alert("已保存实际结算："+(r.saved||items.length)+" 条"); return r; };
  window.sendDailySettlementReport=async function(){ var saved=await saveSettlementReports(true); if(!saved)return; if(!confirm("确认发送 "+rdate()+" 当日结算通报？\n老板邮箱："+(D().settlement_boss_email||bossDefault)+"\n女孩邮箱没填会自动跳过。"))return; var r=await post("/api/settlements/notify",{date:rdate()}); await loadReports(); renderSettlement(); var sg=(r.girls||[]).filter(function(x){return x.sent;}).length, sk=(r.girls||[]).length-sg, bt=r.boss&&r.boss.sent?"老板已发送":"老板邮件未发送："+((r.boss&&r.boss.reason)||"未知原因"); alert(bt+"\n女孩已发送："+sg+"\n女孩跳过/未发："+sk); };
  window.bulkSettleSettlement=async function(status){ var ids=checkedSettlementIds(); if(!ids.length){alert("请先勾选要修改的日期或女孩");return;} var idset=new Set(ids); await post("/api/orders/bulk_settle",{ids:ids,settlement_status:status}); (D().orders||[]).forEach(function(o){ if(idset.has(Number(o.id))) o.settlement_status=status; }); selected.clear(); if(typeof loadAll==="function")await loadAll(); await loadReports(); renderSettlement(); alert("勾选订单已改为"+status+"："+ids.length+"单"); };
  function durationLabel(seconds){
    seconds=Math.max(0,Number(seconds||0));
    var h=Math.floor(seconds/3600), m=Math.floor((seconds%3600)/60), s=Math.floor(seconds%60);
    if(h)return h+"小时"+String(m).padStart(2,"0")+"分";
    if(m)return m+"分"+String(s).padStart(2,"0")+"秒";
    return s+"秒";
  }
  function ensureLoginAudit(){
    if(!isBoss()){
      var oldBtn=document.getElementById("loginAuditNav"); if(oldBtn)oldBtn.remove();
      var oldSec=document.getElementById("loginAudit"); if(oldSec)oldSec.remove();
      return;
    }
    var nav=document.querySelector(".nav");
    if(nav&&!document.getElementById("loginAuditNav")){
      var btn=document.createElement("button");
      btn.id="loginAuditNav";
      btn.className="boss-only";
      btn.type="button";
      btn.textContent="🔐 登录检测";
      btn.onclick=function(){ if(typeof show==="function")show("loginAudit",btn); setTimeout(loadLoginAudit,50); };
      nav.appendChild(btn);
    }
    var main=document.querySelector("main.main")||document.querySelector("main");
    if(main&&!document.getElementById("loginAudit")){
      var sec=document.createElement("section");
      sec.id="loginAudit";
      sec.className="panel";
      sec.innerHTML='<div class="card"><h2>登录检测</h2><p class="note">老板专用：查看 IP、权限、在线时长、密码错误警报，并管理账号用户名和明码密码。</p><div class="form"><input id="loginAuditDate" type="date"><button class="soft" onclick="setLoginAuditToday()">今天</button><button class="primary" onclick="loadLoginAudit()">刷新登录记录</button></div><div id="loginAuditSummary" class="note"></div><div id="loginAuditTable"></div><h2 style="margin-top:18px">账号管理</h2><div class="user-admin-form"><input id="newUserName" placeholder="新用户名"><select id="newUserRole"><option value="admin">管理员</option><option value="user">普通用户</option></select><input id="newUserPass" type="text" placeholder="新密码"><button class="primary" onclick="createManagedUser()">新增账号</button></div><div id="userAdminTable"></div></div>';
      main.appendChild(sec);
      var d=document.getElementById("loginAuditDate");
      if(d&&!d.value)d.value=today();
    }
  }
  window.setLoginAuditToday=function(){ var d=document.getElementById("loginAuditDate"); if(d)d.value=today(); loadLoginAudit(); };
  window.loadLoginAudit=async function(){
    ensureLoginAudit();
    if(!isBoss()){alert("只有老板可以查看登录检测");return;}
    var d=document.getElementById("loginAuditDate");
    var day=(d&&d.value)||today();
    var table=document.getElementById("loginAuditTable"), summary=document.getElementById("loginAuditSummary");
    if(table)table.innerHTML='<p class="note">正在读取登录记录...</p>';
    var r=await fetch("/api/login_audit?date="+encodeURIComponent(day)+"&v="+Date.now(),{cache:"no-store",headers:H()});
    var j=await r.json().catch(function(){return {ok:false,error:"服务器返回不是JSON"};});
    if(!r.ok||j.ok===false){ if(table)table.innerHTML='<p class="note">读取失败：'+esc(j.error||r.status)+'</p>'; return; }
    var rows=j.sessions||[], s=j.summary||{};
    if(summary)summary.innerHTML='日期：<b>'+esc(day)+'</b>｜记录：<b>'+Number(s.total||0)+'</b>｜账号数：<b>'+Number(s.users||0)+'</b>｜IP数：<b>'+Number(s.ips||0)+'</b>｜在线中：<b>'+Number(s.active||0)+'</b>｜密码警报：<b class="login-alert-badge">'+Number(s.alerts||0)+'</b>';
    var html='<div class="table"><table><thead><tr><th>登录时间</th><th>账号</th><th>权限</th><th>IP</th><th>登录多久</th><th>最后活跃</th><th>退出时间</th><th>设备</th><th>状态</th><th>操作</th></tr></thead><tbody>';
    rows.forEach(function(x){
      var alert=x.is_alert||x.status==="password_alert";
      var active=x.status==="active"&&!x.logout_at;
      html+='<tr class="'+(alert?'login-alert-row':'')+'"><td>'+esc(x.login_at||"")+'</td><td>'+esc(x.username||"")+'</td><td>'+esc(x.role_label||x.role||"")+'</td><td>'+esc(x.ip||"")+'</td><td><b>'+durationLabel(x.duration_seconds)+'</b></td><td>'+esc(x.last_seen_at||"")+'</td><td>'+esc(x.logout_at||"")+'</td><td style="max-width:300px;white-space:normal">'+esc(x.user_agent||"")+'</td><td>'+(alert?'<span class="login-alert-badge">密码错3次</span>':esc(x.status_label||x.status||""))+'</td><td>'+(active?'<button class="soft" onclick="logoutSessionByUser('+js(x.username)+')">强制下线</button>':'')+'</td></tr>';
    });
    html+=(rows.length?'':'<tr><td colspan="10">这一天还没有登录记录</td></tr>')+'</tbody></table></div>';
    if(table)table.innerHTML=html;
    await loadUserAdmin();
  };
  window.loadUserAdmin=async function(){
    if(!isBoss())return;
    var box=document.getElementById("userAdminTable"); if(box)box.innerHTML='<p class="note">正在读取账号...</p>';
    var r=await fetch("/api/users?v="+Date.now(),{cache:"no-store",headers:H()});
    var j=await r.json().catch(function(){return {ok:false,error:"服务器返回不是JSON"};});
    if(!r.ok||j.ok===false){ if(box)box.innerHTML='<p class="note">账号读取失败：'+esc(j.error||r.status)+'</p>'; return; }
    renderUserAdmin(j.users||[]);
  };
  function renderUserAdmin(users){
    var box=document.getElementById("userAdminTable"); if(!box)return;
    var html='<div class="table"><table><thead><tr><th>用户名</th><th>权限</th><th>状态</th><th>在线</th><th>IP</th><th>在线时间</th><th>密码</th><th>管理</th></tr></thead><tbody>';
    users.forEach(function(u){
      var boss=u.role==="boss", online=Number(u.active_sessions||0)>0;
      html+='<tr data-username="'+esc(u.username)+'"><td><input class="managed-username" value="'+esc(u.username)+'"></td><td>'+(boss?'<b>老板</b>':'<select class="managed-role"><option value="admin" '+(u.role==="admin"?"selected":"")+'>管理员</option><option value="user" '+(u.role==="user"?"selected":"")+'>普通用户</option></select>')+'</td><td>'+(boss?'保护账号':'<select class="managed-active"><option value="1" '+(u.is_active?"selected":"")+'>启用</option><option value="0" '+(!u.is_active?"selected":"")+'>禁用</option></select>')+'</td><td class="'+(online?'user-online':'user-offline')+'">'+(online?'在线':'离线')+'</td><td>'+esc(u.ip||"")+'</td><td>'+durationLabel(u.online_seconds||0)+'</td><td><input class="managed-pass" type="text" value="'+esc(u.password||"")+'" placeholder="密码"></td><td><button class="soft save-managed-user">保存</button> <button class="soft logout-managed-user">强制下线</button> '+(boss?'老板账号不可删除':'<button class="danger delete-managed-user">删除账号</button>')+'</td></tr>';
    });
    html+=(users.length?'':'<tr><td colspan="8">还没有账号</td></tr>')+'</tbody></table></div>';
    box.innerHTML=html;
    box.querySelectorAll(".save-managed-user").forEach(function(b){ b.onclick=function(){ saveManagedUser(b.closest("tr")); }; });
    box.querySelectorAll(".logout-managed-user").forEach(function(b){ b.onclick=function(){ logoutManagedUser(b.closest("tr").getAttribute("data-username")); }; });
    box.querySelectorAll(".delete-managed-user").forEach(function(b){ b.onclick=function(){ deleteManagedUser(b.closest("tr").getAttribute("data-username")); }; });
  }
  window.createManagedUser=async function(){
    var username=clean((document.getElementById("newUserName")||{}).value), role=(document.getElementById("newUserRole")||{}).value||"user", password=(document.getElementById("newUserPass")||{}).value||"";
    if(!username||!password){alert("请填写用户名和密码");return;}
    await post("/api/users/save",{username:username,role:role,password:password,is_active:1});
    document.getElementById("newUserName").value=""; document.getElementById("newUserPass").value="";
    await loadUserAdmin(); alert("账号已新增");
  };
  window.saveManagedUser=async function(row){
    var oldUsername=row.getAttribute("data-username"), username=clean((row.querySelector(".managed-username")||{}).value||oldUsername), boss=row.querySelector(".managed-role")===null, role=boss?"boss":((row.querySelector(".managed-role")||{}).value||"user"), active=boss?1:Number((row.querySelector(".managed-active")||{}).value||1), password=(row.querySelector(".managed-pass")||{}).value||"";
    var res=await post("/api/users/save",{old_username:oldUsername,username:username,role:role,password:password,is_active:active});
    if(auth&&oldUsername===auth.username&&res&&res.username){ auth.username=res.username; auth.role=res.role||auth.role; auth.label=res.label||auth.label; localStorage.setItem("alice_auth",JSON.stringify(auth)); applyRole(); }
    await loadUserAdmin(); alert("账号已保存");
  };
  window.logoutManagedUser=async function(username){
    await post("/api/users/logout",{username:username});
    await loadLoginAudit(); alert("已强制下线："+username);
  };
  window.logoutSessionByUser=window.logoutManagedUser;
  window.deleteManagedUser=async function(username){
    if(!confirm("确认删除账号 "+username+"？删除后该账号会被强制下线。"))return;
    await post("/api/users/delete",{username:username});
    await loadLoginAudit(); alert("账号已删除："+username);
  };
  function patchLoginSession(){
    if(window.__loginAuditAuthPatched)return;
    window.__loginAuditAuthPatched=true;
    var oldAuthHeaders=window.authHeaders;
    window.authHeaders=function(){ var h=oldAuthHeaders?oldAuthHeaders():{}; try{ if(auth&&auth.session_token)h["X-Alice-Session"]=auth.session_token; if(auth&&auth.username)h["X-Alice-User"]=auth.username; }catch(e){} return h; };
    var oldApplyRole=window.applyRole;
    window.applyRole=function(){
      if(oldApplyRole)oldApplyRole();
      try{
        document.body.classList.toggle("role-boss", isBoss());
        var badge=document.getElementById("roleBadge");
        if(auth&&badge)badge.textContent=auth.role==="boss"?"老板模式":(auth.role==="admin"?"管理员模式":"普通用户模式");
        ensureLoginAudit();
      }catch(e){}
    };
    window.doLogin=async function(){
      var r=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username:loginUser.value,password:loginPass.value})});
      var j=await r.json().catch(function(){return {ok:false,error:"登录失败"};});
      if(!r.ok||!j.ok){alert(j.error||"登录失败");return;}
      auth={username:j.username,role:j.role,label:j.label,session_token:j.session_token||""};
      localStorage.setItem("alice_auth",JSON.stringify(auth));
      applyRole();
      await loadAll();
    };
    window.logout=async function(){
      try{ if(auth&&auth.session_token){ await fetch("/api/login/logout",{method:"POST",headers:Object.assign({"Content-Type":"application/json"},authHeaders()),body:JSON.stringify({session_token:auth.session_token})}); } }catch(e){}
      localStorage.removeItem("alice_auth"); auth=null; location.reload();
    };
    var oldApi=window.api;
    if(typeof oldApi==="function"&&!oldApi.__sessionGuarded){
      var guarded=async function(url, body){
        try{return await oldApi(url, body);}
        catch(e){
          if(e&&String(e.message||"").indexOf("重新登录")>=0){ localStorage.removeItem("alice_auth"); auth=null; alert(e.message); location.reload(); return; }
          throw e;
        }
      };
      guarded.__sessionGuarded=true;
      window.api=guarded;
    }
    try{ window.applyRole(); }catch(e){}
  }
  function wrapLoginAuditShow(){
    var oldShow=window.show;
    if(typeof oldShow==="function"&&!oldShow.__loginAuditWrapped){
      var showWrapped=function(id,btn){ if(id==="loginAudit"&&!isBoss()){alert("只有老板可以查看登录检测");return;} var r=oldShow.apply(this,arguments); if(id==="loginAudit")setTimeout(loadLoginAudit,50); return r; };
      showWrapped.__loginAuditWrapped=true;
      window.show=showWrapped;
    }
  }
  window.refreshChainFreeTimes=async function(){
    var free=document.getElementById("chainFreeOut");
    if(!free)return;
    var old=free.value;
    try{
      free.value="正在刷新空闲时间...";
      if(typeof loadChainPage==="function") await loadChainPage();
      else if(typeof refreshChainExports==="function") await refreshChainExports();
    }catch(e){
      free.value=old;
      alert("刷新空闲时间失败："+(e&&e.message?e.message:e));
    }
  };
  function ensureChainFreeRefresh(){
    var free=document.getElementById("chainFreeOut");
    if(!free || document.getElementById("chainFreeRefreshBtn"))return;
    var bar=free.nextElementSibling;
    if(!bar || bar.tagName!=="DIV"){
      bar=document.createElement("div");
      bar.style.marginTop="8px";
      free.parentNode.insertBefore(bar, free.nextSibling);
    }
    var btn=document.createElement("button");
    btn.id="chainFreeRefreshBtn";
    btn.type="button";
    btn.className="soft";
    btn.textContent="刷新空闲时间";
    btn.onclick=window.refreshChainFreeTimes;
    bar.insertBefore(btn, bar.firstChild);
    if(bar.children.length>1) bar.insertBefore(document.createTextNode(" "), bar.children[1]);
  }
  function wrapChainRefreshers(){
    var save=window.saveChainOrder;
    if(typeof save==="function"&&!save.__freeRefreshWrapped){
      var saveWrapped=async function(){ var r=await save.apply(this,arguments); setTimeout(window.refreshChainFreeTimes,0); return r; };
      saveWrapped.__freeRefreshWrapped=true;
      window.saveChainOrder=saveWrapped;
    }
    var cancel=window.cancelChainOrder;
    if(typeof cancel==="function"&&!cancel.__freeRefreshWrapped){
      var cancelWrapped=async function(){ var r=await cancel.apply(this,arguments); setTimeout(window.refreshChainFreeTimes,0); return r; };
      cancelWrapped.__freeRefreshWrapped=true;
      window.cancelChainOrder=cancelWrapped;
    }
  }
  var oldRender=window.render; if(typeof oldRender==="function"&&!oldRender.__settleWrapped){ var wrapped=function(){ var r=oldRender.apply(this,arguments); setTimeout(function(){ loadReports().then(function(){ ensureSettlement(); ensureEmailPanel(); ensureLoginAudit(); patchLoginSession(); wrapLoginAuditShow(); renderEmails(); ensureChainFreeRefresh(); wrapChainRefreshers(); if(document.getElementById("settlement")&&document.getElementById("settlement").classList.contains("on"))renderSettlement(); if(document.getElementById("loginAudit")&&document.getElementById("loginAudit").classList.contains("on"))loadLoginAudit(); }); },0); return r; }; wrapped.__settleWrapped=true; window.render=wrapped; }
  document.addEventListener("DOMContentLoaded",function(){ ensureStyle(); ensureSettlement(); ensureEmailPanel(); ensureLoginAudit(); patchLoginSession(); wrapLoginAuditShow(); ensureChainFreeRefresh(); wrapChainRefreshers(); loadReports().then(function(){ renderEmails(); if(document.getElementById("settlement")&&document.getElementById("settlement").classList.contains("on"))renderSettlement(); }); });
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
    import re as _re
    import secrets

    def _is_package_time(v):
        s = str(v or "")
        return "包夜" in s or "鍖呭" in s

    def _clock_parts(v):
        raw = str(v or "").strip()
        m = _re.search(r"(\d{1,2})(?:[:.](\d{1,2}))?", raw)
        if not m:
            return None
        h = int(m.group(1))
        mi = int(m.group(2) or 0)
        return h, min(mi, 59)

    def _interval_minutes(start_value, end_value):
        sp = _clock_parts(start_value)
        ep = _clock_parts(end_value)
        if not sp or not ep:
            return None
        sh, sm = sp
        eh, em = ep

        if sh >= 24:
            start = sh * 60 + sm
        elif sh >= 12:
            start = sh * 60 + sm
        elif sh == 0:
            start = 24 * 60 + sm
        elif sh <= 3 and eh <= 5:
            start = (24 + sh) * 60 + sm
        else:
            start = (12 + sh) * 60 + sm

        if eh >= 24:
            end = eh * 60 + em
        elif eh >= 12:
            end = eh * 60 + em
        elif eh == 0:
            end = 24 * 60 + em
        elif eh <= 5 and (sh >= 6 or sh >= 12 or sh <= 3):
            end = (24 + eh) * 60 + em
        else:
            end = (12 + eh) * 60 + em
        if end <= start:
            end += 24 * 60
        return start, end

    def _patched_parse_interval_text(text):
        t = str(text or "").strip()
        if not t:
            return None
        if _is_package_time(t):
            return (24 * 60, 29 * 60)
        m = _re.search(r"(\d{1,2}(?:[:.]\d{1,2})?)\s*(?:[-~ー－到至]\s*)(\d{1,2}(?:[:.]\d{1,2})?)", t)
        if not m:
            m = _re.search(r"(\d{1,2}(?:[:.]\d{1,2})?)\s*[-~]\s*(\d{1,2}(?:[:.]\d{1,2})?)", t)
        if not m:
            return None
        return _interval_minutes(m.group(1), m.group(2))

    def _patched_clock_to_minutes(v, default_end=False):
        raw = str(v or "").strip()
        if not raw:
            return None
        if _is_package_time(raw):
            return 29 * 60 if default_end else 24 * 60
        parts = _clock_parts(raw)
        if not parts:
            return None
        h, mi = parts
        if h == 0:
            h = 24
        elif h <= 3:
            h += 24
        elif h < 12:
            h += 12
        return h * 60 + mi

    def _patched_fmt_free_minute(m, is_end=False):
        if is_end and m >= 29 * 60:
            return "包夜"
        h = (m // 60) % 24
        mi = m % 60
        dh = h - 12 if 13 <= h <= 23 else h
        return f"{dh}.{mi:02d}" if mi else str(dh)

    old_build_chain_free_rows = getattr(module, "build_chain_free_rows", None)
    module._clock_to_minutes = _patched_clock_to_minutes
    module._parse_interval_text = _patched_parse_interval_text
    module._fmt_free_minute = _patched_fmt_free_minute
    if callable(old_build_chain_free_rows):
        def _patched_build_chain_free_rows(c, date_str):
            result = []
            for sft in module.pure_shift_rows_for_date(c, date_str):
                girl = sft.get("girl") or ""
                if _is_package_time(sft.get("start")) or _is_package_time(sft.get("end")):
                    base = (24 * 60, 29 * 60)
                else:
                    base = _interval_minutes(sft.get("start"), sft.get("end"))
                if not base:
                    continue
                busy = []
                for o in c.execute("""SELECT service_time FROM orders
                                    WHERE order_date=? AND girl_name=? AND COALESCE(order_status,'') NOT IN ('取消','鍙栨秷')""", (date_str, girl)).fetchall():
                    itv = _patched_parse_interval_text(o["service_time"])
                    if itv:
                        busy.append(itv)
                free = module._subtract_intervals(base, busy)
                if not free:
                    continue
                segments = "".join([f"{_patched_fmt_free_minute(a)}-{_patched_fmt_free_minute(b, True)}空" for a, b in free])
                result.append({"girl": girl, "segments": segments, "text": f"{girl}{segments}"})
            try:
                dt = module.datetime.strptime(date_str, "%Y-%m-%d")
                header = f"{dt.month:02d}{dt.day:02d}出勤"
            except Exception:
                header = f"{date_str}出勤"
            return {"header": header, "lines": result, "text": header + ("\n" + "\n".join(x["text"] for x in result) if result else "") + "\n\nhttps://ailisi99.com/"}
        module.build_chain_free_rows = _patched_build_chain_free_rows

    def _now_jst():
        return (datetime.utcnow() + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")

    def _client_ip():
        forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return forwarded or request.headers.get("X-Real-IP") or request.remote_addr or ""

    def _role_label(role, label=""):
        if label:
            return label
        return "老板" if role == "boss" else ("管理员" if role == "admin" else ("普通用户" if role == "user" else role))

    def _seconds_between(start, end):
        try:
            a = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
            b = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
            return max(0, int((b - a).total_seconds()))
        except Exception:
            return 0

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
            c.execute("""CREATE TABLE IF NOT EXISTS login_sessions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT DEFAULT '',
                role TEXT DEFAULT '',
                role_label TEXT DEFAULT '',
                ip TEXT DEFAULT '',
                user_agent TEXT DEFAULT '',
                session_token TEXT DEFAULT '',
                login_at TEXT DEFAULT '',
                last_seen_at TEXT DEFAULT '',
                logout_at TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_login_sessions_day ON login_sessions(login_at, username, ip)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_login_sessions_token ON login_sessions(session_token)")
            c.execute("""CREATE TABLE IF NOT EXISTS app_users(
                username TEXT PRIMARY KEY,
                password TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'user',
                label TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
            defaults = {
                "Star": {"password": "9941", "role": "boss", "label": "老板"},
                "admin": {"password": "admin123", "role": "admin", "label": "管理员"},
                "user": {"password": "user123", "role": "user", "label": "普通用户"},
            }
            try:
                defaults.update(getattr(module, "USERS", {}) or {})
                defaults["Star"] = {"password": "9941", "role": "boss", "label": "老板"}
            except Exception:
                pass
            boss_exists = c.execute("SELECT 1 FROM app_users WHERE role='boss' LIMIT 1").fetchone()
            for username, info in defaults.items():
                role = str(info.get("role") or "user")
                if role == "boss" and boss_exists and not c.execute("SELECT 1 FROM app_users WHERE username=? AND role='boss'", (username,)).fetchone():
                    continue
                c.execute("""INSERT OR IGNORE INTO app_users(username,password,role,label,is_active)
                             VALUES(?,?,?,?,1)""",
                          (username, str(info.get("password") or ""), role, _role_label(role, str(info.get("label") or ""))))

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

    def _app_user(c, username):
        return c.execute("SELECT * FROM app_users WHERE username=?", (username,)).fetchone()

    def _boss_required():
        role = (request.headers.get("X-Alice-Role") or request.args.get("role") or "").strip()
        if role != "boss":
            return jsonify(ok=False, error="只有老板可以操作"), 403
        token = (request.headers.get("X-Alice-Session") or "").strip()
        if not token:
            return jsonify(ok=False, error="登录已失效，请重新登录"), 401
        with conn() as c:
            row = c.execute("""SELECT username,role,status,logout_at FROM login_sessions WHERE session_token=?""", (token,)).fetchone()
            if not row or row["role"] != "boss" or row["status"] != "active" or row["logout_at"]:
                return jsonify(ok=False, error="只有老板可以操作"), 403
        return None

    def _status_label(status):
        return {
            "active": "在线",
            "logout": "已退出",
            "forced_logout": "老板强制下线",
            "replaced": "被新登录替换",
            "deleted": "账号已删除",
            "password_error": "密码错误",
            "password_alert": "密码错误警报",
        }.get(status or "", status or "")

    old_login_view = app.view_functions.get("api_login")
    if callable(old_login_view) and not getattr(old_login_view, "_login_audit_wrapped", False):
        def _login_audit_login(*args, **kwargs):
            schema()
            d = request.get_json(silent=True) or {}
            username = str(d.get("username") or "").strip()
            password = str(d.get("password") or "").strip()
            now = _now_jst()
            with conn() as c:
                info = _app_user(c, username)
                if not info or int(info["is_active"] or 0) != 1 or str(info["password"] or "") != password:
                    day = now[:10]
                    failures = c.execute("""SELECT COUNT(*) FROM login_sessions
                                            WHERE username=? AND ip=? AND substr(login_at,1,10)=?
                                              AND status IN ('password_error','password_alert')""",
                                         (username, _client_ip(), day)).fetchone()[0] + 1
                    status = "password_alert" if failures >= 3 else "password_error"
                    role = str(info["role"] if info else "")
                    label = _role_label(role, str(info["label"] if info else ""))
                    c.execute("""INSERT INTO login_sessions(username,role,role_label,ip,user_agent,session_token,login_at,last_seen_at,logout_at,status)
                                 VALUES(?,?,?,?,?,?,?,?,?,?)""",
                              (username, role, label, _client_ip(), request.headers.get("User-Agent", ""), "", now, now, now, status))
                    msg = "密码错误已达三次，已记录登录警报" if status == "password_alert" else "用户名或密码错误"
                    return jsonify(ok=False, error=msg, alert=(status == "password_alert")), 401
                role = str(info["role"] or "")
                label = _role_label(role, str(info["label"] or ""))
                token = secrets.token_urlsafe(24)
                c.execute("""UPDATE login_sessions SET logout_at=?,last_seen_at=?,status='replaced',updated_at=CURRENT_TIMESTAMP
                             WHERE username=? AND status='active' AND COALESCE(logout_at,'')=''""", (now, now, username))
                c.execute("""INSERT INTO login_sessions(username,role,role_label,ip,user_agent,session_token,login_at,last_seen_at,status)
                             VALUES(?,?,?,?,?,?,?,?,?)""",
                          (username, role, label, _client_ip(), request.headers.get("User-Agent", ""), token, now, now, "active"))
            return jsonify(ok=True, username=username, role=role, label=label, session_token=token)
        _login_audit_login._login_audit_wrapped = True
        app.view_functions["api_login"] = _login_audit_login

    @app.before_request
    def _login_audit_touch():
        if not request.path.startswith("/api/") or request.path in ("/api/login", "/api/login/logout"):
            return None
        public_api = {"/api/health", "/api/db_info", "/api/customer_register", "/api/customer_login", "/api/customer_available", "/api/customer_reserve"}
        if request.path in public_api:
            return None
        token = (request.headers.get("X-Alice-Session") or "").strip()
        role = (request.headers.get("X-Alice-Role") or request.args.get("role") or "").strip()
        username = (request.headers.get("X-Alice-User") or "").strip()
        if role in ("boss", "admin", "user") and not token:
            return jsonify(ok=False, error="登录已失效，请重新登录"), 401
        try:
            now = _now_jst()
            with conn() as c:
                row = c.execute("""SELECT username,role,status,logout_at FROM login_sessions WHERE session_token=?""", (token,)).fetchone()
                if role in ("boss", "admin", "user") and (not row or row["status"] != "active" or row["logout_at"]):
                    return jsonify(ok=False, error="账号已在其他地方登录或被下线，请重新登录"), 401
                if row and role and row["role"] != role:
                    return jsonify(ok=False, error="登录权限不一致，请重新登录"), 401
                if row and username and row["username"] != username:
                    return jsonify(ok=False, error="登录身份不一致，请重新登录"), 401
                c.execute("""UPDATE login_sessions SET last_seen_at=?,status='active',updated_at=CURRENT_TIMESTAMP
                             WHERE session_token=? AND COALESCE(logout_at,'')=''""", (now, token))
        except Exception:
            if role in ("boss", "admin", "user"):
                return jsonify(ok=False, error="登录检测失败，请重新登录"), 401
        return None

    @app.route("/api/login/logout", methods=["POST"])
    def _login_audit_logout():
        schema()
        d = request.json or {}
        token = str(d.get("session_token") or request.headers.get("X-Alice-Session") or "").strip()
        if token:
            now = _now_jst()
            with conn() as c:
                c.execute("""UPDATE login_sessions SET logout_at=?,last_seen_at=?,status='logout',updated_at=CURRENT_TIMESTAMP
                             WHERE session_token=? AND COALESCE(logout_at,'')=''""", (now, now, token))
        return jsonify(ok=True)

    @app.route("/api/login_audit", methods=["GET"])
    def _login_audit_list():
        schema()
        role = (request.headers.get("X-Alice-Role") or request.args.get("role") or "").strip()
        if role != "boss":
            return jsonify(ok=False, error="只有老板可以查看登录检测"), 403
        day = str(request.args.get("date") or _now_jst()[:10]).strip()[:10]
        with conn() as c:
            data = rows(c.execute("""SELECT id,username,role,role_label,ip,user_agent,login_at,last_seen_at,logout_at,status
                                     FROM login_sessions
                                     WHERE substr(login_at,1,10)=?
                                     ORDER BY login_at DESC,id DESC""", (day,)).fetchall())
        now = _now_jst()
        for row in data:
            end = row.get("logout_at") or (now if row.get("status") == "active" else row.get("last_seen_at")) or now
            row["duration_seconds"] = _seconds_between(row.get("login_at") or end, end)
            row["is_alert"] = row.get("status") == "password_alert"
            row["status_label"] = _status_label(row.get("status"))
        summary = {
            "total": len(data),
            "users": len(set(x.get("username") or "" for x in data if x.get("username"))),
            "ips": len(set(x.get("ip") or "" for x in data if x.get("ip"))),
            "active": len([x for x in data if x.get("status") == "active" and not x.get("logout_at")]),
            "alerts": len([x for x in data if x.get("status") == "password_alert"]),
        }
        return jsonify(ok=True, date=day, sessions=data, summary=summary)

    @app.route("/api/users", methods=["GET"])
    def _users_list():
        schema()
        denied = _boss_required()
        if denied:
            return denied
        now = _now_jst()
        with conn() as c:
            users = rows(c.execute("""SELECT username,password,role,label,is_active,created_at,updated_at
                                      FROM app_users
                                      ORDER BY CASE role WHEN 'boss' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, username COLLATE NOCASE""").fetchall())
            active_rows = rows(c.execute("""SELECT username,ip,login_at,last_seen_at,COUNT(*) AS active_sessions
                                            FROM login_sessions
                                            WHERE status='active' AND COALESCE(logout_at,'')=''
                                            GROUP BY username
                                            ORDER BY MAX(last_seen_at) DESC""").fetchall())
        active_map = {r["username"]: r for r in active_rows}
        for u in users:
            a = active_map.get(u["username"]) or {}
            u["role_label"] = _role_label(u.get("role"), u.get("label"))
            u["is_active"] = bool(u.get("is_active"))
            u["active_sessions"] = int(a.get("active_sessions") or 0)
            u["ip"] = a.get("ip") or ""
            u["login_at"] = a.get("login_at") or ""
            u["last_seen_at"] = a.get("last_seen_at") or ""
            u["online_seconds"] = _seconds_between(a.get("login_at") or now, now) if a else 0
        return jsonify(ok=True, users=users)

    @app.route("/api/users/save", methods=["POST"])
    def _users_save():
        schema()
        denied = _boss_required()
        if denied:
            return denied
        d = request.json or {}
        old_username = str(d.get("old_username") or d.get("username") or "").strip()
        username = str(d.get("username") or "").strip()
        password = str(d.get("password") or "")
        role = str(d.get("role") or "user").strip()
        is_active = 1 if str(d.get("is_active", 1)) not in ("0", "false", "False") else 0
        if not username:
            return jsonify(ok=False, error="缺少用户名"), 400
        if role not in ("boss", "admin", "user"):
            return jsonify(ok=False, error="权限不正确"), 400
        with conn() as c:
            old = _app_user(c, old_username or username)
            if old:
                old_username = old["username"]
                old_role = str(old["role"] or "user")
                if old_role == "boss":
                    role = "boss"
                    is_active = 1
                elif role == "boss":
                    return jsonify(ok=False, error="不能新增或升级老板账号"), 400
                if username != old_username and _app_user(c, username):
                    return jsonify(ok=False, error="用户名已存在"), 400
                final_password = password if password else str(old["password"] or "")
                label = _role_label(role)
                if password:
                    final_password = password
                c.execute("""UPDATE app_users SET username=?,password=?,role=?,label=?,is_active=?,updated_at=CURRENT_TIMESTAMP
                             WHERE username=?""", (username, final_password, role, label, is_active, old_username))
                if username != old_username:
                    c.execute("""UPDATE login_sessions SET username=?,updated_at=CURRENT_TIMESTAMP
                                 WHERE username=? AND status='active' AND COALESCE(logout_at,'')=''""", (username, old_username))
            else:
                if role == "boss":
                    return jsonify(ok=False, error="不能新增老板账号"), 400
                if not password:
                    return jsonify(ok=False, error="新增账号必须填写密码"), 400
                c.execute("""INSERT INTO app_users(username,password,role,label,is_active)
                             VALUES(?,?,?,?,?)""", (username, password, role, _role_label(role), is_active))
            if not is_active:
                now = _now_jst()
                c.execute("""UPDATE login_sessions SET logout_at=?,last_seen_at=?,status='forced_logout',updated_at=CURRENT_TIMESTAMP
                             WHERE username=? AND status='active' AND COALESCE(logout_at,'')=''""", (now, now, username))
        return jsonify(ok=True, username=username, role=role, label=_role_label(role))

    @app.route("/api/users/logout", methods=["POST"])
    def _users_logout():
        schema()
        denied = _boss_required()
        if denied:
            return denied
        username = str((request.json or {}).get("username") or "").strip()
        if not username:
            return jsonify(ok=False, error="缺少用户名"), 400
        now = _now_jst()
        with conn() as c:
            c.execute("""UPDATE login_sessions SET logout_at=?,last_seen_at=?,status='forced_logout',updated_at=CURRENT_TIMESTAMP
                         WHERE username=? AND status='active' AND COALESCE(logout_at,'')=''""", (now, now, username))
        return jsonify(ok=True)

    @app.route("/api/users/delete", methods=["POST"])
    def _users_delete():
        schema()
        denied = _boss_required()
        if denied:
            return denied
        username = str((request.json or {}).get("username") or "").strip()
        if not username:
            return jsonify(ok=False, error="缺少用户名"), 400
        with conn() as c:
            old = _app_user(c, username)
            if not old:
                return jsonify(ok=True)
            if old["role"] == "boss":
                return jsonify(ok=False, error="默认老板账号不可删除"), 400
            now = _now_jst()
            c.execute("DELETE FROM app_users WHERE username=?", (username,))
            c.execute("""UPDATE login_sessions SET logout_at=?,last_seen_at=?,status='deleted',updated_at=CURRENT_TIMESTAMP
                         WHERE username=? AND status='active' AND COALESCE(logout_at,'')=''""", (now, now, username))
        return jsonify(ok=True)

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
                    body = body.replace("</body>", '<script src="/alice_settlement_patch.js?v=20260701d"></script></body>')
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
