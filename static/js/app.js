let statusState = {};
let allChampions = [];
let allSkins = [];
let stepperValues = { aa: 0.0, il: 0.3, ab: 0.3 };
let currentTheme = localStorage.getItem('star_theme') || 'night';

function $(id) { return document.getElementById(id); }

function applyTheme(theme) {
  currentTheme = theme;
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('star_theme', theme);
  
  const nightBtn = $('theme-btn-night');
  const lightBtn = $('theme-btn-light');
  if (nightBtn) nightBtn.classList.toggle('active', theme === 'night');
  if (lightBtn) lightBtn.classList.toggle('active', theme === 'light');
}

function toast(msg, type = 'info') {
  const box = $('toast-box');
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 2800);
}

async function api(path, method = 'GET', body = null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  return res.json();
}

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    const tabName = btn.dataset.tab;
    const panel = $('tab-' + tabName);
    if (panel) panel.classList.add('active');
  });
});

async function pollStatus() {
  try {
    const data = await api('/api/status');
    statusState = data;
    renderStatus(data);
  } catch (e) {}
}

function renderStatus(data) {
  const ind = $('status-indicator');
  const dot = $('status-dot');
  const name = $('status-name');

  if (data.connected) {
    ind.classList.add('connected');
    dot.classList.add('connected');
    name.textContent = data.account_text || `${data.account_name}#${data.account_tag}`;
  } else {
    ind.classList.remove('connected');
    dot.classList.remove('connected');
    name.textContent = '';
  }

  const f = data.features || {};
  $('toggle-auto-accept').checked = Boolean(f.auto_accept);
  const badgeAA = $('badge-auto-accept');
  badgeAA.textContent = f.auto_accept ? 'ON' : 'OFF';
  badgeAA.classList.toggle('on', Boolean(f.auto_accept));

  $('toggle-instalock').checked = Boolean(f.instalock_enabled);
  $('val-instalock-champ').textContent = f.instalock_champion || 'Random';

  $('toggle-autoban').checked = Boolean(f.autoban_enabled);
  $('val-autoban-champ').textContent = f.autoban_champion || 'None';

  const chatBadge = $('badge-chat-state');
  chatBadge.textContent = f.chat_state || 'LIVE';
  chatBadge.className = 'card-badge ' + (f.chat_state === 'OFFLINE' ? 'offline' : 'live');

  const cfg = data.config || {};
  stepperValues.aa = cfg.auto_accept_delay || 0.0;
  stepperValues.il = cfg.instalock_delay || 0.3;
  stepperValues.ab = cfg.autoban_delay || 0.3;
  updateDelayDisplays();
  
  const prov = $('cfg-provider');
  if (prov && cfg.lobby_reveal_provider) {
    prov.value = cfg.lobby_reveal_provider;
  }
}

async function toggleAutoAccept() {
  const res = await api('/api/auto-accept/toggle', 'POST');
  if (res.ok) {
    toast(res.enabled ? 'Auto Accept enabled' : 'Auto Accept disabled', res.enabled ? 'success' : 'info');
    pollStatus();
  } else {
    toast(res.error, 'error');
    $('toggle-auto-accept').checked = !$('toggle-auto-accept').checked;
  }
}

async function toggleInstalock() {
  const res = await api('/api/instalock/toggle', 'POST');
  if (res.ok) {
    toast(res.enabled ? `Instalock (${res.champion}) enabled` : 'Instalock disabled', res.enabled ? 'success' : 'info');
    pollStatus();
  } else {
    toast(res.error, 'error');
    $('toggle-instalock').checked = !$('toggle-instalock').checked;
  }
}

async function toggleAutoBan() {
  const res = await api('/api/autoban/toggle', 'POST');
  if (res.ok) {
    toast(res.enabled ? `AutoBan (${res.champion}) enabled` : 'AutoBan disabled', res.enabled ? 'success' : 'info');
    pollStatus();
  } else {
    toast(res.error, 'error');
    $('toggle-autoban').checked = !$('toggle-autoban').checked;
  }
}

async function openChampionPicker(mode) {
  showModal(mode === 'instalock' ? 'Select Instalock Champion' : 'Select AutoBan Champion', '<div style="color:var(--text-muted)">Loading champions...</div>');

  if (allChampions.length === 0) {
    const res = await api('/api/champions');
    if (res.ok) allChampions = res.champions;
  }

  const modalBody = $('modal-body');
  modalBody.innerHTML = `
    <input type="text" class="modal-search" id="champ-filter" placeholder="Search champion..." />
    <div class="modal-stats" id="champ-count"></div>
    <div class="modal-list" id="champ-list"></div>
  `;

  function renderList(query) {
    const q = query.toLowerCase().trim();
    const list = $('champ-list');
    const filtered = allChampions.filter(c => c.toLowerCase().includes(q));
    $('champ-count').textContent = `${filtered.length} champions`;
    list.innerHTML = filtered.map(c => `
      <div class="modal-item" onclick="selectChampion('${mode}', '${c}')">
        <span>${c}</span>
        <span style="color:var(--text-muted); font-size:11px">›</span>
      </div>
    `).join('') || '<div style="color:var(--text-muted); padding: 12px;">No champions found</div>';
  }

  renderList('');
  $('champ-filter').addEventListener('input', e => renderList(e.target.value));
  $('champ-filter').focus();
}

async function selectChampion(mode, champ) {
  closeModal();
  const res = await api('/api/champion/set', 'POST', { mode, champion: champ });
  if (res.ok) {
    toast(`${mode === 'instalock' ? 'Instalock' : 'AutoBan'} set to ${champ}`, 'success');
    pollStatus();
  } else {
    toast(res.error, 'error');
  }
}

async function openSkinPicker() {
  showModal('Browse All Champion Skins', '<div style="color:var(--text-muted); padding: 8px;">Loading all 2,000+ skins...</div>');

  if (allSkins.length === 0) {
    const res = await api('/api/skins');
    if (res.ok) allSkins = res.skins;
  }

  const modalBody = $('modal-body');
  modalBody.innerHTML = `
    <input type="text" class="modal-search" id="skin-filter" placeholder="Search champion or skin name (e.g. Yasuo, Project, K/DA)..." />
    <div class="modal-stats">
      <span id="skin-count">${allSkins.length} skins available</span>
      <span>Click any skin to apply</span>
    </div>
    <div class="modal-list" id="skin-list"></div>
  `;

  let currentMatches = allSkins;
  let renderOffset = 0;
  const CHUNK_SIZE = 150;

  function appendChunk() {
    const list = $('skin-list');
    if (!list) return;
    const nextChunk = currentMatches.slice(renderOffset, renderOffset + CHUNK_SIZE);
    if (nextChunk.length === 0) return;

    const html = nextChunk.map(s => `
      <div class="modal-item" onclick="selectSkin('${s.id}', '${encodeURIComponent(s.champion + ' - ' + s.name)}')">
        <span><strong>${s.champion}</strong> — ${s.name}</span>
        <span style="color:var(--text-muted); font-size:11px">${s.id}</span>
      </div>
    `).join('');

    list.insertAdjacentHTML('beforeend', html);
    renderOffset += nextChunk.length;
  }

  function renderFiltered(query) {
    const q = query.toLowerCase().trim();
    const list = $('skin-list');
    list.innerHTML = '';
    renderOffset = 0;

    if (!q) {
      currentMatches = allSkins;
    } else {
      currentMatches = allSkins.filter(s => (s.champion + ' ' + s.name).toLowerCase().includes(q));
    }

    $('skin-count').textContent = `${currentMatches.length} / ${allSkins.length} skins`;

    if (currentMatches.length === 0) {
      list.innerHTML = '<div style="color:var(--text-muted); padding: 12px;">No skins found</div>';
      return;
    }

    appendChunk();
  }

  $('skin-list').addEventListener('scroll', e => {
    const el = e.target;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 200) {
      appendChunk();
    }
  });

  renderFiltered('');
  $('skin-filter').addEventListener('input', e => renderFiltered(e.target.value));
  $('skin-filter').focus();
}

async function selectSkin(skinId, encodedName) {
  closeModal();
  const name = decodeURIComponent(encodedName);
  const res = await api('/api/background', 'POST', { skin_id: skinId, skin_name: name });
  if (res.ok) {
    toast(`Background changed to ${name}`, 'success');
  } else {
    toast(res.error, 'error');
  }
}

async function submitIcon(clientOnly) {
  const inputId = clientOnly ? 'input-client-icon' : 'input-profile-icon';
  const val = $(inputId).value.trim();
  if (!val || parseInt(val) < 1) {
    toast('Enter a valid icon ID', 'error');
    return;
  }
  const res = await api('/api/icon', 'POST', { icon_id: val, client_only: clientOnly });
  if (res.ok) {
    toast(`${clientOnly ? 'Client' : 'Profile'} icon updated!`, 'success');
    $(inputId).value = '';
  } else {
    toast(res.error, 'error');
  }
}

async function submitRiotID() {
  const name = $('input-riot-name').value.trim();
  const tag = $('input-riot-tag').value.trim();
  if (!name || !tag) {
    toast('Game name and tag are required', 'error');
    return;
  }
  const res = await api('/api/riotid', 'POST', { name, tag });
  if (res.ok) {
    toast('Riot ID updated!', 'success');
    $('input-riot-name').value = '';
    $('input-riot-tag').value = '';
  } else {
    toast(res.error, 'error');
  }
}

function onBadgeModeChange() {
  const mode = $('select-badge-mode').value;
  $('input-badge-gid').style.display = mode === 'glitched' ? 'block' : 'none';
}

async function submitBadges() {
  const mode = $('select-badge-mode').value;
  const gid = $('input-badge-gid').value || '0';
  const res = await api('/api/badges', 'POST', { mode, glitched_id: gid });
  if (res.ok) {
    toast('Badges updated!', 'success');
  } else {
    toast(res.error, 'error');
  }
}

async function submitStatus() {
  const text = $('input-status-text').value;
  const res = await api('/api/status-msg', 'POST', { status: text });
  if (res.ok) {
    toast('Status message updated!', 'success');
    $('input-status-text').value = '';
  } else {
    toast(res.error, 'error');
  }
}

async function submitReveal() {
  toast('Revealing lobby...', 'info');
  const res = await api('/api/reveal', 'POST');
  if (res.ok) {
    toast('Lobby revealed!', 'success');
    if (res.url) {
      window.open(res.url, '_blank');
    }
  } else {
    toast(res.error, 'error');
  }
}

async function submitDodge() {
  if (!confirm('Leave champion select now?')) return;
  const res = await api('/api/dodge', 'POST');
  if (res.ok) {
    toast('Lobby dodged successfully!', 'success');
  } else {
    toast(res.error, 'error');
  }
}

async function submitRestartUX() {
  const res = await api('/api/restart-ux', 'POST');
  if (res.ok) {
    toast('Client UI restart requested!', 'success');
  } else {
    toast(res.error, 'error');
  }
}

async function submitToggleChat() {
  const res = await api('/api/chat/toggle', 'POST');
  if (res.ok) {
    toast(`Chat state: ${res.state}`, 'success');
    pollStatus();
  } else {
    toast(res.error, 'error');
  }
}

async function submitRemoveFriends() {
  if (!confirm('WARNING: Permanently remove all friends?')) return;
  const res = await api('/api/remove-friends', 'POST');
  if (res.ok) {
    toast(`Removed ${res.count} friends`, 'success');
  } else {
    toast(res.error, 'error');
  }
}

function changeDelay(key, delta) {
  stepperValues[key] = Math.max(0, Math.min(2, Math.round((stepperValues[key] + delta) * 10) / 10));
  updateDelayDisplays();
}

function updateDelayDisplays() {
  const aa = $('delay-val-aa');
  const il = $('delay-val-il');
  const ab = $('delay-val-ab');
  if (aa) aa.textContent = stepperValues.aa.toFixed(1) + 's';
  if (il) il.textContent = stepperValues.il.toFixed(1) + 's';
  if (ab) ab.textContent = stepperValues.ab.toFixed(1) + 's';
}

async function submitSettings() {
  const prov = $('cfg-provider').value;
  const res = await api('/api/settings', 'POST', {
    provider: prov,
    auto_accept_delay: stepperValues.aa,
    instalock_delay: stepperValues.il,
    autoban_delay: stepperValues.ab,
  });
  if (res.ok) {
    toast('Settings saved!', 'success');
  } else {
    toast(res.error, 'error');
  }
}

function showModal(title, bodyHTML) {
  $('modal-title').textContent = title;
  $('modal-body').innerHTML = bodyHTML;
  $('modal-overlay').classList.remove('hidden');
}

function closeModal() {
  $('modal-overlay').classList.add('hidden');
}

$('modal-overlay').addEventListener('click', e => {
  if (e.target === $('modal-overlay')) closeModal();
});

applyTheme(currentTheme);
pollStatus();
setInterval(pollStatus, 2000);
