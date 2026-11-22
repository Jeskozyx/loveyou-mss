// ===========================================================
//  LOVEYOU APP — Main JavaScript
// ===========================================================

/* ---- Floating Hearts Generator ---- */
function spawnHearts(container, count = 14) {
  const symbols = ['❤️','💕','💖','💗','💓','🌹','💘','✨'];
  for (let i = 0; i < count; i++) {
    const h = document.createElement('div');
    h.className = 'heart-float';
    h.textContent = symbols[Math.floor(Math.random() * symbols.length)];
    h.style.left = Math.random() * 100 + 'vw';
    h.style.fontSize = (0.9 + Math.random() * 1.4) + 'rem';
    h.style.animationDuration = (8 + Math.random() * 12) + 's';
    h.style.animationDelay = (Math.random() * 10) + 's';
    container.appendChild(h);
  }
}

/* ---- Typewriter Effect ---- */
function typewriter(el, text, speed = 60, onDone) {
  if (typeof el === 'string') el = document.getElementById(el);
  if (!el) return;
  el.textContent = '';
  let i = 0;
  el.classList.add('typewriter-cursor');
  const id = setInterval(() => {
    el.textContent += text[i++];
    if (i >= text.length) {
      clearInterval(id);
      setTimeout(() => el.classList.remove('typewriter-cursor'), 600);
      if (onDone) onDone();
    }
  }, speed);
}

/* ---- Sync URL hash/query params into sessionStorage ---- */
function syncUrlParamsToSession() {
  try {
    const sources = [];
    if (window.location.hash && window.location.hash.length > 1) {
      sources.push(new URLSearchParams(window.location.hash.slice(1)));
    }
    if (window.location.search && window.location.search.length > 1) {
      sources.push(new URLSearchParams(window.location.search));
    }
    for (const params of sources) {
      const hName = (params.get('name') || '').trim();
      if (hName && hName !== 'anonymous') {
        sessionStorage.setItem('lover_name', hName);
      }
      const dText = (params.get('date_text') || '').trim();
      if (dText) {
        sessionStorage.setItem('custom_date_text', dText);
      }
      const dTarget = (params.get('target') || '').trim();
      if (dTarget) {
        sessionStorage.setItem('custom_date_target', dTarget);
      }
      const dTanggal = (params.get('tanggal_date') || '').trim();
      if (dTanggal) {
        sessionStorage.setItem('custom_tanggal_date', dTanggal);
      }
    }
  } catch (e) {}
}
syncUrlParamsToSession();

/* ---- Persistent name & date getters from URL hash/query or sessionStorage ---- */
function getName() {
  syncUrlParamsToSession();
  return sessionStorage.getItem('lover_name') || 'Sayang';
}
function setName(name) { sessionStorage.setItem('lover_name', name); }

function getCustomDateText(fallback = '') {
  syncUrlParamsToSession();
  return sessionStorage.getItem('custom_date_text') || fallback;
}
function getCustomDateTarget(fallback = '') {
  syncUrlParamsToSession();
  return sessionStorage.getItem('custom_date_target') || fallback;
}
function getCustomTanggalDate(fallback = '') {
  syncUrlParamsToSession();
  return sessionStorage.getItem('custom_tanggal_date') || fallback;
}

/* ---- Init shared features ---- */
document.addEventListener('DOMContentLoaded', () => {
  // Stars bg — create if not in HTML
  if (!document.querySelector('.stars')) {
    const s = document.createElement('div');
    s.className = 'stars';
    document.body.prepend(s);
  }

  // Hearts — spawn into existing container or create one
  let hc = document.getElementById('hearts') || document.querySelector('.hearts-container');
  if (!hc) {
    hc = document.createElement('div');
    hc.className = 'hearts-container';
    document.body.prepend(hc);
  }
  spawnHearts(hc, 18);
});

/* ---- Utility: element by id shorthand ---- */
function $(id) { return document.getElementById(id); }
