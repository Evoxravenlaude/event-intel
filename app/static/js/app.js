/* ═══════════════════════════════════════════════════════════════════════
   pulse — app logic.

   SAFETY: every string that came from the API or a feed is escaped before
   it goes into innerHTML. Ingested titles come from untrusted RSS/ICS/HTML;
   an unescaped <img onerror=…> would run in an operator's browser.
═══════════════════════════════════════════════════════════════════════ */
function esc(v) {
  return String(v ?? '').replace(/[&<>"'`]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;'}[c]));
}

/* ── compat ───────────────────────────────────────────────────────────── */
if (!AbortSignal.timeout) AbortSignal.timeout = ms => { const c = new AbortController(); setTimeout(() => c.abort(), ms); return c.signal; };
function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxxxxxx4xxxyxxxxxxxxxxxxxxx'.replace(/[xy]/g, c => { const r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 3 | 8)).toString(16); });
}
function lsGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch {} }
const IS_APPLE = /iPad|iPhone|iPod|Macintosh/.test(navigator.userAgent) && !/Android/.test(navigator.userAgent);
const IS_IOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
const IS_ANDROID = /Android/.test(navigator.userAgent);

/* ── state ────────────────────────────────────────────────────────────── */
let API_BASE = null;
let STATS = null;
let REVIEW_ITEMS = [];
let LOCATION = null;        // {lat, lng, accuracy(m), city, country, label, source: gps|ip|search|city|map|saved}
let CONFIG = null;          // /config — map provider, styles, keys meant for browsers, limits
let IS_ADMIN = false;
let SORT_MODE = lsGet('ei_sort') || 'time';
let RADIUS_KM = Number(lsGet('ei_radius')) || 25;
let COVERAGE = null;
let SCANNING = false;
let CATEGORIES = ['web3', 'tech', 'business', 'culture', 'sports', 'food', 'nightlife', 'wellness', 'community', 'education'];
let API_KEY = '';
let activeCity = 'Global';
let activeFilter = 'all';
let activeWhen = 'all';      // all | today | tomorrow | week | later (from the board)
let currentEvents = [];
let savedEvents = new Set(JSON.parse(lsGet('ei_saved') || '[]'));
let rsvpdEvents = new Set(JSON.parse(lsGet('ei_going') || '[]'));
let currentDetailEvent = null;
let selectedMapEventId = null;
let mapFilterActive = 'all';
let searchFilterActive = 'all';
let FEED_LIMIT = 24;
let _prevScreen = 'home';
const SCROLL = {};
const RADIUS_PRESETS = [5, 10, 25, 50, 100];
const persistSets = () => { lsSet('ei_saved', JSON.stringify([...savedEvents])); lsSet('ei_going', JSON.stringify([...rsvpdEvents])); };

const CITIES_DATA = [
  {name: 'London', lat: 51.5074, lng: -0.1278}, {name: 'New York', lat: 40.7128, lng: -74.0060},
  {name: 'San Francisco', lat: 37.7749, lng: -122.4194}, {name: 'Singapore', lat: 1.3521, lng: 103.8198},
  {name: 'Tokyo', lat: 35.6762, lng: 139.6503}, {name: 'Berlin', lat: 52.5200, lng: 13.4050},
  {name: 'Paris', lat: 48.8566, lng: 2.3522}, {name: 'Dubai', lat: 25.2048, lng: 55.2708},
  {name: 'Mumbai', lat: 19.0760, lng: 72.8777}, {name: 'São Paulo', lat: -23.5505, lng: -46.6333},
  {name: 'Sydney', lat: -33.8688, lng: 151.2093}, {name: 'Toronto', lat: 43.6532, lng: -79.3832},
  {name: 'Amsterdam', lat: 52.3676, lng: 4.9041}, {name: 'Seoul', lat: 37.5665, lng: 126.9780},
  {name: 'Lagos', lat: 6.5244, lng: 3.3792}, {name: 'Nairobi', lat: -1.2921, lng: 36.8219},
  {name: 'Cairo', lat: 30.0444, lng: 31.2357}, {name: 'Accra', lat: 5.6037, lng: -0.1870},
  {name: 'Bangalore', lat: 12.9716, lng: 77.5946}, {name: 'Bangkok', lat: 13.7563, lng: 100.5018},
  {name: 'Istanbul', lat: 41.0082, lng: 28.9784}, {name: 'Barcelona', lat: 41.3851, lng: 2.1734},
  {name: 'Lisbon', lat: 38.7169, lng: -9.1399}, {name: 'Bogotá', lat: 4.7110, lng: -74.0721},
  {name: 'Mexico City', lat: 19.4326, lng: -99.1332}, {name: 'Kuala Lumpur', lat: 3.1390, lng: 101.6869},
  {name: 'Jakarta', lat: -6.2088, lng: 106.8456}, {name: 'Riyadh', lat: 24.7136, lng: 46.6753},
  {name: 'Johannesburg', lat: -26.2041, lng: 28.0473}, {name: 'Cape Town', lat: -33.9249, lng: 18.4241},
];
const ALL_CITIES = CITIES_DATA.map(c => c.name).concat(['Vienna', 'Stockholm', 'Copenhagen', 'Zurich', 'Prague', 'Warsaw', 'Athens',
  'Karachi', 'Manila', 'Ho Chi Minh City', 'Buenos Aires', 'Lima', 'Santiago', 'Medellín', 'Casablanca', 'Doha', 'Abuja', 'Dakar', 'Kigali', 'Libreville', 'Port Harcourt']);

/* Demo data is dated relative to today so the board and "this week" always have something. */
const _in = (days, h, m = 0) => { const d = new Date(); d.setDate(d.getDate() + days); d.setHours(h, m, 0, 0); return d.toISOString(); };
const MOCK_EVENTS = [
  {id: 1, title: 'Afrobeats × Tech Night', city: 'Lagos', place_label: 'Victoria Island, Lagos', category: 'culture', status: 'upcoming', confidence_score: .81, evidence: 3, start_time: _in(0, 20), lat: 6.4281, lng: 3.4219, organizer: 'Lagos Creative Collective', price: '₦5,000'},
  {id: 2, title: 'Founders Breakfast Vol. II', city: 'Lagos', place_label: 'Ikoyi, Lagos', category: 'business', status: 'upcoming', confidence_score: .77, evidence: 2, start_time: _in(1, 8, 30), lat: 6.4549, lng: 3.4366, organizer: 'Ikoyi Founders', price: 'free'},
  {id: 3, title: 'Onchain Builders Meetup', city: 'Lagos', place_label: 'Yaba, Lagos', category: 'web3', status: 'upcoming', confidence_score: .69, evidence: 2, start_time: _in(2, 17), lat: 6.5095, lng: 3.3711, organizer: 'Web3 Lagos', price: 'free'},
  {id: 4, title: 'Sunday Run Club — Lekki Loop', city: 'Lagos', place_label: 'Lekki Phase 1, Lagos', category: 'sports', status: 'upcoming', confidence_score: .88, evidence: 4, start_time: _in(4, 6, 30), lat: 6.4474, lng: 3.4723, organizer: 'Lekki Runners', price: 'free'},
  {id: 5, title: 'Design & Product Night', city: 'Lagos', place_label: 'Ikeja, Lagos', category: 'tech', status: 'upcoming', confidence_score: .74, evidence: 3, start_time: _in(5, 18), lat: 6.6018, lng: 3.3515, organizer: 'Product Lagos', price: 'free'},
  {id: 6, title: 'Rooftop Supper Club', city: 'Lagos', place_label: 'Victoria Island, Lagos', category: 'food', status: 'upcoming', confidence_score: .63, evidence: 1, start_time: _in(6, 19, 30), lat: 6.4308, lng: 3.4153, organizer: 'Table Lagos', price: '₦25,000'},
  {id: 7, title: 'Global Web3 Summit', city: 'London', category: 'web3', status: 'upcoming', confidence_score: .91, evidence: 6, start_time: _in(9, 18), lat: 51.5074, lng: -0.1278, organizer: 'Web3 Foundation', price: 'free'},
  {id: 8, title: 'DeFi & DAO Forum', city: 'Dubai', category: 'web3', status: 'upcoming', confidence_score: .69, evidence: 3, start_time: _in(12, 9), token_gated: true, lat: 25.2048, lng: 55.2708, organizer: 'DIFC Innovation', price: 'token'},
  {id: 9, title: 'Nairobi Jazz in the Park', city: 'Nairobi', category: 'culture', status: 'upcoming', confidence_score: .72, evidence: 2, start_time: _in(3, 16), lat: -1.2921, lng: 36.8219, organizer: 'Nairobi Arts', price: 'KSh 1,000'},
  {id: 10, title: 'São Paulo VC & Founders Forum', city: 'São Paulo', category: 'business', status: 'upcoming', confidence_score: .85, evidence: 5, start_time: _in(15, 8), lat: -23.5505, lng: -46.6333, organizer: 'Latam Ventures', price: 'R$150'},
];
const MOCK_SIGNALS = [
  {source_type: 'luma', title: 'Afrobeats × Tech Night — RSVPs open', location_text: 'Lagos', ingested_at: '2m ago', source_confidence: .9},
  {source_type: 'ticketmaster', title: 'Jazz in the Park — tickets on sale', location_text: 'Nairobi', ingested_at: '8m ago', source_confidence: .86},
  {source_type: 'telegram', title: 'Onchain builders meetup this Wednesday', location_text: 'Yaba, Lagos', ingested_at: '14m ago', source_confidence: .63},
  {source_type: 'x', title: 'Run club route changed to Lekki loop', location_text: 'Lekki', ingested_at: '22m ago', source_confidence: .45},
];

/* ── router ───────────────────────────────────────────────────────────── */
const TAB_SCREENS = ['home', 'map', 'saved', 'explore'];
function goTo(screen) {
  const prev = document.querySelector('.screen.active');
  if (prev) { const id = prev.id.replace('screen-', ''); if (id !== screen) { _prevScreen = id; SCROLL[id] = window.scrollY; } }
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  const el = document.getElementById('screen-' + screen);
  if (el) { el.classList.add('active'); announce(`${screen === 'explore' ? 'control' : screen === 'home' ? 'feed' : screen}`); }
  document.body.dataset.screen = screen;
  if (TAB_SCREENS.includes(screen)) setTabs(screen);
  window.scrollTo(0, screen === 'detail' ? 0 : (SCROLL[screen] || 0));
  if (screen === 'map') initMap();
  if (screen === 'home') ensureHomeMap();
  if (screen === 'saved') renderItinerary();
  if (screen === 'explore') renderEngineTag();
}
function nav(screen) { if (screen !== 'detail' && location.search.includes('event=')) history.replaceState(null, '', location.pathname); goTo(screen); }
function setTabs(screen) {
  document.querySelectorAll('[data-tab]').forEach(b => { if (b.dataset.tab === screen) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current'); });
}
function goBack() {
  if (location.search.includes('event=')) history.replaceState(null, '', location.pathname);
  const back = _prevScreen && _prevScreen !== 'detail' && _prevScreen !== 'splash' ? _prevScreen : (LOCATION ? 'home' : 'splash');
  goTo(back);
}
function announce(msg) {
  const lr = document.getElementById('live-region');
  lr.textContent = ''; setTimeout(() => { lr.textContent = msg; }, 50);
}

/* ── location ─────────────────────────────────────────────────────────── */
function nearestKnownCity(lat, lng) {
  let nearest = CITIES_DATA[0], min = Infinity;
  for (const c of CITIES_DATA) {
    const d = Math.hypot(c.lat - lat, (c.lng - lng) * Math.cos(lat * Math.PI / 180));
    if (d < min) { min = d; nearest = c; }
  }
  return {...nearest, approxKm: min * 111};
}
/* Best GPS fix within a time budget: watchPosition with high accuracy, keep the
   most accurate reading, stop early once it's good enough. */
function getBestPosition({timeout = 12000, good = 60, onProgress} = {}) {
  return new Promise((resolve, reject) => {
    if (!('geolocation' in navigator)) return reject({code: 0, message: 'unsupported'});
    if (window.isSecureContext === false) return reject({code: 0, message: 'insecure'});
    let best = null, done = false, id = null, lastErr = null;
    const finish = err => {
      if (done) return; done = true;
      if (id !== null) navigator.geolocation.clearWatch(id);
      clearTimeout(timer);
      best ? resolve(best) : reject(err || lastErr || {code: 3, message: 'timeout'});
    };
    const timer = setTimeout(() => finish(), timeout);
    id = navigator.geolocation.watchPosition(pos => {
      if (!best || pos.coords.accuracy < best.coords.accuracy) { best = pos; onProgress && onProgress(pos.coords.accuracy); }
      if (pos.coords.accuracy <= good) finish();
    }, err => { lastErr = err; if (err.code === 1) finish(err); }, {enableHighAccuracy: true, maximumAge: 0, timeout});
  });
}
function getCoarsePosition() {
  return new Promise((resolve, reject) => navigator.geolocation.getCurrentPosition(resolve, reject, {enableHighAccuracy: false, maximumAge: 600000, timeout: 8000}));
}
function fmtAccuracy(m) {
  if (m == null) return '';
  if (m <= 1000) return `±${Math.max(5, Math.round(m / 5) * 5)} m`;
  return `±${(m / 1000).toFixed(m < 10000 ? 1 : 0)} km`;
}
function setLocStatus(msg) {
  const s = document.getElementById('splash-status'); if (s) s.textContent = msg || '';
  if (msg) announce(msg);
}
function locationHelp(reason) {
  if (reason === 'insecure') return 'Location needs a secure (https) connection.';
  if (reason === 'unsupported') return 'This browser can’t share your location.';
  if (reason === 'denied') {
    if (IS_IOS) return 'Location is off for this site. iPhone: Settings → Privacy & Security → Location Services → Safari Websites → While Using.';
    if (IS_ANDROID) return 'Location is blocked for this site. Tap the icon left of the address → Permissions → Location → Allow.';
    return 'Location is blocked for this site. Click the location icon in the address bar and allow it.';
  }
  return 'Couldn’t get a GPS fix.';
}

let _locating = false;
async function requestLocation(manual = false) {
  if (_locating) return;
  _locating = true;
  const btn = document.getElementById('splash-locate'); if (btn) btn.disabled = true;
  setLocStatus('finding you…');
  try {
    let pos;
    try {
      pos = await getBestPosition({onProgress: acc => setLocStatus(`finding you… ${fmtAccuracy(acc)}`)});
    } catch (err) {
      if (err && err.code === 1) throw err;
      pos = await getCoarsePosition();
    }
    const c = pos.coords;
    setLocStatus('');
    await setLocation({lat: c.latitude, lng: c.longitude, accuracy: Math.round(c.accuracy), source: 'gps'});
  } catch (err) {
    const reason = !err ? 'unavailable' : err.code === 1 ? 'denied' : err.code === 0 ? (err.message || 'unsupported') : 'unavailable';
    let ipLoc = null;
    if (API_BASE) { try { ipLoc = await api('/geo/ip', {signal: AbortSignal.timeout(6000)}); } catch {} }
    if (ipLoc) {
      setLocStatus('');
      await setLocation({lat: ipLoc.latitude, lng: ipLoc.longitude, city: ipLoc.city, country: ipLoc.country,
                         label: ipLoc.label || ipLoc.city, accuracy: 25000, source: 'ip', why: reason});
    } else {
      setLocStatus(locationHelp(reason));
      if (manual || document.body.dataset.screen === 'splash') setTimeout(() => goTo('city'), manual ? 0 : 1600);
    }
  } finally {
    _locating = false; if (btn) btn.disabled = false;
  }
}

/* The single entry point for "where am I looking". */
async function setLocation(loc, {stay = false} = {}) {
  LOCATION = {...loc};
  if (!LOCATION.city && !API_BASE) {
    const n = nearestKnownCity(loc.lat, loc.lng);
    LOCATION.city = n.approxKm < 80 ? n.name : `${loc.lat.toFixed(2)}, ${loc.lng.toFixed(2)}`;
  }
  LOCATION.label = LOCATION.label || LOCATION.city || 'locating…';
  activeCity = LOCATION.city || activeCity;
  if (loc.source !== 'saved') lsSet('ei_loc', JSON.stringify({...LOCATION, ts: Date.now()}));
  activeWhen = 'all';
  updateLocationUI();
  if (!stay) nav('home');
  updateMapsForLocation(true);
  if (API_BASE) await loadLiveData(); else renderFeed(MOCK_EVENTS);
}

function updateLocationUI() {
  if (!LOCATION) return;
  const short = LOCATION.label || LOCATION.city || 'nearby';
  document.getElementById('nav-city').textContent = short;
  const gps = LOCATION.source === 'gps' || LOCATION.source === 'saved';
  document.getElementById('nav-acc').textContent = gps && LOCATION.accuracy ? fmtAccuracy(LOCATION.accuracy) : '';
  document.getElementById('loc-pip').classList.toggle('approx', !gps || LOCATION.accuracy > 3000);
  const area = (LOCATION.label || LOCATION.city || 'here').split(',')[0];
  document.getElementById('hero-place').textContent = area.toLowerCase();
  document.getElementById('hero-kicker').textContent = gps ? `near you · ${LOCATION.city || ''}${LOCATION.country ? ', ' + LOCATION.country : ''}`.replace(/ · $/, '')
    : LOCATION.source === 'map' ? 'map area' : LOCATION.source === 'ip' ? 'approximate area' : (LOCATION.country || 'selected place');
  const il = document.getElementById('ingest-city-label'); if (il) il.textContent = LOCATION.city || short;
  const banner = document.getElementById('loc-banner'), text = document.getElementById('loc-banner-text');
  const approx = LOCATION.source === 'ip' || (LOCATION.source === 'gps' && LOCATION.accuracy > 3000);
  if (approx) {
    text.textContent = LOCATION.source === 'ip'
      ? `Showing ${LOCATION.city || 'your area'} from your connection — not your exact spot.${LOCATION.why === 'denied' ? ' Location access is off.' : ''}`
      : `Your device gave a rough fix (${fmtAccuracy(LOCATION.accuracy)}). Distances may be off by a few km.`;
    banner.hidden = false;
  } else banner.hidden = true;
}

/* ── place search ─────────────────────────────────────────────────────── */
function renderCityChips() {
  const popular = ['Lagos', 'Nairobi', 'London', 'New York', 'Berlin', 'Dubai', 'Singapore', 'Tokyo', 'São Paulo', 'Accra'];
  document.getElementById('city-chips').innerHTML = popular.map(c => `<button class="chip" onclick="selectCity(${esc(JSON.stringify(c))})">${esc(c)}</button>`).join('');
}
let _citySearchTimer = null;
let _placeSession = uuid();
const KIND_ICON = {venue: 'i-pin', area: 'i-map', address: 'i-pin', city: 'i-city'};
function suggestionRow(sg) {
  return `<li><button class="result" role="option" data-sugg="${esc(JSON.stringify(sg))}" onclick="pickSuggestion(this)">
      <span class="ic"><svg class="ico sm"><use href="#${KIND_ICON[sg.kind] || 'i-city'}"/></svg></span>
      <span style="min-width:0"><b>${esc(sg.primary)}</b>${sg.secondary ? `<small>${esc(sg.secondary)}</small>` : ''}</span>
      ${sg.kind ? `<span class="k">${esc(sg.kind)}</span>` : ''}
    </button></li>`;
}
function filterCities(q) {
  const res = document.getElementById('city-results');
  q = (q || '').trim();
  if (!q) { res.innerHTML = ''; return; }
  if (!API_BASE) {
    const matches = ALL_CITIES.filter(c => c.toLowerCase().includes(q.toLowerCase())).slice(0, 10);
    res.innerHTML = matches.map(c => suggestionRow({id: 'local:' + c, primary: c, kind: 'city'})).join('') || '<li class="results-note">no matches — try a city name.</li>';
    return;
  }
  clearTimeout(_citySearchTimer);
  _citySearchTimer = setTimeout(async () => {
    const bias = LOCATION ? `&lat=${LOCATION.lat}&lng=${LOCATION.lng}` : '';
    try {
      const hits = await api(`/places/autocomplete?q=${encodeURIComponent(q)}${bias}&session=${_placeSession}`);
      if (document.getElementById('city-input').value.trim() !== q) return;
      res.innerHTML = hits.length ? hits.map(suggestionRow).join('') : '<li class="results-note">nothing found — try a city name.</li>';
    } catch { res.innerHTML = '<li class="results-note">search is unavailable right now.</li>'; }
  }, 220);
}
async function pickSuggestion(el) {
  const sg = JSON.parse(el.dataset.sugg);
  if (sg.id.startsWith('local:')) return selectCity(sg.primary);
  let place = null;
  if (sg.latitude != null) {
    place = {latitude: sg.latitude, longitude: sg.longitude, city: sg.kind === 'city' ? sg.primary : null,
             label: sg.secondary && sg.kind !== 'city' ? `${sg.primary}, ${sg.secondary.split(',')[0]}` : sg.primary};
  } else {
    try { place = await api(`/places/resolve?id=${encodeURIComponent(sg.id)}&session=${_placeSession}`); } catch {}
  }
  _placeSession = uuid();
  document.getElementById('city-input').value = '';
  document.getElementById('city-results').innerHTML = '';
  if (!place) { announce('Could not open that place'); return; }
  selectPlace(place);
}
function selectPlace(p) {
  setLocation({lat: p.latitude, lng: p.longitude, city: p.city, country: p.country,
               label: p.label || p.city, accuracy: p.accuracy === 'city' ? 5000 : 50, source: 'search'});
}
function selectCity(city) {
  const known = CITIES_DATA.find(c => c.name === city);
  if (known) { setLocation({lat: known.lat, lng: known.lng, city: known.name, label: known.name, accuracy: 5000, source: 'city'}); return; }
  if (API_BASE) {
    api(`/places/autocomplete?q=${encodeURIComponent(city)}`).then(async hits => {
      if (!hits.length) return;
      const h = hits[0];
      if (h.latitude != null) selectPlace({latitude: h.latitude, longitude: h.longitude, city: h.primary, label: h.primary, accuracy: 'city'});
      else selectPlace(await api(`/places/resolve?id=${encodeURIComponent(h.id)}`));
    }).catch(() => {});
    return;
  }
  activeCity = city;
  document.getElementById('nav-city').textContent = city;
  document.getElementById('hero-place').textContent = city.toLowerCase();
  nav('home'); renderFeed(MOCK_EVENTS);
}

/* ── formatting ───────────────────────────────────────────────────────── */
function fmtDistance(km) {
  if (km == null || !Number.isFinite(km)) return '';
  if (km < 1) return `${Math.max(10, Math.round(km * 100) * 10)} m`;
  return km < 10 ? `${km.toFixed(1)} km` : `${Math.round(km)} km`;
}
function dayDiff(d) { const a = new Date(); a.setHours(0, 0, 0, 0); const b = new Date(d); b.setHours(0, 0, 0, 0); return Math.round((b - a) / 86400000); }
function fmtDate(iso) {
  if (!iso) return {mon: '—', day: '—', full: 'date tba', time: '', wk: '', rel: 'date tba', bar: 'date tba'};
  const d = new Date(iso);
  const time = d.toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'});
  const wk = d.toLocaleDateString('en-GB', {weekday: 'short'});
  const dd = dayDiff(d);
  const rel = dd === 0 ? (d.getHours() >= 17 ? 'tonight' : 'today') : dd === 1 ? 'tomorrow' : dd > 1 && dd < 7 ? wk.toLowerCase() : dd < 0 ? 'past' : d.toLocaleDateString('en-GB', {day: 'numeric', month: 'short'}).toLowerCase();
  return {
    mon: d.toLocaleDateString('en-GB', {month: 'short'}).toUpperCase(), day: d.getDate(), time, wk, rel, dd,
    full: d.toLocaleDateString('en-GB', {weekday: 'short', month: 'short', day: 'numeric'}) + ', ' + time,
    bar: `${wk} ${d.getDate()} ${d.toLocaleDateString('en-GB', {month: 'short'})} · ${time}`,
  };
}
function whenBucket(iso) {
  if (!iso) return 'later';
  const dd = dayDiff(iso);
  return dd <= 0 ? 'today' : dd === 1 ? 'tomorrow' : dd < 7 ? 'week' : 'later';
}
function confLabel(c) { return c >= .75 ? 'confirmed' : c >= .5 ? 'likely' : 'unconfirmed'; }
function timeAgo(iso) {
  if (!iso) return 'just now';
  const diff = (Date.now() - new Date(iso)) / 1000;
  if (diff < 60) return `${Math.max(1, Math.round(diff))}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

/* "Ilé MIMU" + "Ilé MIMU, Lagos" → "Ilé MIMU, Lagos"; never say the venue twice. */
function whereText(ev) {
  const place = ev.place_label || ev.venue?.city || ev.city || '';
  const v = ev.venue?.name;
  if (!v || v === 'Pinned location' || v === ev.venue?.city) return place;
  const vl = v.toLowerCase();
  if (place.toLowerCase().startsWith(vl)) return place;
  const city = ev.venue?.city || place.split(', ').pop();
  return city && !vl.includes(city.toLowerCase()) ? `${v}, ${city}` : v;
}

/* ── cards ────────────────────────────────────────────────────────────── */
function makeEventCard(ev, i = 0, {compact = false} = {}) {
  const d = fmtDate(ev.start_time);
  const c = ev.confidence_score || 0, pct = Math.round(c * 100);
  const gone = rsvpdEvents.has(ev.id), saved = savedEvents.has(ev.id);
  const place = whereText(ev) || '—';
  const approx = ev.geo_accuracy === 'city' || ev.geo_accuracy === 'region';
  const label = `${ev.title}, ${d.rel} ${d.time}, ${place}${ev.distance_km != null ? ', ' + fmtDistance(ev.distance_km) + ' away' : ''}`;
  return `<li><article class="card win plated" data-cat="${esc(ev.category || 'general')}" style="--i:${Math.min(i, 12)}" tabindex="0" role="link"
      aria-label="${esc(label)}" onclick="openDetail(${ev.id})" onkeydown="if(event.target===this&&(event.key==='Enter'||event.key===' ')){event.preventDefault();openDetail(${ev.id})}">
    <div class="win-bar"><span>${esc(ev.category || 'event')}</span><span class="when">${esc(d.rel === 'past' ? d.bar : d.rel + ' · ' + d.time)}</span></div>
    <div class="card-body">
      <h3 class="card-title">${esc(ev.title)}</h3>
      <div class="card-where">
        ${ev.distance_km != null ? `<span class="dist">${fmtDistance(ev.distance_km)}</span>` : ''}
        <span><span class="arrow">↳</span> ${esc(place)}</span>
      </div>
      ${compact ? '' : `<div class="card-foot">
        <span class="conf" title="confidence ${pct}%" style="--p:${pct}%"><i></i>${pct}%</span>
        ${c >= .75 ? '<span class="badge mint">confirmed</span>' : ''}
        ${approx ? '<span class="badge ghost" title="Exact venue not published">area only</span>' : ''}
        ${ev.token_gated ? '<span class="badge">token</span>' : ''}
        <span class="grow"></span>
        <button class="save" onclick="event.stopPropagation();toggleSave(${ev.id})" aria-pressed="${saved}" aria-label="${saved ? 'Remove from saved' : 'Save'}"><svg class="ico sm"><use href="#i-heart"/></svg></button>
        <button class="rsvp ${gone ? 'going' : ''}" onclick="event.stopPropagation();${ev.token_gated ? 'openWalletSheet()' : 'openRsvp(' + ev.id + ')'}">${gone ? '✓ going' : 'rsvp'}</button>
      </div>`}
    </div>
  </article></li>`;
}
function skeletonCards(n = 4) {
  return Array.from({length: n}, () => `<li><div class="card win plated skel"><div class="win-bar">loading</div><div class="card-body" style="display:block">
    <div class="bone" style="width:72%;height:18px"></div><div class="bone" style="width:48%"></div><div class="bone" style="width:30%"></div></div></div></li>`).join('');
}

/* ── feed ─────────────────────────────────────────────────────────────── */
function withDistances(events) {
  if (!LOCATION) return events;
  return events.map(e => (e.distance_km == null && Number.isFinite(e.lat) && Number.isFinite(e.lng))
    ? {...e, distance_km: Math.round(haversine(LOCATION.lat, LOCATION.lng, e.lat, e.lng) * 100) / 100} : e);
}
function renderFeed(events) {
  events = withDistances(events);
  if (SORT_MODE === 'distance') events = [...events].sort((a, b) => (a.distance_km ?? 1e9) - (b.distance_km ?? 1e9));
  else events = [...events].sort((a, b) => (a.start_time ? new Date(a.start_time) : Infinity) - (b.start_time ? new Date(b.start_time) : Infinity));
  currentEvents = events;
  renderMapPins(events);
  const filtered = events.filter(e => (activeFilter === 'all' || e.category === activeFilter) && (activeWhen === 'all' || whenBucket(e.start_time) === activeWhen));
  const list = document.getElementById('main-events-list'), empty = document.getElementById('main-empty'), more = document.getElementById('load-more-btn');
  if (SCANNING && !events.length) { list.innerHTML = skeletonCards(); empty.hidden = true; more.hidden = true; }
  else if (!filtered.length) {
    list.innerHTML = ''; empty.hidden = false;
    document.getElementById('main-empty-text').textContent = events.length
      ? 'Nothing matches that filter — try another category or day.'
      : (API_BASE && LOCATION ? `No events found within ${RADIUS_KM} km yet. Widen the radius or rescan.` : 'Pick a place to see what’s on.');
    more.hidden = true;
  } else {
    empty.hidden = true;
    list.innerHTML = filtered.slice(0, FEED_LIMIT).map((e, i) => makeEventCard(e, i)).join('');
    more.hidden = filtered.length <= FEED_LIMIT;
    more.textContent = `show more (${filtered.length - FEED_LIMIT})`;
  }
  renderBoard(events);
  const web3 = events.filter(e => e.category === 'web3').slice(0, 4);
  document.getElementById('web3-section').hidden = !web3.length || activeFilter === 'web3';
  document.getElementById('web3-list').innerHTML = web3.map((e, i) => makeEventCard(e, i)).join('');
  setStat('s-events', events.length);
  setStat('s-week', events.filter(e => { const b = whenBucket(e.start_time); return b !== 'later'; }).length);
  setStat('s-confirmed', events.filter(e => (e.confidence_score || 0) >= .75).length);
  const cap = document.getElementById('home-map-caption');
  if (cap) cap.textContent = LOCATION ? `${events.length} within ${RADIUS_KM} km` : `${events.length} events`;
  renderTicker();
  renderRadius();
}
const BOARD_COLS = [
  {key: 'today', label: 'today', c: 'var(--signal)'}, {key: 'tomorrow', label: 'tomorrow', c: 'var(--c-business)'},
  {key: 'week', label: 'this week', c: 'var(--c-web3)'}, {key: 'later', label: 'later', c: 'var(--c-tech)'},
];
function renderBoard(events) {
  const upcoming = events.filter(e => !e.start_time || dayDiff(e.start_time) >= 0);
  document.getElementById('week-board').innerHTML = BOARD_COLS.map(col => {
    const items = upcoming.filter(e => whenBucket(e.start_time) === col.key);
    const rows = items.slice(0, 4).map(e => {
      const d = fmtDate(e.start_time);
      return `<li><button class="board-item" onclick="openDetail(${e.id})"><time>${esc(col.key === 'week' || col.key === 'later' ? d.wk.toLowerCase() + ' ' + d.day : d.time)}</time>
        <span style="min-width:0"><b>${esc(e.title)}</b><small>${e.distance_km != null ? fmtDistance(e.distance_km) + ' · ' : ''}${esc(e.place_label || e.city || '')}</small></span></button></li>`;
    }).join('');
    return `<div class="win board-col" style="--col-c:${col.c}">
      <div class="win-bar"><b>${col.label}</b><span>${items.length}</span></div>
      ${items.length ? `<ul class="board-list">${rows}</ul>` : `<div class="board-empty">nothing ${col.key === 'today' ? 'left today' : 'yet'}.</div>`}
      ${items.length > 4 ? `<button class="board-more" onclick="setWhen('${col.key}')">+${items.length - 4} more →</button>` : ''}
    </div>`;
  }).join('');
}
function setWhen(w) {
  activeWhen = w; renderFilters(); renderFeed(currentEvents);
  document.getElementById('main-events-list').scrollIntoView({behavior: 'smooth', block: 'start'});
}
function renderTicker() {
  const t = document.getElementById('home-ticker'); if (!t) return;
  const ev = currentEvents.filter(e => !e.start_time || dayDiff(e.start_time) >= 0);
  const next = ev.filter(e => e.start_time && new Date(e.start_time) > new Date()).sort((a, b) => new Date(a.start_time) - new Date(b.start_time))[0];
  const place = (LOCATION && (LOCATION.label || LOCATION.city)) || activeCity;
  const items = [
    `${API_BASE ? 'live' : 'demo'} · ${ev.length} events ${LOCATION ? `within ${RADIUS_KM} km of ${place}` : 'worldwide'}`,
    next ? `next up: ${next.title} — ${fmtDate(next.start_time).rel} ${fmtDate(next.start_time).time}` : null,
    `${ev.filter(e => whenBucket(e.start_time) === 'today').length} today`,
    `${ev.filter(e => (e.confidence_score || 0) >= .75).length} confirmed`,
    COVERAGE && COVERAGE.last_scanned_at ? `scanned ${timeAgo(COVERAGE.last_scanned_at)}` : null,
    'sources: luma · ticketmaster · seatgeek',
  ].filter(Boolean);
  const html = items.map(s => `<span>${esc(s)}</span>`).join('');
  t.innerHTML = html + html;
  t.style.setProperty('--tick-dur', `${Math.max(24, items.join('').length * 0.32)}s`);
}
function renderSplashTicker() {
  const t = document.getElementById('splash-ticker');
  const html = CITIES_DATA.slice(0, 18).map(c => `<span>${esc(c.name)} ${Math.abs(c.lat).toFixed(2)}°${c.lat >= 0 ? 'N' : 'S'} ${Math.abs(c.lng).toFixed(2)}°${c.lng >= 0 ? 'E' : 'W'}</span>`).join('');
  t.innerHTML = html + html; t.style.setProperty('--tick-dur', '70s');
}
function renderRadius() {
  const wrap = document.getElementById('radius-chips'); if (!wrap) return;
  const max = (CONFIG && CONFIG.discover_max_radius_km) || 200;
  const opts = [...new Set([...RADIUS_PRESETS.filter(r => r <= max), RADIUS_KM])].sort((a, b) => a - b);
  wrap.innerHTML = opts.map(r => `<button class="chip" aria-pressed="${r === RADIUS_KM}" onclick="setRadius(${r})">${r} km</button>`).join('');
  document.getElementById('radius-row').hidden = !LOCATION;
}
function renderFilters() {
  const chip = (f, label, pressed, extra = '') => `<button class="chip ${extra}" aria-pressed="${pressed}" onclick="${f}">${label}</button>`;
  const when = activeWhen !== 'all' ? chip(`setWhen('all')`, `${BOARD_COLS.find(c => c.key === activeWhen).label} ✕`, true, 'filter-when') : '';
  const cats = ['all', ...CATEGORIES].map(f => chip(`setFilter('${f}')`, f === 'all' ? 'all' : `<span class="sw" data-cat="${f}" style="--sw:var(--c)"></span>${f}`, f === activeFilter)).join('');
  document.getElementById('main-filters').innerHTML = when + cats;
  document.getElementById('map-filters').innerHTML = ['all', ...CATEGORIES].map(f =>
    chip(`mapFilter('${f}')`, f === 'all' ? 'all' : `<span class="sw" data-cat="${f}" style="--sw:var(--c)"></span>${f}`, f === mapFilterActive)).join('');
  document.getElementById('search-filters').innerHTML = ['all', ...CATEGORIES, 'free', 'thisweek'].map(f =>
    chip(`searchFilter('${f}')`, f === 'thisweek' ? 'this week' : f, f === searchFilterActive)).join('');
}
function setFilter(f) { activeFilter = f; FEED_LIMIT = 24; renderFilters(); renderFeed(currentEvents); announce(`showing ${f}`); }
function loadMore() { FEED_LIMIT += 24; renderFeed(currentEvents); }

function renderSignals(signals) {
  document.getElementById('sig-feed').innerHTML = signals.slice(0, 6).map(s => `
    <div class="sig" role="article">
      <span class="src">${esc(s.source_type)}</span>
      <span style="min-width:0"><b>${esc(s.title || '(untitled)')}</b><small>${esc(s.location_text || '—')} · ${esc(s.ingested_at || 'just now')}</small></span>
      <span class="c">${(s.source_confidence ?? 0).toFixed(2)}</span>
    </div>`).join('') || '<div class="sig muted">no signals yet.</div>';
}
function renderCitiesSidebar() {
  let rows = [];
  if (STATS && STATS.by_city) {
    const max = Math.max(1, ...STATS.by_city.map(b => b.count));
    rows = STATS.by_city.map(b => ({name: b.key, count: b.count, pct: Math.round(100 * b.count / max)}));
  }
  const el = document.getElementById('cities-widget');
  if (!rows.length) { el.innerHTML = '<li class="rq muted">No cities yet — scan or ingest something.</li>'; return; }
  el.innerHTML = rows.map(c => `<li><button class="city-bar" onclick="selectCity(${esc(JSON.stringify(c.name))})" aria-label="${esc(c.name)}, ${c.count} events">
    <span class="nm">${esc(c.name)}</span><span class="bar"><i style="width:${c.pct}%"></i></span><span class="n">${c.count}</span></button></li>`).join('');
}
function toggleSave(id) {
  if (id == null) return;
  if (savedEvents.has(id)) { savedEvents.delete(id); announce('removed from saved'); }
  else { savedEvents.add(id); announce('saved'); }
  persistSets();
  renderFeed(currentEvents);
  syncDetailSave();
  if (document.body.dataset.screen === 'saved') renderItinerary();
}
function syncDetailSave() {
  const b = document.getElementById('detail-save');
  if (b && currentDetailEvent) b.setAttribute('aria-pressed', savedEvents.has(currentDetailEvent.id));
}
function refreshSignals() { if (API_BASE) loadLiveData(); else renderSignals(MOCK_SIGNALS); }

/* ── detail ───────────────────────────────────────────────────────────── */
function findEvent(id) { return currentEvents.find(e => e.id === id) || (API_BASE ? null : MOCK_EVENTS.find(e => e.id === id)); }
function openDetail(id) {
  const ev = findEvent(id);
  if (!ev) return;
  currentDetailEvent = ev;
  const d = fmtDate(ev.start_time), c = ev.confidence_score || 0;
  const city = ev.venue?.city || ev.city || '—';
  const cat = ev.category || 'general';
  for (const sel of ['#detail-hero', '#screen-detail .cta-bar', '#org-avatar']) document.querySelector(sel).dataset.cat = cat;
  document.getElementById('detail-flag').textContent = `${cat} · ${d.rel}`;
  document.getElementById('detail-title').textContent = ev.title;
  document.getElementById('detail-back-title').textContent = ev.title;
  document.getElementById('detail-date').textContent = d.full;
  document.getElementById('detail-venue').textContent = whereText(ev) || city;
  history.replaceState(null, '', `?event=${ev.id}`);
  document.getElementById('detail-conf').textContent = `${Math.round(c * 100)}% · ${confLabel(c)}`;
  document.getElementById('footer-event-title').textContent = ev.title;
  document.getElementById('success-event-title').textContent = ev.title;
  document.getElementById('success-event-date').textContent = `${ev.place_label || city} · ${d.full}`;
  document.getElementById('rsvp-sheet-title').textContent = ev.title;
  document.getElementById('rsvp-sheet-sub').textContent = `${ev.place_label || city} · ${cat}`;
  document.getElementById('rsvp-date-display').textContent = d.full;
  const orgName = ev.organizer || 'organiser not listed';
  document.getElementById('org-name').textContent = orgName;
  document.getElementById('org-avatar').textContent = (ev.organizer || '?')[0].toUpperCase();
  document.querySelector('#screen-detail .org').classList.toggle('none', !ev.organizer);
  const priceLabel = ev.price === 'free' || !ev.price ? 'free' : ev.price === 'token' ? 'token required' : ev.price;
  document.getElementById('rsvp-box-price').textContent = priceLabel;
  document.getElementById('footer-event-price').textContent = `${d.rel} · ${d.time} · ${priceLabel}`;
  const rsvpBtn = document.getElementById('detail-rsvp-btn');
  const going = rsvpdEvents.has(id);
  rsvpBtn.textContent = going ? '✓ you’re going' : ev.token_gated ? 'verify & rsvp' : 'rsvp';
  rsvpBtn.classList.toggle('done', going);
  document.getElementById('detail-tags').innerHTML = [
    c >= .75 ? '<span class="badge mint">confirmed</span>' : `<span class="badge">${confLabel(c)}</span>`,
    ev.evidence ? `<span class="badge ghost">${ev.evidence} source${ev.evidence === 1 ? '' : 's'}</span>` : '',
    ev.distance_km != null ? `<span class="badge signal">${fmtDistance(ev.distance_km)} away</span>` : '',
    ev.token_gated ? '<span class="badge">token gated</span>' : '',
  ].join('');
  document.getElementById('token-banner').hidden = !ev.token_gated;
  renderWhere(ev);
  const descEl = document.getElementById('detail-desc');
  descEl.textContent = ev.description || (API_BASE ? 'No description was published for this event.' : 'A demo event. Connect the API to see real listings, descriptions and organisers.');
  descEl.classList.add('clamp');
  const btn = document.getElementById('expand-btn');
  btn.hidden = descEl.textContent.length < 320; btn.textContent = 'read more ↓'; btn.setAttribute('aria-expanded', 'false');
  const pool = API_BASE ? currentEvents : MOCK_EVENTS;
  const related = pool.filter(e => e.id !== id)
    .map(e => ({e, d: (Number.isFinite(ev.lat) && Number.isFinite(e.lat)) ? haversine(ev.lat, ev.lng, e.lat, e.lng) : 1e9}))
    .sort((a, b) => (a.e.category === ev.category ? 0 : 1) - (b.e.category === ev.category ? 0 : 1) || a.d - b.d)
    .slice(0, 6).map(x => x.e);
  document.getElementById('related-section').hidden = !related.length;
  document.getElementById('related-scroll').innerHTML = related.map((r, i) => makeEventCard(withDistances([r])[0], i, {compact: true}).replace(/^<li>|<\/li>$/g, '')).join('');
  syncDetailSave();
  goTo('detail');
}
function directionLinks(ev) {
  const hasPt = Number.isFinite(ev.lat) && Number.isFinite(ev.lng);
  const v = ev.venue || {};
  const q = [v.name !== 'Pinned location' ? v.name : null, v.address || ev.place_label || ev.city].filter(Boolean).join(', ');
  if (!hasPt && !q) return [];
  const dest = hasPt ? `${ev.lat},${ev.lng}` : encodeURIComponent(q);
  const google = {label: 'Google Maps', href: hasPt ? `https://www.google.com/maps/dir/?api=1&destination=${dest}` : `https://www.google.com/maps/search/?api=1&query=${dest}`};
  const apple = {label: 'Apple Maps', href: hasPt ? `https://maps.apple.com/?daddr=${dest}&q=${encodeURIComponent(v.name || ev.title)}` : `https://maps.apple.com/?q=${dest}`};
  const primary = IS_APPLE ? apple : google, secondary = IS_APPLE ? google : apple;
  return [{...primary, primary: true}, secondary];
}
let detailMapPending = null;
function renderWhere(ev) {
  const sec = document.getElementById('detail-where-section');
  const v = ev.venue || {};
  const hasPt = Number.isFinite(ev.lat) && Number.isFinite(ev.lng);
  if (!hasPt && !v.address && !v.name) { sec.hidden = true; return; }
  sec.hidden = false;
  const approx = ev.geo_accuracy === 'city' || ev.geo_accuracy === 'region';
  const parts = [];
  if (v.name && v.name !== v.city && v.name !== 'Pinned location') parts.push(`<b>${esc(v.name)}</b>`);
  parts.push(esc(v.address || [v.neighbourhood, v.city, v.country].filter(Boolean).join(', ') || ev.place_label || ev.city || ''));
  if (ev.distance_km != null) parts.push(`<span class="dist">${fmtDistance(ev.distance_km)} from ${LOCATION && LOCATION.source === 'gps' ? 'you' : 'the centre of your search'}</span>`);
  document.getElementById('detail-addr').innerHTML = parts.filter(Boolean).join('<br>') + (approx ? '<br><span class="approx-note"><span class="badge ghost">area only</span>the organiser hasn’t published the exact venue</span>' : '');
  document.getElementById('detail-directions').innerHTML = directionLinks(ev).map(l =>
    `<a class="btn ${l.primary ? 'primary' : ''}" href="${esc(l.href)}" target="_blank" rel="noopener noreferrer"><svg class="ico sm"><use href="#i-dir"/></svg>${esc(l.label)}</a>`).join('');
  const src = document.getElementById('detail-source');
  if (ev.source_url) { src.href = ev.source_url; src.hidden = false; } else src.hidden = true;
  const mapEl = document.getElementById('detail-map');
  mapEl.hidden = !hasPt;
  if (!hasPt) return;
  detailMapPending = ev;
  setTimeout(async () => {
    if (detailMapPending !== ev) return;
    if (!MAPS.detail) { MAPS.detail = new EIMap(mapEl, {gestures: 'cooperative', zoom: 15, controls: false}); await MAPS.detail.init(ev.lat, ev.lng, 15); }
    MAPS.detail.invalidate();
    MAPS.detail.setPins([ev], null, ev.id);
    const gps = LOCATION && LOCATION.source === 'gps';
    if (gps) MAPS.detail.setUser(LOCATION.lat, LOCATION.lng, null);
    if (gps && !approx && haversine(LOCATION.lat, LOCATION.lng, ev.lat, ev.lng) < 10)
      MAPS.detail.fitPoints([{lat: ev.lat, lng: ev.lng}, {lat: LOCATION.lat, lng: LOCATION.lng}], 16);
    else MAPS.detail.setView(ev.lat, ev.lng, approx ? 12 : 15);
    document.getElementById('detail-provider').textContent = engineName();
  }, 60);
}
function expandDesc() {
  const desc = document.getElementById('detail-desc'), btn = document.getElementById('expand-btn');
  const collapsed = desc.classList.toggle('clamp');
  btn.textContent = collapsed ? 'read more ↓' : 'show less ↑';
  btn.setAttribute('aria-expanded', String(!collapsed));
}
function openRsvpFromDetail() {
  if (!currentDetailEvent || rsvpdEvents.has(currentDetailEvent.id)) return;
  if (currentDetailEvent.token_gated) { openWalletSheet(); return; }
  openRsvp(currentDetailEvent.id);
}
window.addEventListener('scroll', () => {
  const bar = document.getElementById('detail-bar');
  if (bar && document.body.dataset.screen === 'detail') bar.classList.toggle('scrolled', window.scrollY > 140);
}, {passive: true});

/* ── sheets ───────────────────────────────────────────────────────────── */
let _sheetReturn = null;
function openSheet(id) {
  _sheetReturn = document.activeElement;
  const s = document.getElementById(id);
  s.classList.add('open');
  document.body.style.overflow = 'hidden';
  const first = s.querySelector('input,select,button:not(.sheet-close)');
  if (first) setTimeout(() => first.focus(), 80);
}
function closeSheet(id) {
  const s = document.getElementById(id);
  if (!s.classList.contains('open')) return;
  s.classList.remove('open');
  document.body.style.overflow = '';
  if (_sheetReturn && _sheetReturn.focus) _sheetReturn.focus();
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { ['rsvp-sheet', 'wallet-sheet'].forEach(closeSheet); closeSearch(); hideMapPeek(); }
});
let currentRsvpId = null;
function openRsvp(id) {
  currentRsvpId = id;
  const ev = findEvent(id);
  if (ev) {
    document.getElementById('rsvp-sheet-title').textContent = ev.title;
    document.getElementById('rsvp-sheet-sub').textContent = `${ev.place_label || ev.city || ''} · ${ev.category}`;
    document.getElementById('rsvp-date-display').textContent = fmtDate(ev.start_time).full;
  }
  const name = lsGet('ei_name'), email = lsGet('ei_email');
  if (name) document.getElementById('rsvp-name').value = name;
  if (email) document.getElementById('rsvp-email').value = email;
  openSheet('rsvp-sheet');
}
function selectTicket(el) {
  document.querySelectorAll('#rsvp-sheet .opt').forEach(t => { t.classList.remove('sel'); t.setAttribute('aria-checked', 'false'); });
  el.classList.add('sel'); el.setAttribute('aria-checked', 'true');
}
function confirmRsvp() {
  const nameEl = document.getElementById('rsvp-name'), emailEl = document.getElementById('rsvp-email');
  const name = nameEl.value.trim(), email = emailEl.value.trim();
  if (!name) { nameEl.focus(); return; }
  if (!email || !/^\S+@\S+\.\S+$/.test(email)) { emailEl.focus(); return; }
  lsSet('ei_name', name); lsSet('ei_email', email);
  const id = currentRsvpId || (currentDetailEvent && currentDetailEvent.id);
  const ev = findEvent(id);
  if (ev) currentDetailEvent = ev;
  if (id) { rsvpdEvents.add(id); savedEvents.add(id); persistSets(); }
  closeSheet('rsvp-sheet');
  document.getElementById('success-attendee').textContent = name;
  if (ev) {
    document.getElementById('success-event-title').textContent = ev.title;
    document.getElementById('success-event-date').textContent = `${ev.place_label || ev.city || ''} · ${fmtDate(ev.start_time).full}`;
  }
  document.getElementById('success-ticket-id').textContent = 'PULSE-' + Date.now().toString(36).toUpperCase();
  renderFeed(currentEvents);
  spawnConfetti();
  goTo('success');
  announce('RSVP confirmed');
}

/* ── wallet (token-gated events) ──────────────────────────────────────── */
function openWalletSheet() { resetWallet(); openSheet('wallet-sheet'); }
function resetWallet() {
  document.getElementById('wallet-options').hidden = false;
  ['verify-loading', 'verify-success', 'verify-fail'].forEach(id => document.getElementById(id).classList.remove('show'));
}
function showVerify(state) {
  document.getElementById('wallet-options').hidden = true;
  ['verify-loading', 'verify-success', 'verify-fail'].forEach(id => document.getElementById(id).classList.remove('show'));
  document.getElementById('verify-' + state).classList.add('show');
}
function connectWallet() {
  showVerify('loading');
  setTimeout(() => {
    if (Math.random() > 0.4) { showVerify('success'); setTimeout(() => { closeSheet('wallet-sheet'); if (currentDetailEvent) openRsvp(currentDetailEvent.id); }, 1200); }
    else showVerify('fail');
  }, 1800);
}
function showWhatIsWallet() {
  const ex = document.getElementById('wallet-explainer');
  ex.hidden = !ex.hidden;
  document.getElementById('what-wallet-link').setAttribute('aria-expanded', String(!ex.hidden));
}

/* ── search overlay ───────────────────────────────────────────────────── */
function openSearch() {
  document.getElementById('search-overlay').classList.add('open');
  document.body.style.overflow = 'hidden';
  setTimeout(() => document.getElementById('search-input').focus(), 60);
  doSearch(document.getElementById('search-input').value);
}
function closeSearch() {
  const o = document.getElementById('search-overlay');
  if (!o.classList.contains('open')) return;
  o.classList.remove('open'); document.body.style.overflow = '';
}
function searchFilter(f) { searchFilterActive = f; renderFilters(); doSearch(document.getElementById('search-input').value); }
function doSearch(q) {
  const res = document.getElementById('search-results');
  let pool = API_BASE ? [...currentEvents] : [...currentEvents, ...MOCK_EVENTS.filter(m => !currentEvents.find(e => e.id === m.id))];
  if (!['all', 'free', 'thisweek'].includes(searchFilterActive)) pool = pool.filter(e => e.category === searchFilterActive);
  if (searchFilterActive === 'free') pool = pool.filter(e => !e.price || e.price === 'free');
  if (searchFilterActive === 'thisweek') pool = pool.filter(e => whenBucket(e.start_time) !== 'later');
  if (q.trim()) {
    const ql = q.toLowerCase();
    pool = pool.filter(e => [e.title, e.place_label, e.venue?.name, e.venue?.city, e.city, e.category, e.organizer].some(s => (s || '').toLowerCase().includes(ql)));
  }
  if (!pool.length) {
    res.innerHTML = `<div class="feed-empty" style="grid-column:1/-1;margin:0"><b>no matches${q ? ` for “${esc(q)}”` : ''}.</b><p>Try another place, category or word.</p></div>`;
    return;
  }
  res.innerHTML = pool.slice(0, 60).map((ev, i) => makeEventCard(ev, i, {compact: true}).replace('<li>', '<div role="listitem">').replace('</li>', '</div>')
    .replaceAll(`openDetail(${ev.id})`, `closeSearch();openDetail(${ev.id})`)).join('');
  announce(`${pool.length} result${pool.length !== 1 ? 's' : ''}`);
}

/* ── maps glue ────────────────────────────────────────────────────────── */
const DESKTOP_MQ = window.matchMedia('(min-width: 1100px)');
function engineName() {
  return {maplibre: 'openstreetmap', apple: 'apple maps', google: 'google maps', none: 'no map'}[MAPS.provider] || '';
}
function renderEngineTag() {
  const t = document.getElementById('map-engine-tag'); if (t) t.textContent = MAPS.provider ? `map: ${engineName()}` : '';
}
async function initMap() {
  const el = document.getElementById('main-map');
  const c = LOCATION || CITIES_DATA.find(x => x.name === activeCity) || {lat: 6.5244, lng: 3.3792};
  if (!MAPS.main) {
    MAPS.main = new EIMap(el, {gestures: 'greedy', zoom: LOCATION ? 12 : 3, onMoveEnd: onMainMapMoved});
    await MAPS.main.init(c.lat, c.lng);
  }
  setTimeout(() => { MAPS.main.invalidate(); renderMapPins(currentEvents.length ? currentEvents : MOCK_EVENTS); updateMapsForLocation(true); }, 60);
}
async function ensureHomeMap() {
  if (!DESKTOP_MQ.matches) return;
  const el = document.getElementById('home-map'); if (!el) return;
  const c = LOCATION || {lat: 6.5244, lng: 3.3792};
  if (!MAPS.home) {
    MAPS.home = new EIMap(el, {gestures: 'cooperative', zoom: 11});
    await MAPS.home.init(c.lat, c.lng);
    document.getElementById('home-map-provider').textContent = engineName();
  }
  setTimeout(() => { MAPS.home.invalidate(); MAPS.home.setPins(currentEvents, ev => openDetail(ev.id)); updateMapsForLocation(true); }, 60);
}
DESKTOP_MQ.addEventListener ? DESKTOP_MQ.addEventListener('change', ensureHomeMap) : DESKTOP_MQ.addListener(ensureHomeMap);
function updateMapsForLocation(refit) {
  if (!LOCATION) return;
  for (const m of [MAPS.main, MAPS.home]) {
    if (!m || !m.map) continue;
    m.setUser(LOCATION.lat, LOCATION.lng, LOCATION.source === 'gps' ? LOCATION.accuracy : null);
    m.setRadius(LOCATION.lat, LOCATION.lng, RADIUS_KM);
    if (refit) m.fitCircle(LOCATION.lat, LOCATION.lng, RADIUS_KM);
  }
  const btn = document.getElementById('map-search-here'); if (btn) btn.hidden = true;
}
function onMainMapMoved(m) {
  if (!LOCATION || !API_BASE) return;
  const c = m.center(); if (!c) return;
  const moved = haversine(c.lat, c.lng, LOCATION.lat, LOCATION.lng);
  const r = m.visibleRadiusKm() || RADIUS_KM;
  document.getElementById('map-search-here').hidden = !(moved > Math.max(1, RADIUS_KM * 0.25) || r > RADIUS_KM * 1.6);
}
async function searchThisArea() {
  const m = MAPS.main; const c = m && m.center(); if (!c) return;
  const r = Math.round(Math.min(Math.max(m.visibleRadiusKm() || RADIUS_KM, 2), (CONFIG && CONFIG.discover_max_radius_km) || 150));
  RADIUS_KM = r; lsSet('ei_radius', String(r));
  document.getElementById('map-search-here').hidden = true;
  await setLocation({lat: c.lat, lng: c.lng, accuracy: null, source: 'map'}, {stay: true});
}
function renderMapPins(events) {
  if (MAPS.main) MAPS.main.setPins(events, ev => showMapPopover(ev), selectedMapEventId);
  if (MAPS.home) MAPS.home.setPins(events, ev => openDetail(ev.id));
}
function showMapPopover(ev) {
  selectedMapEventId = ev.id;
  const d = fmtDate(ev.start_time);
  const where = whereText(ev);
  const p = document.getElementById('map-popover');
  p.dataset.cat = ev.category || 'general';
  document.getElementById('pop-tag').textContent = ev.category || 'event';
  document.getElementById('pop-when').textContent = `${d.rel} · ${d.time}`;
  document.getElementById('pop-title').textContent = ev.title;
  document.getElementById('pop-meta').textContent = `${ev.distance_km != null ? fmtDistance(ev.distance_km) + ' · ' : ''}${where}`;
  p.hidden = false;
  document.querySelector('.map-stage').style.setProperty('--peek-h', p.offsetHeight + 12 + 'px');
  MAPS.main && MAPS.main.setPins(MAPS.main.events, MAPS.main.onPin, ev.id);
  announce(`${ev.title} selected`);
}
function hideMapPeek() {
  const p = document.getElementById('map-popover');
  if (p && !p.hidden) {
    p.hidden = true; selectedMapEventId = null;
    document.querySelector('.map-stage').style.setProperty('--peek-h', '0px');
    MAPS.main && MAPS.main.setPins(MAPS.main.events, MAPS.main.onPin);
  }
}
function viewEventFromMap() { if (selectedMapEventId) openDetail(selectedMapEventId); }
function mapFilter(f) {
  mapFilterActive = f; renderFilters();
  renderMapPins(currentEvents.length ? currentEvents : MOCK_EVENTS);
  hideMapPeek();
}

/* ── saved ────────────────────────────────────────────────────────────── */
function renderItinerary() {
  const stack = document.getElementById('ticket-stack');
  const ids = [...new Set([...savedEvents, ...rsvpdEvents])];
  const pool = [...currentEvents, ...MOCK_EVENTS];
  const events = ids.map(id => pool.find(e => e.id === id)).filter(Boolean).sort((a, b) => new Date(a.start_time || 0) - new Date(b.start_time || 0));
  if (!events.length) {
    stack.innerHTML = `<div class="feed-empty" style="margin:0"><b>nothing saved yet.</b><p>Tap ♡ on anything that looks good — it lands here.</p>
      <button class="btn primary" style="margin-top:14px" onclick="nav('home')">browse what’s on →</button></div>`;
    return;
  }
  const now = new Date();
  stack.innerHTML = events.map(ev => {
    const d = fmtDate(ev.start_time), past = ev.start_time && new Date(ev.start_time) < now;
    const diff = new Date(ev.start_time) - now;
    const cd = past ? 'ended' : diff < 3600000 ? 'soon' : diff < 86400000 ? `in ${Math.round(diff / 3600000)}h` : `in ${Math.floor(diff / 86400000)}d`;
    return `<div class="win plated ticket ${past ? 'past' : ''}" data-cat="${esc(ev.category || 'general')}" role="listitem">
      <div class="ticket-main">
        <div class="kicker">${esc(ev.category || 'event')}${rsvpdEvents.has(ev.id) ? ' · going' : ''}</div>
        <b style="cursor:pointer" onclick="openDetail(${ev.id})">${esc(ev.title)}</b>
        <small>${esc(ev.place_label || ev.city || '')} · ${esc(d.full)}</small>
        <div class="ticket-acts">
          <button class="btn sm" onclick="addToCalendar(${ev.id})"><svg class="ico sm"><use href="#i-cal"/></svg>calendar</button>
          <button class="btn sm" onclick="openDetail(${ev.id})">open →</button>
          <button class="icon-btn" onclick="savedEvents.delete(${ev.id});rsvpdEvents.delete(${ev.id});persistSets();renderFeed(currentEvents);renderItinerary()" aria-label="Remove ${esc(ev.title)}" title="Remove"><svg class="ico sm"><use href="#i-x"/></svg></button>
        </div>
      </div>
      <div class="ticket-stub"><span class="m">${esc(d.mon)}</span><span class="d">${esc(d.day)}</span><span class="cd">${esc(cd)}</span></div>
    </div>`;
  }).join('');
}

/* ── success, calendar, share ─────────────────────────────────────────── */
function spawnConfetti() {
  const box = document.getElementById('confetti-container');
  box.innerHTML = '';
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const colors = ['#FF5A1F', '#B7D9C2', '#C9B6EA', '#F3D98B', '#A9CBEA', '#E8B4B8'];
  for (let i = 0; i < 36; i++) {
    const el = document.createElement('i');
    el.style.left = Math.random() * 100 + '%';
    el.style.background = colors[i % colors.length];
    el.style.borderRadius = Math.random() > .5 ? '50%' : '3px';
    el.style.animationDelay = Math.random() * 0.8 + 's';
    el.style.animationDuration = (1.6 + Math.random() * 1.2) + 's';
    box.appendChild(el);
  }
}
/* A real .ics file: opens in Apple Calendar / Outlook, imports into Google Calendar. */
function addToCalendar(id) {
  const ev = id != null ? findEvent(id) : currentDetailEvent;
  if (!ev || !ev.start_time) { announce('No date published for this event'); return; }
  const start = new Date(ev.start_time), end = ev.end_time ? new Date(ev.end_time) : new Date(start.getTime() + 2 * 3600000);
  const f = d => d.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}/, '');
  const icsEsc = s => String(s || '').replace(/\\/g, '\\\\').replace(/[,;]/g, m => '\\' + m).replace(/\n/g, '\\n');
  const where = [ev.venue?.name !== 'Pinned location' ? ev.venue?.name : null, ev.venue?.address || ev.place_label || ev.city].filter(Boolean).join(', ');
  const url = `${location.origin}/?event=${ev.id}`;
  const lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//pulse//events//EN', 'CALSCALE:GREGORIAN', 'BEGIN:VEVENT',
    `UID:pulse-${ev.id}@${location.host || 'pulse'}`, `DTSTAMP:${f(new Date())}`, `DTSTART:${f(start)}`, `DTEND:${f(end)}`,
    `SUMMARY:${icsEsc(ev.title)}`, `LOCATION:${icsEsc(where)}`, `DESCRIPTION:${icsEsc((ev.source_url ? ev.source_url + '\n' : '') + url)}`, `URL:${url}`,
    ...(Number.isFinite(ev.lat) ? [`GEO:${ev.lat};${ev.lng}`] : []), 'END:VEVENT', 'END:VCALENDAR'];
  const blob = new Blob([lines.join('\r\n')], {type: 'text/calendar'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = `${ev.title.replace(/[^\w\- ]+/g, '').trim().slice(0, 60) || 'event'}.ics`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  announce('Calendar file downloaded');
}
async function shareEvent() {
  if (!currentDetailEvent) return;
  const url = `${location.origin}/?event=${currentDetailEvent.id}`;
  try {
    if (navigator.share) { await navigator.share({title: currentDetailEvent.title, text: `${currentDetailEvent.title} — on pulse`, url}); return; }
    await navigator.clipboard.writeText(url);
    announce('Link copied');
  } catch {}
}

/* ── API ──────────────────────────────────────────────────────────────── */
function apiHeaders(extra = {}) { return {...(API_KEY ? {'X-API-Key': API_KEY} : {}), ...extra}; }
async function api(path, opts = {}) {
  const r = await fetch(`${API_BASE}${path}`, {
    ...opts,
    headers: apiHeaders({...(opts.body ? {'Content-Type': 'application/json'} : {}), ...(opts.headers || {})}),
    signal: opts.signal || AbortSignal.timeout(20000),
  });
  if (r.status === 204) return null;
  let data = null;
  try { data = await r.json(); } catch {}
  if (!r.ok) {
    const detail = data && data.detail ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : `${r.status} ${r.statusText}`;
    const err = new Error(detail); err.status = r.status; throw err;
  }
  return data;
}
function setConnected(url, health) {
  API_BASE = url;
  const status = document.getElementById('api-status-msg');
  status.textContent = `connected — ${health.app} (${health.env}${health.version ? ' · v' + health.version : ''})`; status.className = 'ctl-status ok';
  document.getElementById('api-badge').classList.add('on');
  document.getElementById('api-state-tag').textContent = 'live';
  document.getElementById('api-url-input').value = url;
}
async function connectApi() {
  const url = document.getElementById('api-url-input').value.trim().replace(/\/$/, '');
  const keyInput = document.getElementById('api-key-input').value.trim();
  if (!url) return;
  API_KEY = keyInput;
  try { if (keyInput) sessionStorage.setItem('ei_api_key', keyInput); else sessionStorage.removeItem('ei_api_key'); } catch {}
  const status = document.getElementById('api-status-msg');
  status.textContent = 'connecting…'; status.className = 'ctl-status';
  try {
    const r = await fetch(`${url}/health`, {signal: AbortSignal.timeout(8000)});
    const d = await r.json();
    if (!d.ok) throw new Error('health check failed');
    const probe = await fetch(`${url}/signals?page_size=1`, {headers: apiHeaders(), signal: AbortSignal.timeout(8000)});
    if (probe.status === 401 && keyInput) { status.textContent = '✕ API key rejected (401)'; status.className = 'ctl-status err'; return; }
    setConnected(url, d);
    try { CONFIG = await api('/config'); } catch {}
    IS_ADMIN = probe.ok;
    document.getElementById('signals-panel').hidden = !IS_ADMIN;
    if (!IS_ADMIN) status.textContent += ' · public access (add the API key for admin tools)';
    await loadLiveData(); announce('API connected');
  } catch {
    status.textContent = '✕ could not connect — check the URL'; status.className = 'ctl-status err';
  }
}
function mapEvent(e) {
  const v = e.venue || {};
  const area = v.neighbourhood || (v.name && v.name !== v.city && v.name !== 'Pinned location' ? v.name : null);
  return {
    ...e,
    city: v.city || v.name || '—',
    place_label: area && v.city && area !== v.city ? `${area}, ${v.city}` : (v.city || v.name || '—'),
    lat: v.latitude, lng: v.longitude, geo_accuracy: v.geo_accuracy,
    organizer: e.organizer?.name, price: 'free', token_gated: false,
    evidence: e.evidence_count || 0,
  };
}
function setSort(mode) {
  SORT_MODE = mode; lsSet('ei_sort', mode);
  for (const m of ['time', 'distance']) {
    const b = document.getElementById('sort-' + m);
    if (b) { b.classList.toggle('on', m === mode); b.setAttribute('aria-pressed', String(m === mode)); }
  }
  renderFeed(currentEvents);
}
function setStat(id, value) { const el = document.getElementById(id); if (el) el.textContent = value; }
function setRadius(v) {
  RADIUS_KM = Number(v) || 25;
  lsSet('ei_radius', String(RADIUS_KM));
  renderRadius();
  updateMapsForLocation(true);
  if (API_BASE && LOCATION) loadLiveData(); else renderFeed(currentEvents.length ? currentEvents : MOCK_EVENTS);
}
function renderCoverage(cov, state) {
  const line = document.getElementById('coverage-line'), txt = document.getElementById('coverage-text');
  if (!LOCATION) { line.hidden = true; return; }
  line.hidden = false;
  line.className = 'status-line';
  const place = esc(LOCATION.label || LOCATION.city || 'this area');
  const acc = (LOCATION.source === 'gps' || LOCATION.source === 'saved') && LOCATION.accuracy ? ` ${fmtAccuracy(LOCATION.accuracy)}` : '';
  if (!API_BASE) { txt.innerHTML = `demo data · connect the API for live events around ${place}`; return; }
  if (state === 'scanning') { line.classList.add('scanning'); txt.innerHTML = `scanning ${RADIUS_KM} km around ${place}${acc}…`; return; }
  if (!cov) { txt.textContent = ''; return; }
  const rescan = `<button onclick="loadLiveData(true)">rescan</button>`;
  if (cov.reason === 'error') { line.classList.add('warn'); txt.innerHTML = `couldn’t reach the server. ${rescan}`; return; }
  if (cov.reason === 'no_sources') { line.classList.add('warn'); txt.innerHTML = `${place}${acc} · no live sources configured — showing saved events. ${rescan}`; return; }
  if (cov.reason === 'rate_limited') { txt.innerHTML = `${place}${acc} · recent results (scan limit reached, try again later)`; return; }
  const when = cov.last_scanned_at ? timeAgo(cov.last_scanned_at) : 'just now';
  const added = cov.scanned && cov.events_created ? ` · ${cov.events_created} new` : '';
  txt.innerHTML = `${place}${acc} · updated ${esc(when)}${added} · ${rescan}`;
}
async function loadLiveData(refresh = false) {
  if (!API_BASE) return;
  const havePoint = LOCATION && Number.isFinite(LOCATION.lat);
  const eventsUrl = havePoint
    ? `/discover?lat=${LOCATION.lat.toFixed(6)}&lng=${LOCATION.lng.toFixed(6)}&radius_km=${RADIUS_KM}&sort=${SORT_MODE}&page_size=150${refresh ? '&refresh=true' : ''}`
    : `/events?page_size=100&sort=start_time${activeCity && activeCity !== 'Global' ? `&city=${encodeURIComponent(activeCity)}` : ''}`;
  if (havePoint) { SCANNING = true; renderCoverage(null, 'scanning'); if (!currentEvents.length || refresh) { currentEvents = []; renderFeed([]); } }
  let ev;
  try { ev = await api(eventsUrl, {signal: AbortSignal.timeout(45000)}); }
  catch (e) { console.warn('Event load failed', e); SCANNING = false; if (havePoint) renderCoverage({reason: 'error'}); renderFeed(currentEvents); return; }
  SCANNING = false;
  if (havePoint && ev) {
    COVERAGE = ev.coverage;
    if (ev.center) {
      LOCATION.city = ev.center.city || LOCATION.city;
      LOCATION.country = ev.center.country || LOCATION.country;
      if (['gps', 'map', 'saved'].includes(LOCATION.source) || !LOCATION.label || LOCATION.label === 'locating…')
        LOCATION.label = ev.center.label || ev.center.city || LOCATION.label;
      activeCity = LOCATION.city || activeCity;
      lsSet('ei_loc', JSON.stringify({...LOCATION, ts: Date.now()}));
      updateLocationUI();
    }
    renderCoverage(ev.coverage);
  }
  if (ev && ev.items) renderFeed(ev.items.map(mapEvent));
  const [stats, sig, review] = await Promise.allSettled([
    api('/stats'),
    IS_ADMIN ? api('/signals?page_size=8') : Promise.reject('not admin'),
    IS_ADMIN ? api('/review-queue?status=pending&page_size=25') : Promise.reject('not admin'),
  ]);
  if (stats.status === 'fulfilled') {
    const st = stats.value; STATS = st;
    if (st.categories && st.categories.length) { CATEGORIES = st.categories.filter(c => c !== 'general'); renderFilters(); }
    setStat('s-events-e', st.events_total); setStat('s-signals-e', st.signals_total);
    setStat('s-confirmed-e', st.events_confirmed); setStat('s-review-e', st.review_pending);
    setStat('s-unprocessed-e', st.signals_unprocessed); setStat('s-areas-e', st.areas_covered);
    renderSourcesStatus(st); renderCitiesSidebar();
  }
  if (sig.status === 'fulfilled' && sig.value.items) renderSignals(sig.value.items.map(s => ({...s, ingested_at: timeAgo(s.ingested_at)})));
  if (review.status === 'fulfilled' && review.value.items) { REVIEW_ITEMS = review.value.items; renderReviewQueue(review.value.total); }
}
function renderSourcesStatus(stats) {
  const el = document.getElementById('sources-status'); if (!el || !stats.geo_sources) return;
  el.innerHTML = Object.entries(stats.geo_sources).map(([name, ok]) =>
    `<span class="badge ${ok ? 'mint' : 'ghost'}" title="${ok ? 'configured' : 'no API key — set it in the API environment'}">${esc(name)} ${ok ? '✓' : '✕'}</span>`).join('');
}
async function runIngest() {
  const btn = document.getElementById('ingest-btn'), result = document.getElementById('ingest-result');
  const source = document.getElementById('ingest-source').value;
  const query = document.getElementById('ingest-query').value.trim();
  const urlsRaw = (document.getElementById('ingest-urls').value || '').trim();
  const urls = urlsRaw ? urlsRaw.split(/[\n,]+/).map(u => u.trim()).filter(Boolean) : undefined;
  if (!API_BASE) { result.textContent = '⚠ connect the API first'; result.className = 'ctl-status err'; return; }
  btn.disabled = true; btn.textContent = 'ingesting…';
  result.textContent = '—'; result.className = 'ctl-status';
  try {
    const body = {source, query: query || undefined, city: activeCity !== 'Global' ? activeCity : undefined, urls};
    if (LOCATION && Number.isFinite(LOCATION.lat)) { body.lat = LOCATION.lat; body.lng = LOCATION.lng; body.radius_km = RADIUS_KM; }
    const d = await api('/signals/ingest', {method: 'POST', body: JSON.stringify(body)});
    const c = await api('/signals/cluster', {method: 'POST'});
    result.textContent = `✓ run #${d.run_id} — ${d.count} signal${d.count !== 1 ? 's' : ''} · ${c.created_event_ids.length} new · ${c.linked_signal_ids.length} linked · ${c.queued_review_ids.length} to review`;
    result.className = 'ctl-status ok';
    await loadLiveData();
  } catch (e) {
    result.textContent = `✕ ingest failed — ${e.message}`; result.className = 'ctl-status err';
  } finally { btn.disabled = false; btn.textContent = 'ingest signals'; }
}
function renderReviewQueue(total) {
  const list = document.getElementById('review-list');
  document.getElementById('review-count').textContent = total ?? REVIEW_ITEMS.length;
  if (!REVIEW_ITEMS.length) { list.innerHTML = '<div class="rq muted">Nothing to review — every signal was linked or seeded automatically.</div>'; return; }
  list.innerHTML = REVIEW_ITEMS.map(it => {
    const sig = it.signal || {}, ev = it.candidate_event || {};
    const sd = fmtDate(sig.detected_start_time), ed = fmtDate(ev.start_time);
    return `<div class="rq" id="rq-${it.id}" role="group" aria-label="Review item ${it.id}">
      <div class="rq-head"><span class="badge">${Math.round((it.score || 0) * 100)}% match</span><span>#${it.id} · ${esc(timeAgo(it.created_at))}</span></div>
      <div class="rq-pair">
        <div class="rq-side"><div class="kicker">signal · ${esc(sig.source_type || '?')}</div><b>${esc(sig.title || '(untitled)')}</b>
          <small>${esc(sig.location_text || '—')} · ${esc(sd.full)}</small>${sig.url ? `<a class="link-btn" style="min-height:28px;font-size:12px" href="${esc(sig.url)}" target="_blank" rel="noopener noreferrer">source ↗</a>` : ''}</div>
        <div class="rq-x" aria-hidden="true">⇄</div>
        <div class="rq-side"><div class="kicker">candidate event</div><b>${esc(ev.title || '(deleted)')}</b>
          <small>${esc(ev.venue?.city || ev.venue?.name || '—')} · ${esc(ed.full)} · ${ev.evidence_count ?? 0} signal${ev.evidence_count === 1 ? '' : 's'}</small></div>
      </div>
      <div class="rq-acts">
        <button class="btn sm mint" onclick="resolveReview(${it.id},'approve_link')" ${ev.id ? '' : 'disabled'}>✓ same event</button>
        <button class="btn sm" onclick="resolveReview(${it.id},'reject')">different event</button>
        <button class="btn sm" onclick="resolveReview(${it.id},'discard')">not an event</button>
      </div>
    </div>`;
  }).join('');
}
async function resolveReview(id, action) {
  const el = document.getElementById(`rq-${id}`);
  if (el) el.classList.add('busy');
  try {
    const r = await api(`/review-queue/${id}/resolve`, {method: 'POST', body: JSON.stringify({action})});
    REVIEW_ITEMS = REVIEW_ITEMS.filter(i => i.id !== id);
    renderReviewQueue();
    announce(`review ${r.status}`);
    await loadLiveData();
  } catch (e) {
    if (el) el.classList.remove('busy');
    alert(`Could not resolve: ${e.message}`);
  }
}
async function refreshReview() { await loadLiveData(); announce('review queue refreshed'); }

/* ── boot ─────────────────────────────────────────────────────────────── */
renderSplashTicker();
renderCityChips();
renderFilters();
renderFeed(MOCK_EVENTS);
renderSignals(MOCK_SIGNALS);
renderCitiesSidebar();

(async function boot() {
  for (const m of ['time', 'distance']) { const b = document.getElementById('sort-' + m); if (b) { b.classList.toggle('on', m === SORT_MODE); b.setAttribute('aria-pressed', String(m === SORT_MODE)); } }
  try { API_KEY = sessionStorage.getItem('ei_api_key') || ''; if (API_KEY) document.getElementById('api-key-input').value = API_KEY; } catch {}
  if (window.location.protocol === 'file:') return;
  const origin = window.location.origin;
  try {
    const r = await fetch(origin + '/health', {signal: AbortSignal.timeout(6000)});
    const d = await r.json();
    if (!d.ok) return;
    setConnected(origin, d);
  } catch { return; }
  try { CONFIG = await api('/config'); } catch { CONFIG = null; }
  if (CONFIG && !lsGet('ei_radius')) RADIUS_KM = CONFIG.discover_default_radius_km || 25;
  try { const pr = await fetch(origin + '/signals?page_size=1', {headers: apiHeaders(), signal: AbortSignal.timeout(6000)}); IS_ADMIN = pr.ok; } catch {}
  document.getElementById('signals-panel').hidden = !IS_ADMIN;
  currentEvents = []; renderFeed([]);

  const params = new URLSearchParams(location.search);
  const deepEvent = parseInt(params.get('event'), 10);
  let saved = null;
  try { saved = JSON.parse(lsGet('ei_loc') || 'null'); } catch {}
  const fresh = saved && Number.isFinite(saved.lat) && (Date.now() - (saved.ts || 0) < 6 * 3600 * 1000);

  if (fresh) await setLocation({...saved, source: saved.source === 'gps' ? 'saved' : saved.source}, {stay: !!deepEvent});
  if (deepEvent) {
    try {
      const m = mapEvent(await api(`/events/${deepEvent}`));
      if (!currentEvents.find(x => x.id === m.id)) currentEvents = [withDistances([m])[0], ...currentEvents];
      openDetail(m.id);
    } catch { history.replaceState(null, '', location.pathname); }
  }
  if (!fresh && !deepEvent && navigator.permissions && navigator.permissions.query) {
    try { const st = await navigator.permissions.query({name: 'geolocation'}); if (st.state === 'granted') requestLocation(); } catch {}
  } else if (fresh && saved.source === 'gps') {
    getBestPosition({timeout: 8000, good: 100}).then(pos => {
      const c = pos.coords;
      if (haversine(c.latitude, c.longitude, LOCATION.lat, LOCATION.lng) > 0.5)
        setLocation({lat: c.latitude, lng: c.longitude, accuracy: Math.round(c.accuracy), source: 'gps'}, {stay: true});
      else { LOCATION.accuracy = Math.round(c.accuracy); LOCATION.source = 'gps'; updateLocationUI(); updateMapsForLocation(false); }
    }).catch(() => {});
  }
})();
