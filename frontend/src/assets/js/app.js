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

/* ---- Persistent name from sessionStorage ---- */
function getName()         { return sessionStorage.getItem('lover_name') || 'Sayang'; }
function setName(name)     { sessionStorage.setItem('lover_name', name); }

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
