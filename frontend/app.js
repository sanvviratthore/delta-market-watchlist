/*
 * Vanilla JS, no build step — the engineering effort for this project
 * went into the backend (change-detection engine, data-source
 * resilience, fan-out scaling). The UI's job is to render that
 * clearly and honestly, not to be a framework showcase.
 */

// The frontend is served BY the same FastAPI app as the API (see the
// StaticFiles mount in main.py), so it's always same-origin -- no
// hardcoded backend URL to maintain, works identically local and deployed.
const API_BASE = window.location.origin;
const WS_BASE = API_BASE.replace("http", "ws"); // http->ws and https->wss both work with this replace

let token = localStorage.getItem("delta_token"); // auth token only; watchlist state stays server-side
const sockets = {};
const digestCache = {}; // symbol -> latest /changes response, used to compute the attention line

const els = {
  authScreen: document.getElementById("auth-screen"),
  mainScreen: document.getElementById("main-screen"),
  authForm: document.getElementById("auth-form"),
  email: document.getElementById("email"),
  password: document.getElementById("password"),
  registerBtn: document.getElementById("register-btn"),
  authError: document.getElementById("auth-error"),
  dateline: document.getElementById("dateline"),
  addForm: document.getElementById("add-form"),
  symbolInput: document.getElementById("symbol-input"),
  ledgerBody: document.getElementById("ledger-body"),
  emptyState: document.getElementById("empty-state"),
  attentionLine: document.getElementById("attention-line"),
  rowTemplate: document.getElementById("row-template"),
  connectionBanner: document.getElementById("connection-banner"),
  logoutBtn: document.getElementById("logout-btn"),
};

function updateDateline() {
  const now = new Date();
  els.dateline.textContent = now.toLocaleString(undefined, {
    weekday: "short", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}
updateDateline();
setInterval(updateDateline, 30_000);

function api(path, opts = {}) {
  const headers = opts.headers || {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return fetch(`${API_BASE}${path}`, { ...opts, headers })
    .then(async (r) => {
      hideConnectionBanner(); // request reached the server at all, so it's up
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body.detail || r.statusText);
      }
      return r.json();
    })
    .catch((err) => {
      // fetch() throws a TypeError specifically when the server can't be
      // reached at all (down, wrong port, CORS blocked) -- distinct from
      // a normal HTTP error response, which means the server IS up.
      if (err instanceof TypeError) {
        showConnectionBanner();
      }
      throw err;
    });
}

let reconnectTimer = null;

function showConnectionBanner() {
  els.connectionBanner.classList.remove("hidden");
  if (reconnectTimer) return;
  reconnectTimer = setInterval(async () => {
    try {
      const r = await fetch(`${API_BASE}/health`);
      if (r.ok) {
        hideConnectionBanner();
        if (token) loadWatchlist(); // pick back up where we left off
      }
    } catch {
      /* still down, keep waiting */
    }
  }, 5000);
}

function hideConnectionBanner() {
  els.connectionBanner.classList.add("hidden");
  if (reconnectTimer) {
    clearInterval(reconnectTimer);
    reconnectTimer = null;
  }
}

async function login(email, password) {
  const form = new URLSearchParams();
  form.set("username", email);
  form.set("password", password);
  let resp;
  try {
    resp = await fetch(`${API_BASE}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: form,
    });
  } catch (err) {
    if (err instanceof TypeError) showConnectionBanner();
    throw new Error("Can't reach the server right now.");
  }
  hideConnectionBanner();
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || "That email or password doesn't match our records.");
  }
  const data = await resp.json();
  setToken(data.access_token);
}

async function register(email, password) {
  const data = await api("/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  setToken(data.access_token);
}

function setToken(t) {
  token = t;
  localStorage.setItem("delta_token", t);
  showMain();
}

function showMain() {
  els.authScreen.classList.add("hidden");
  els.mainScreen.classList.remove("hidden");
  els.logoutBtn.classList.remove("hidden");
  loadWatchlist();
}

function logout() {
  token = null;
  localStorage.removeItem("delta_token");
  Object.values(sockets).forEach((ws) => ws.close());
  Object.keys(sockets).forEach((k) => delete sockets[k]);
  Object.keys(digestCache).forEach((k) => delete digestCache[k]);
  hideConnectionBanner();
  els.mainScreen.classList.add("hidden");
  els.logoutBtn.classList.add("hidden");
  els.authScreen.classList.remove("hidden");
  els.email.value = "";
  els.password.value = "";
}

els.logoutBtn.addEventListener("click", logout);

async function loadWatchlist() {
  const items = await api("/watchlist");
  els.ledgerBody.innerHTML = "";
  Object.keys(digestCache).forEach((k) => delete digestCache[k]);

  els.emptyState.classList.toggle("hidden", items.length > 0);

  for (const item of items) {
    renderRow(item.symbol);
    subscribeLive(item.symbol);
    loadDigest(item.symbol);
  }
  if (!items.length) updateAttentionLine();
}

function rowFor(symbol) {
  return document.getElementById(`row-${symbol}`);
}

function renderRow(symbol) {
  if (rowFor(symbol)) return;
  const node = els.rowTemplate.content.cloneNode(true);
  const row = node.querySelector(".ledger-row");
  row.id = `row-${symbol}`;
  row.querySelector(".symbol").textContent = symbol;
  row.querySelector(".remove-btn").addEventListener("click", () => removeSymbol(symbol));
  els.ledgerBody.appendChild(node);
}

function updateAttentionLine() {
  const symbols = Object.keys(digestCache);
  const meaningful = symbols.filter((s) => digestCache[s]?.events?.length > 0);

  if (symbols.length === 0) {
    els.attentionLine.textContent = "";
    return;
  }
  if (meaningful.length === 0) {
    els.attentionLine.className = "attention-line quiet";
    els.attentionLine.textContent = `Everything's quiet — none of your ${symbols.length} position${symbols.length === 1 ? "" : "s"} has moved meaningfully since you last checked.`;
    return;
  }
  els.attentionLine.className = "attention-line active";
  els.attentionLine.innerHTML =
    `<b>${meaningful.length} of ${symbols.length}</b> position${symbols.length === 1 ? "" : "s"} moved meaningfully since you last checked.`;
}

async function loadDigest(symbol) {
  try {
    const data = await api(`/watchlist/${symbol}/changes`);
    digestCache[symbol] = data;
    const row = rowFor(symbol);
    if (!row) return;

    row.querySelector(".price").textContent = `$${data.current_price.toFixed(2)}`;

    const quality = row.querySelector(".quality-tag");
    quality.textContent = data.data_quality;
    quality.className = "quality-tag";

    const signal = row.querySelector(".signal-text");
    if (!data.events.length) {
      signal.className = "signal-text";
      signal.textContent = data.last_seen_price
        ? `Quiet since you checked — still near $${data.last_seen_price.toFixed(2)}.`
        : "Just added — tracking from here.";
    } else {
      signal.className = "signal-text meaningful";
      const pct = data.pct_change_since_last_seen;
      const pctStr = pct != null ? `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%` : "";
      const last = data.events[data.events.length - 1];
      signal.innerHTML = `<b>${pctStr}</b> — ${last.detail}.`;
    }

    const marketNote = row.querySelector(".market-context");
    if (data.market_context) {
      marketNote.textContent = data.market_context;
      marketNote.classList.remove("hidden");
    } else {
      marketNote.textContent = "";
      marketNote.classList.add("hidden");
    }

    updateAttentionLine();
  } catch (e) {
    console.warn(`digest failed for ${symbol}`, e);
  }
}

function subscribeLive(symbol) {
  if (sockets[symbol]) return;
  const ws = new WebSocket(`${WS_BASE}/ws/${symbol}`);
  ws.onmessage = (evt) => {
    const data = JSON.parse(evt.data);
    const row = rowFor(symbol);
    if (!row) return;

    const priceEl = row.querySelector(".price");
    const prevPrice = parseFloat(priceEl.textContent.replace("$", "")) || data.price;
    priceEl.textContent = `$${data.price.toFixed(2)}`;

    const deltaEl = row.querySelector(".live-delta");
    const diff = data.price - prevPrice;
    if (Math.abs(diff) > 0.0001) {
      deltaEl.textContent = `${diff >= 0 ? "+" : "−"}${Math.abs(diff).toFixed(2)}`;
      deltaEl.className = `live-delta ${diff >= 0 ? "up" : "down"}`;
    }

    const quality = row.querySelector(".quality-tag");
    quality.textContent = data.source === "simulated" ? "simulated" : data.quality;
    quality.className = `quality-tag ${data.source === "simulated" ? "simulated" : ""}`;

    row.classList.add("flash");
    setTimeout(() => row.classList.remove("flash"), 700);

    if (data.is_meaningful) loadDigest(symbol);
  };
  ws.onclose = () => delete sockets[symbol];
  sockets[symbol] = ws;
}

async function removeSymbol(symbol) {
  await api(`/watchlist/${symbol}`, { method: "DELETE" });
  if (sockets[symbol]) sockets[symbol].close();
  delete digestCache[symbol];
  const row = rowFor(symbol);
  if (row) row.remove();
  els.emptyState.classList.toggle("hidden", els.ledgerBody.children.length > 0);
  updateAttentionLine();
}

els.authForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  els.authError.textContent = "";
  try {
    await login(els.email.value, els.password.value);
  } catch (err) {
    els.authError.textContent = err.message;
  }
});

els.registerBtn.addEventListener("click", async () => {
  els.authError.textContent = "";
  try {
    await register(els.email.value, els.password.value);
  } catch (err) {
    els.authError.textContent = err.message;
  }
});

els.addForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const symbol = els.symbolInput.value.trim().toUpperCase();
  if (!symbol) return;
  els.symbolInput.value = "";
  await api("/watchlist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol }),
  });
  els.emptyState.classList.add("hidden");
  renderRow(symbol);
  subscribeLive(symbol);
  setTimeout(() => loadDigest(symbol), 1200); // give the poller a beat to fetch the first tick
});

if (token) showMain();