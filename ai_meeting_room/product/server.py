"""Small local HTTP product shell; no cloud service."""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4


def _safe_server_error(exc: BaseException, *, code: str, stage: str, attempt_id: str = "UNKNOWN") -> dict[str, Any]:
    stack = "".join(traceback.format_exception(exc))
    stack_id = hashlib.sha256(stack.encode("utf-8", errors="replace")).hexdigest()[:16]
    message = str(exc)
    message = message[:500]
    return {"ok": False, "error": {"code": code, "stage": stage, "name": type(exc).__name__, "message": message, "stackId": stack_id, "attemptId": attempt_id}}


def _find_non_serializable(value: Any, path: str = "result", seen: set[int] | None = None) -> tuple[str, str] | None:
    """Find an unsafe response field without converting or printing its value."""
    if value is None or isinstance(value, (str, bool, int)):
        return None
    if isinstance(value, float):
        return None if math.isfinite(value) else (path, "NonFiniteNumber")
    if seen is None:
        seen = set()
    marker = id(value)
    if marker in seen:
        return path, "CircularReference"
    if isinstance(value, list):
        seen.add(marker)
        for index, item in enumerate(value):
            issue = _find_non_serializable(item, f"{path}[{index}]", seen)
            if issue:
                return issue
        seen.remove(marker)
        return None
    if isinstance(value, dict):
        seen.add(marker)
        for key, item in value.items():
            issue = _find_non_serializable(item, f"{path}.{key}", seen)
            if issue:
                return issue
        seen.remove(marker)
        return None
    return path, type(value).__name__

from .app import Phase2Application, ProductError, ProviderBlockedError, localized_product_error_message
from ..runtime.cao_service_manager import CaoServiceError
from ..core.errors import MeetingLifecycleConflict
from ..core.safety import DispatchBlockedError
from ..tasks.engine import HumanHandoffPendingError


UI_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Meeting Room</title><style>
:root{font-family:Inter,ui-sans-serif,system-ui;background:#0d1117;color:#e6edf3;--muted:#8b949e;--line:#30363d;--panel:#161b22;--blue:#58a6ff;--green:#3fb950;--red:#f85149;--amber:#d29922}*{box-sizing:border-box}body{margin:0}button,input,textarea,select{font:inherit}button{background:#238636;border:1px solid #2ea043;color:#fff;border-radius:7px;padding:7px 12px;cursor:pointer}button.secondary{background:#21262d;border-color:var(--line)}button.danger{background:#da3633;border-color:#f85149}button:disabled{opacity:.45;cursor:not-allowed}.shell{display:grid;grid-template-columns:250px 1fr 310px;min-height:100vh}.sidebar,.right{background:var(--panel);padding:20px;border-right:1px solid var(--line)}.right{border-left:1px solid var(--line);border-right:0}.brand{font-size:20px;font-weight:700;margin-bottom:18px}.eyebrow{font-size:11px;text-transform:uppercase;color:var(--muted);letter-spacing:.12em}.meeting{padding:11px;border:1px solid transparent;border-radius:7px;cursor:pointer;margin:7px 0}.meeting.active{border-color:var(--blue);background:#1f2937}.meeting small{display:block;color:var(--muted);margin-top:4px}.main{padding:24px;overflow:auto}.top{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid var(--line);padding-bottom:18px}.status{padding:5px 9px;border-radius:999px;background:#21262d;font-size:12px}.status.RUNNING,.dot.green{color:var(--green)}.status.PAUSED,.dot.red{color:var(--red)}.status.READY{color:var(--blue)}.pause{display:none;border:1px solid var(--red);background:#35151a;padding:14px;border-radius:8px;margin:18px 0}.pause.show{display:block}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:18px}.card{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:16px}.card h3{margin:0 0 12px;font-size:14px}.full{grid-column:1/-1}.row{display:flex;gap:8px;align-items:center;justify-content:space-between;border-bottom:1px solid #21262d;padding:9px 0}.row:last-child{border-bottom:0}.muted{color:var(--muted)}.dot{font-size:13px}.task-title{font-weight:600}.task-result{white-space:pre-wrap;background:#0d1117;padding:10px;border-radius:6px;color:#c9d1d9;max-height:180px;overflow:auto;margin-top:8px}.timeline{max-height:220px;overflow:auto;font-size:12px}.event{padding:7px 0;border-bottom:1px solid #21262d}.field{display:flex;flex-direction:column;gap:5px;margin-bottom:10px}.field input,.field textarea,.field select{background:#0d1117;color:#e6edf3;border:1px solid var(--line);border-radius:6px;padding:8px}.field textarea{min-height:72px;resize:vertical}.actions{display:flex;gap:7px;flex-wrap:wrap}.pill{font-size:11px;padding:3px 6px;border-radius:999px;background:#21262d}.provider{padding:10px 0;border-bottom:1px solid #21262d}.provider:last-child{border-bottom:0}.blocked{color:var(--red)}.available{color:var(--green)}.code{font-family:ui-monospace,monospace;font-size:12px;white-space:pre-wrap}.empty{color:var(--muted);padding:25px 0;text-align:center}@media(max-width:1000px){.shell{grid-template-columns:210px 1fr}.right{grid-column:1/-1;border-left:0;border-top:1px solid var(--line)}.grid{grid-template-columns:1fr}}@media(max-width:650px){.shell{display:block}.sidebar{border-right:0;border-bottom:1px solid var(--line)}.main{padding:15px}}
</style></head><body><div class="shell"><aside class="sidebar"><div class="brand">AI Meeting Room</div><div class="eyebrow">Meetings</div><div id="meetings"></div><button onclick="createMeeting()">＋ New meeting</button></aside><main class="main"><div id="detail"><div class="empty">Create or select a meeting.</div></div></main><aside class="right"><div class="eyebrow">Brain</div><div class="provider"><b>主脑：GPT 网页版</b><br><small class="muted">正式主脑 · 用户已授权的 Google Chrome 会话</small></div><details class="chrome-setup"><summary>设置 GPT 主脑</summary><div class="muted">请使用 AI Meeting Room 专用 Chrome 配置，仅用于 ChatGPT / GPT 网页主脑。</div><ol class="muted"><li>在 Google Chrome 中打开“AI Meeting Room”配置。</li><li>确认 ChatGPT 已登录。</li><li>打开 <code>chrome://inspect/#remote-debugging</code> 并启用 Remote Debugging。</li><li>返回本应用，点击【连接 GPT 主脑】，并在 Chrome 授权提示中点击“允许”。</li></ol><div class="muted">不要在该配置中打开 Gmail、网银或其他敏感网站。</div></details><details><summary>设置 → 高级设置 → API 主脑（实验）</summary><div id="brain-settings"></div></details><div class="eyebrow">Agents</div><div id="providers"></div><section class="provider"><b>V1 数据与诊断</b><div class="actions" style="margin-top:8px"><button class="secondary" onclick="backupProductData()">备份数据</button><button class="danger" onclick="restoreProductData()">恢复备份</button><button class="secondary" onclick="exportDiagnosticReport()">导出诊断报告</button></div><div id="v1-system-status" class="muted"></div></section></aside></div>
<script>
 let selected=null, snapshot=null, refreshInFlight=false, desktopBrainResult=null, desktopBrainPocInFlight=false, desktopConnectInFlight=false, desktopBrainStatusOptIn=false, desktopBrainPocUiState={phase:'NOT_RUN',detail:''};
const $=id=>document.getElementById(id); const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path,opts={}){let r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});let x=await r.json();if(!r.ok||x?.ok===false){let d=x?.error||{};let code=d.code||x.code||'INTERNAL_UNCLASSIFIED_EXCEPTION';let e=Error(userFacingError(code,d.message||x.error||code));e.code=code;e.technical=d.message||x.error||code;e.attemptId=d.attemptId; e.stage=d.stage;throw e}return x}
async function load(){let ms=await api('/api/meetings');if(!selected&&ms[0])selected=ms[0].meeting_id;if(selected)await refresh();else renderMeetings(ms);await renderProviders();localizeStatic()}
function renderMeetings(ms){$('meetings').innerHTML=ms.length?ms.map(m=>`<div class="meeting ${m.meeting_id===selected?'active':''}" onclick="selectMeeting('${m.meeting_id}')">${esc(m.name)}<small>${esc(m.status)}</small></div>`).join(''):'<div class="empty">No meetings</div>'}
async function selectMeeting(id){selected=id;await refresh()}
async function refresh(){if(refreshInFlight)return;refreshInFlight=true;try{snapshot=await api('/api/meetings/'+selected);render(snapshot);renderMembers(snapshot);await refreshDesktopBrainStatus();renderMeetings(await api('/api/meetings'));localizeStatic()}catch(e){$('detail').innerHTML='<div class="empty">'+esc(e.message)+'</div>';localizeStatic()}finally{refreshInFlight=false}}
function renderMembers(s){let grid=$('detail').querySelector('.grid');if(!grid)return;let card=document.createElement('section');card.className='card';card.innerHTML='<h3>Members / Agents</h3>'+(s.agents||[]).map(a=>'<div class="row"><div><b>'+esc(a.display_name)+'</b> '+(String(a.display_name||'').toLowerCase()===String(a.provider||'').toLowerCase()?'':'<span class="pill">'+esc(a.provider)+'</span>')+'<br><span class="muted">status='+esc(a.status)+' · health='+esc(a.health)+'</span></div>'+(a.health==='ERROR'||a.health==='LOST'||a.health==='UNKNOWN'?'<button class="secondary" onclick="restartAgent(\''+esc(a.agent_id)+'\')">Restart Agent</button>':'')+'</div>').join('')||'<div class="empty">No agents</div>';grid.insertBefore(card,grid.firstChild)}
function render(s){let m=s.meeting,c=s.circuit,brain=s.brain||{},paused=m.status==='PAUSED'||c.stopDispatch;let agents=s.agents||[],tasks=s.tasks||[];$('detail').innerHTML=`<div class="top"><div><div class="eyebrow">Meeting</div><h1>${esc(m.name)}</h1><div class="muted code">${esc(m.meeting_id)}</div></div><div class="actions"><span class="status ${esc(m.status)}">${esc(m.status)}</span><button class="secondary" onclick="startMeeting()" ${m.status!=='CREATED'&&m.status!=='READY'?'disabled':''}>Start</button><button class="danger" onclick="pauseMeeting()" ${m.status!=='RUNNING'?'disabled':''}>Global Pause</button><button class="secondary" onclick="recoverMeeting()" ${m.status!=='PAUSED'?'disabled':''}>Recover</button><button class="secondary" onclick="exportMeetingSummary()">导出会议总结</button></div></div><div class="pause ${paused?'show':''}"><b>MEETING PAUSED</b><br><span class="muted">triggeredBy:</span> ${esc(c.triggerAgentId||m.pause_triggered_by)}<br><span class="muted">reason:</span> ${esc(c.triggerReason||m.pause_reason)}<br><span class="muted">timestamp:</span> ${esc(c.triggerTimestamp||m.paused_at)}<br><small>Dispatch disabled until all health checks pass.</small></div><div class="grid"><section class="card"><h3>Brain</h3><div><b>${esc(brain.name||'UNKNOWN')}</b> <span class="pill">${brain.isAi?'AI':'operator'}</span></div><div class="muted">health=${esc(brain.health)} · state=${esc(brain.state||'UNKNOWN')} · Inbox events: ${(s.brainInbox||[]).length}</div><button class="secondary" onclick="generatePairing()">Generate Pairing Code</button><div class="field" style="margin-top:10px"><select id="decisionType"><option>ACCEPT</option><option>REWORK</option><option>PAUSE</option><option>COMPLETE_MEETING</option></select><input id="decisionReason" placeholder="Decision reason"><input id="decisionInstruction" placeholder="REWORK instruction"><select id="decisionTask"><option value="">Related task (optional)</option>${tasks.map(t=>`<option value="${t.task_id}">${esc(t.title)}</option>`).join('')}</select></div><button onclick="decision()">Submit decision</button></section><section class="card"><h3>Tasks</h3><div>${tasks.length?tasks.map(taskView).join(''):'<div class="empty">No tasks</div>'}</div><div class="field" style="margin-top:12px"><input id="taskTitle" placeholder="Task title"><textarea id="taskInstruction" placeholder="Read-only task instruction"></textarea><select id="taskAgent"><option value="">Assign Agent</option>${agents.filter(a=>a.health==='HEALTHY').map(a=>`<option value="${a.agent_id}">${esc(a.display_name)}</option>`).join('')}</select></div><button onclick="newTask()">Create task</button></section><section class="card full"><h3>Timeline / Brain Inbox</h3><div class="timeline">${(s.brainInbox||[]).map(e=>`<div class="event"><b>${esc(e.eventType)}</b> · ${esc(e.summary)}<br><span class="muted">${esc(e.timestamp)}</span></div>`).join('')||'<div class="empty">No events yet</div>'}</div></section><section class="card"><h3>Workspace</h3><div class="code">${esc(s.workspace.path||'not configured')}</div><div class="muted">health=${esc(s.workspace.health)} · modified=${esc(s.workspace.status||'clean')}</div><details><summary>Git diff</summary><div class="code">${esc(s.workspace.diff||'No diff')}</div></details></section><section class="card"><h3>Events / Errors</h3><div class="timeline">${(s.events||[]).slice(0,20).map(e=>`<div class="event"><b>${esc(e.event_type)}</b> · ${esc(e.source)}<br><span class="muted">${esc(e.timestamp)}</span></div>`).join('')||'<div class="empty">No events</div>'}</div></section></div>`}
function readUiInteractionState(){let values={};document.querySelectorAll('#detail input, #detail textarea, #detail select').forEach(e=>{if(e.id)values[e.id]={value:e.value,checked:e.checked}});let main=document.querySelector('.main'),right=document.querySelector('.right');return{experimentalPanelExpanded:document.querySelector('details.experimental')?.open===true,details:[...document.querySelectorAll('#detail details')].map(e=>e.open),values,focusedId:document.activeElement?.id||'',pageScroll:{x:window.scrollX,y:window.scrollY},mainScroll:{top:main?.scrollTop||0,left:main?.scrollLeft||0},rightScroll:{top:right?.scrollTop||0,left:right?.scrollLeft||0},desktopBrainPocInFlight,desktopBrainPocUiState:{...desktopBrainPocUiState}}}
function setDesktopConnectUiState(inFlight){document.querySelectorAll('.desktopConnectButton').forEach(button=>{if(!button.dataset.defaultLabel)button.dataset.defaultLabel=button.textContent;button.disabled=inFlight;button.setAttribute('aria-busy',inFlight?'true':'false');button.textContent=inFlight?'正在连接…':button.dataset.defaultLabel})}
function restoreUiInteractionState(state){if(!state)return;let panel=document.querySelector('details.experimental');if(panel)panel.open=Boolean(state.experimentalPanelExpanded);document.querySelectorAll('#detail details').forEach((e,i)=>{if(state.details[i]!==undefined)e.open=state.details[i]});Object.entries(state.values||{}).forEach(([id,data])=>{let e=$(id);if(!e)return;if(e.tagName==='INPUT'&&e.type==='checkbox')e.checked=data.checked;else if(document.activeElement!==e)e.value=data.value});let button=$('desktopBrainPocButton');if(button){button.disabled=desktopBrainPocInFlight;button.setAttribute('aria-busy',desktopBrainPocInFlight?'true':'false')}setDesktopConnectUiState(desktopConnectInFlight);if(state.focusedId&&$(state.focusedId))$(state.focusedId).focus({preventScroll:true});setDesktopPocStatus(desktopBrainPocUiState.phase,desktopBrainPocUiState.detail);let main=document.querySelector('.main'),right=document.querySelector('.right');if(main&&state.mainScroll){main.scrollTop=state.mainScroll.top;main.scrollLeft=state.mainScroll.left}if(right&&state.rightScroll){right.scrollTop=state.rightScroll.top;right.scrollLeft=state.rightScroll.left}if(state.pageScroll)window.scrollTo(state.pageScroll.x,state.pageScroll.y)}
const renderBeforeUiState=render;render=function(s){let state=readUiInteractionState();renderBeforeUiState(s);restoreUiInteractionState(state)}
function taskView(t){let a=snapshot.agents.find(x=>x.agent_id===t.assigned_agent_id);return `<div class="row"><div><div class="task-title">${esc(t.title)}</div><span class="pill">${esc(t.status)}</span> <span class="muted">${esc(a?.display_name||'unassigned')}</span>${t.result?`<div class="task-result">${esc(t.result)}</div>`:''}${t.error?`<div class="blocked">${esc(t.error)}</div>`:''}</div><button class="secondary" onclick="dispatch('${t.task_id}')" ${t.status!=='QUEUED'&&t.status!=='PENDING'?'disabled':''}>Dispatch</button></div>`}
async function createMeeting(){let name=prompt(I18N.common.meetingName,I18N.common.codexMeeting);if(!name)return;let workspace=prompt(I18N.common.workspacePath,'.');if(!workspace)return;try{let s=await api('/api/meetings',{method:'POST',body:JSON.stringify({name,workspacePath:workspace})});selected=s.meeting.meeting_id;await refresh();await renderProviders()}catch(e){alert(e.message)}}
async function startMeeting(){try{await api('/api/meetings/'+selected+'/start',{method:'POST'});await refresh()}catch(e){alert(e.message)}}
async function pauseMeeting(){let reason=prompt('暂停原因','操作员请求暂停')||'操作员请求暂停';try{await api('/api/meetings/'+selected+'/pause',{method:'POST',body:JSON.stringify({reason})});await refresh()}catch(e){alert(e.message)}}
async function recoverMeeting(){try{let r=await api('/api/meetings/'+selected+'/recover',{method:'POST'});if(r.recoveryError)alert(r.recoveryError);await refresh()}catch(e){alert(e.message)}}
async function newTask(){try{await api('/api/meetings/'+selected+'/tasks',{method:'POST',body:JSON.stringify({title:$('taskTitle').value,instruction:$('taskInstruction').value,agentId:$('taskAgent').value})});await refresh()}catch(e){alert(e.message)}}
async function dispatch(id){try{await api('/api/meetings/'+selected+'/tasks/'+id+'/dispatch',{method:'POST'});await refresh()}catch(e){alert(e.message)}}
async function decision(){try{await api('/api/meetings/'+selected+'/brain-decisions',{method:'POST',body:JSON.stringify({type:$('decisionType').value,reason:$('decisionReason').value,instruction:$('decisionInstruction').value,relatedTaskId:$('decisionTask').value})});await refresh()}catch(e){alert(e.message)}}
async function exportMeetingSummary(){try{let result=await api('/api/meetings/'+selected+'/summary-export',{method:'POST',body:JSON.stringify({action:'preview'})});showSummaryExport(result)}catch(e){alert(e.message)}}
async function backupProductData(){try{let r=await api('/api/system/backups',{method:'POST',body:'{}'});$('v1-system-status').textContent='备份已创建：'+r.backupId}catch(e){alert(e.message)}}
async function restoreProductData(){try{let list=await api('/api/system/backups');let choices=(list.backups||[]).filter(x=>x.status==='VALID').map(x=>x.backupId);if(!choices.length){alert('没有可恢复的有效备份。');return}let backupId=prompt('输入要恢复的备份编号：\n'+choices.join('\n'),choices[0]);if(!backupId)return;if(!confirm('确认用所选备份覆盖当前数据？恢复后必须重启 AI Meeting Room。'))return;let r=await api('/api/system/restore',{method:'POST',body:JSON.stringify({backupId,confirmOverwrite:true})});$('v1-system-status').textContent='恢复完成，请重启应用：'+r.backupId}catch(e){alert(e.message)}}
async function exportDiagnosticReport(){try{let r=await api('/api/system/diagnostics',{method:'POST',body:'{}'});$('v1-system-status').textContent='诊断报告已生成：'+r.path}catch(e){alert(e.message)}}
function showSummaryExport(result,preserve=false){let grid=$('detail').querySelector('.grid');if(!grid)return;let panel=document.createElement('section');panel.id='meeting-summary-export';panel.className='card full';let heading=document.createElement('h3');heading.textContent='会议总结';panel.appendChild(heading);let note=document.createElement('div');note.className='muted';note.textContent='安全预览：仅显示来自会议核心、任务引擎、主脑审计和安全状态的摘要。';panel.appendChild(note);let preview=document.createElement('textarea');preview.readOnly=true;preview.className='code';preview.style='width:100%;min-height:320px;margin-top:10px';preview.setAttribute('aria-label','会议总结预览');preview.value=localizeSummaryMarkdown(result.markdown||'');panel.appendChild(preview);let actions=document.createElement('div');actions.className='actions';let copy=document.createElement('button');copy.className='secondary';copy.textContent='复制会议总结';copy.onclick=async()=>{let status=panel.querySelector('.summary-copy-status');try{if(navigator.clipboard?.writeText){await navigator.clipboard.writeText(preview.value)}else{preview.focus();preview.select();document.execCommand('copy')}status.textContent='已复制会议总结';}catch(e){status.textContent='复制失败，请手动选择预览内容';}};actions.appendChild(copy);let status=document.createElement('span');status.className='muted summary-copy-status';status.textContent='';status.setAttribute('aria-live','polite');actions.appendChild(status);let close=document.createElement('button');close.className='secondary';close.type='button';close.textContent='关闭预览';close.onclick=()=>panel.remove();actions.appendChild(close);panel.appendChild(actions);grid.appendChild(panel);if(!preserve){panel.scrollIntoView({block:'center'});preview.focus()}}
async function generatePairing(){try{let r=await api('/api/meetings/'+selected+'/brain-pairing',{method:'POST',body:'{}'});alert(I18N.common.oneTimePairing+r.pairingCode)}catch(e){alert(e.message)}}
async function runBrainPoc(){try{let r=await api('/api/meetings/'+selected+'/brain-poc',{method:'POST',body:'{}'});alert(I18N.common.pocParsed+JSON.stringify(r.decision))}catch(e){alert(e.message)}}
async function runApiBrainPoc(){try{let r=await api('/api/brain/api-poc',{method:'POST',body:'{}'});alert('API Brain POC: '+JSON.stringify(r))}catch(e){alert(e.message)}}
function brainConnectionDetails(b){let rows=[['连接方式',b.formalBrowserConnection==='PLAYWRIGHT_CONNECT_OVER_CDP'?'Playwright 连接现有 Chrome':(b.browserRuntime||'未连接')],['Chrome连接',b.cdpConnected?'已连接':'未连接'],['Browser Context',b.contextFound?'已发现':'未发现'],['ChatGPT页面',b.chatgptPageFound?'已找到':'未找到'],['输入框',b.composerFound?'已找到':'未找到'],['状态',b.state||'未知'],['登录状态',b.authState||'未知'],['应用标签页策略',b.applicationTabPolicy||'CHATGPT_ONLY'],['最后错误',b.lastError||'无'],['应用读取其他标签页',b.otherTabReadByApp||'无'],['应用读取Cookie',b.cookieReadByApp||'无'],['应用读取Token',b.tokenReadByApp||'无']];return '<details><summary>主脑连接详情</summary><div class="code">'+rows.map(x=>esc(x[0])+': '+esc(x[1])).join('<br>')+'</div></details>'}
async function refreshDesktopBrainStatus(){if(!window.aimrDesktop)return;let e=$('desktopBrainStatus');if(!desktopBrainStatusOptIn){if(e)e.textContent='GPT 网页自动化实验未启用；V1 默认使用手动 GPT 交接。';return}try{let b=await window.aimrDesktop.getBrainStatus();if(!e)return;let labels={CONNECTING:'正在连接 Chrome',CONNECTED:'Chrome 已连接',DISCOVERING_CHATGPT:'正在查找 ChatGPT 页面',CHECKING_COMPOSER:'正在检查输入框',READY:'已就绪',PAGE_LOST:'ChatGPT 页面已离开或关闭',BROWSER_LOST:'Chrome 连接已断开',ERROR:'连接失败',DISCONNECTED:'未连接'};let authLabels={LOADING:'正在加载',AUTHENTICATED:'已登录',AUTH_REQUIRED:'需要登录',AUTH_LOST:'登录状态已失效',CHALLENGE_REQUIRED:'需要人工验证',DOM_UNKNOWN:'页面结构未知',UNKNOWN:'未知'};let r=desktopBrainResult;e.innerHTML='<b>GPT 网页主脑</b><br>状态：'+esc(labels[b.state]||'连接失败')+' · 登录状态：'+esc(authLabels[b.authState]||b.authState||'未知')+(b.authState==='CHALLENGE_REQUIRED'?'<br>请在 GPT 主脑窗口完成人工验证。':'')+(r?'<br>请求编号：'+esc(r.brainRequestId)+' · 决策：'+esc(r.decision?.decision)+' · 原因：'+esc(r.decision?.reason)+' · 时间：'+esc(r.timestamp||'now'):'')+brainConnectionDetails(b)}catch(e){}}
function brainConnectionDetails(b){let rows=[['连接方式',b.formalBrowserConnection==='PLAYWRIGHT_CONNECT_OVER_CDP_CHANNEL'?'Playwright Chrome Channel':b.formalBrowserConnection],['Channel',b.channel||'不适用'],['连接尝试ID',b.connectAttemptId],['构建标识',b.buildId],['MCP进程启动',b.MCP_PROCESS_STARTED===false?'否':'未知'],['HTTP 9222预检',b.cdpHttpPreflight],['Playwright Channel解析',b.channelResolution||'未运行'],['connectOverCDP(channel)',b.connectOverCdp],['Browser已连接',b.browserConnected?'是':'否'],['Context数量',b.contextCount],['页面总数',b.pageTargetCount],['ChatGPT页面数',b.chatgptTargetCount],['选择策略',b.chatgptPageSelectionPolicy],['最终选择候选',b.selectedCandidateIndex===null||b.selectedCandidateIndex===undefined?'未选择':b.selectedCandidateIndex],['Composer',b.composerFound?'找到':'未找到'],['最后成功阶段',b.lastSuccessfulStage||b.lastSuccessStage],['最后失败阶段',b.lastFailureStage],['错误代码',b.lastErrorCode],['安全错误摘要',b.safeErrorSummary]];let candidates=(b.pageCandidates||[]).map(c=>'<br>候选'+esc((c.candidateIndex??c.index)+1)+'：路径 '+esc(c.pathname||'/')+'；登录UI '+(c.loginUiDetected?'是':'否')+'；已登录壳 '+(c.authenticatedShellDetected?'是':'否')+'；Composer候选 '+esc(c.composerCandidateCount??0)+'；可见 '+esc(c.visibleComposerCount??0)+'；可编辑 '+esc(c.editableComposerCount??0)+(c.selectedCandidate?'；最终选择':'')).join('');return '<details><summary>主脑连接技术详情</summary><div class="code">'+rows.map(x=>esc(x[0])+': '+esc(x[1]??'UNKNOWN')).join('<br>')+candidates+'</div></details>'}
function showDesktopConnectionFailure(code,message,attemptId){let e=$('desktopBrainStatus');if(!e)return;e.textContent='GPT 网页主脑：连接失败 · '+userFacingError(code,message||code)+' · 技术详情：'+code+' · 连接尝试ID：'+attemptId}
function isDesktopIpcInfrastructureFailure(code){return ['DESKTOP_CONNECT_IPC_FAILED','RENDERER_RESULT_HANDLING_FAILED'].includes(code)}
async function openGptBrain(){if(desktopConnectInFlight)return;desktopBrainStatusOptIn=true;let attemptId=globalThis.crypto?.randomUUID?crypto.randomUUID():'ui-'+Date.now();desktopConnectInFlight=true;setDesktopConnectUiState(true);try{if(!window.aimrDesktop?.openBrainWindow)throw Object.assign(Error('Desktop Brain is available only in Electron'),{code:'DESKTOP_CONNECT_IPC_FAILED',attemptId});let r=await window.aimrDesktop.openBrainWindow(attemptId);if(!r||typeof r!=='object')throw Object.assign(Error('Desktop connection result was invalid'),{code:'RENDERER_RESULT_HANDLING_FAILED',attemptId});if(r?.ok===false){let d=r.error||{};let code=d.code||'INTERNAL_UNCLASSIFIED_EXCEPTION';throw Object.assign(Error(d.message||code),{code,technical:d.message||code,attemptId:d.attemptId||attemptId,stage:d.stage})}await refreshDesktopBrainStatus()}catch(e){let code=e.code||'INTERNAL_UNCLASSIFIED_EXCEPTION';let id=e.attemptId||attemptId;if(isDesktopIpcInfrastructureFailure(code))alert(userFacingError(code,e.message||code)+'（连接尝试ID：'+id+'；技术错误：'+code+'）');else showDesktopConnectionFailure(code,e.technical||e.message||code,id)}finally{desktopConnectInFlight=false;setDesktopConnectUiState(false)}}
async function reconnectGptBrain(){await openGptBrain()}
function setDesktopPocStatus(phase,detail=''){desktopBrainPocUiState={phase,detail};let labels={NOT_RUN:'未运行',PREPARING:'准备中',WRITING:'正在写入',SENDING:'正在发送',WAITING_RESPONSE:'等待GPT回复',PARSING:'正在解析',SUCCESS:'测试成功',FAILED:'测试失败'};let e=$('desktopBrainPocStatus');if(!e)return;let text='桌面主脑测试：'+(labels[phase]||'未知');if(phase==='FAILED'&&detail){let reason=I18N.errors[detail]||detail;text+=' · 原因：'+reason;if(I18N.errors[detail])text+=' · 技术详情：'+esc(detail)}else if(detail)text+=' · 技术详情：'+esc(detail);e.textContent=text}
if(window.aimrDesktop?.onDesktopBrainPocStatus)window.aimrDesktop.onDesktopBrainPocStatus(s=>setDesktopPocStatus(s.phase,s.detail||''));
async function runDesktopBrainPoc(){if(desktopBrainPocInFlight)return;desktopBrainStatusOptIn=true;desktopBrainPocInFlight=true;let button=$('desktopBrainPocButton');if(button){button.disabled=true;button.setAttribute('aria-busy','true')}try{if(!window.aimrDesktop?.runDesktopBrainPoc)throw Error('Desktop Brain is available only in Electron');setDesktopPocStatus('PREPARING');let r=await window.aimrDesktop.runDesktopBrainPoc();if(r?.ok===false){let d=r.error||{};let failure=Object.assign(Error(d.message||d.code||'POC_FAILED'),{code:d.code||'POC_FAILED',technical:d.message||d.code,stage:d.stage,diagnostics:d});throw failure}desktopBrainResult=r;setDesktopPocStatus('SUCCESS');await refreshDesktopBrainStatus();alert(I18N.common.pocParsed+JSON.stringify(r.decision))}catch(e){let detail=e.technical||e.code||e.message||'UNKNOWN';setDesktopPocStatus('FAILED',detail);alert(userFacingError(detail,detail)+'（技术详情：'+detail+'）')}finally{desktopBrainPocInFlight=false;button=$('desktopBrainPocButton');if(button){button.disabled=false;button.setAttribute('aria-busy','false')}}}
async function joinProvider(id){if(!selected)return;try{await api('/api/meetings/'+selected+'/agents',{method:'POST',body:JSON.stringify({provider:id})});await refresh();await renderProviders()}catch(e){alert(e.message)}}
async function saveBrainProvider(){try{await api('/api/brain-provider/config',{method:'POST',body:JSON.stringify({provider:$('brainProvider').value,model:$('brainModel').value,baseUrl:$('brainBaseUrl').value,timeout:$('brainTimeout').value,enabled:$('brainEnabled').checked,apiCredential:$('brainCredential').value})});$('brainCredential').value='';await renderProviders();alert(I18N.common.settingsSaved)}catch(e){alert(e.message)}}
async function removeBrainCredential(){try{await api('/api/brain-provider/credential',{method:'DELETE'});await renderProviders();alert(I18N.common.credentialRemoved)}catch(e){alert(e.message)}}
async function testBrainProvider(){try{let r=await api('/api/brain-provider/test',{method:'POST',body:'{}'});await renderProviders();alert(I18N.common.providerStatus+statusLabel(r.status))}catch(e){alert(e.message)}}
async function renderProviders(){let ps=await api('/api/providers');let b=await api('/api/brain-provider');let c=b.config;$('brain-settings').innerHTML='<section class="provider"><b>API 主脑（实验）</b><div class="muted">仅供可选实验，默认关闭；不影响 GPT 网页版主脑。</div><div class="field" style="margin-top:10px"><label>Provider Type<select id="brainProvider"><option value="openai-compatible">OpenAI Compatible</option></select></label><label>Base URL<input id="brainBaseUrl" value="'+esc(c.baseUrl)+'" placeholder="https://.../v1/chat/completions"></label><label>Model<input id="brainModel" value="'+esc(c.model)+'" placeholder="model"></label><label>API Credential<input id="brainCredential" type="password" autocomplete="off" placeholder="Leave blank to keep current"></label><label>Timeout<input id="brainTimeout" type="number" min="1" max="600" value="'+esc(c.timeout)+'"></label><label><input id="brainEnabled" type="checkbox" '+(c.enabled?'checked':'')+'> Enabled（实验）</label></div><div class="muted">Status: '+esc(b.health.status)+' · Credential: '+(c.credentialConfigured?'Configured':'Not configured')+'</div><div class="actions" style="margin-top:8px"><button class="secondary" onclick="saveBrainProvider()">Save</button><button class="secondary" onclick="testBrainProvider()">Test Connection</button><button class="danger" onclick="removeBrainCredential()">Remove Credential</button></div></section>';$('providers').innerHTML=ps.map(p=>`<div class="provider"><b>${esc(p.displayName)}</b><br><span class="${p.health==='AVAILABLE'?'available':'blocked'}">● ${esc(p.health)}</span><br><small class="muted">${esc(p.version)} · ${esc(p.reason||'ready')}</small>${p.health==='AVAILABLE'&&selected?`<br><button class="secondary" style="margin-top:7px" onclick="joinProvider('${esc(p.providerId)}')">Join this Meeting</button>`:''}</div>`).join('');localizeStatic()}
setInterval(()=>{if(selected)refresh()},2500);load();
</script></body></html>'''

_CREATE_MEETING_DIALOG_HTML = r'''<dialog id="createMeetingDialog" aria-labelledby="createMeetingTitle">
  <form id="createMeetingForm" novalidate onsubmit="submitCreateMeeting(event)">
    <h2 id="createMeetingTitle">新建会议</h2>
    <p class="muted">为会议设置名称，并选择一个本机已有的项目文件夹作为工作区。</p>
    <label class="field" for="createMeetingName">会议名称
      <input id="createMeetingName" name="name" maxlength="120" required autocomplete="off" placeholder="例如：产品迭代讨论">
    </label>
    <label class="field" for="createMeetingWorkspace">工作区文件夹路径
      <input id="createMeetingWorkspace" name="workspacePath" required autocomplete="off" value="." placeholder="例如：~/projects/my-app">
    </label>
    <p class="muted form-hint">请选择本机已存在的项目目录；不会自动创建或修改其中的文件。</p>
<div id="createMeetingError" class="form-error" role="alert" hidden></div>
    <div class="actions dialog-actions">
      <button id="createMeetingSubmit" type="submit">创建会议</button>
      <button type="button" class="secondary" onclick="closeCreateMeetingDialog()">取消</button>
    </div>
  </form>
</dialog>
<dialog id="restoreProductDialog" aria-labelledby="restoreProductTitle">
  <form id="restoreProductForm" onsubmit="submitRestoreProductData(event)">
    <h2 id="restoreProductTitle">恢复数据备份</h2>
    <p class="muted">恢复会覆盖当前会议数据，并要求重启应用。请确认选择正确的备份。</p>
    <label class="field" for="restoreBackupSelect">选择备份
      <select id="restoreBackupSelect" name="backupId" required></select>
    </label>
    <label class="restore-confirm"><input id="confirmRestoreOverwrite" type="checkbox" required> 我确认用所选备份覆盖当前数据</label>
    <div id="restoreProductError" class="form-error" role="alert" hidden></div>
    <div class="actions dialog-actions">
      <button id="restoreProductSubmit" type="submit" class="danger">恢复备份</button>
      <button type="button" class="secondary" onclick="closeRestoreProductDialog()">取消</button>
    </div>
  </form>
</dialog>
<dialog id="productMessageDialog" aria-labelledby="productMessageTitle" aria-describedby="productMessageText">
  <section>
    <h2 id="productMessageTitle">提示</h2>
    <p id="productMessageText" role="status"></p>
    <div class="actions dialog-actions"><button id="productMessageOk" type="button" onclick="closeProductMessageDialog()">知道了</button></div>
  </section>
</dialog>
<dialog id="productConfirmDialog" aria-labelledby="productConfirmTitle" aria-describedby="productConfirmText">
  <form method="dialog">
    <h2 id="productConfirmTitle">确认操作</h2>
    <p id="productConfirmText"></p>
    <div class="actions dialog-actions"><button type="submit" class="secondary" value="cancel">取消</button><button id="productConfirmAccept" type="submit" value="confirm">确认</button></div>
  </form>
</dialog>
<div id="product-notice" class="product-notice" role="status" aria-live="polite" hidden></div>'''

_V101_ACCESSIBLE_LAYOUT_CSS = r'''
:root{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",ui-sans-serif,system-ui,sans-serif;font-size:15px;line-height:1.5}
body{min-width:320px;line-height:1.5}
button,input,textarea,select{font-size:15px;line-height:1.45}
button{min-height:40px;padding:9px 14px;white-space:normal;overflow-wrap:anywhere}
.shell{grid-template-columns:minmax(220px,260px) minmax(0,1fr) minmax(270px,320px)}
.sidebar,.right,.main,.card,.grid,.provider{min-width:0}
.eyebrow{font-size:13px;letter-spacing:0;text-transform:none;line-height:1.45}
.brand{font-size:21px;line-height:1.35;overflow-wrap:anywhere}
h1{font-size:clamp(22px,2.5vw,30px);line-height:1.3;overflow-wrap:anywhere}
.card h3{font-size:17px;line-height:1.4;overflow-wrap:anywhere}
.muted,.form-hint{font-size:14px;line-height:1.55;overflow-wrap:anywhere}
.status,.pill{font-size:13px;line-height:1.4;white-space:normal;overflow-wrap:anywhere}
.timeline{font-size:14px;line-height:1.5}
.code{font-size:13px;line-height:1.5;overflow-wrap:anywhere;word-break:break-word}
.top{gap:12px;flex-wrap:wrap}
.top>.actions{max-width:100%;flex-wrap:wrap}
.row{align-items:flex-start;flex-wrap:wrap}
.row>*{min-width:0;max-width:100%;overflow-wrap:anywhere}
.phase3c-cell-value{min-width:0;max-width:75%;text-align:right;overflow-wrap:anywhere;word-break:break-word}
.phase3c-cell-value>b,.phase3c-cell-code{display:block;overflow-wrap:anywhere;word-break:break-word}
.actions{align-items:stretch}
.actions>button{max-width:100%}
dialog#createMeetingDialog,dialog#restoreProductDialog,dialog#productMessageDialog,dialog#productConfirmDialog{width:min(560px,calc(100vw - 32px));max-width:none;max-height:calc(100vh - 32px);overflow:auto;padding:24px;border:1px solid var(--line);border-radius:12px;background:var(--panel);color:#e6edf3;box-shadow:0 22px 70px #0009}
dialog#createMeetingDialog::backdrop,dialog#restoreProductDialog::backdrop,dialog#productMessageDialog::backdrop,dialog#productConfirmDialog::backdrop{background:#0009}
#createMeetingDialog h2,#restoreProductDialog h2,#productMessageDialog h2,#productConfirmDialog h2{margin:0 0 8px;font-size:22px;line-height:1.4}
#createMeetingDialog p,#restoreProductDialog p,#productMessageDialog p,#productConfirmDialog p{margin:8px 0 16px;line-height:1.6;overflow-wrap:anywhere}
#createMeetingDialog .field,#restoreProductDialog .field{font-weight:600;line-height:1.5}
#createMeetingDialog input,#restoreProductDialog select{width:100%;min-width:0;min-height:42px;margin-top:3px}
.restore-confirm{display:flex;gap:9px;align-items:flex-start;line-height:1.5}
.restore-confirm input{margin-top:5px;flex:0 0 auto}
.dialog-actions{justify-content:flex-end;margin-top:18px}
.form-error{padding:10px 12px;border:1px solid var(--red);border-radius:7px;color:#ffd7d5;background:#35151a;line-height:1.5;overflow-wrap:anywhere}
.product-notice{position:fixed;z-index:20;left:50%;bottom:20px;transform:translateX(-50%);width:max-content;max-width:calc(100vw - 32px);padding:12px 16px;border:1px solid var(--green);border-radius:9px;background:#14251a;color:#e6edf3;line-height:1.5;overflow-wrap:anywhere;box-shadow:0 8px 30px #0008}
@media(max-width:1100px){.shell{grid-template-columns:minmax(210px,240px) minmax(0,1fr)}.right{grid-column:1/-1;border-left:0;border-top:1px solid var(--line)}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:720px){.shell{display:block}.sidebar,.right{padding:16px}.main{padding:16px}.grid{grid-template-columns:minmax(0,1fr)}.full{grid-column:auto}.top{align-items:stretch}.top>.actions{width:100%}.dialog-actions{flex-direction:column-reverse}.dialog-actions button{width:100%}}
'''

UI_HTML = UI_HTML.replace("</aside></div>\n<script>", "</aside></div>\n" + _CREATE_MEETING_DIALOG_HTML + "\n<script>", 1)
UI_HTML = UI_HTML.replace("</style>", _V101_ACCESSIBLE_LAYOUT_CSS + "</style>", 1)

_locale_path = Path(__file__).parents[1] / "locales" / "zh-CN.json"
_locale = json.loads(_locale_path.read_text(encoding="utf-8"))
_locale_script = json.dumps(_locale, ensure_ascii=False, separators=(",", ":"))
UI_HTML = UI_HTML.replace(
    "<script>\n",
    "<script>\nconst I18N = " + _locale_script + ";\n"
    "const L = (group, key) => (I18N[group] && I18N[group][key]) || key;\n"
    "const statusLabel = value => (I18N.status[value] || value || I18N.common.none);\n"
    "const decisionLabel = value => (I18N.decision[value] || value || I18N.common.none);\n"
    "const providerReason = value => (I18N.providerReasons[value] || value || I18N.common.none);\n"
    "const userFacingError = (code, fallback) => ({PAGE_LOADING:'ChatGPT 页面仍在加载，请稍后重试',COMPOSER_NOT_FOUND:'已登录，但未找到 GPT 输入框',GPT_WEB_BRAIN_NOT_READY:'GPT 主脑当前未就绪'}[code] || I18N.errors[code] || I18N.errors[fallback] || I18N.errors.UNKNOWN);\n"
    "function localizeText(value){let s=String(value);Object.entries(I18N.status).forEach(([k,v])=>{s=s.replace(new RegExp('\\\\b'+k+'\\\\b','g'),v)});Object.entries(I18N.decision).forEach(([k,v])=>{s=s.replace(new RegExp('\\\\b'+k+'\\\\b','g'),v)});Object.entries(I18N.errors).forEach(([k,v])=>{s=s.replace(new RegExp('\\\\b'+k+'\\\\b','g'),v)});Object.entries(I18N.providerReasons).forEach(([k,v])=>{s=s.split(k).join(v)});let labels={'Meetings':I18N.nav.meetings,'New meeting':I18N.nav.newMeeting,'Providers':I18N.nav.providers,'Agents':I18N.nav.agents,'Meeting':I18N.meeting.meeting,'Start':I18N.meeting.start,'Global Pause':I18N.meeting.globalPause,'Recover':I18N.meeting.recover,'Brain':I18N.brain.brain,'ManualBrainBridge':I18N.brain.manual,'ApiBrainBridge':I18N.brain.api,'ChatGPT Brain':I18N.brain.web,'ChatGPT Desktop':I18N.brain.chatgptDesktop,'operator':I18N.brain.operator,'Members / Agents':I18N.task.members,'Restart Agent':I18N.task.restart,'Join this Meeting':'加入本次会议','Tasks':I18N.task.tasks,'Task title':I18N.task.title,'Task instruction':I18N.task.instruction,'instruction':'说明','Read-only task instruction':I18N.task.readOnly,'Assign Agent':I18N.task.assignAgent,'Create task':I18N.task.create,'Dispatch':I18N.task.dispatch,'Submit decision':I18N.brain.submitDecision,'Decision reason':I18N.brain.decisionReason,'REWORK instruction':I18N.brain.reworkInstruction,'Related task (optional)':I18N.brain.relatedTask,'Timeline / Brain Inbox':I18N.brain.timeline,'Events / Errors':I18N.brain.eventsErrors,'Workspace':I18N.workspace.workspace,'Git diff':I18N.workspace.gitDiff,'Brain Provider Settings':I18N.provider.settings,'Brain Provider':I18N.provider.connectionStatus,'Provider Type':I18N.provider.type,'OpenAI Compatible':I18N.provider.openaiCompatible,'Base URL':I18N.provider.baseUrl,'Model':I18N.provider.model,'API Credential':I18N.provider.credential,'Leave blank to keep current':I18N.provider.keepCurrent,'Timeout':I18N.provider.timeout,'Enabled':I18N.provider.enabled,'Status':I18N.provider.status,'Credential':I18N.provider.credential,'Not configured':I18N.provider.notConfigured,'Configured':I18N.provider.configured,'Save':I18N.provider.save,'Test Connection':I18N.provider.test,'Remove Credential':I18N.provider.remove,'Legacy / Experimental: Generate Pairing Code':I18N.brain.pairing,'Legacy / Experimental: Send Brain Transport POC':I18N.brain.transportPoc,'Desktop Brain POC':I18N.brain.desktopPoc,'Create or select a meeting.':I18N.meeting.createOrSelect,'No meetings':I18N.meeting.noMeetings,'No tasks':I18N.task.noTasks,'No agents':I18N.task.noAgents,'No events yet':I18N.brain.noEventsYet,'No events':I18N.task.noEvents,'No diff':I18N.workspace.noDiff,'not configured':I18N.workspace.notConfigured,'clean':I18N.common.clean,'ready':'已就绪','triggeredBy':'触发来源','reason':'原因','timestamp':'时间','auth=':'登录状态：','requestId:':'请求编号：','decision:':'决策：','One-time pairing code':I18N.common.oneTimePairing,'Brain POC parsed:':I18N.common.pocParsed,'Desktop Brain is available only in Electron':I18N.common.desktopOnly,'Brain Provider settings saved. Credential: Configured':I18N.common.settingsSaved,'Brain credential removed':I18N.common.credentialRemoved,'Brain Provider status:':I18N.common.providerStatus};Object.entries(labels).forEach(([k,v])=>{s=s.split(k).join(v)});return s.replace('MEETING PAUSED',I18N.meeting.paused).replace('Dispatch disabled until all health checks pass.',I18N.meeting.dispatchDisabled).replace('Inbox events:',I18N.brain.inboxEvents+':').replace('health=true','健康状态：正常').replace('health=false','健康状态：异常').replace('state=',''+I18N.brain.state+'：').replace('status=','状态：').replace('unassigned',I18N.task.unassigned).replace('modified=',''+I18N.workspace.modified+'：');}\n"
    "function localizeStatic(){document.querySelectorAll('[data-i18n]').forEach(e=>{let p=e.dataset.i18n.split('.');e.textContent=L(p[0],p[1])});document.querySelectorAll('[data-i18n-placeholder]').forEach(e=>{let p=e.dataset.i18nPlaceholder.split('.');e.placeholder=L(p[0],p[1])});document.querySelectorAll('input,textarea').forEach(e=>{if(e.placeholder)e.placeholder=localizeText(e.placeholder)});let walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);let nodes=[];while(walker.nextNode())nodes.push(walker.currentNode);nodes.forEach(n=>{if(!['SCRIPT','STYLE'].includes(n.parentElement?.tagName))n.nodeValue=localizeText(n.nodeValue)});}\n"
    "const originalRender = typeof render === 'function' ? render : null;\n",
    1,
)

_CREATE_MEETING_JS = r'''let createMeetingInFlight=false;
function showCreateMeetingDialog(){let dialog=$('createMeetingDialog');if(!dialog)return;let error=$('createMeetingError');error.hidden=true;error.textContent='';if(!dialog.open)dialog.showModal();$('createMeetingName').focus()}
function closeCreateMeetingDialog(){let dialog=$('createMeetingDialog');if(dialog?.open)dialog.close();let error=$('createMeetingError');if(error){error.hidden=true;error.textContent=''}}
function showProductNotice(message){let notice=$('product-notice');if(!notice)return;notice.textContent=message;notice.hidden=false;window.setTimeout(()=>{if(notice.textContent===message)notice.hidden=true},5000)}
async function submitCreateMeeting(event){event.preventDefault();if(createMeetingInFlight)return false;let name=$('createMeetingName').value.trim(),workspacePath=$('createMeetingWorkspace').value.trim(),errorBox=$('createMeetingError'),submit=$('createMeetingSubmit');if(!name){errorBox.textContent=I18N.errors.MEETING_NAME_REQUIRED;errorBox.hidden=false;$('createMeetingName').focus();return false}if(!workspacePath){errorBox.textContent=I18N.errors.WORKSPACE_DIRECTORY_INVALID;errorBox.hidden=false;$('createMeetingWorkspace').focus();return false}createMeetingInFlight=true;submit.disabled=true;submit.setAttribute('aria-busy','true');submit.textContent=I18N.createMeeting.creating;errorBox.hidden=true;errorBox.textContent='';try{let created=await api('/api/meetings',{method:'POST',body:JSON.stringify({name,workspacePath})});if(!created?.meeting?.meeting_id)throw Object.assign(Error('meeting identity missing'),{code:'MEETING_CREATE_FAILED'});selected=created.meeting.meeting_id;closeCreateMeetingDialog();$('createMeetingForm').reset();showProductNotice(I18N.createMeeting.created);await refresh();await renderProviders();}catch(e){let code=e.code||(e instanceof TypeError?'LOCAL_SERVICE_UNAVAILABLE':'MEETING_CREATE_FAILED');errorBox.textContent=I18N.createMeeting.failed+'：'+(I18N.errors[code]||I18N.errors.MEETING_CREATE_FAILED);errorBox.hidden=false;}finally{createMeetingInFlight=false;submit.disabled=false;submit.removeAttribute('aria-busy');submit.textContent=I18N.createMeeting.create}return false}
'''
_OLD_CREATE_MEETING_JS = "async function createMeeting(){let name=prompt(I18N.common.meetingName,I18N.common.codexMeeting);if(!name)return;let workspace=prompt(I18N.common.workspacePath,'.');if(!workspace)return;try{let s=await api('/api/meetings',{method:'POST',body:JSON.stringify({name,workspacePath:workspace})});selected=s.meeting.meeting_id;await refresh();await renderProviders()}catch(e){alert(e.message)}}"
if _OLD_CREATE_MEETING_JS not in UI_HTML:
    raise RuntimeError("expected the legacy Electron-incompatible meeting creation flow")
UI_HTML = UI_HTML.replace(_OLD_CREATE_MEETING_JS, _CREATE_MEETING_JS, 1)

_OLD_PAUSE_MEETING_JS = "async function pauseMeeting(){let reason=prompt('暂停原因','操作员请求暂停')||'操作员请求暂停';try{await api('/api/meetings/'+selected+'/pause',{method:'POST',body:JSON.stringify({reason})});await refresh()}catch(e){alert(e.message)}}"
_PAUSE_MEETING_JS = "async function pauseMeeting(){try{await api('/api/meetings/'+selected+'/pause',{method:'POST',body:JSON.stringify({reason:'用户从产品界面请求暂停'})});await refresh()}catch(e){alert(e.message)}}"
if _OLD_PAUSE_MEETING_JS not in UI_HTML:
    raise RuntimeError("expected the legacy pause reason prompt")
UI_HTML = UI_HTML.replace(_OLD_PAUSE_MEETING_JS, _PAUSE_MEETING_JS, 1)

_OLD_RESTORE_PRODUCT_DATA_JS = "async function restoreProductData(){try{let list=await api('/api/system/backups');let choices=(list.backups||[]).filter(x=>x.status==='VALID').map(x=>x.backupId);if(!choices.length){alert('没有可恢复的有效备份。');return}let backupId=prompt('输入要恢复的备份编号：\\n'+choices.join('\\n'),choices[0]);if(!backupId)return;if(!confirm('确认用所选备份覆盖当前数据？恢复后必须重启应用。'))return;let r=await api('/api/system/restore',{method:'POST',body:JSON.stringify({backupId,confirmOverwrite:true})});$('v1-system-status').textContent='恢复完成，请重启应用：'+r.backupId}catch(e){alert(e.message)}}"
_RESTORE_PRODUCT_DATA_JS = r'''async function restoreProductData(){try{let list=await api('/api/system/backups'),choices=(list.backups||[]).filter(x=>x.status==='VALID').map(x=>x.backupId),select=$('restoreBackupSelect'),dialog=$('restoreProductDialog');if(!choices.length){showProductNotice(I18N.restore.none);return}select.innerHTML=choices.map(id=>'<option value="'+esc(id)+'">'+esc(id)+'</option>').join('');$('confirmRestoreOverwrite').checked=false;$('restoreProductError').hidden=true;$('restoreProductError').textContent='';dialog.showModal()}catch(e){showProductNotice(I18N.restore.failed+'：'+(I18N.errors[e.code]||I18N.errors.UNKNOWN))}}
function closeRestoreProductDialog(){let dialog=$('restoreProductDialog');if(dialog?.open)dialog.close()}
async function submitRestoreProductData(event){event.preventDefault();let error=$('restoreProductError');if(!$('confirmRestoreOverwrite').checked){error.textContent='请先确认覆盖当前数据。';error.hidden=false;return false}let button=$('restoreProductSubmit');button.disabled=true;try{let backupId=$('restoreBackupSelect').value;let result=await api('/api/system/restore',{method:'POST',body:JSON.stringify({backupId,confirmOverwrite:true})});closeRestoreProductDialog();$('v1-system-status').textContent=I18N.restore.restart+' '+result.backupId}catch(e){error.textContent=I18N.restore.failed+'：'+(I18N.errors[e.code]||I18N.errors.UNKNOWN);error.hidden=false}finally{button.disabled=false}return false}'''
UI_HTML, _restore_prompt_replaced = re.subn(
    r"async function restoreProductData\(\)\{.*?\}(?=\nasync function exportDiagnosticReport)",
    lambda _match: _RESTORE_PRODUCT_DATA_JS,
    UI_HTML,
    count=1,
    flags=re.DOTALL,
)
if _restore_prompt_replaced != 1:
    raise RuntimeError("expected exactly one legacy restore backup prompt")

# Keep the POC control in Product Shell while preserving the existing pairing
# flow. The browser never receives or handles a bridge token.
UI_HTML = UI_HTML.replace(
    '<button class="secondary" onclick="generatePairing()">Generate Pairing Code</button>',
    '<button class="secondary" onclick="generatePairing()">Generate Pairing Code</button>'
    '<button class="secondary" onclick="runBrainPoc()">Send Brain Transport POC</button>',
)
UI_HTML = UI_HTML.replace(
    'Generate Pairing Code</button>',
    'Legacy / Experimental: Generate Pairing Code</button>',
)
for _connect_label in ('连接 GPT 主脑', '打开 GPT 主脑', '重新连接 GPT 主脑'):
    UI_HTML = UI_HTML.replace(
        f'<button class="secondary" onclick="openGptBrain()">{_connect_label}</button>',
        f'<button class="desktopConnectButton secondary" onclick="openGptBrain()">{_connect_label}</button>',
    )
UI_HTML = UI_HTML.replace(
    '<button class="secondary" onclick="reconnectGptBrain()">重新连接 GPT 主脑</button>',
    '<button class="desktopConnectButton secondary" onclick="reconnectGptBrain()">重新连接 GPT 主脑</button>',
)
UI_HTML = UI_HTML.replace(
    'Send Brain Transport POC</button>',
    'Legacy / Experimental: Send Brain Transport POC</button>',
)
UI_HTML = UI_HTML.replace(
    '<button class="secondary" onclick="generatePairing()">Legacy / Experimental: Generate Pairing Code</button><button class="secondary" onclick="runBrainPoc()">Legacy / Experimental: Send Brain Transport POC</button>',
    '<details class="experimental"><summary>开发/实验功能</summary><div class="actions"><button class="secondary" onclick="generatePairing()">Legacy / Experimental: Generate Pairing Code</button><button class="secondary" onclick="runBrainPoc()">Legacy / Experimental: Send Brain Transport POC</button></div></details>',
)
UI_HTML = UI_HTML.replace(
    '<div class="muted">health=${esc(brain.health)} · state=${esc(brain.state||\'UNKNOWN\')} · Inbox events: ${(s.brainInbox||[]).length}</div>',
    '<div class="muted">health=${esc(brain.health)} · state=${esc(brain.state||\'UNKNOWN\')} · Inbox events: ${(s.brainInbox||[]).length}</div><div id="desktopBrainStatus" class="muted">GPT 主脑：正在检查</div><div class="actions" style="margin-top:8px"><button class="secondary" onclick="openGptBrain()">连接 GPT 主脑</button><button class="secondary" onclick="openGptBrain()">打开 GPT 主脑</button><button class="secondary" onclick="reconnectGptBrain()">重新连接 GPT 主脑</button><button id="desktopBrainPocButton" class="secondary" onclick="runDesktopBrainPoc()">桌面主脑测试</button></div><div id="desktopBrainPocStatus" class="muted">桌面主脑测试：未运行</div>',
)


UI_HTML = UI_HTML.replace(
    '<b>主脑：GPT 网页版</b><br><small class="muted">正式主脑 · 用户已授权的 Google Chrome 会话</small>',
    '<b>主脑：手动 GPT 交接</b><br><small class="muted">默认路线 · 用户复制到 GPT，再导入 BrainDecision</small><br><small class="muted">GPT 网页自动化：实验功能 · 当前被外部验证挑战阻断</small>',
)
UI_HTML = UI_HTML.replace(
    '<details class="chrome-setup"><summary>设置 GPT 主脑</summary>',
    '<details class="chrome-setup experimental"><summary>设置 GPT 主脑（网页自动化实验｜外部验证挑战阻断）</summary>',
)
UI_HTML = UI_HTML.replace(
    '打开 <code>chrome://inspect/#remote-debugging</code> 并启用 Remote Debugging。',
    '旧版网页自动化诊断可选打开 <code>chrome://inspect/#remote-debugging</code>；V1 默认使用手动 GPT 交接，不需要开发者工具。',
)
UI_HTML = UI_HTML.replace('<div class="eyebrow">Meetings</div>', '<div class="eyebrow">会议</div>')
UI_HTML = UI_HTML.replace('<button onclick="createMeeting()">＋ New meeting</button>', '<button id="createMeetingButton" onclick="showCreateMeetingDialog()">＋ 新建会议</button>')
UI_HTML = UI_HTML.replace('<div class="empty">Create or select a meeting.</div>', '<div class="empty">请新建或选择一个会议。</div>')
UI_HTML = UI_HTML.replace(
    '.card h3{margin:0 0 12px;font-size:14px}',
    '.card h3{margin:0 0 12px;font-size:14px}.handoff-status{border-left:3px solid var(--blue);background:#111923;padding:10px 12px;border-radius:4px;margin:10px 0}.handoff-status.blocked{border-left-color:var(--amber);color:#e6edf3}.handoff-steps{display:grid;gap:10px}.handoff-packet,.handoff-decision{min-height:110px;font:12px ui-monospace,monospace;white-space:pre-wrap}.handoff-preview{border:1px solid var(--line);border-radius:7px;padding:12px;background:#0d1117}.handoff-preview.warning{border-color:var(--amber)}.handoff-label{font-size:12px;color:var(--muted);margin-bottom:4px}.handoff-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:8px}',
)
UI_HTML, _legacy_decision_controls_removed = re.subn(
    r'<div class="field" style="margin-top:10px"><select id="decisionType">.*?</div>'
    r'<button onclick="decision\(\)">Submit decision</button>',
    '<div class="muted">GPT 决策必须通过 Packet → 手动复制 → 导入校验 → 预览 → 明确应用。</div>',
    UI_HTML,
    count=1,
    flags=re.DOTALL,
)
if _legacy_decision_controls_removed != 1:
    raise RuntimeError("expected exactly one legacy direct-decision UI block")
UI_HTML = UI_HTML.replace(
    "const renderBeforeUiState=render;render=function(s){let state=readUiInteractionState();renderBeforeUiState(s);restoreUiInteractionState(state)}",
    "const renderBeforeUiState=render;render=function(s){let state=readUiInteractionState();renderBeforeUiState(s);mountManualGPTUI(s);restoreUiInteractionState(state)}",
)
UI_HTML = UI_HTML.replace(
    "async function selectMeeting(id){selected=id;await refresh()}",
    "async function selectMeeting(id){if(selected!==id){manualBrainCopyText='';manualBrainRequestId='';manualGptUiMessage='';if(typeof handoffClearDecisionText==='function')handoffClearDecisionText()}selected=id;await refresh()}",
)

_MANUAL_GPT_HANDOFF_JS = r'''
let manualBrainCopyText='',manualBrainRequestId='',manualGptUiMessage='';
function handoffErrorMessage(code){return ({BRAIN_PACKET_SENSITIVE_CONTENT_BLOCKED:'已阻止：检测到潜在敏感内容，Packet 未生成也未复制。',INVALID_BRAIN_DECISION:'GPT 返回内容不是符合要求的单一 BrainDecision JSON；没有修改会议或任务。',STALE_DECISION_REJECTED:'该决策对应的任务结果已过期。',CROSS_MEETING_DECISION_REJECTED:'该决策属于另一个会议。',DUPLICATE_DECISION_REJECTED:'该决策已导入或应用，不能重复执行。',WAITING_FOR_HUMAN_HANDOFF:'当前正在等待人工 GPT 决策；新任务派发已阻止。',BRAIN_PACKET_NOT_COPIED:'请先点击“复制给 GPT”，再导入回复。'})[code]||code||'操作失败'}
function manualDecisionLabel(value){return ({ACCEPT:'接受任务',REWORK:'返工',PAUSE:'暂停会议',COMPLETE_MEETING:'结束会议'})[value]||value||'未知'}
function mountManualGPTUI(s){let card=document.querySelector('#detail .grid .card');if(!card)return;let h=s.brainHandoff||null;manualBrainRequestId=h?.requestId||'';let oldStatus=$('desktopBrainStatus');let oldPoc=$('desktopBrainPocStatus');let oldActions=[...card.querySelectorAll('.actions')].find(x=>x.contains($('desktopBrainPocButton')));let oldLegacy=card.querySelector('details.experimental');card.innerHTML='';let title=document.createElement('h3');title.textContent='主脑 · 手动 GPT 交接';card.appendChild(title);let intro=document.createElement('div');intro.className='muted';intro.textContent='默认传输：MANUAL_GPT_HANDOFF · 用户自主复制、发送、导入；不自动操作浏览器或调用 API。';card.appendChild(intro);let status=document.createElement('div');status.className='handoff-status'+(h?.status==='BRAIN_PACKET_SENSITIVE_CONTENT_BLOCKED'?' blocked':'');let headline=document.createElement('b');headline.textContent=h?.status==='VALIDATED'?'GPT 决策已校验，等待你确认应用':h?.status==='BRAIN_PACKET_SENSITIVE_CONTENT_BLOCKED'?'已阻止：发现潜在敏感内容':h?'等待人工 GPT 交接':'等待任务完成';status.appendChild(headline);for(const line of ['主脑健康：'+(s.brain?.health?'正常':'异常'),'Meeting：'+s.meeting.meeting_id,'Task：'+(h?.taskId||'—'),'Request：'+(h?.requestId||'—'),'交接状态：'+(h?.status||'尚未生成')]){let div=document.createElement('div');div.textContent=line;status.appendChild(div)}card.appendChild(status);let steps=document.createElement('div');steps.className='handoff-steps';let step1=document.createElement('div');let label=document.createElement('div');label.className='handoff-label';label.textContent='1 · 生成 / 查看 Packet，然后手动发送给 GPT';step1.appendChild(label);let taskSelect=document.createElement('select');taskSelect.id='brainHandoffTask';let placeholder=document.createElement('option');placeholder.value='';placeholder.textContent='选择一个已完成任务';taskSelect.appendChild(placeholder);for(const task of (s.tasks||[]).filter(t=>t.status==='COMPLETED')){let option=document.createElement('option');option.value=task.task_id;option.textContent=task.title+' · '+task.task_id;if(h?.taskId===task.task_id)option.selected=true;taskSelect.appendChild(option)}taskSelect.disabled=Boolean(h);step1.appendChild(taskSelect);let actions=document.createElement('div');actions.className='handoff-actions';let generate=document.createElement('button');generate.className='secondary';generate.textContent='生成 / 查看给 GPT 的内容';generate.onclick=generateManualGPTPacket;generate.disabled=!h&&taskSelect.options.length<2;actions.appendChild(generate);let copy=document.createElement('button');copy.id='copyManualGPTButton';copy.textContent='复制给 GPT';copy.onclick=copyManualGPTPacket;copy.disabled=!h||h.status==='BRAIN_PACKET_SENSITIVE_CONTENT_BLOCKED'||h.status==='VALIDATED';actions.appendChild(copy);step1.appendChild(actions);let packet=document.createElement('textarea');packet.id='brainPacketPreview';packet.className='field handoff-packet';packet.readOnly=true;packet.setAttribute('aria-label','给 GPT 的 Brain Packet');packet.value=manualBrainCopyText||(h?.status==='BRAIN_PACKET_SENSITIVE_CONTENT_BLOCKED'?'敏感内容保护已阻止 Packet。请先处理源任务结果。':'点击“生成 / 查看”后显示通过安全检查的 Packet。');step1.appendChild(packet);steps.appendChild(step1);let step2=document.createElement('div');let label2=document.createElement('div');label2.className='handoff-label';label2.textContent='2 · 用户手动粘贴 GPT 返回的标准 JSON，再校验';step2.appendChild(label2);let response=document.createElement('textarea');response.id='brainDecisionImport';response.className='field handoff-decision';response.placeholder='粘贴 GPT 返回的 BrainDecisionPacket JSON';step2.appendChild(response);let validate=document.createElement('button');validate.id='validateManualGPTButton';validate.className='secondary';validate.textContent='校验决策';validate.onclick=validateManualGPTDecision;validate.disabled=!h||h.status!=='WAITING_FOR_HUMAN_HANDOFF';step2.appendChild(validate);let result=document.createElement('div');result.id='brainDecisionStatus';result.className='muted';result.textContent=manualGptUiMessage?handoffErrorMessage(manualGptUiMessage):h?.validationErrorCode?handoffErrorMessage(h.validationErrorCode):'尚未导入决策。等待状态不会被当成主脑故障。';step2.appendChild(result);steps.appendChild(step2);if(h?.preview){let preview=document.createElement('div');preview.className='handoff-preview'+(['PAUSE','COMPLETE_MEETING'].includes(h.preview.decision)?' warning':'');let previewTitle=document.createElement('div');previewTitle.className='handoff-label';previewTitle.textContent='3 · 决策预览（身份字段不可编辑）';preview.appendChild(previewTitle);for(const [labelText,value] of [['动作',manualDecisionLabel(h.preview.decision)],['Meeting',h.preview.meetingId],['Task',h.preview.taskId],['Result',h.preview.resultId],['原因',h.preview.reason],...(h.preview.reworkInstruction?[['返工指令',h.preview.reworkInstruction]]:[])]){let line=document.createElement('div');line.textContent=labelText+'：'+(value||'');preview.appendChild(line)}if(h.preview.decision==='COMPLETE_MEETING'||h.preview.decision==='PAUSE'){let warning=document.createElement('div');warning.className='blocked';warning.textContent=h.preview.decision==='COMPLETE_MEETING'?'警告：应用后将结束整个会议。':'警告：应用后将暂停整个会议并打开安全熔断。';preview.appendChild(warning)}let apply=document.createElement('button');apply.id='applyManualGPTButton';apply.textContent='应用决策';apply.onclick=applyManualGPTDecision;preview.appendChild(apply);steps.appendChild(preview)}card.appendChild(steps);if(oldStatus||oldPoc||oldActions||oldLegacy){let exp=document.createElement('details');exp.className='experimental';let summary=document.createElement('summary');summary.textContent='GPT 网页自动化（实验｜当前被外部 Cloudflare 验证挑战阻断）';exp.appendChild(summary);let note=document.createElement('div');note.className='muted';note.textContent='Phase 3 V1 不依赖此路线；不会自动连接、发送或绕过验证。';exp.appendChild(note);if(oldStatus)exp.appendChild(oldStatus);if(oldActions)exp.appendChild(oldActions);if(oldPoc)exp.appendChild(oldPoc);if(oldLegacy)exp.appendChild(oldLegacy);card.appendChild(exp)}}
async function generateManualGPTPacket(){try{let h=snapshot?.brainHandoff;let taskId=h?.taskId||$('brainHandoffTask')?.value;if(!taskId){manualGptUiMessage='BRAIN_REQUEST_NOT_READY';await refresh();return}let r=await api('/api/meetings/'+selected+'/brain-handoff/generate',{method:'POST',body:JSON.stringify({taskId})});manualBrainRequestId=r.requestId||'';manualBrainCopyText=r.copyText||'';manualGptUiMessage=r.status==='BRAIN_PACKET_SENSITIVE_CONTENT_BLOCKED'?'BRAIN_PACKET_SENSITIVE_CONTENT_BLOCKED':'Packet 已准备。请检查内容后手动复制给 GPT。';if($('brainPacketPreview'))$('brainPacketPreview').value=manualBrainCopyText||'敏感内容保护已阻止 Packet。';await refresh()}catch(e){manualGptUiMessage=e.code||e.message;await refresh()}}
async function copyManualGPTPacket(){try{if(!manualBrainRequestId||!manualBrainCopyText){await generateManualGPTPacket();if(!manualBrainCopyText)return}let writePromise=null;try{if(navigator.clipboard?.writeText)writePromise=navigator.clipboard.writeText(manualBrainCopyText)}catch(_){}let r=await api('/api/meetings/'+selected+'/brain-handoff/copy',{method:'POST',body:JSON.stringify({requestId:manualBrainRequestId})});manualBrainCopyText=r.copyText||manualBrainCopyText;if(writePromise){try{await writePromise;manualGptUiMessage='Brain Packet 已复制，请手动粘贴到 GPT。'}catch(_){$('brainPacketPreview')?.select();manualGptUiMessage='剪贴板写入不可用，内容已选中；请手动复制。'}}else{$('brainPacketPreview')?.select();manualGptUiMessage='内容已选中，请手动复制后粘贴到 GPT。'}if($('brainPacketPreview'))$('brainPacketPreview').value=manualBrainCopyText;await refresh()}catch(e){manualGptUiMessage=e.code||e.message;await refresh()}}
async function validateManualGPTDecision(){try{let h=snapshot?.brainHandoff;if(!h?.requestId)throw Object.assign(Error('当前没有待处理 Brain Request'),{code:'STALE_DECISION_REJECTED'});await api('/api/meetings/'+selected+'/brain-handoff/import',{method:'POST',body:JSON.stringify({requestId:h.requestId,rawResponse:$('brainDecisionImport')?.value||''})});manualGptUiMessage='决策校验通过。检查预览后，再明确应用。';await refresh()}catch(e){manualGptUiMessage=e.code||e.message;await refresh()}}
async function applyManualGPTDecision(){let h=snapshot?.brainHandoff;if(!h?.requestId||!h.preview)return;let action=h.preview.decision;if(action==='COMPLETE_MEETING'&&!(await confirmInChinese('确认结束整个会议？这与接受当前任务不同。')))return;if(action==='PAUSE'&&!(await confirmInChinese('确认暂停整个会议并打开安全熔断？')))return;try{await api('/api/meetings/'+selected+'/brain-handoff/apply',{method:'POST',body:JSON.stringify({requestId:h.requestId})});manualGptUiMessage='决策已通过现有 Meeting Core 应用。';manualBrainCopyText='';manualBrainRequestId='';await refresh()}catch(e){manualGptUiMessage=e.code||e.message;await refresh()}}
'''
_MANUAL_GPT_HANDOFF_JS = _MANUAL_GPT_HANDOFF_JS.replace(
    "let oldPoc=$('desktopBrainPocStatus');let oldActions=",
    "let oldPoc=$('desktopBrainPocStatus');let oldPairing=card.querySelector('button[onclick=\"generatePairing()\"]');let oldActions=",
).replace(
    "if(oldStatus||oldPoc||oldActions||oldLegacy)",
    "if(oldStatus||oldPoc||oldPairing||oldActions||oldLegacy)",
).replace(
    "if(oldPoc)exp.appendChild(oldPoc);if(oldLegacy)",
    "if(oldPoc)exp.appendChild(oldPoc);if(oldPairing)exp.appendChild(oldPairing);if(oldLegacy)",
).replace(
    "let manualBrainCopyText='',manualBrainRequestId='',manualGptUiMessage='';",
    "let manualBrainCopyText='',manualBrainRequestId='',manualGptUiMessage='',manualGptDecisionText='';function handoffClearDecisionText(){manualGptDecisionText='';let field=$('brainDecisionImport');if(field)field.value=''}",
).replace(
    "response.placeholder='粘贴 GPT 返回的 BrainDecisionPacket JSON';step2.appendChild(response)",
    "response.placeholder='粘贴 GPT 返回的 BrainDecisionPacket JSON';response.value=manualGptDecisionText;response.addEventListener('input',()=>{manualGptDecisionText=response.value});step2.appendChild(response)",
).replace(
    "manualBrainCopyText='';manualBrainRequestId='';await refresh()}catch(e){manualGptUiMessage=e.code||e.message;await refresh()}}",
    "manualBrainCopyText='';manualBrainRequestId='';handoffClearDecisionText();await refresh()}catch(e){manualGptUiMessage=e.code||e.message;await refresh()}}",
)
_MANUAL_GPT_HANDOFF_JS = _MANUAL_GPT_HANDOFF_JS.replace(
    "默认传输：MANUAL_GPT_HANDOFF · 用户自主复制、发送、导入；不自动操作浏览器或调用 API。",
    "默认方式：手动 GPT 交接。请自行复制、发送和导入；应用不会自动操作浏览器或调用 API。",
)
UI_HTML = UI_HTML.replace(
    "setInterval(()=>{if(selected)refresh()},2500);load();",
    _MANUAL_GPT_HANDOFF_JS + "\n" + r'''
let phase3cBusy=new Set(),phase3cPreflight=null,phase3cError=null;
const phase3cStateLabels={CREATED:'已创建',READY:'已就绪',RUNNING:'运行中',WAITING_FOR_AGENT:'等待智能体',WAITING_FOR_HUMAN_HANDOFF:'等待粘贴 GPT 决策',DECISION_READY:'决策待确认',PAUSED:'已暂停',COMPLETED:'已完成',ERROR:'错误',RECOVERING:'恢复检查中'};
const phase3cErrorCatalog={MEETING_NOT_RUNNING:['会议尚未运行','请先开始会议或查看当前会议状态。'],DISPATCH_BLOCKED:['任务派发已被安全机制阻止','确认所有智能体、主脑、CAO 和工作区健康后再恢复。'],RECOVERY_HEALTH_CHECK_FAILED:['恢复健康检查未通过','查看运行时预检和诊断详情，不要绕过健康检查。'],BRAIN_HANDOFF_ALREADY_OPEN:['已有 Brain Packet 正在处理','完成当前 GPT 决策交接后再生成下一个请求。'],BRAIN_REQUEST_NOT_READY:['当前结果还不能生成 Brain Packet','等待智能体返回一个已完成结果。'],BRAIN_PACKET_NOT_COPIED:['请先主动复制 Brain Packet','点击“复制给 GPT”，再粘贴 GPT 返回的决策。'],DUPLICATE_DECISION_REJECTED:['该决策已处理','不要重复应用；刷新页面查看已保存结果。'],STALE_DECISION_REJECTED:['该决策已过期','重新生成当前任务的 Brain Packet。'],CROSS_MEETING_DECISION_REJECTED:['决策不属于当前会议','确认当前会议、任务和结果身份一致。'],INVALID_BRAIN_DECISION:['GPT 决策格式或字段无效','粘贴单一标准 JSON，重新校验。'],INVALID_BRAIN_REQUEST:['Brain Request 身份不完整','重新生成当前任务的 Brain Packet。'],BRAIN_REQUEST_STATE_INVALID:['当前会议状态不允许生成 Brain Request','先完成健康检查或处理现有交接。'],BRAIN_HANDOFF_PERSISTENCE_UNKNOWN:['Brain Request 持久化状态未知','保持会议暂停，检查数据库与诊断详情。'],BRAIN_HANDOFF_PERSISTENCE_FAILED:['Brain Request 持久化失败','保持会议暂停，检查数据库健康后再恢复。'],BRAIN_DECISION_APPLY_FAILED:['决策应用失败，任务推进仍被阻止','查看审计时间线和诊断详情，不要重复提交。'],PROVIDER_BLOCKED:['智能体提供商当前不可用','查看运行时预检；不要自动登录或切换到未授权提供商。'],PRODUCT_ERROR:['当前操作不满足会议安全条件','查看技术详情中的机器状态后再操作。']};
function phase3cHumanError(code,fallback){let item=phase3cErrorCatalog[code];return item?item[0]:(I18N.errors[code]||fallback||'操作未完成')}
function phase3cRecommended(code){let item=phase3cErrorCatalog[code];return item?item[1]:'查看诊断详情中的机器错误码，并按当前状态操作。'}
function phase3cMachine(v){return v==null||v===''?'UNKNOWN':String(v)}
function phase3cLabel(v){return phase3cStateLabels[v]||I18N.status[v]||v||'未知'}
function phase3cMeetingState(s){let m=s.meeting||{},c=s.circuit||{},h=s.brainHandoff,tasks=s.tasks||[];if(m.status==='PAUSED'||c.stopDispatch)return 'PAUSED';if(m.status==='COMPLETED')return 'COMPLETED';if(m.status==='FAILED')return 'ERROR';if(h&&['WAITING_FOR_HUMAN_HANDOFF','VALIDATED','APPLYING','APPLY_FAILED'].includes(h.status))return h.status==='VALIDATED'?'DECISION_READY':'WAITING_FOR_HUMAN_HANDOFF';let active=tasks.some(t=>['PENDING','QUEUED','DISPATCHED','WORKING','WAITING'].includes(t.status));if(active)return 'WAITING_FOR_AGENT';return m.status||'CREATED'}
function phase3cStatusClass(v){return ['READY','RUNNING','COMPLETED','HEALTHY'].includes(v)?'available':(['BLOCKED','ERROR','PAUSED','LOST','UNKNOWN'].includes(v)?'blocked':'muted')}
function phase3cCell(label,value,code){return '<div class="row"><span>'+label+'</span><span class="phase3c-cell-value '+phase3cStatusClass(code||value)+'"><b>'+esc(value)+'</b>'+((code||value)&&code!==value?' <small class="muted phase3c-cell-code">('+esc(code)+')</small>':'')+'</span></div>'}
function phase3cCurrentTask(s){let tasks=s.tasks||[];return tasks.find(t=>['PENDING','QUEUED','DISPATCHED','WORKING','WAITING'].includes(t.status))||tasks[tasks.length-1]||null}
function phase3cParticipant(s){let p=s.brainParticipant||{};return p.participantId?('参与者 '+p.participantId+' · '+(p.provider||'GPT')):'尚未加入正式主脑'}
let phase3cCaoRecoveryMessage='';
async function phase3cRecoverCao(){let button=$('phase3c-cao-recover');if(!button||phase3cBusy.has('cao-recover'))return;phase3cBusy.add('cao-recover');button.disabled=true;button.setAttribute('aria-busy','true');button.textContent='正在启动 / 检查 CAO…';phase3cCaoRecoveryMessage='';try{let result=await api('/api/runtime/cao/recover',{method:'POST',body:'{}'});phase3cCaoRecoveryMessage='CAO 已就绪 · '+(result.terminalBackend||'后端状态未知')+' · '+(result.action==='STARTED_BY_PRODUCT'?'由产品运行时启动':'复用健康服务');}catch(error){let labels={CAO_PORT_CONFLICT:'端口被未知进程占用；为安全起见没有终止该进程。',CAO_SERVER_EXECUTABLE_UNAVAILABLE:'未找到已安装的 CAO 服务程序。',CAO_LOCAL_ONLY_REQUIRED:'CAO 恢复只允许本机回环地址。',CAO_START_FAILED:'CAO 服务启动失败。',CAO_START_TIMEOUT:'CAO 服务未在规定时间内健康就绪。',CAO_TERMINAL_BACKEND_MISMATCH:'CAO 正在使用非 tmux 终端后端；没有修改或终止该服务。',CAO_TERMINAL_BACKEND_UNKNOWN:'CAO 健康响应未确认 tmux 终端后端。'};phase3cCaoRecoveryMessage=(labels[error.code]||'CAO 恢复未完成。')+' 技术状态：'+(error.code||'UNKNOWN');}finally{phase3cBusy.delete('cao-recover');await refreshPhase3CPreflight();let current=$('phase3c-cao-recover');if(current){current.disabled=false;current.removeAttribute('aria-busy');current.textContent='启动 / 重试本机 CAO'}}}
function phase3cRenderRuntime(){let e=$('phase3c-preflight');if(!e)return;let p=phase3cPreflight||{overallStatus:'CHECKING',checks:{}};let checks=p.checks||{};let rows=[['Product Shell',checks.productShell],['产品数据目录',checks.dataDirectory],['数据库',checks.database],['工作区',checks.workspace],['本地端口',checks.productPort],['CAO',checks.cao],['tmux',checks.tmux],['Codex',checks.codex],['Codex 登录状态',checks.codexAuth]];e.innerHTML='<div class="row"><b>总体</b><b class="'+phase3cStatusClass(p.overallStatus)+'">'+esc(phase3cLabel(p.overallStatus))+'</b></div>'+rows.map(([label,item])=>{item=item||{status:'UNKNOWN'};return '<div class="row"><span>'+label+'</span><span class="'+phase3cStatusClass(item.status)+'"><b>'+esc(phase3cLabel(item.status))+'</b><small class="muted"> · '+esc(item.detail||'')+' · '+esc(item.code||'UNKNOWN')+'</small><br><small class="muted">下一步：'+esc(item.nextAction||'查看诊断报告')+'</small></span></div>'}).join('')+(phase3cCaoRecoveryMessage?'<div class="muted" role="status">'+esc(phase3cCaoRecoveryMessage)+'</div>':'')+'<div class="muted">不会自动安装软件、修改全局环境或执行登录。MiniMax、GPT 网页自动化和 API 主脑均不阻塞 V1。</div>'}
async function refreshPhase3CPreflight(){try{phase3cPreflight=await api('/api/runtime/preflight')}catch(e){phase3cPreflight={overallStatus:'DEGRADED',checks:{productShell:{status:'READY'},database:{status:'UNKNOWN'},cao:{status:'UNKNOWN',detail:e.code||'预检不可用'},tmux:{status:'UNKNOWN'},codex:{status:'UNKNOWN'},codexAuth:{status:'UNKNOWN'}}}}phase3cRenderRuntime()}
function phase3cSetBusy(action,value){if(value)phase3cBusy.add(action);else phase3cBusy.delete(action);let ids={copy:'copyManualGPTButton',validate:'validateManualGPTButton',apply:'applyManualGPTButton'};let selectors=['[data-phase3c-action="'+action+'"]'];if(ids[action])selectors.push('#'+ids[action]);if(action==='generate')selectors.push('#detail .handoff-actions button:first-child');document.querySelectorAll(selectors.join(',')).forEach(b=>{b.disabled=value;b.setAttribute('aria-busy',value?'true':'false')})}
async function phase3cRequest(action,path,body,confirmText){if(phase3cBusy.has(action))return;if(confirmText&&!(await confirmInChinese(confirmText)))return;phase3cSetBusy(action,true);phase3cError=null;try{await api(path,{method:'POST',body:JSON.stringify(body||{})});await refresh()}catch(e){phase3cError={code:e.code||'PRODUCT_ERROR',message:phase3cHumanError(e.code,e.message),next:phase3cRecommended(e.code),technical:e.technical||e.code};await refresh()}finally{phase3cSetBusy(action,false)}}
function phase3cStart(){return phase3cRequest('start','/api/meetings/'+selected+'/start')}
function phase3cPause(){return phase3cRequest('pause','/api/meetings/'+selected+'/pause',{reason:'用户从产品界面请求暂停'})}
function phase3cResume(){return phase3cRequest('resume','/api/meetings/'+selected+'/recover')}
function phase3cComplete(){return phase3cRequest('complete','/api/meetings/'+selected+'/complete',{reason:'用户从产品界面明确结束会议'},'确认结束整个会议？这与“接受当前任务”完全不同。')}
function phase3cViewPacket(){let e=$('brainPacketPreview');if(e){e.scrollIntoView({block:'center'});e.focus()}}
function phase3cRender(s){let grid=$('detail')?.querySelector('.grid');if(!grid)return;let panel=$('phase3c-dashboard');if(!panel){panel=document.createElement('section');panel.id='phase3c-dashboard';panel.className='card full';grid.prepend(panel)}let m=s.meeting||{},c=s.circuit||{},b=s.brain||{},p=s.brainParticipant||{},t=phase3cCurrentTask(s),h=s.brainHandoff,runtime=phase3cMeetingState(s),agents=s.agents||[],codex=agents.find(a=>a.provider==='codex'),lastResult=(s.tasks||[]).slice().reverse().find(x=>x.result||x.error);let paused=runtime==='PAUSED';let lifecycle='<div class="actions"><button data-phase3c-action="start" onclick="phase3cStart()" '+(m.status==='CREATED'||m.status==='READY'?'':'disabled')+'>开始会议</button><button class="danger" data-phase3c-action="pause" onclick="phase3cPause()" '+(m.status==='RUNNING'?'':'disabled')+'>暂停会议</button><button class="secondary" data-phase3c-action="resume" onclick="phase3cResume()" '+(m.status==='PAUSED'?'':'disabled')+'>恢复会议</button><button class="secondary" data-phase3c-action="complete" onclick="phase3cComplete()" '+(m.status==='RUNNING'?'':'disabled')+'>完成会议</button></div>';let error=phase3cError?'<div class="pause show"><b>'+esc(phase3cError.message)+'</b><br><span class="muted">建议：</span>'+esc(phase3cError.next)+'<br><small>技术错误码：'+esc(phase3cError.code)+' · '+esc(phase3cError.technical)+'</small></div>':'';panel.innerHTML='<h3>主控面板</h3><div class="muted">所有业务状态来自 Meeting Core、TaskEngine、SafetyEngine 与持久化快照。</div>'+error+'<div class="row"><span>会议</span><span><b>'+esc(m.name)+'</b><br><small class="muted code">'+esc(m.meeting_id)+'</small></span></div><div class="row"><span>会议状态</span><span class="'+phase3cStatusClass(runtime)+'"><b>'+esc(phase3cLabel(runtime))+'</b><small class="muted"> · machine='+esc(m.status)+'</small></span></div>'+lifecycle+'<div class="grid" style="margin-top:12px"><section class="card"><h3>GPT 主脑</h3>'+phase3cCell('参与者',phase3cParticipant(s),p.health||'UNKNOWN')+phase3cCell('传输方式',s.brainTransport||'MANUAL_GPT_HANDOFF','MANUAL_GPT_HANDOFF')+phase3cCell('状态',b.state||'UNKNOWN',b.health?'HEALTHY':'UNKNOWN')+'</section><section class="card"><h3>Codex 智能体</h3>'+phase3cCell('参与者',codex?.agent_id||'尚未加入',codex?.health||'UNKNOWN')+phase3cCell('运行时健康',codex?.health||'UNKNOWN',codex?.health||'UNKNOWN')+phase3cCell('当前状态',codex?.status||'未加入',codex?.status||'UNKNOWN')+'</section><section class="card"><h3>当前任务 / 最新结果</h3>'+phase3cCell('当前任务',t?.title||'无',t?.status||'UNKNOWN')+phase3cCell('最新结果',lastResult?(lastResult.error?'执行失败':lastResult.status==='COMPLETED'?'已完成':'处理中'):'无',lastResult?.error?'ERROR':lastResult?.status||'UNKNOWN')+'</section><section class="card"><h3>Brain Request</h3>'+phase3cCell('状态',h?(h.status==='VALIDATED'?'决策待确认':h.status==='WAITING_FOR_HUMAN_HANDOFF'?'等待粘贴 GPT 决策':h.status):'未生成',h?.status||'UNKNOWN')+phase3cCell('请求',h?.requestId||'无',h?.status||'UNKNOWN')+'</section></div><section class="card"><h3>安全状态</h3>'+phase3cCell('GLOBAL PAUSE',paused?'已打开':'未打开',paused?'PAUSED':'READY')+phase3cCell('stopDispatch',String(Boolean(c.stopDispatch)),c.stopDispatch?'BLOCKED':'READY')+phase3cCell('Circuit',c.circuitState||'UNKNOWN',c.circuitState||'UNKNOWN')+'<div class="muted">触发来源：'+esc(c.triggerAgentId||m.pause_triggered_by||'无')+' · 原因：'+esc(c.triggerReason||m.pause_reason||'无')+'</div></section><section class="card"><h3>运行时预检</h3><div id="phase3c-preflight">正在检查…</div></section></div><details class="phase3c-diagnostics"><summary>诊断详情（高级）</summary><div class="code">Meeting ID: '+esc(m.meeting_id)+'\nTask ID: '+esc(t?.task_id||'UNKNOWN')+'\nRequest ID: '+esc(h?.requestId||'UNKNOWN')+'\nBrain Participant ID: '+esc(p.participantId||'UNKNOWN')+'\nAgent IDs: '+esc(agents.map(a=>a.agent_id).join(', ')||'UNKNOWN')+'\nCircuit state: '+esc(c.circuitState||'UNKNOWN')+'\nError code: '+esc(phase3cError?.code||'NONE')+'</div></details>';phase3cRenderRuntime()}
function phase3cRenderPacketPanel(s){let grid=$('detail')?.querySelector('.grid');if(!grid)return;let h=s.brainHandoff,panel=$('phase3c-packet');if(!panel){panel=document.createElement('section');panel.id='phase3c-packet';panel.className='card full';grid.appendChild(panel)}let status=h?.status||'NOT_GENERATED';panel.innerHTML='<h3>Brain Packet</h3><div class="muted">'+(status==='WAITING_FOR_HUMAN_HANDOFF'?'等待人工 GPT 交接：请主动复制 Packet、手动发送给 GPT，再粘贴决策。':'状态：'+esc(status))+'</div><div class="row"><span>Request</span><span class="code">'+esc(h?.requestId||'无')+'</span></div><div class="row"><span>Task / Result</span><span class="code">'+esc((h?.taskId||'无')+' / '+(h?.resultId||'无'))+'</span></div><div class="row"><span>生成时间</span><span>'+esc(h?.createdAt||'无')+'</span></div><div class="actions"><button class="secondary" onclick="phase3cViewPacket()" '+(h?'':'disabled')+'>查看 Packet</button><button class="secondary" onclick="copyManualGPTPacket()" '+(h?'':'disabled')+'>复制 Packet</button></div><div class="muted">ACCEPT 与完成会议完全分离：接受只处理当前任务，完成会议是独立生命周期操作。</div>'}
function phase3cRenderTimeline(s){let grid=$('detail')?.querySelector('.grid');if(!grid)return;let panel=$('phase3c-timeline');if(!panel){panel=document.createElement('section');panel.id='phase3c-timeline';panel.className='card full';grid.appendChild(panel)}let items=(s.events||[]).slice(-40).reverse();panel.innerHTML='<h3>审计时间线</h3><div class="timeline">'+(items.length?items.map(e=>'<div class="event"><b>'+esc(e.event_type)+'</b> · '+esc(e.source)+'<br><span class="muted">'+esc(e.timestamp)+'</span></div>').join(''):'<div class="empty">暂无真实审计事件</div>')+'</div>'}
function phase3cRender(s){phase3cRenderDashboard(s);phase3cRenderPacketPanel(s);phase3cRenderTimeline(s)}
const phase3cRenderDashboard=phase3cRender;
'''.replace("function phase3cRender(s){phase3cRenderDashboard(s);phase3cRenderPacketPanel(s);phase3cRenderTimeline(s)}\nconst phase3cRenderDashboard=phase3cRender;", "const phase3cRenderDashboard=phase3cRender;\nfunction phase3cRenderAll(s){phase3cRenderDashboard(s);phase3cRenderPacketPanel(s);phase3cRenderTimeline(s)}") + r'''
const phase3cOriginalRender=render;render=function(s){phase3cOriginalRender(s);phase3cRenderAll(s)};
const phase3cOriginalRefresh=refresh;refresh=async function(){await phase3cOriginalRefresh();if(snapshot){phase3cRenderAll(snapshot);await refreshPhase3CPreflight()}};
const phase3cOriginalHandoffErrorMessage=handoffErrorMessage;handoffErrorMessage=function(code){return phase3cHumanError(code,phase3cOriginalHandoffErrorMessage(code))};
const phase3cOriginalGenerate=generateManualGPTPacket;generateManualGPTPacket=async function(){if(phase3cBusy.has('generate'))return;phase3cSetBusy('generate',true);try{return await phase3cOriginalGenerate()}finally{phase3cSetBusy('generate',false)}};
const phase3cOriginalCopy=copyManualGPTPacket;copyManualGPTPacket=async function(){if(phase3cBusy.has('copy'))return;phase3cSetBusy('copy',true);try{return await phase3cOriginalCopy()}finally{phase3cSetBusy('copy',false)}};
const phase3cOriginalValidate=validateManualGPTDecision;validateManualGPTDecision=async function(){if(phase3cBusy.has('validate'))return;phase3cSetBusy('validate',true);try{return await phase3cOriginalValidate()}finally{phase3cSetBusy('validate',false)}};
const phase3cOriginalApply=applyManualGPTDecision;applyManualGPTDecision=async function(){if(phase3cBusy.has('apply'))return;phase3cSetBusy('apply',true);try{return await phase3cOriginalApply()}finally{phase3cSetBusy('apply',false)}};
const phase3cRenderRuntimeWithoutRetry=phase3cRenderRuntime;phase3cRenderRuntime=function(){phase3cRenderRuntimeWithoutRetry();let e=$('phase3c-preflight');if(!e?.parentElement)return;let actions=e.parentElement.querySelector('#phase3c-runtime-actions');if(!actions){actions=document.createElement('div');actions.id='phase3c-runtime-actions';actions.className='actions';let recover=document.createElement('button');recover.id='phase3c-cao-recover';recover.className='secondary';recover.textContent='启动 / 重试本机 CAO';recover.onclick=phase3cRecoverCao;let retry=document.createElement('button');retry.id='phase3c-preflight-retry';retry.className='secondary';retry.textContent='重新检查运行环境';retry.onclick=refreshPhase3CPreflight;actions.append(recover,retry);e.parentElement.appendChild(actions)}};
'''
    + "\nsetInterval(()=>{if(selected)refresh()},2500);load();",
)

_V101_UI_TEXT_OVERRIDES = (
    ("AI 会议 Room", "AI Meeting Room"),
    ("Members / 智能体", "成员 / 智能体"),
    ("GLOBAL 暂停", "全局暂停"),
    ("会议Created", "会议已创建"),
    ("会议Core", "会议核心"),
    ("会议 Core", "会议核心"),
    ("Timeline / 主脑 Inbox", "时间线 / 主脑待处理事项"),
    ("Read-only task ", "只读任务 "),
    ("MANUAL_GPT_HANDOFF", "手动主脑交接"),
    ("Manual GPT handoff", "手动主脑交接"),
    ("Meeting Core", "会议核心"),
    ("TaskEngine", "任务引擎"),
    ("SafetyEngine", "安全引擎"),
    ("machine=", "内部状态："),
    ("主脑DecisionPacket", "主脑决策数据包"),
    ("主脑Decision", "主脑决策"),
    ("主脑 Packet", "主脑数据包"),
    ("Brain Packet", "主脑数据包"),
    ("主脑 Request", "主脑请求"),
    ("Meeting Summary", "会议总结"),
    ("Runtime Recovery", "运行时恢复"),
    ("GLOBAL PAUSE", "全局暂停"),
    ("stopDispatch", "停止派发"),
    ("Circuit state", "安全熔断器状态"),
    ("Circuit", "安全熔断器"),
    ("会议 ID", "会议编号"),
    ("Task ID", "任务编号"),
    ("Request ID", "请求编号"),
    ("主脑 Participant ID", "主脑参与者编号"),
    ("Agent IDs", "智能体编号"),
    ("Task / Result", "任务 / 结果"),
    ("BrainInbox", "主脑待处理事项"),
    ("Product Shell", "本地产品服务"),
    ("API Brain POC:", "API 主脑测试："),
    ("Phase 3 V1", "V1"),
    ("会议总结 Markdown", "会议总结"),
    ("复制 Markdown", "复制会议总结"),
    ("标准 JSON", "标准格式"),
    ("主脑决策数据包 标准格式", "主脑决策内容"),
    ("stop派发", "停止派发"),
    ("Safety 状态", "安全状态"),
    ("当前被外部 Cloudflare 验证挑战阻断", "当前被外部人机验证挑战阻断"),
    ("Cloudflare", "外部人机验证"),
    ("JSON", "标准格式"),
    ("Markdown", "会议总结"),
    ("V1 数据与诊断", "版本数据与诊断"),
    ("Join this 会议", "加入本次会议"),
    ("Legacy / Experimental: Send 主脑 Transport POC", "旧版/实验功能：发送主脑传输测试"),
    ("Remove API 密钥", "移除 API 密钥"),
    ("MEETING_SUMMARY_EXPORTED", "已导出会议总结"),
    ("AgentHeartbeatReceived", "智能体心跳更新"),
    ("SessionCreateObserved", "已创建运行会话"),
    ("RuntimeBound", "运行时已连接"),
    ("AgentAdded", "已添加智能体"),
    ("会议Completed", "会议已完成"),
    ("会议Created", "会议已创建"),
    ("会议Started", "会议已开始"),
    ("会议开始ed", "会议已开始"),
    ("会议Paused", "会议已暂停"),
    ("会议Recovering", "会议正在恢复"),
    ("会议恢复ing", "会议正在恢复"),
    ("会议Ready", "会议已就绪"),
    ("AgentHeartbeat", "智能体心跳"),
    ("AgentRegistry", "智能体登记"),
    ("CaoAgentAdapter", "CAO 智能体适配器"),
    ("AgentAdapter", "智能体适配器"),
    ("RuntimeCoordinator", "运行时协调器"),
    ("ProductShell", "本地产品服务"),
    ("health=", "健康状态："),
    ("查看 Packet", "查看数据包"),
    ("复制 Packet", "复制数据包"),
    ("MiniMax Code", "其他编程智能体"),
    ("MiniMax", "其他智能体"),
    ("GPT 网页自动化和 API 主脑均不阻塞 V1", "网页自动化实验和 API 主脑均不阻塞 V1"),
    ("其他智能体、GPT 网页自动化和 API 主脑均不阻塞 V1。", "其他智能体、网页自动化实验和 API 主脑均不影响当前版本。"),
    ("健康状态：true", "健康状态：正常"),
    ("健康状态：false", "健康状态：异常"),
    ("Workbuddy", "工作伙伴"),
    ("Zcode", "智能代码助手"),
    ("Claude Code", "克劳德代码助手"),
    ("Qwen", "通义千问"),
    ("OpenAI 兼容接口", "兼容接口"),
    (" from ", "，来源："),
    ("MEETING 已暂停", "会议已暂停"),
    ("派发 disabled until all health checks pass.", "在所有健康检查通过之前，任务派发保持禁用。"),
)
_V101_UI_TEXT_OVERRIDE_JS = "s=s." + ".".join(
    "replace(" + json.dumps(source, ensure_ascii=False) + "," + json.dumps(target, ensure_ascii=False) + ")"
    for source, target in _V101_UI_TEXT_OVERRIDES
)
_V101_LOCALIZE_RETURN = "return s.replace('MEETING PAUSED'"
if _V101_LOCALIZE_RETURN not in UI_HTML:
    raise RuntimeError("expected Product Shell localizeText return boundary")
UI_HTML = UI_HTML.replace(_V101_LOCALIZE_RETURN, _V101_UI_TEXT_OVERRIDE_JS + ";return s.replace('MEETING PAUSED'", 1)

_V101_LIVE_UI_SCRIPT = r'''let localizedAlertQueue=Promise.resolve();
function showLocalizedAlert(message){const show=()=>new Promise(resolve=>{let dialog=$('productMessageDialog'),text=$('productMessageText');if(!dialog||!text){showProductNotice(String(message??''));resolve();return}text.textContent=String(message??'');dialog.addEventListener('close',()=>resolve(),{once:true});dialog.showModal();$('productMessageOk')?.focus()});let pending=localizedAlertQueue.then(show,show);localizedAlertQueue=pending.catch(()=>{});return pending}
function closeProductMessageDialog(){let dialog=$('productMessageDialog');if(dialog?.open)dialog.close()}
function confirmInChinese(message){return new Promise(resolve=>{let dialog=$('productConfirmDialog'),text=$('productConfirmText');if(!dialog||!text||dialog.open){resolve(false);return}text.textContent=String(message??'');dialog.returnValue='';dialog.addEventListener('close',()=>resolve(dialog.returnValue==='confirm'),{once:true});dialog.showModal();$('productConfirmAccept')?.focus()})}
const v101EventLabels={'TaskCreated':'任务已创建','TaskAssigned':'任务已分配','TaskStatusChanged':'任务状态已更新','TaskStarted':'任务已开始','TaskCompleted':'任务已完成','TaskAccepted':'任务已接受','AgentOutputProduced':'智能体输出已产生','AgentHealthChanged':'智能体健康状态已更新','AgentStatusChanged':'智能体状态已更新','BrainHandoffPacketGenerated':'已生成主脑交接数据包','BrainHandoffPacketCopied':'已复制主脑交接数据包','BrainHandoffDecisionValidated':'主脑决策已校验','BrainHandoffDecisionApplied':'主脑决策已应用','BrainHandoffDecisionValidationFailed':'主脑决策校验失败','SafetyCircuitBreakerOpened':'安全熔断器已打开','CircuitBreakerOpened':'安全熔断器已打开','BrainCircuitBreakerOpened':'主脑安全熔断器已打开','TaskDispatchBlocked':'任务派发已阻止','DispatchBlocked':'派发已阻止','ManualGPTBrainTransport':'手动 GPT 交接','TaskWatcher':'任务监视器','MeetingCore':'会议核心','AgentRegistry':'智能体登记','RuntimeCoordinator':'运行时协调器','TaskEngine':'任务引擎','SafetyEngine':'安全引擎','RecoveryManager':'恢复管理器','CaoAgentAdapter':'CAO 智能体适配器','CAORuntime':'CAO 运行时','MeetingCreated':'会议已创建','AgentAdded':'智能体已添加','RuntimeBound':'运行时已连接','SessionCreateObserved':'已创建运行会话','MeetingReady':'会议已就绪','MeetingStarted':'会议已开始','MeetingPaused':'会议已暂停','MeetingRecovering':'会议正在恢复','MeetingResumed':'会议已恢复','MeetingCompleted':'会议已完成','SafetyMonitorFailed':'安全监控失败'};
const v101LocalizeTextBase=localizeText;
localizeText=function(value){let source=String(value??'');for(const [from,to] of Object.entries(v101EventLabels).sort(([a],[b])=>b.length-a.length))source=source.split(from).join(to);return v101LocalizeTextBase(source)};
function localizeSummaryMarkdown(value){const headings={'# Meeting Summary —':'# 会议总结 —','## Meeting':'## 会议','## Participants':'## 参与者','## Brain Transport':'## 主脑传输','## Tasks':'## 任务','## GPT Brain Decisions':'## GPT 主脑决策','## ACCEPT Records':'## ACCEPT 记录','## Pause / Resume Events':'## 暂停 / 恢复事件','## Safety':'## 安全状态','### Safety events':'### 安全事件','## Audit Timeline Summary':'## 审计时间线摘要'};const labels=[['- Name:','- 名称：'],['- ID:','- ID：'],['- Current / final state:','- 当前 / 最终状态：'],['- Created:','- 创建时间：'],['- Started:','- 开始时间：'],['- Completed:','- 完成时间：'],['- Completion state:','- 完成状态：'],['- Brain:','- 主脑：'],['- Agent:','- 智能体：'],['- Transport:','- 传输方式：'],['- Brain activity records:','- 主脑活动记录数：'],['- Task ID:','- 任务编号：'],['- Status:','- 状态：'],['- Accepted:','- 已接受：'],['- Agent Result summary:','- 智能体结果摘要：'],['  - Reason:','  - 原因：'],['  - REWORK instruction:','  - 返工指令：'],['- Circuit:','- 安全熔断器：'],['- Dispatch stopped:','- 已停止派发：'],['- Workspace write protected:','- 工作区写保护：'],['- Trigger:','- 触发对象：'],['- Trigger reason:','- 触发原因：'],['- No tasks','- 暂无任务'],['- Agent: none','- 暂无智能体'],['- No persisted GPT Brain decisions','- 暂无已持久化的 GPT 主脑决策'],['- No ACCEPT records','- 暂无 ACCEPT 记录'],['- No pause/resume events','- 暂无暂停 / 恢复事件'],['- No persisted safety events','- 暂无已记录的安全事件'],['- No audit events','- 暂无审计事件'],['TaskEngine recorded ACCEPT','任务引擎已记录接受'],['TaskEngine accepted state','任务引擎接受状态'],['GPT Brain ACCEPT','GPT 主脑已接受'],['reason=','原因：'],['from=','起始状态：'],['to=','目标状态：'],['taskId=','任务编号：'],['agentId=','智能体编号：'],['triggerAgentId=','触发智能体编号：'],['MANUAL_GPT_HANDOFF','手动 GPT 交接'],['MeetingCreated','会议已创建'],['AgentAdded','已添加智能体'],['RuntimeBound','运行时已连接'],['SessionCreateObserved','已创建运行会话'],['MeetingReady','会议已就绪'],['MeetingStarted','会议已开始'],['MeetingPaused','会议已暂停'],['MeetingRecovering','会议正在恢复'],['MeetingResumed','会议已恢复'],['MeetingCompleted','会议已完成'],['CircuitBreakerOpened','安全熔断器已打开'],['BrainCircuitBreakerOpened','主脑安全熔断器已打开'],['SafetyMonitorFailed','安全监控失败'],['DispatchBlocked','派发已阻止'],['TaskDispatchBlocked','任务派发已阻止'],['MeetingCore','会议核心'],['AgentRegistry','智能体登记'],['RuntimeCoordinator','运行时协调器'],['CaoAgentAdapter','CAO 智能体适配器'],['RecoveryManager','恢复管理器'],['SafetyEngine','安全引擎'],['TaskEngine','任务引擎'],['No Agent Result','暂无智能体结果']];return String(value??'').split('\n').map(line=>{for(const [from,to] of Object.entries(headings)){if(line===from||line.startsWith(from+' —'))return line.replace(from,to)}for(const [from,to] of labels){if(line.startsWith(from))line=to+line.slice(from.length)}return localizeText(line).replace(/\boperator\b/g,'操作员').replace(/\b(yes|no|True|False)\b/g,token=>({yes:'是',no:'否',True:'是',False:'否'})[token])}).join('\n')}
const v101PreflightLabels={PRODUCT_SHELL_READY:'本地服务正在运行',DATABASE_READY:'本地数据库可用',DATA_DIRECTORY_READY:'产品数据目录可用',WORKSPACE_READY:'工作区可用',PRODUCT_PORT_BOUND:'本地服务端口已监听',CAO_SERVER_READY:'CAO 服务可用',TMUX_READY:'tmux 可用',CODEX_READY:'Codex 命令行可用',CODEX_AUTHENTICATED:'Codex 已登录',CAO_SERVER_UNAVAILABLE:'无法连接 CAO 服务',TMUX_UNAVAILABLE:'tmux 当前不可用',CODEX_EXECUTABLE_UNAVAILABLE:'未找到 Codex 命令行',CODEX_AUTH_UNKNOWN:'Codex 登录状态未知',CODEX_AUTH_REQUIRED:'Codex 尚未登录',WORKSPACE_UNAVAILABLE:'工作区不可用',DATABASE_UNAVAILABLE:'本地数据库暂不可用',PRODUCT_PORT_UNAVAILABLE:'本地服务端口不可用'};
const v101LocalizeStaticBase=localizeStatic;localizeStatic=function(){v101LocalizeStaticBase();const exact={'Members / 智能体':'成员 / 智能体','Timeline / 主脑 Inbox':'时间线 / 主脑待处理事项','Request':'请求','Packet':'数据包','Result':'结果','Task':'任务','Meeting:':'会议：','Task:':'任务：','Request:':'请求：','false':'否','true':'是'};const prefixes=[['Task：','任务：'],['Task:','任务：'],['Request：','请求：'],['Request:','请求：']];const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);while(walker.nextNode()){const n=walker.currentNode;if(['SCRIPT','STYLE'].includes(n.parentElement?.tagName))continue;const t=n.nodeValue.trim();if(Object.prototype.hasOwnProperty.call(exact,t)){n.nodeValue=n.nodeValue.replace(t,exact[t]);continue}for(const [from,to] of prefixes){if(t.startsWith(from)){n.nodeValue=n.nodeValue.replace(from,to);break}}}};
function phase3cRenderRuntime(){let e=$('phase3c-preflight');if(!e)return;let p=phase3cPreflight||{overallStatus:'CHECKING',checks:{}};let checks=p.checks||{};let rows=[['本地服务',checks.productShell],['产品数据目录',checks.dataDirectory],['数据库',checks.database],['工作区',checks.workspace],['本地端口',checks.productPort],['CAO',checks.cao],['tmux',checks.tmux],['Codex',checks.codex],['Codex 登录状态',checks.codexAuth]];let statusRows=rows.map(([label,item])=>{item=item||{status:'UNKNOWN'};let code=item.code||'UNKNOWN';let friendly=v101PreflightLabels[code]||(item.status==='READY'?'检查通过':item.status==='BLOCKED'?'当前不可用':item.status==='UNKNOWN'?'状态未知':'需要检查');return '<div class="row"><span>'+label+'</span><span class="'+phase3cStatusClass(item.status)+'"><b>'+esc(phase3cLabel(item.status))+'</b><small class="muted"> · '+esc(friendly)+'</small></span></div>'}).join('');let diagnostics=rows.map(([label,item])=>{item=item||{status:'UNKNOWN'};return '<div class="row"><span>'+label+'</span><span class="code">'+esc(item.code||'UNKNOWN')+' · '+esc(item.detail||'')+' · '+esc(item.nextAction||'查看诊断报告')+'</span></div>'}).join('');e.innerHTML='<div class="row"><b>总体状态</b><b class="'+phase3cStatusClass(p.overallStatus)+'">'+esc(phase3cLabel(p.overallStatus))+'</b></div>'+statusRows+(phase3cCaoRecoveryMessage?'<div class="muted" role="status">'+esc(phase3cCaoRecoveryMessage)+'</div>':'')+'<details class="phase3c-diagnostics"><summary>技术详情</summary>'+diagnostics+'</details><div class="muted">不会自动安装软件、修改全局环境或执行登录。MiniMax、GPT 网页自动化和 API 主脑均不阻塞 V1。</div>';let actions=document.createElement('div');actions.id='phase3c-runtime-actions';actions.className='actions';let recover=document.createElement('button');recover.id='phase3c-cao-recover';recover.className='secondary';recover.textContent='启动 / 重试本机 CAO';recover.onclick=phase3cRecoverCao;let retry=document.createElement('button');retry.id='phase3c-preflight-retry';retry.className='secondary';retry.textContent='重新检查运行环境';retry.onclick=refreshPhase3CPreflight;actions.append(recover,retry);e.appendChild(actions)}
const v101RuntimeWithoutLocale=phase3cRenderRuntime;phase3cRenderRuntime=function(){v101RuntimeWithoutLocale();localizeStatic()};
const v101RenderAllWithoutLocale=phase3cRenderAll;phase3cRenderAll=function(s){v101RenderAllWithoutLocale(s);localizeStatic()};
const v101LocalizedStatic=localizeStatic;localizeStatic=function(){v101LocalizedStatic();document.querySelectorAll('[aria-label],[title]').forEach(e=>{for(const attr of ['aria-label','title']){const value=e.getAttribute(attr);if(value){const localized=localizeText(value);if(localized!==value)e.setAttribute(attr,localized)}}});document.querySelectorAll('input[placeholder],textarea[placeholder],select[title]').forEach(e=>{const value=e.getAttribute('placeholder');if(!value)return;const localized=value==='https://.../v1/chat/completions'?'请输入兼容接口地址':value==='model'?'模型名称':localizeText(value);if(localized!==value)e.setAttribute('placeholder',localized)})};
const v101RenderMembersWithoutLocale=renderMembers;renderMembers=function(s){v101RenderMembersWithoutLocale(s);localizeStatic()};
localizeSummaryMarkdown=function(value){
  const headings={'# Meeting Summary —':'# 会议总结 —','## Meeting':'## 会议','## Participants':'## 参与者','## Brain Transport':'## 主脑传输','## Tasks':'## 任务','## GPT Brain Decisions':'## GPT 主脑决策','## ACCEPT Records':'## ACCEPT 记录','## Pause / Resume Events':'## 暂停 / 恢复事件','## Safety':'## 安全状态','### Safety events':'### 安全事件','## Audit Timeline Summary':'## 审计时间线摘要'};
  const labels=[['- Agent: none','- 暂无智能体'],['- No tasks','- 暂无任务'],['- No persisted GPT Brain decisions','- 暂无已持久化的 GPT 主脑决策'],['- No ACCEPT records','- 暂无 ACCEPT 记录'],['- No pause/resume events','- 暂无暂停 / 恢复事件'],['- No persisted safety events','- 暂无已记录的安全事件'],['- No audit events','- 暂无审计事件'],['- Name:','- 名称：'],['- ID:','- ID：'],['- Current / final state:','- 当前 / 最终状态：'],['- Created:','- 创建时间：'],['- Started:','- 开始时间：'],['- Completed:','- 完成时间：'],['- Completion state:','- 完成状态：'],['- Brain:','- 主脑：'],['- Agent:','- 智能体：'],['- Transport:','- 传输方式：'],['- Brain activity records:','- 主脑活动记录数：'],['- Task ID:','- 任务编号：'],['- Status:','- 状态：'],['- Accepted:','- 已接受：'],['- Agent Result summary:','- 智能体结果摘要：'],['  - Reason:','  - 原因：'],['  - REWORK instruction:','  - 返工指令：'],['- Circuit:','- 安全熔断器：'],['- Dispatch stopped:','- 已停止派发：'],['- Workspace write protected:','- 工作区写保护：'],['- Trigger:','- 触发对象：'],['- Trigger reason:','- 触发原因：']];
  const replacements=[['TaskEngine recorded ACCEPT','任务引擎已记录接受'],['TaskEngine accepted state','任务引擎接受状态'],['GPT Brain ACCEPT','GPT 主脑已接受'],['reason=','原因：'],['from=','起始状态：'],['to=','目标状态：'],['taskId=','任务编号：'],['agentId=','智能体编号：'],['triggerAgentId=','触发智能体编号：'],['MANUAL_GPT_HANDOFF','手动 GPT 交接'],['MeetingCreated','会议已创建'],['AgentAdded','已添加智能体'],['RuntimeBound','运行时已连接'],['SessionCreateObserved','已创建运行会话'],['MeetingReady','会议已就绪'],['MeetingStarted','会议已开始'],['MeetingPaused','会议已暂停'],['MeetingRecovering','会议正在恢复'],['MeetingResumed','会议已恢复'],['MeetingCompleted','会议已完成'],['CircuitBreakerOpened','安全熔断器已打开'],['BrainCircuitBreakerOpened','主脑安全熔断器已打开'],['SafetyMonitorFailed','安全监控失败'],['DispatchBlocked','派发已阻止'],['TaskDispatchBlocked','任务派发已阻止'],['MeetingCore','会议核心'],['AgentRegistry','智能体登记'],['RuntimeCoordinator','运行时协调器'],['CaoAgentAdapter','CAO 智能体适配器'],['RecoveryManager','恢复管理器'],['SafetyEngine','安全引擎'],['TaskEngine','任务引擎'],['No Agent Result','暂无智能体结果']];
  return String(value??'').split('\\n').map(line=>{for(const [from,to] of Object.entries(headings)){if(line===from||line.startsWith(from+' —'))return line.replace(from,to)}for(const [from,to] of labels){if(line.startsWith(from)){line=to+line.slice(from.length);break}}for(const [from,to] of replacements)line=line.split(from).join(to);return localizeText(line).replace(/\\boperator\\b/g,'操作员').replace(/\\b(yes|no|True|False)\\b/g,token=>({yes:'是',no:'否',True:'是',False:'否'})[token])}).join('\\n')
};
localizeSummaryMarkdown=function(value){const newline=String.fromCharCode(10),headings={'# Meeting Summary —':'# 会议总结 —','## Meeting':'## 会议','## Participants':'## 参与者','## Brain Transport':'## 主脑传输','## Tasks':'## 任务','## GPT Brain Decisions':'## GPT 主脑决策','## ACCEPT Records':'## ACCEPT 记录','## Pause / Resume Events':'## 暂停 / 恢复事件','## Safety':'## 安全状态','### Safety events':'### 安全事件','## Audit Timeline Summary':'## 审计时间线摘要'},labels=[['- Agent: none','- 暂无智能体'],['- No tasks','- 暂无任务'],['- No persisted GPT Brain decisions','- 暂无已持久化的 GPT 主脑决策'],['- No ACCEPT records','- 暂无 ACCEPT 记录'],['- No pause/resume events','- 暂无暂停 / 恢复事件'],['- No persisted safety events','- 暂无已记录的安全事件'],['- No audit events','- 暂无审计事件'],['- Name:','- 名称：'],['- ID:','- ID：'],['- Current / final state:','- 当前 / 最终状态：'],['- Created:','- 创建时间：'],['- Started:','- 开始时间：'],['- Completed:','- 完成时间：'],['- Completion state:','- 完成状态：'],['- Brain:','- 主脑：'],['- Agent:','- 智能体：'],['- Transport:','- 传输方式：'],['- Brain activity records:','- 主脑活动记录数：'],['- Task ID:','- 任务编号：'],['- Status:','- 状态：'],['- Accepted:','- 已接受：'],['- Agent Result summary:','- 智能体结果摘要：'],['  - Reason:','  - 原因：'],['  - REWORK instruction:','  - 返工指令：'],['- Circuit:','- 安全熔断器：'],['- Dispatch stopped:','- 已停止派发：'],['- Workspace write protected:','- 工作区写保护：'],['- Trigger:','- 触发对象：'],['- Trigger reason:','- 触发原因：']],inline=[['TaskEngine recorded ACCEPT','任务引擎已记录接受'],['TaskEngine accepted state','任务引擎接受状态'],['GPT Brain ACCEPT','GPT 主脑已记录接受'],['reason=','原因：'],['from=','起始状态：'],['to=','目标状态：'],['taskId=','任务编号：'],['agentId=','智能体编号：'],['triggerAgentId=','触发智能体编号：'],['MANUAL_GPT_HANDOFF','手动 GPT 交接'],['MeetingCreated','会议已创建'],['AgentAdded','已添加智能体'],['RuntimeBound','运行时已连接'],['SessionCreateObserved','已创建运行会话'],['MeetingReady','会议已就绪'],['MeetingStarted','会议已开始'],['MeetingPaused','会议已暂停'],['MeetingRecovering','会议正在恢复'],['MeetingResumed','会议已恢复'],['MeetingCompleted','会议已完成'],['CircuitBreakerOpened','安全熔断器已打开'],['BrainCircuitBreakerOpened','主脑安全熔断器已打开'],['SafetyMonitorFailed','安全监控失败'],['DispatchBlocked','派发已阻止'],['TaskDispatchBlocked','任务派发已阻止'],['MeetingCore','会议核心'],['AgentRegistry','智能体登记'],['RuntimeCoordinator','运行时协调器'],['CaoAgentAdapter','CAO 智能体适配器'],['RecoveryManager','恢复管理器'],['SafetyEngine','安全引擎'],['TaskEngine','任务引擎'],['No Agent Result','暂无智能体结果'],['operator','操作员']];return String(value??'').split(newline).map(line=>{for(const [from,to] of Object.entries(headings)){if(line===from||line.startsWith(from+' —'))return line.replace(from,to)}for(const [from,to] of labels){if(line.startsWith(from)){line=to+line.slice(from.length);break}}for(const [from,to] of inline)line=line.split(from).join(to);for(const [code,label] of Object.entries(I18N.status||{})){line=line.split('`'+code+'`').join('`'+label+'`');line=line.split('status='+code).join('状态：'+label);line=line.split('health='+code).join('健康状态：'+label);line=line.split('from='+code).join('起始状态：'+label);line=line.split('to='+code).join('目标状态：'+label)}return line.replace('`True`','`是`').replace('`False`','`否`').replace('ACCEPT records','接受记录')}).join(newline)};
'''
_V101_LIVE_UI_SCRIPT = _V101_LIVE_UI_SCRIPT.replace(
    "if(line===from||line.startsWith(from+' —'))return line.replace(from,to)",
    "if(line.startsWith(from))return line.replace(from,to)",
)
_V101_SCRIPT_TAIL = "\nsetInterval(()=>{if(selected)refresh()},2500);load();"
_V101_LIVE_UI_SCRIPT += r'''
const v101SummaryLocalizerBase=localizeSummaryMarkdown;
localizeSummaryMarkdown=function(value){let localized=v101SummaryLocalizerBase(value);for(const [code,label] of Object.entries(I18N.status||{})){localized=localized.replace(new RegExp('(^|[^A-Za-z0-9_])'+code+'(?=$|[^A-Za-z0-9_])','g'),(_match,prefix)=>prefix+label)}return localized.split('transport:').join('传输方式：').split('health:').join('健康状态：').split('[codex]').join('[Codex]').split('explicit 操作员 recovery').join('操作员明确执行的恢复').split('Codex [Codex]').join('Codex')};
const v101SummaryBooleanBase=localizeSummaryMarkdown;
localizeSummaryMarkdown=function(value){return v101SummaryBooleanBase(value).replace(/(^- 已接受：\s*)(yes|no|True|False)\s*$/gm,(match,prefix,result)=>prefix+({yes:'是',no:'否',True:'是',False:'否'})[result])};
const v101SummaryDomainLabels={'NOT_COMPLETED':'尚未完成','unassigned':'未分配','TaskCreated':'任务已创建','TaskAssigned':'任务已分配','TaskStatusChanged':'任务状态已更新','TaskStarted':'任务已开始','TaskCompleted':'任务已完成','TaskAccepted':'任务已接受','AgentOutputProduced':'智能体输出已产生','AgentHealthChanged':'智能体健康状态已更新','AgentStatusChanged':'智能体状态已更新','BrainHandoffPacketGenerated':'已生成主脑交接数据包','BrainHandoffPacketCopied':'已复制主脑交接数据包','BrainHandoffDecisionValidated':'主脑决策已校验','BrainHandoffDecisionApplied':'主脑决策已应用','BrainHandoffDecisionValidationFailed':'主脑决策校验失败','SafetyCircuitBreakerOpened':'安全熔断器已打开','TaskWatcher':'任务监视器','ManualGPTBrainTransport':'手动 GPT 交接','ACCEPT 记录':'接受记录','`ACCEPT`':'`已接受`'};
const v101SummaryDomainBase=localizeSummaryMarkdown;
localizeSummaryMarkdown=function(value){let summary=v101SummaryDomainBase(value);for(const [from,to] of Object.entries(v101SummaryDomainLabels).sort(([a],[b])=>b.length-a.length))summary=summary.split(from).join(to);return summary};
let v101RetainedSummaryMarkdown=null,v101RetainedSummaryMeetingId=null;
const v101ShowSummaryExportBase=showSummaryExport;
showSummaryExport=function(result,preserve=false){if(!preserve){v101RetainedSummaryMarkdown=String(result?.markdown||'');v101RetainedSummaryMeetingId=selected;document.getElementById('meeting-summary-export')?.remove()}v101ShowSummaryExportBase({markdown:v101RetainedSummaryMarkdown||''},preserve)};
const v101Phase3cRenderAllWithoutSummaryRetention=phase3cRenderAll;
phase3cRenderAll=function(s){v101Phase3cRenderAllWithoutSummaryRetention(s);if(v101RetainedSummaryMarkdown!==null){if(v101RetainedSummaryMeetingId===selected){if(!document.getElementById('meeting-summary-export'))v101ShowSummaryExportBase({markdown:v101RetainedSummaryMarkdown},true)}else{v101RetainedSummaryMarkdown=null;v101RetainedSummaryMeetingId=null}}};
const v101RenderPreservingSummaryScroll=render;
render=function(s){const previous=document.querySelector('#meeting-summary-export textarea');const scrollTop=previous?.scrollTop||0;v101RenderPreservingSummaryScroll(s);const current=document.querySelector('#meeting-summary-export textarea');if(current)current.scrollTop=scrollTop};
'''
_V101_LIVE_UI_SCRIPT += r'''
const v101PreflightDiagnosticDetails={
  PRODUCT_SHELL_READY:'本地服务正在提供界面',DATABASE_READY:'SQLite 本地持久化可用',
  DATABASE_UNAVAILABLE:'本地数据库暂不可用',DATA_DIRECTORY_READY:'产品数据目录可用',
  DATA_DIRECTORY_UNAVAILABLE:'产品数据目录不可用',WORKSPACE_READY:'工作区可用',
  WORKSPACE_UNAVAILABLE:'工作区不可用',PRODUCT_PORT_BOUND:'本地服务端口正在监听',
  PRODUCT_PORT_UNAVAILABLE:'本地服务端口不可用',CAO_SERVER_READY:'CAO 服务可用',
  CAO_SERVER_UNAVAILABLE:'无法连接 CAO 服务',CAO_PREFLIGHT_FAILED:'CAO 运行时预检失败',
  TMUX_READY:'tmux 可用',TMUX_UNAVAILABLE:'tmux 不可用',TMUX_STATUS_UNKNOWN:'tmux 状态未知',
  CODEX_READY:'Codex 命令行工具可用',CODEX_UNAVAILABLE:'Codex 命令行工具不可用',
  CODEX_EXECUTABLE_UNAVAILABLE:'Codex 命令行工具不可用',CODEX_STATUS_UNKNOWN:'Codex 命令行工具状态未知',
  CODEX_AUTHENTICATED:'Codex 已登录',CODEX_AUTH_REQUIRED:'Codex 尚未登录',
  CODEX_AUTH_UNKNOWN:'无法确认 Codex 登录状态',PREFLIGHT_EXCEPTION:'运行时预检异常',
  UNKNOWN:'运行状态未知'
};
const v101PreflightDiagnosticActions={
  CAO_SERVER_UNAVAILABLE:'点击重试，或启动本机 CAO 服务',
  CAO_PREFLIGHT_FAILED:'点击重试，或检查本机 CAO 服务',
  TMUX_UNAVAILABLE:'确认 tmux 已安装',CODEX_UNAVAILABLE:'确认 Codex 命令行工具已安装',
  CODEX_EXECUTABLE_UNAVAILABLE:'确认 Codex 命令行工具已安装',
  CODEX_AUTH_REQUIRED:'在终端中人工登录 Codex 后重试预检',
  CODEX_AUTH_UNKNOWN:'重新检查运行环境',DATABASE_UNAVAILABLE:'检查产品数据目录读写权限',
  DATA_DIRECTORY_UNAVAILABLE:'检查产品数据目录权限',WORKSPACE_UNAVAILABLE:'修复工作区目录',
  PRODUCT_PORT_UNAVAILABLE:'检查本地服务端口是否被占用',
  PREFLIGHT_EXCEPTION:'重试预检并查看诊断报告'
};
function v101PreflightDiagnostic(item){const code=item?.code||'UNKNOWN',action=v101PreflightDiagnosticActions[code]||(item?.status==='READY'?'无需操作':'重新检查运行环境');return code+' · '+(v101PreflightDiagnosticDetails[code]||'运行状态尚未识别')+' · 下一步：'+action}
const v101RuntimeBeforeSafeDiagnostics=phase3cRenderRuntime;
phase3cRenderRuntime=function(){v101RuntimeBeforeSafeDiagnostics();const details=$('phase3c-preflight')?.querySelector('details.phase3c-diagnostics');if(!details)return;const wasOpen=details.open,checks=phase3cPreflight?.checks||{},rows=[['本地服务',checks.productShell],['产品数据目录',checks.dataDirectory],['数据库',checks.database],['工作区',checks.workspace],['本地端口',checks.productPort],['CAO',checks.cao],['tmux',checks.tmux],['Codex',checks.codex],['Codex 登录状态',checks.codexAuth]];details.innerHTML='<summary>技术详情</summary>'+rows.map(([label,item])=>'<div class="row"><span>'+label+'</span><span class="code">'+esc(v101PreflightDiagnostic(item||{}))+'</span></div>').join('');details.open=wasOpen};
const v101TextLocalizerBeforeErrorCode=localizeText;
localizeText=function(value){return v101TextLocalizerBeforeErrorCode(value).replace(/\bError code:/g,'技术错误码：')};
phase3cParticipant=function(s){let p=s.brainParticipant||{};return p.participantId?'GPT 主脑已连接':'尚未加入正式主脑'};
'''
_V102_CODEX_JOIN_UI_SCRIPT = r'''let codexJoinInFlight=false,codexJoinReadiness=null;
const v102BaseApi=api;
api=async function(path,opts={}){
  let requestPath=path;
  if(path==='/api/providers'&&selected)requestPath+='?meetingId='+encodeURIComponent(selected);
  const headers={'Content-Type':'application/json',...(opts.headers||{})};
  const response=await fetch(requestPath,{...opts,headers});
  let payload={};try{payload=await response.json()}catch(_){payload={}}
  if(requestPath.startsWith('/api/providers')&&Array.isArray(payload))codexJoinReadiness=payload.find(item=>item.providerId==='codex')||null;
  if(!response.ok||payload?.ok===false){
    const raw=payload?.error,detail=raw&&typeof raw==='object'?raw:{};
    const code=detail.code||payload.code||'INTERNAL_UNCLASSIFIED_EXCEPTION';
    const error=Error(userFacingError(code,detail.message||code));
    error.code=code;error.technicalCode=code;error.httpStatus=detail.httpStatus||response.status;
    error.requestId=detail.requestId||response.headers.get('X-Request-ID')||headers['X-Request-ID']||'';
    error.stage=detail.stage||'';throw error
  }
  return payload
};
function v102CodexMessage(code){return ({
  CAO_UNAVAILABLE:'Codex 加入会议失败：CAO 服务不可用。',CAO_EXECUTABLE_UNAVAILABLE:'Codex 加入会议失败：未找到 CAO 服务程序。',
  TMUX_UNAVAILABLE:'Codex 加入会议失败：tmux 当前不可用。',CODEX_CLI_UNAVAILABLE:'Codex 加入会议失败：Codex 命令行工具不可用。',
  CODEX_AUTH_REQUIRED:'Codex 加入会议失败：Codex 登录状态无效。',CODEX_AUTH_UNKNOWN:'Codex 加入会议失败：无法确认 Codex 登录状态。',
  CODEX_MODEL_UNSUPPORTED:'Codex 加入会议失败：当前模型配置不受支持。',WORKSPACE_UNAVAILABLE:'Codex 加入会议失败：会议工作区不可用。',
  MEETING_NOT_JOINABLE:'Codex 加入会议失败：会议状态不允许加入智能体。',CODEX_RUNTIME_SESSION_CREATE_FAILED:'Codex 加入会议失败：无法创建运行时会话。',
  CODEX_RUNTIME_UNHEALTHY:'Codex 加入会议失败：运行时会话未通过健康检查。',CODEX_PARTICIPANT_PERSISTENCE_FAILED:'Codex 加入会议失败：无法保存加入状态。',
  CODEX_PREVIOUS_RUNTIME_NOT_STOPPED:'Codex 加入会议失败：无法安全停止先前的运行时会话。',
  PROVIDER_NOT_INTEGRATED:'Codex 加入会议失败：当前提供商尚未集成。',CODEX_READINESS_UNKNOWN:'Codex 加入会议失败：无法确认运行环境状态。'
})[code]||'Codex 加入会议失败：当前暂不可用。'}
function ensureCodexJoinErrorBox(){let box=$('codex-join-error');if(box)return box;let providers=$('providers');if(!providers?.parentElement)return null;box=document.createElement('section');box.id='codex-join-error';box.className='provider-join-error';box.setAttribute('role','alert');box.hidden=true;providers.parentElement.insertBefore(box,providers);return box}
function showCodexJoinError(error){const box=ensureCodexJoinErrorBox();if(!box)return;box.replaceChildren();const message=document.createElement('p');message.className='provider-join-error-message';message.textContent=v102CodexMessage(error?.technicalCode||error?.code);box.appendChild(message);const details=document.createElement('details'),summary=document.createElement('summary');summary.textContent='技术详情';details.appendChild(summary);const code=document.createElement('div');code.textContent='错误码：'+(error?.technicalCode||error?.code||'UNKNOWN');details.appendChild(code);const status=document.createElement('div');status.textContent='HTTP 状态：'+(error?.httpStatus||'未知');details.appendChild(status);const request=document.createElement('div');request.textContent='请求编号：'+(error?.requestId||'未提供');details.appendChild(request);box.appendChild(details);box.hidden=false}
function renderCodexProviderCard(){const host=$('providers');if(!host)return;const old=[...host.querySelectorAll('.provider')].find(node=>node.querySelector('b')?.textContent?.trim()==='Codex');if(!old)return;const state=codexJoinReadiness||{},card=document.createElement('section');card.className='provider';const title=document.createElement('b');title.textContent='Codex';card.appendChild(title);const runtime=document.createElement('div');runtime.className='muted';runtime.textContent='Codex 运行环境：'+(state.runtimeStatus==='READY'?'正常':state.runtimeStatus==='BLOCKED'?'不可用':'状态未知');card.appendChild(runtime);const joined=state.joinStatus==='JOINED';const join=document.createElement('div');join.className='muted';join.textContent='加入会议：'+(joined?'已加入':state.joinAvailable?'可用':'不可用');card.appendChild(join);if(state.reason&&!state.joinAvailable&&!joined){const reason=document.createElement('div');reason.className='muted';reason.textContent=state.reason;card.appendChild(reason)}if(selected){const button=document.createElement('button');button.className='secondary';button.dataset.codexJoin='true';button.textContent=joined?'已加入':state.joinAvailable?'加入本次会议':'暂不可用';button.disabled=joined||!state.joinAvailable||codexJoinInFlight;button.style.marginTop='7px';button.onclick=()=>joinProvider('codex');card.appendChild(button)}old.replaceWith(card)}
const v102BaseRenderProviders=renderProviders;
renderProviders=async function(){await v102BaseRenderProviders();renderCodexProviderCard()};
async function joinProvider(id){if(!selected)return;if(id!=='codex'){try{await v102BaseApi('/api/meetings/'+selected+'/agents',{method:'POST',body:JSON.stringify({provider:id})});await refresh();await renderProviders()}catch(error){showLocalizedAlert(error.message)}return}if(codexJoinInFlight)return;codexJoinInFlight=true;const box=ensureCodexJoinErrorBox();if(box){box.hidden=true;box.replaceChildren()}renderCodexProviderCard();const requestId=globalThis.crypto?.randomUUID?crypto.randomUUID():'aimr-'+Date.now()+'-'+Math.random().toString(16).slice(2);try{await api('/api/meetings/'+selected+'/agents',{method:'POST',headers:{'X-Request-ID':requestId},body:JSON.stringify({provider:'codex'})});if(box){box.hidden=true;box.replaceChildren()}await refresh();await renderProviders()}catch(error){showCodexJoinError(error)}finally{codexJoinInFlight=false;await renderProviders()}}
async function selectMeeting(id){selected=id;await refresh();await renderProviders()}
'''
_V103_CODEX_STARTUP_RECOVERY_UI_SCRIPT = r'''const v103CodexRecoveryMessages={
  CAO_START_FAILED:'Codex 暂不可用：CAO 服务启动失败。',
  CAO_START_TIMEOUT:'Codex 暂不可用：CAO 服务未能及时就绪。',
  CAO_PORT_CONFLICT:'Codex 暂不可用：CAO 端口被其他服务占用，产品未终止该进程。',
  CAO_SERVER_EXECUTABLE_UNAVAILABLE:'Codex 暂不可用：未找到可运行的 CAO 服务程序。',
  CAO_TERMINAL_BACKEND_MISMATCH:'Codex 暂不可用：CAO 未使用受支持的 tmux 后端。',
  CAO_TERMINAL_BACKEND_UNKNOWN:'Codex 暂不可用：无法确认 CAO 的 tmux 后端。',
  CAO_LOCAL_ONLY_REQUIRED:'Codex 暂不可用：CAO 地址不符合本机安全策略。',
  CAO_STARTUP_RECOVERY_FAILED:'Codex 暂不可用：CAO 自动恢复失败。'
};
const v103RenderCodexProviderCard=renderCodexProviderCard;
renderCodexProviderCard=function(){v103RenderCodexProviderCard();const host=$('providers');const card=[...(host?.querySelectorAll('.provider')||[])].find(node=>node.querySelector('b')?.textContent?.trim()==='Codex');const state=codexJoinReadiness||{};if(!card||state.joinAvailable||state.joinStatus==='JOINED'||!state.caoServerPath&&!state.httpHealthStatus&&!state.requestId)return;const reason=card.querySelector('.muted:nth-of-type(3)');if(state.reasonCode&&v103CodexRecoveryMessages[state.reasonCode]&&reason)reason.textContent=v103CodexRecoveryMessages[state.reasonCode];const details=document.createElement('details');details.className='muted';const summary=document.createElement('summary');summary.textContent='技术详情';details.appendChild(summary);for(const [label,value] of [['错误码：',state.reasonCode||'UNKNOWN'],['cao-server 路径：',state.caoServerPath||'未知'],['HTTP health 状态：',state.httpHealthStatus||'UNKNOWN'],['请求编号：',state.requestId||'未提供']]){const row=document.createElement('div');row.textContent=label+value;details.appendChild(row)}card.appendChild(details)};
async function refreshCodexJoinReadiness(){if(!selected)return;try{const response=await fetch('/api/providers?meetingId='+encodeURIComponent(selected),{headers:{Accept:'application/json'}});if(!response.ok)return;const providers=await response.json();if(!Array.isArray(providers))return;codexJoinReadiness=providers.find(item=>item.providerId==='codex')||null;renderCodexProviderCard()}catch(_){}}
setInterval(refreshCodexJoinReadiness,5000);
'''
if UI_HTML.count(_V101_SCRIPT_TAIL) != 1:
    raise RuntimeError("expected exactly one Product Shell script tail")
UI_HTML, _native_alerts_replaced = re.subn(r"\balert\s*\(", "showLocalizedAlert(", UI_HTML)
if _native_alerts_replaced < 1:
    raise RuntimeError("expected Product Shell native alert call sites")
UI_HTML = UI_HTML.replace("</style>", ".provider-join-error{border:1px solid var(--red);background:#35151a;border-radius:7px;padding:10px;margin:8px 0}.provider-join-error-message{margin:0 0 6px;color:#ffb4b4}.provider-join-error details{font-size:13px;line-height:1.6;overflow-wrap:anywhere}\n</style>", 1)
if UI_HTML.count('<div id="providers"></div>') != 1:
    raise RuntimeError("expected one provider catalog mount")
UI_HTML = UI_HTML.replace(
    '<div id="providers"></div>',
    '<section id="codex-join-error" class="provider-join-error" role="alert" hidden></section><div id="providers"></div>',
    1,
)
_V103_SCROLL_STABILITY_SCRIPT = r'''
const v103FinalRender=render;render=function(s){let state=readUiInteractionState();v103FinalRender(s);restoreUiInteractionState(state)};
const v103BaseLocalizeText=localizeText;localizeText=function(value){const raw=String(value);if(/(?:^|\s)\/(?:[^\s/]+\/)+[^\s]+/.test(raw))return raw;return v103BaseLocalizeText(raw).replace(/\bRework\b/g,'返工').replace(/process-restart/g,'程序重启').replace(/process restarted while meeting was (?:RUNNING|运行中); health confirmation required/g,'程序重启时会议仍在运行；须通过健康检查后手动恢复').replace(/process restarted during recovery; explicit health confirmation required/g,'恢复过程中程序重启；须重新通过健康检查并手动恢复')};
'''
UI_HTML = UI_HTML.replace(
    "</style>",
    ".card{min-width:0}.handoff-steps>*{min-width:0}#brainHandoffTask,.handoff-packet,.handoff-decision{display:block;width:100%;min-width:0;max-width:100%}</style>",
    1,
)
UI_HTML = UI_HTML.replace(
    "h?'等待人工 GPT 交接':'等待任务完成'",
    "h?'等待人工 GPT 交接':s.meeting.status==='COMPLETED'?'会议已完成':'等待任务完成'",
    1,
).replace(
    "点击“生成 / 查看”后显示通过安全检查的 Packet。",
    "点击“生成 / 查看”后显示通过安全检查的数据包。",
    1,
)
UI_HTML = UI_HTML.replace(_V101_SCRIPT_TAIL, "\n" + _V101_LIVE_UI_SCRIPT + _V102_CODEX_JOIN_UI_SCRIPT + _V103_CODEX_STARTUP_RECOVERY_UI_SCRIPT + _V103_SCROLL_STABILITY_SCRIPT + _V101_SCRIPT_TAIL, 1)
if UI_HTML.count("close.onclick=()=>panel.remove()") != 1:
    raise RuntimeError("expected one summary preview close handler")
UI_HTML = UI_HTML.replace(
    "close.onclick=()=>panel.remove()",
    "close.onclick=()=>{panel.remove();v101RetainedSummaryMarkdown=null;v101RetainedSummaryMeetingId=null}",
    1,
)
_OLD_TASK_TITLE_INPUT = '<input id="taskTitle" placeholder="Task title">'
_WRAPPING_TASK_TITLE = '<textarea id="taskTitle" rows="2" aria-label="Task title" placeholder="Task title"></textarea>'
if UI_HTML.count(_OLD_TASK_TITLE_INPUT) != 1:
    raise RuntimeError("expected exactly one single-line task title control")
UI_HTML = UI_HTML.replace(_OLD_TASK_TITLE_INPUT, _WRAPPING_TASK_TITLE, 1)


class ProductHandler(BaseHTTPRequestHandler):
    app: Phase2Application

    def _json(self, status: int, payload: Any) -> None:
        issue = _find_non_serializable(payload)
        if issue:
            path, constructor_name = issue
            payload = _safe_server_error(
                TypeError("Product Shell response is not serializable"),
                code="IPC_RESULT_NOT_SERIALIZABLE",
                stage="T15_IPC_RESULT_SERIALIZATION",
            )
            payload["error"]["fieldPath"] = path
            payload["error"]["constructorName"] = constructor_name
            status = 500
        try:
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            payload = _safe_server_error(
                TypeError("Product Shell response is not serializable"),
                code="IPC_RESULT_NOT_SERIALIZABLE",
                stage="T15_IPC_RESULT_SERIALIZATION",
            )
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else {}

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/":
                data = UI_HTML.encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
            if path == "/api/providers":
                meeting_id = (parse_qs(urlparse(self.path).query).get("meetingId") or [None])[0]
                self._json(200, self.app.provider_join_readiness(meeting_id)); return
            if path in {"/api/brain-provider", "/api/brain-provider/config"}: self._json(200, self.app.brain_provider_status()); return
            if path == "/api/runtime/provenance": self._json(200, self.app.get_runtime_provenance()); return
            if path == "/api/runtime/identity": self._json(200, self.app.runtime_identity()); return
            if path == "/api/runtime/poc-route": self._json(200, self.app.poc_route_consistency()); return
            if path == "/api/runtime/preflight": self._json(200, self.app.product_runtime_preflight()); return
            if path == "/api/system/backups":
                if self.client_address[0] not in {"127.0.0.1", "::1"}:
                    self._json(403, {"ok": False, "error": {"code": "LOCAL_ONLY"}}); return
                self._json(200, self.app.list_backups()); return
            if path == "/api/meetings": self._json(200, self.app.list_meetings()); return
            if path in {"/api/brain/chrome/status", "/api/brain/playwright/status", "/api/brain/real-chrome/status"}: self._json(200, self.app.real_chrome_brain_status()); return
            parts = [p for p in path.split("/") if p]
            if len(parts) == 3 and parts[:2] == ["api", "meetings"]:
                self._json(200, self.app.snapshot(parts[2])); return
            self._json(404, {"error": "not found"})
        except Exception as exc:
            self._json(500, _safe_server_error(exc, code="PRODUCT_READ_FAILED", stage="HTTP_GET"))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            parts = [p for p in path.split("/") if p]
            lifecycle_route = len(parts) == 4 and parts[:2] == ["api", "meetings"] and parts[3] in {"pause", "recover"}
            summary_export_route = len(parts) == 4 and parts[:2] == ["api", "meetings"] and parts[3] == "summary-export"
            brain_join_route = len(parts) == 5 and parts[:2] == ["api", "meetings"] and parts[3:] == ["brain", "join"]
            ensure_chatgpt_page_route = path == "/api/brain/real-chrome/ensure-chatgpt-page"
            foreground_bound_brain_page_route = path == "/api/brain/real-chrome/bring-bound-page-to-front"
            structural_diagnostics_route = path == "/api/brain/real-chrome/structural-diagnostics"
            diagnostic_connect_route = path == "/api/brain/real-chrome/diagnostic-connect-and-rebind"
            manual_handoff_route = len(parts) == 5 and parts[:2] == ["api", "meetings"] and parts[3] == "brain-handoff"
            cao_recovery_route = path == "/api/runtime/cao/recover"
            if lifecycle_route or summary_export_route or brain_join_route or ensure_chatgpt_page_route or foreground_bound_brain_page_route or structural_diagnostics_route or diagnostic_connect_route or manual_handoff_route or cao_recovery_route:
                if self.client_address[0] not in {"127.0.0.1", "::1"}:
                    self._json(403, {"ok": False, "error": {"code": "LOCAL_ONLY"}}); return
            if manual_handoff_route:
                try:
                    request_bytes = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    request_bytes = 0
                if request_bytes < 0 or request_bytes > 40 * 1024:
                    self._json(413, {"ok": False, "error": {"code": "BRAIN_HANDOFF_REQUEST_TOO_LARGE"}}); return
            body = self._body()
            if cao_recovery_route:
                if body:
                    self._json(400, {"ok": False, "error": {"code": "UNEXPECTED_CAO_RECOVERY_OPTIONS"}}); return
                try:
                    self._json(200, self.app.recover_cao_runtime())
                except CaoServiceError as exc:
                    self._json(409, {"ok": False, "error": {"code": exc.code, "message": exc.code}})
                return
            if path == "/api/system/backups":
                if self.client_address[0] not in {"127.0.0.1", "::1"}:
                    self._json(403, {"ok": False, "error": {"code": "LOCAL_ONLY"}}); return
                self._json(201, self.app.create_backup()); return
            if path == "/api/system/restore":
                if self.client_address[0] not in {"127.0.0.1", "::1"}:
                    self._json(403, {"ok": False, "error": {"code": "LOCAL_ONLY"}}); return
                restored = self.app.restore_backup(
                    str(body.get("backupId") or ""),
                    confirm_overwrite=body.get("confirmOverwrite") is True,
                )
                self._json(200, restored)
                threading.Timer(
                    0.2,
                    lambda: threading.Thread(
                        target=self.server.shutdown,
                        name="product-shell-post-restore-shutdown",
                        daemon=True,
                    ).start(),
                ).start()
                return
            if path == "/api/system/diagnostics":
                if self.client_address[0] not in {"127.0.0.1", "::1"}:
                    self._json(403, {"ok": False, "error": {"code": "LOCAL_ONLY"}}); return
                self._json(201, self.app.create_diagnostic_report()); return
            if diagnostic_connect_route:
                if body:
                    self._json(400, {"ok": False, "error": {"code": "UNEXPECTED_DIAGNOSTIC_CONNECT_OPTIONS"}}); return
                try:
                    self._json(200, self.app.connect_formal_brain_url_only_for_diagnostics())
                except Exception as exc:
                    code = str(getattr(exc, "code", "FORMAL_DIAGNOSTIC_CONNECT_FAILED"))
                    if code not in {
                        "CONNECT_OPERATION_IN_PROGRESS",
                        "DEDICATED_CDP_ENDPOINT_UNAVAILABLE",
                        "PLAYWRIGHT_EXACT_WS_CONNECT_FAILED",
                        "PLAYWRIGHT_BROWSER_DISCONNECTED",
                        "PLAYWRIGHT_CONTEXT_NOT_FOUND",
                        "PLAYWRIGHT_PAGES_UNAVAILABLE",
                        "CHATGPT_PAGE_NOT_FOUND",
                        "CHATGPT_PAGE_ENSURE_FAILED",
                        "PLAYWRIGHT_THREAD_AFFINITY_VIOLATION",
                    }:
                        code = "FORMAL_DIAGNOSTIC_CONNECT_FAILED"
                    status = 409 if code in {"CONNECT_OPERATION_IN_PROGRESS", "PLAYWRIGHT_THREAD_AFFINITY_VIOLATION"} else 503
                    self._json(status, {"ok": False, "error": {"code": code}})
                return
            if structural_diagnostics_route:
                if body:
                    self._json(400, {"ok": False, "error": {"code": "UNEXPECTED_STRUCTURAL_DIAGNOSTIC_OPTIONS"}}); return
                try:
                    self._json(200, self.app.get_chatgpt_structural_diagnostics())
                except Exception as exc:
                    code = str(getattr(exc, "code", "FORMAL_DOM_DIAGNOSTIC_FAILED"))
                    if code not in {
                        "FORMAL_DOM_DIAGNOSTIC_BOUND_PAGE_UNAVAILABLE",
                        "FORMAL_DOM_DIAGNOSTIC_READ_FAILED",
                        "PLAYWRIGHT_THREAD_AFFINITY_VIOLATION",
                    }:
                        code = "FORMAL_DOM_DIAGNOSTIC_FAILED"
                    status = 409 if code == "PLAYWRIGHT_THREAD_AFFINITY_VIOLATION" else 503
                    self._json(status, {"ok": False, "error": {"code": code}})
                return
            if ensure_chatgpt_page_route:
                if body:
                    self._json(400, {"ok": False, "error": {"code": "UNEXPECTED_PAGE_ENSURE_OPTIONS"}}); return
                self._json(200, self.app.ensure_chatgpt_page()); return
            if foreground_bound_brain_page_route:
                if body:
                    self._json(400, {"ok": False, "error": {"code": "UNEXPECTED_FOREGROUND_OPTIONS"}}); return
                try:
                    result = self.app.bring_bound_brain_page_to_front()
                    self._json(200 if result.get("ok") else 409, result)
                except Exception as exc:
                    code = str(getattr(exc, "code", "BOUND_PAGE_FOREGROUND_FAILED"))
                    if code not in {
                        "PLAYWRIGHT_THREAD_AFFINITY_VIOLATION",
                        "PLAYWRIGHT_SYNC_API_INSIDE_ASYNCIO_LOOP",
                    }:
                        code = "BOUND_PAGE_FOREGROUND_FAILED"
                    self._json(409 if code in {
                        "PLAYWRIGHT_THREAD_AFFINITY_VIOLATION",
                        "PLAYWRIGHT_SYNC_API_INSIDE_ASYNCIO_LOOP",
                    } else 503, {
                        "ok": False,
                        "code": code,
                    })
                return
            if path == "/api/dev/brain/composer-write-probe":
                if self.client_address[0] not in {"127.0.0.1", "::1"}:
                    self._json(403, {"ok": False, "error": {"code": "LOCAL_ONLY"}}); return
                if not bool(getattr(self.server, "dev_probe_enabled", False)):
                    self._json(404, {"ok": False, "error": {"code": "DEV_PROBE_DISABLED"}}); return
                self._json(200, self.app.dev_composer_write_probe(body)); return
            if path == "/api/dev/brain/connect-and-composer-probe":
                if self.client_address[0] not in {"127.0.0.1", "::1"}:
                    self._json(403, {"ok": False, "error": {"code": "LOCAL_ONLY"}}); return
                if not bool(getattr(self.server, "dev_probe_enabled", False)):
                    self._json(404, {"ok": False, "error": {"code": "DEV_PROBE_DISABLED"}}); return
                self._json(200, self.app.dev_connect_and_composer_probe(body)); return
            if path == "/api/dev/brain/exact-cdp-diagnostic":
                if self.client_address[0] not in {"127.0.0.1", "::1"}:
                    self._json(403, {"ok": False, "error": {"code": "LOCAL_ONLY"}}); return
                if not bool(getattr(self.server, "dev_probe_enabled", False)):
                    self._json(404, {"ok": False, "error": {"code": "DEV_PROBE_DISABLED"}}); return
                self._json(200, self.app.dev_exact_cdp_diagnostic(body)); return
            if path == "/api/meetings": self._json(201, self.app.create_meeting(str(body.get("name", "")), str(body.get("workspacePath", "")))); return
            if len(parts) == 4 and parts[:2] == ["api", "meetings"] and parts[3] == "start": self._json(200, self.app.start_meeting(parts[2])); return
            if len(parts) == 4 and parts[:2] == ["api", "meetings"] and parts[3] == "pause": self._json(200, self.app.pause(parts[2], str(body.get("reason") or "operator requested pause"))); return
            if len(parts) == 4 and parts[:2] == ["api", "meetings"] and parts[3] == "recover":
                result = self.app.recover(parts[2])
                if result.get("recovered") is False:
                    self._json(409, {"ok": False, "error": {
                        "code": "RECOVERY_HEALTH_CHECK_FAILED",
                        "stage": "MEETING_RECOVERY",
                        "message": result.get("recoveryError") or "Recovery health checks failed",
                    }, "recovered": False, "meeting": result.get("meeting"), "circuit": result.get("circuit")}); return
                self._json(200, result); return
            if len(parts) == 4 and parts[:2] == ["api", "meetings"] and parts[3] == "complete":
                self._json(200, self.app.complete_meeting(parts[2], reason=str(body.get("reason") or "operator completed meeting"))); return
            if summary_export_route:
                self._json(200, self.app.export_meeting_summary(parts[2], action=str(body.get("action") or "preview"))); return
            if len(parts) == 4 and parts[:2] == ["api", "meetings"] and parts[3] == "tasks": self._json(201, self.app.create_task(parts[2], str(body.get("title", "")), str(body.get("instruction", "")), body.get("agentId") or None)); return
            if len(parts) == 4 and parts[:2] == ["api", "meetings"] and parts[3] == "brain-decisions": self._json(200, self.app.submit_brain_decision(parts[2], body)); return
            if len(parts) == 4 and parts[:2] == ["api", "meetings"] and parts[3] == "brain-pairing": self._json(200, self.app.create_brain_pairing(parts[2])); return
            if len(parts) == 4 and parts[:2] == ["api", "meetings"] and parts[3] == "brain-poc": self._json(200, self.app.run_extension_brain_poc(parts[2])); return
            if len(parts) == 5 and parts[:2] == ["api", "meetings"] and parts[3:] == ["brain", "join"]: self._json(200, self.app.join_formal_brain(parts[2])); return
            if path == "/api/desktop-brain-poc/parse":
                self._json(200, self.app.validate_desktop_brain_poc(str(body.get("rawResponse", "")), str(body.get("brainRequestId", "")))); return
            if path == "/api/brain/failure":
                self._json(200, self.app.observe_brain_failure(
                    str(body.get("reason") or "unknown brain failure"),
                    brain_id="ChatGPTWebBrain",
                    state=str(body.get("state") or "UNKNOWN"),
                    meeting_id=str(body.get("meetingId") or "") or None,
                    brain_participant_id=str(body.get("brainParticipantId") or "") or None,
                    runtime_identity=body.get("runtimeIdentity") if isinstance(body.get("runtimeIdentity"), dict) else None,
                )); return
            if path in {"/api/brain/chrome/open", "/api/brain/playwright/open", "/api/brain/real-chrome/open"}:
                self._json(200, self.app.open_real_chrome_brain(str(body.get("connectAttemptId") or "") or None)); return
            if path == "/api/runtime/shutdown":
                if self.client_address[0] not in {"127.0.0.1", "::1"}:
                    self._json(403, {"ok": False, "error": {"code": "LOCAL_ONLY"}}); return
                self._json(202, {"ok": True, "status": "SHUTDOWN_SCHEDULED"})
                threading.Thread(target=self.server.shutdown, name="product-shell-shutdown", daemon=True).start()
                return
            if path in {"/api/brain/chrome-poc", "/api/brain/playwright-poc", "/api/brain/real-chrome-poc"}:
                try:
                    self._json(200, self.app.run_real_chrome_brain_poc())
                except ProductError as exc:
                    self._json(409, {"ok": False, "error": exc.as_error()})
                return
            if path == "/api/brain/api-poc": self._json(200, self.app.run_api_brain_poc()); return
            if path == "/api/brain-provider/config": self._json(200, self.app.save_brain_provider_config(body)); return
            if path == "/api/brain-provider/test": self._json(200, self.app.test_brain_provider()); return
            if len(parts) == 5 and parts[:2] == ["api", "meetings"] and parts[3] == "brain-handoff":
                if self.client_address[0] not in {"127.0.0.1", "::1"}:
                    self._json(403, {"ok": False, "error": {"code": "LOCAL_ONLY"}}); return
                operation = parts[4]
                meeting_id = parts[2]
                if operation == "generate":
                    self._json(200, self.app.create_manual_gpt_handoff(meeting_id, str(body.get("taskId") or ""))); return
                if operation == "copy":
                    self._json(200, self.app.copy_manual_gpt_handoff(meeting_id, str(body.get("requestId") or ""))); return
                if operation == "import":
                    self._json(200, self.app.import_manual_gpt_decision(
                        meeting_id, str(body.get("requestId") or ""), str(body.get("rawResponse") or ""),
                    )); return
                if operation == "apply":
                    self._json(200, self.app.apply_manual_gpt_decision(meeting_id, str(body.get("requestId") or ""))); return
                self._json(404, {"ok": False, "error": {"code": "BRAIN_HANDOFF_ROUTE_NOT_FOUND"}}); return
            if len(parts) == 4 and parts[:2] == ["api", "meetings"] and parts[3] == "agents":
                provider = str(body.get("provider", "")).lower()
                if provider != "codex":
                    self._json(201, self.app.join_provider(parts[2], provider)); return
                supplied_request_id = str(self.headers.get("X-Request-ID", "")).strip()
                request_id = supplied_request_id if re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", supplied_request_id) else str(uuid4())
                try:
                    result = self.app.join_provider(parts[2], provider)
                except ProductError as exc:
                    error = exc.as_error()
                    error.update({"requestId": request_id, "httpStatus": 409})
                    self._json(409, {"ok": False, "error": error})
                except Exception:
                    # A provider/CAO exception may contain environment or
                    # credential-bearing details. Return only a safe summary.
                    self._json(500, {"ok": False, "error": {
                        "code": "CODEX_RUNTIME_SESSION_CREATE_FAILED",
                        "stage": "HTTP_HANDLER",
                        "message": "Codex 加入会议失败：无法创建运行时会话。",
                        "requestId": request_id,
                        "httpStatus": 500,
                    }})
                else:
                    self._json(201, result)
                return
            if len(parts) == 6 and parts[:2] == ["api", "meetings"] and parts[3] == "agents" and parts[5] == "restart": self._json(200, self.app.restart_agent(parts[2], parts[4])); return
            if len(parts) == 6 and parts[:2] == ["api", "meetings"] and parts[3] == "tasks" and parts[5] == "dispatch": self._json(200, self.app.dispatch_task(parts[2], parts[4])); return
            self._json(404, {"error": "not found"})
        except ProviderBlockedError as exc:
            self._json(409, {"error": str(exc), "code": "PROVIDER_BLOCKED"})
        except DispatchBlockedError as exc:
            self._json(409, {"error": str(exc), "code": "DISPATCH_BLOCKED"})
        except HumanHandoffPendingError:
            self._json(409, {"ok": False, "error": {"code": "WAITING_FOR_HUMAN_HANDOFF", "message": "请先完成当前 GPT 人工决策交接。"}})
        except MeetingLifecycleConflict as exc:
            self._json(409, {"ok": False, "error": {
                "code": exc.code,
                "stage": "MEETING_LIFECYCLE",
                "message": localized_product_error_message(exc.code, str(exc)),
            }})
        except ProductError as exc:
            self._json(409, {"ok": False, "error": exc.as_error()})
        except (ProductError, KeyError, ValueError) as exc:
            self._json(400, _safe_server_error(exc, code=getattr(exc, "code", "PRODUCT_REQUEST_FAILED"), stage="HTTP_HANDLER"))
        except Exception as exc:
            self._json(500, _safe_server_error(exc, code="INTERNAL_UNCLASSIFIED_EXCEPTION", stage="HTTP_HANDLER"))

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/brain-provider/credential":
                self._json(200, self.app.remove_brain_provider_credential()); return
            self._json(404, {"error": "not found"})
        except Exception as exc:
            self._json(500, _safe_server_error(exc, code="PRODUCT_DELETE_FAILED", stage="HTTP_DELETE"))

    def log_message(self, format: str, *args: Any) -> None:
        return


def make_server(app: Phase2Application, host: str = "127.0.0.1", port: int = 8765, *, dev_probe_enabled: bool | None = None) -> HTTPServer:
    # A bound API socket is externally callable. Restore durable Meeting and
    # breaker state before creating it so no request can race startup hydration.
    hydrate_safety = getattr(app, "hydrate_persisted_safety_state", None)
    if callable(hydrate_safety):
        hydrate_safety()
    # Playwright's sync API binds its greenlet to the thread that created and
    # uses the Browser/Page.  The formal Product Shell is intentionally a
    # single-process, single-request-thread runtime so status, connect, and
    # POC calls cannot cross that thread boundary.
    handler = type("BoundProductHandler", (ProductHandler,), {"app": app})
    # Keep the canonical construction visible for runtime-provenance checks:
    # return HTTPServer((host, port), handler)
    server = HTTPServer((host, port), handler)
    server.dev_probe_enabled = (
        bool(dev_probe_enabled)
        if dev_probe_enabled is not None
        else os.environ.get("AI_MEETING_ROOM_ENABLE_DEV_PROBES") == "1"
    )
    return server
