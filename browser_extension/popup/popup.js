const status = document.getElementById('status');
function call(message) { return new Promise(resolve => chrome.runtime.sendMessage(message, resolve)); }
async function show() { const result = await call({type:'STATUS'}); status.textContent = result.ok ? JSON.stringify(result.status, null, 2) : result.error; }
document.getElementById('pair').onclick = async () => { const result = await call({type:'PAIR', pairingCode:document.getElementById('pairing').value.trim()}); status.textContent = result.ok ? `Paired to meeting ${result.meetingId}` : result.error; };
document.getElementById('bind').onclick = async () => { const [tab] = await chrome.tabs.query({active:true,currentWindow:true}); const result = await call({type:'BIND', tabId:tab.id, url:tab.url}); status.textContent = result.ok ? 'Bound current ChatGPT tab' : result.error; };
document.getElementById('diagnostics').onclick = async () => { const result = await call({type:'DOM_DIAGNOSTICS'}); status.textContent = result.ok ? JSON.stringify(result.diagnostics, null, 2) : result.error; };
document.getElementById('unbind').onclick = async () => { const result = await call({type:'UNBIND'}); status.textContent = result.ok ? 'Unbound' : result.error; };
show();
