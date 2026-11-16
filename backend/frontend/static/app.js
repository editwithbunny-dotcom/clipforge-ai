'use strict';
const $ = id => document.getElementById(id);
const S = { user: null, settings: null, keys: {}, models: {}, mode: 'upload', file: null, regMode: false, polls: {} };
const CATS = { comedy: 'catComedy', engagement: 'catEngagement', important: 'catImportant' };

function toast(t) { const el = $('toast'); el.textContent = t; el.style.display = 'block'; clearTimeout(toast.t); toast.t = setTimeout(() => el.style.display = 'none', 4200); }
async function api(path, opt = {}) {
  const o = { credentials: 'same-origin', ...opt, headers: { 'X-Requested-With': 'ClipForge', ...(opt.headers || {}) } };
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers['Content-Type'] = 'application/json'; delete o.json; }
  const r = await fetch(path, o);
  let d = null; try { d = await r.json(); } catch (e) {}
  if (!r.ok) { const err = new Error((d && d.detail) || `Request failed (${r.status})`); err.status = r.status; throw err; }
  return d;
}
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };

// ---------------- auth
function showAuth() { $('authView').classList.remove('hidden'); $('studioView').classList.add('hidden'); $('topActions').classList.add('hidden'); }
function showStudio() { $('authView').classList.add('hidden'); $('studioView').classList.remove('hidden'); $('topActions').classList.remove('hidden'); $('who').textContent = S.user.email; }
$('authToggle').onclick = e => { e.preventDefault(); S.regMode = !S.regMode; $('authTitle').textContent = S.regMode ? 'Create account' : 'Sign in'; $('authSubmit').textContent = S.regMode ? 'Create account' : 'Sign in';
  $('authToggle').textContent = S.regMode ? 'Have an account? Sign in' : 'Need an account? Register'; $('inviteField').classList.toggle('hidden', !S.regMode); $('password').autocomplete = S.regMode ? 'new-password' : 'current-password'; $('authErr').textContent = ''; };
$('authSubmit').onclick = async () => {
  $('authErr').textContent = ''; $('authSubmit').disabled = true;
  try { await api(S.regMode ? '/api/auth/register' : '/api/auth/login', { method: 'POST', json: { email: $('email').value.trim(), password: $('password').value, invite_code: $('invite').value.trim() } });
    $('password').value = ''; await boot(); } catch (e) { $('authErr').textContent = e.message; } finally { $('authSubmit').disabled = false; }
};
$('password').addEventListener('keydown', e => { if (e.key === 'Enter') $('authSubmit').click(); });
$('logoutBtn').onclick = async () => { try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) {} Object.values(S.polls).forEach(clearInterval); S.polls = {}; S.user = null; $('jobs').replaceChildren(); showAuth(); };

// ---------------- settings
function readSettings() {
  const [mn, mx] = $('duration').value.split('-').map(Number);
  return { provider: $('provider').value, transcription_model: $('transModel').value, analysis_model: $('analysisModel').value,
    categories: Object.keys(CATS).filter(k => $(CATS[k]).checked), min_duration: mn, max_duration: mx, format: $('format').value, quality: Number($('quality').value),
    framing: $('framing').value, caption_style: $('captionStyle').value, caption_language: $('captionLanguage').value, audio: $('audio').value };
}
function applySettings(s) {
  $('provider').value = s.provider; Object.keys(CATS).forEach(k => $(CATS[k]).checked = s.categories.includes(k));
  const d = `${s.min_duration}-${s.max_duration}`; if (![...$('duration').options].some(o => o.value === d)) $('duration').add(new Option(`${s.min_duration}–${s.max_duration} seconds`, d)); $('duration').value = d;
  $('format').value = s.format; $('quality').value = String(s.quality); $('framing').value = s.framing; $('captionStyle').value = s.caption_style; $('captionLanguage').value = s.caption_language; $('audio').value = s.audio;
  fillModels(s.transcription_model, s.analysis_model); renderChips();
}
function fillSelect(sel, items, want, fallback) {
  sel.replaceChildren(); items.forEach(m => sel.add(new Option(m, m)));
  sel.value = items.includes(want) ? want : (items.includes(fallback) ? fallback : (items[0] || ''));
}
function fillModels(wantT, wantA) {
  const p = $('provider').value, m = S.models[p], has = !!S.keys[p];
  $('transModel').disabled = $('analysisModel').disabled = !m;
  if (m) { fillSelect($('transModel'), m.transcription, wantT, m.defaults && m.defaults.transcription); fillSelect($('analysisModel'), m.analysis, wantA, m.defaults && m.defaults.analysis); }
  else { $('transModel').replaceChildren(new Option(has ? 'Loading models…' : 'Save an API key to load models', '')); $('analysisModel').replaceChildren(new Option(has ? 'Loading models…' : 'Save an API key to load models', '')); }
  $('transHint').textContent = p === 'openai' ? 'OpenAI: only Whisper models return the timestamps needed for clipping.' : 'Gemini timestamps are model-generated and can be approximate; check caption timing on a test clip.';
  const k = S.keys[p]; $('keyState').textContent = k ? `Saved key: ${k}` : 'No key saved for this provider.'; $('keyState').className = 'keystate' + (k ? '' : ' none'); $('keyDelete').classList.toggle('hidden', !k);
}
async function loadModels(p) {
  if (!S.keys[p]) return; try { S.models[p] = await api('/api/models/' + p); } catch (e) { toast(e.message); }
  if ($('provider').value === p) fillModels($('transModel').value || S.settings.transcription_model, $('analysisModel').value || S.settings.analysis_model);
}
$('provider').onchange = () => { const p = $('provider').value; fillModels('', ''); if (S.keys[p] && !S.models[p]) loadModels(p); };
$('keySave').onclick = async () => {
  const p = $('provider').value, key = $('keyInput').value.trim(); $('setErr').textContent = ''; if (!key) { $('setErr').textContent = 'Paste an API key first.'; return; }
  $('keySave').disabled = true; $('keySave').textContent = 'Verifying…';
  try { const r = await api('/api/keys/' + p, { method: 'PUT', json: { key } }); S.keys[p] = r.hint; $('keyInput').value = '';
    const prefer = (list, re) => list.find(x => re.test(x)) || list[0] || ''; S.models[p] = { ...r.models, defaults: { transcription: prefer(r.models.transcription, /^whisper-1$|whisper|flash/), analysis: prefer(r.models.analysis, /^gpt-4\.1-mini$|^gpt-4o-mini$|mini|flash/) } };
    fillModels('', ''); toast('Key verified and saved.'); } catch (e) { $('setErr').textContent = e.message; } finally { $('keySave').disabled = false; $('keySave').textContent = 'Verify & save'; }
};
$('keyDelete').onclick = async () => { const p = $('provider').value; try { await api('/api/keys/' + p, { method: 'DELETE' }); delete S.keys[p]; delete S.models[p]; fillModels('', ''); toast('Key removed.'); } catch (e) { toast(e.message); } };
$('settingsBtn').onclick = () => { $('setErr').textContent = ''; $('modal').style.display = 'flex'; };
const closeSet = () => $('modal').style.display = 'none'; $('close').onclick = closeSet; $('cancelSet').onclick = closeSet;
$('saveSet').onclick = async () => {
  $('setErr').textContent = '';
  try { S.settings = await api('/api/settings', { method: 'PUT', json: readSettings() }); applySettings(S.settings); closeSet(); toast('Studio settings saved.'); } catch (e) { $('setErr').textContent = e.message; }
};
function renderChips() {
  const s = readSettings(), c = $('chips'); c.replaceChildren();
  [`${s.categories.length} clip${s.categories.length > 1 ? 's' : ''}`, `${s.min_duration}–${s.max_duration}s`, `${s.format} · ${s.quality}p`, { track: 'Face-tracking crop', center: 'Center crop', blur: 'Blur fit' }[s.framing], s.caption_style === 'none' ? 'No captions' : 'Captions', s.audio === 'off' ? 'Original audio' : 'Audio enhanced'].forEach(t => c.append(el('span', 'chip', t)));
  $('start').textContent = `✦ Analyze & create ${s.categories.length} clip${s.categories.length > 1 ? 's' : ''}`;
}

// ---------------- source
document.querySelectorAll('.tab').forEach(b => b.onclick = () => { document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x === b)); S.mode = b.dataset.tab; $('uploadPane').classList.toggle('hidden', S.mode !== 'upload'); $('urlPane').classList.toggle('hidden', S.mode !== 'url'); });
const pick = f => { S.file = f || null; $('fileInfo').textContent = f ? `${f.name} · ${(f.size / 1048576).toFixed(1)} MB` : 'No video selected'; };
$('file').onchange = e => pick(e.target.files[0]);
const drop = $('drop'); ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('drag'); })); ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('drag'); }));
drop.addEventListener('drop', e => pick(e.dataTransfer.files[0]));
function setProgress(p, t) { $('progress').style.display = 'block'; $('bar').style.width = p + '%'; $('status').textContent = t; }

// ---------------- jobs
$('start').onclick = () => {
  const s = readSettings();
  if (!S.keys[s.provider]) { toast('Save an API key in Studio Settings first.'); $('settingsBtn').click(); return; }
  if (!s.transcription_model || !s.analysis_model) { toast('Choose models in Studio Settings first.'); $('settingsBtn').click(); return; }
  if (!s.categories.length) { toast('Select at least one clip category.'); return; }
  const fd = new FormData(); fd.append('options', JSON.stringify(s));
  if (S.mode === 'upload') { if (!S.file) { toast('Choose a video file first.'); return; } fd.append('file', S.file); }
  else { const u = $('videoUrl').value.trim(); if (!u) { toast('Paste a YouTube link or direct HTTPS video URL.'); return; } fd.append('video_url', u); }
  $('start').disabled = true; setProgress(1, 'Uploading…');
  const x = new XMLHttpRequest(); x.open('POST', '/api/jobs'); x.setRequestHeader('X-Requested-With', 'ClipForge'); x.withCredentials = true;
  x.upload.onprogress = e => { if (e.lengthComputable) setProgress(Math.min(99, Math.round(e.loaded / e.total * 100)), `Uploading… ${Math.round(e.loaded / 1048576)} MB`); };
  x.onload = async () => { $('start').disabled = false; let d = {}; try { d = JSON.parse(x.responseText); } catch (e) {}
    if (x.status !== 200) { setProgress(0, ''); $('progress').style.display = 'none'; toast(d.detail || `Upload failed (${x.status})`); return; }
    $('progress').style.display = 'none'; $('status').textContent = ''; toast('Project queued.'); await loadJobs(); };
  x.onerror = () => { $('start').disabled = false; $('progress').style.display = 'none'; toast('Network error during upload.'); };
  x.send(fd);
};
function badge(j) { const b = el('span', 'badge' + (j.status === 'completed' ? ' ok' : (j.status === 'failed' || j.status === 'cancelled') ? ' bad' : ''), j.status); return b; }
function clipCard(c) {
  const card = el('article', 'clip'), top = el('div', 'cliptop'), v = document.createElement('video'); v.src = c.url; v.controls = true; v.playsInline = true; v.preload = 'metadata'; top.append(v);
  const body = el('div', 'clipbody'); body.append(el('b', '', c.title || c.category));
  const tags = el('p'); tags.append(el('span', 'tag', c.category)); tags.append(el('span', 'tag', (c.meta.selection === 'ai' ? 'AI scored ' + c.score + '/10' : 'heuristic pick')));
  body.append(tags, el('p', '', c.reason));
  const a = el('a', '', '↓ Download MP4'); a.href = c.url + '?download=1'; body.append(a);
  const m = c.meta || {}; body.append(el('div', 'meta', `${c.duration}s · ${Math.floor(c.start / 60)}:${String(Math.floor(c.start % 60)).padStart(2, '0')}–${Math.floor(c.end / 60)}:${String(Math.floor(c.end % 60)).padStart(2, '0')} · ${m.framing_used || ''}${m.faces_found ? ' (' + m.faces_found + ' face samples)' : ''}`));
  card.append(top, body); return card;
}
function renderJob(j) {
  let box = document.getElementById('job-' + j.id); const fresh = !box;
  if (fresh) { box = el('div', 'job'); box.id = 'job-' + j.id; $('jobs').prepend(box); }
  box.replaceChildren();
  const head = el('div', 'jobhead'), left = el('div'); left.append(el('b', '', j.source_name || 'Project'), el('div', 'muted small', new Date(j.created_at * 1000).toLocaleString())); head.append(left);
  const right = el('div'); right.append(badge(j)); head.append(right); box.append(head);
  const active = !['completed', 'failed', 'cancelled'].includes(j.status);
  if (active) { const pr = el('div', 'progress'); pr.style.display = 'block'; const b = el('div'); b.style.width = j.progress + '%'; pr.append(b); box.append(pr, el('div', 'status', j.message));
    const cb = el('button', 'btn', 'Cancel'); cb.style.marginTop = '10px'; cb.onclick = async () => { try { await api(`/api/jobs/${j.id}/cancel`, { method: 'POST' }); } catch (e) { toast(e.message); } }; box.append(cb); }
  else {
    if (j.status === 'failed') box.append(el('div', 'err', j.error || 'Processing failed.'));
    if (j.status === 'completed' && j.message.includes('(')) box.append(el('div', 'hint', j.message));
    if (j.clips.length) { const grid = el('div', 'results'); j.clips.forEach(c => grid.append(clipCard(c))); box.append(grid); }
    const del = el('button', 'linkbtn', 'Delete project and clips'); del.style.marginTop = '10px'; del.onclick = async () => { if (!confirm('Delete this project and its clips?')) return; try { await api('/api/jobs/' + j.id, { method: 'DELETE' }); box.remove(); updateCount(); } catch (e) { toast(e.message); } }; box.append(del);
  }
  if (active && !S.polls[j.id]) S.polls[j.id] = setInterval(() => pollJob(j.id), 2500);
  if (!active && S.polls[j.id]) { clearInterval(S.polls[j.id]); delete S.polls[j.id]; }
  updateCount();
}
function updateCount() { const n = $('jobs').children.length; $('jobCount').textContent = n ? `${n} project${n > 1 ? 's' : ''}` : ''; $('empty').classList.toggle('hidden', n > 0); }
async function pollJob(id) { try { renderJob(await api('/api/jobs/' + id)); } catch (e) { if (e.status === 401) location.reload(); } }
async function loadJobs() { const js = await api('/api/jobs'); $('jobs').replaceChildren(); js.slice().reverse().forEach(renderJob); updateCount(); }

// ---------------- boot
async function boot() {
  try { const me = await api('/api/auth/me'); S.user = me; S.settings = me.settings; S.keys = Object.fromEntries(me.keys.map(k => [k.provider, k.hint])); S.models = {}; }
  catch (e) { showAuth(); return; }
  showStudio(); applySettings(S.settings); await loadJobs();
  await Promise.all(Object.keys(S.keys).map(loadModels));
}
['format', 'quality', 'framing', 'captionStyle', 'audio', 'duration'].forEach(id => $(id).addEventListener('change', renderChips)); Object.values(CATS).forEach(id => $(id).addEventListener('change', renderChips));
boot();
