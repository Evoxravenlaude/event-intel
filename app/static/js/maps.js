/* ═══════════════════════════════════════════════════════════════════════
   MAPS — one small interface over three engines.

   maplibre  (default)  MapLibre GL + OpenFreeMap vector tiles. Free, keyless,
                        commercial use allowed. Restyled to the app's palette.
   apple                Apple MapKit JS. Needs APPLE_MAPKIT_* on the server,
                        which signs short-lived tokens at /mapkit/token.
   google               Google Maps JS. Needs GOOGLE_MAPS_BROWSER_KEY.

   The server picks one in /config. If it can't load (blocked script, key
   rejected, token refused), every map on the page rebuilds on MapLibre; if
   MapLibre can't start either (no WebGL), the map area says so instead of
   throwing. Pins, clusters, the radius ring and "you are here" are plain DOM
   elements styled in app.css, so they look the same on every engine.

   Zoom levels everywhere in this file are in 256-px-tile units (Google's).
   MapLibre uses 512-px tiles, so its adapter converts at the edges.
═══════════════════════════════════════════════════════════════════════ */

const MAPS = {provider: null, loading: null, instances: [], failed: {}};
const MAPLIBRE_VERSION = '4.7.1';
const MAPLIBRE_JS = `https://cdn.jsdelivr.net/npm/maplibre-gl@${MAPLIBRE_VERSION}/dist/maplibre-gl.js`;
const MAPLIBRE_CSS = `https://cdn.jsdelivr.net/npm/maplibre-gl@${MAPLIBRE_VERSION}/dist/maplibre-gl.css`;
const MAPKIT_JS = 'https://cdn.apple-mapkit.com/mk/5.x.x/mapkit.js';
const DEFAULT_STYLE = 'https://tiles.openfreemap.org/styles/positron';
const DEFAULT_STYLE_DARK = 'https://tiles.openfreemap.org/styles/dark';
const PIN_FAN_ZOOM = 16;
const DARK_MQ = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : {matches: false};

function loadScript(src, attrs = {}) {
  return new Promise((res, rej) => {
    const el = document.createElement('script'); el.src = src; el.async = true;
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    el.onload = res; el.onerror = () => rej(new Error('failed to load ' + src));
    document.head.appendChild(el);
  });
}
function loadCss(href) {
  if (document.querySelector(`link[href="${href}"]`)) return;
  const l = document.createElement('link'); l.rel = 'stylesheet'; l.href = href; document.head.appendChild(l);
}
const withTimeout = (p, ms, what) => Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error(what + ' timeout')), ms))]);

/* ── geometry ─────────────────────────────────────────────────────────── */
function haversine(lat1, lng1, lat2, lng2) {
  const R = 6371, toR = Math.PI / 180;
  const dLat = (lat2 - lat1) * toR, dLng = (lng2 - lng1) * toR;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * toR) * Math.cos(lat2 * toR) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}
function circleRing(lat, lng, km, steps = 72) {
  const R = 6371, d = km / R, la = lat * Math.PI / 180, lo = lng * Math.PI / 180, out = [];
  for (let i = 0; i <= steps; i++) {
    const b = 2 * Math.PI * i / steps;
    const la2 = Math.asin(Math.sin(la) * Math.cos(d) + Math.cos(la) * Math.sin(d) * Math.cos(b));
    const lo2 = lo + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(la), Math.cos(d) - Math.sin(la) * Math.sin(la2));
    out.push([lo2 * 180 / Math.PI, la2 * 180 / Math.PI]);
  }
  return out;
}
function circleBounds(lat, lng, km) {
  const dLat = km / 111.32, dLng = km / (111.32 * Math.max(Math.cos(lat * Math.PI / 180), 0.01));
  return {s: lat - dLat, n: lat + dLat, w: lng - dLng, e: lng + dLng};
}
function mercProject(lat, lng, z) {
  const s = 256 * 2 ** z, sin = Math.min(Math.max(Math.sin(lat * Math.PI / 180), -0.9999), 0.9999);
  return {x: (lng + 180) / 360 * s, y: (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * s};
}
function mercUnproject(x, y, z) {
  const s = 256 * 2 ** z, n = Math.PI - 2 * Math.PI * y / s;
  return {lat: 180 / Math.PI * Math.atan(Math.sinh(n)), lng: x / s * 360 - 180};
}
/** Greedy grid clustering of projected points; radius in screen px. */
function clusterPoints(pts, radius) {
  const grid = new Map(), groups = [];
  const key = (cx, cy) => cx + ':' + cy;
  for (const p of pts) {
    const cx = Math.floor(p.x / radius), cy = Math.floor(p.y / radius);
    let home = null;
    for (let dx = -1; dx <= 1 && !home; dx++) for (let dy = -1; dy <= 1 && !home; dy++) {
      for (const g of grid.get(key(cx + dx, cy + dy)) || []) {
        if (Math.hypot(g[0].x - p.x, g[0].y - p.y) <= radius) { home = g; break; }
      }
    }
    if (home) home.push(p);
    else { const g = [p]; groups.push(g); const k = key(cx, cy); (grid.get(k) || grid.set(k, []).get(k)).push(g); }
  }
  return groups;
}

/* ── marker elements (shared by all engines) ─────────────────────────── */
function pinElement(ev, {selected = false} = {}) {
  const el = document.createElement('button');
  const approx = ev.geo_accuracy === 'city' || ev.geo_accuracy === 'region';
  el.type = 'button';
  el.className = `ei-pin${approx ? ' approx' : ''}${selected ? ' sel' : ''}`;
  el.dataset.cat = ev.category || 'general';
  el.setAttribute('aria-label', `${ev.title}${ev.distance_km != null ? ', ' + fmtDistance(ev.distance_km) : ''}`);
  el.title = ev.title;
  if (selected) { const t = document.createElement('span'); t.className = 'ei-pin-label'; t.textContent = ev.title; el.appendChild(t); }
  return el;
}
function clusterElement(n) {
  const el = document.createElement('button');
  const size = n < 10 ? 36 : n < 100 ? 42 : 50;
  el.type = 'button'; el.className = 'ei-cluster';
  el.style.width = el.style.height = size + 'px';
  el.textContent = n > 999 ? '999+' : String(n);
  el.setAttribute('aria-label', `${n} events here — zoom in`);
  return el;
}
function userElement() {
  const el = document.createElement('div'); el.className = 'ei-user'; el.setAttribute('aria-hidden', 'true');
  return el;
}

/* ═══ MapLibre adapter ═══════════════════════════════════════════════════ */
class MapLibreAdapter {
  async create(el, o, hooks) {
    loadCss(MAPLIBRE_CSS);
    const dark = !!o.dark;
    const style = (dark ? CONFIG?.map_style_url_dark : CONFIG?.map_style_url) || (dark ? DEFAULT_STYLE_DARK : DEFAULT_STYLE);
    this.map = new maplibregl.Map({
      container: el, style, center: [o.lng, o.lat], zoom: Math.max(0, o.zoom - 1),
      attributionControl: false, cooperativeGestures: o.gestures === 'cooperative',
      dragRotate: false, pitchWithRotate: false, touchPitch: false, maxPitch: 0, fadeDuration: 120,
    });
    this.map.touchZoomRotate.disableRotation();
    this.map.keyboard.disableRotation && this.map.keyboard.disableRotation();
    this.map.addControl(new maplibregl.AttributionControl({compact: true}), 'bottom-left');
    if (o.controls) this.map.addControl(new maplibregl.NavigationControl({showCompass: false}), 'top-right');
    this.map.on('moveend', () => hooks.moved());
    this.map.on('click', e => { if (!e.originalEvent.target.closest('.ei-pin,.ei-cluster')) hooks.click(); });
    this.map.on('error', e => console.warn('map:', e && e.error ? e.error.message : e));
    this.ready = new Promise(res => this.map.once('load', () => { this._restyle(dark); res(); }));
    this.state = {};
  }
  /* Bring OpenFreeMap's neutral styles in line with the app: warm paper land,
     lavender water, sage parks. Only colours change; the data is untouched. */
  _restyle(dark) {
    const P = dark
      ? {bg: '#1B1A17', water: '#2B2840', green: '#1F2A20', building: '#26241F'}
      : {bg: '#EFEADF', water: '#D5CCEC', green: '#DCE7CF', building: '#E3DDD0'};
    for (const l of this.map.getStyle().layers || []) {
      const id = l.id.toLowerCase(), set = (prop, v) => { try { this.map.setPaintProperty(l.id, prop, v); } catch {} };
      if (l.type === 'background') set('background-color', P.bg);
      else if (l.type === 'fill' && /water|ocean|lake|river/.test(id)) set('fill-color', P.water);
      else if (l.type === 'fill' && /park|grass|wood|forest|landcover|green|garden|pitch/.test(id)) set('fill-color', P.green);
      else if (l.type === 'fill' && /building/.test(id)) set('fill-color', P.building);
      else if (l.type === 'line' && /waterway/.test(id)) set('line-color', P.water);
    }
  }
  async _geo(id, coords, layers) {
    await this.ready;
    const data = {type: 'Feature', geometry: {type: 'Polygon', coordinates: [coords]}, properties: {}};
    const src = this.map.getSource(id);
    if (src) { src.setData(data); return; }
    this.map.addSource(id, {type: 'geojson', data});
    for (const l of layers) this.map.addLayer({...l, source: id});
  }
  center() { const c = this.map.getCenter(); return {lat: c.lat, lng: c.lng}; }
  zoom() { return this.map.getZoom() + 1; }
  bounds() { const b = this.map.getBounds(); return {s: b.getSouth(), w: b.getWest(), n: b.getNorth(), e: b.getEast()}; }
  setView(lat, lng, z) { this.map.easeTo({center: [lng, lat], zoom: z != null ? z - 1 : this.map.getZoom(), duration: 450}); }
  fitBounds(b, {pad = 24, maxZoom = 17} = {}) {
    this.map.fitBounds([[b.w, b.s], [b.e, b.n]], {padding: pad, maxZoom: maxZoom - 1, duration: 500});
  }
  setRing(lat, lng, km) {
    this._geo('ei-ring', circleRing(lat, lng, km), [
      {id: 'ei-ring-fill', type: 'fill', paint: {'fill-color': '#FF5A1F', 'fill-opacity': 0.035}},
      {id: 'ei-ring-line', type: 'line', paint: {'line-color': DARK_MQ.matches ? '#F2EEE4' : '#141414', 'line-width': 2, 'line-dasharray': [2, 2]}},
    ]);
  }
  setUser(lat, lng, accM) {
    if (!this.userMk) this.userMk = new maplibregl.Marker({element: userElement(), anchor: 'center'}).setLngLat([lng, lat]).addTo(this.map);
    else this.userMk.setLngLat([lng, lat]);
    if (accM && accM < 20000) this._geo('ei-acc', circleRing(lat, lng, accM / 1000, 48), [
      {id: 'ei-acc-fill', type: 'fill', paint: {'fill-color': '#2F80ED', 'fill-opacity': 0.12}},
    ]);
  }
  addMarker(lat, lng, el, {z = 0, onClick} = {}) {
    if (onClick) el.addEventListener('click', e => { e.stopPropagation(); onClick(); });
    el.style.zIndex = z;
    const m = new maplibregl.Marker({element: el, anchor: 'center'}).setLngLat([lng, lat]).addTo(this.map);
    return {remove: () => m.remove()};
  }
  resize() { this.map.resize(); }
  destroy() { try { this.map.remove(); } catch {} }
}

/* ═══ Apple MapKit JS adapter ════════════════════════════════════════════ */
class AppleAdapter {
  async create(el, o, hooks) {
    this.el = el;
    this.map = new mapkit.Map(el, {
      center: new mapkit.Coordinate(o.lat, o.lng),
      showsCompass: mapkit.FeatureVisibility.Hidden, showsScale: mapkit.FeatureVisibility.Hidden,
      showsMapTypeControl: false, showsZoomControl: !!o.controls, showsUserLocationControl: false,
      isRotationEnabled: false, colorScheme: o.dark ? mapkit.Map.ColorSchemes.Dark : mapkit.Map.ColorSchemes.Light,
    });
    this.map.region = this._region(o.lat, o.lng, o.zoom);
    this.map.addEventListener('region-change-end', () => hooks.moved());
    this.map.addEventListener('single-tap', () => hooks.click());
  }
  _size() { return {w: this.el.clientWidth || 360, h: this.el.clientHeight || 360}; }
  _region(lat, lng, z) {
    const {w, h} = this._size();
    const lngDelta = Math.min(360, 360 * (w / 256) / 2 ** z);
    const latDelta = Math.min(170, lngDelta * (h / w) * Math.cos(lat * Math.PI / 180));
    return new mapkit.CoordinateRegion(new mapkit.Coordinate(lat, lng), new mapkit.CoordinateSpan(latDelta, lngDelta));
  }
  center() { const c = this.map.center; return {lat: c.latitude, lng: c.longitude}; }
  zoom() { const {w} = this._size(); return Math.log2(360 * (w / 256) / Math.max(this.map.region.span.longitudeDelta, 1e-6)); }
  bounds() { const b = this.map.region.toBoundingRegion(); return {s: b.southLatitude, w: b.westLongitude, n: b.northLatitude, e: b.eastLongitude}; }
  setView(lat, lng, z) { this.map.setRegionAnimated(this._region(lat, lng, z != null ? z : this.zoom())); }
  fitBounds(b, {pad = 24, maxZoom = 17} = {}) {
    const region = new mapkit.BoundingRegion(b.n, b.e, b.s, b.w).toCoordinateRegion();
    const {w} = this._size();
    const z = Math.log2(360 * (w / 256) / Math.max(region.span.longitudeDelta * (1 + 2 * pad / w), 1e-6));
    if (z > maxZoom) return this.setView(region.center.latitude, region.center.longitude, maxZoom);
    region.span = new mapkit.CoordinateSpan(region.span.latitudeDelta * (1 + 2 * pad / this._size().h), region.span.longitudeDelta * (1 + 2 * pad / w));
    this.map.setRegionAnimated(region);
  }
  _circle(lat, lng, m, style) { return new mapkit.CircleOverlay(new mapkit.Coordinate(lat, lng), m, {style: new mapkit.Style(style)}); }
  setRing(lat, lng, km) {
    if (this.ring) this.map.removeOverlay(this.ring);
    this.ring = this._circle(lat, lng, km * 1000, {strokeColor: DARK_MQ.matches ? '#F2EEE4' : '#141414', lineWidth: 2, lineDash: [6, 6], fillColor: '#FF5A1F', fillOpacity: 0.05});
    this.map.addOverlay(this.ring);
  }
  setUser(lat, lng, accM) {
    if (this.userAnn) this.map.removeAnnotation(this.userAnn);
    if (this.acc) { this.map.removeOverlay(this.acc); this.acc = null; }
    if (accM && accM < 20000) { this.acc = this._circle(lat, lng, accM, {strokeOpacity: 0, fillColor: '#2F80ED', fillOpacity: 0.12}); this.map.addOverlay(this.acc); }
    this.userAnn = this._annotation(lat, lng, userElement(), 18, {z: 900});
    this.map.addAnnotation(this.userAnn);
  }
  _annotation(lat, lng, el, size, {z = 0, onClick} = {}) {
    const a = new mapkit.Annotation(new mapkit.Coordinate(lat, lng), () => el, {
      anchorOffset: new DOMPoint(0, size / 2), calloutEnabled: false, animates: false,
      displayPriority: Math.min(1000, 500 + z), collisionMode: mapkit.Annotation.CollisionMode.None,
    });
    if (onClick) a.addEventListener('select', () => { onClick(); setTimeout(() => { a.selected = false; }, 0); });
    return a;
  }
  addMarker(lat, lng, el, {z = 0, onClick, size = 22} = {}) {
    const a = this._annotation(lat, lng, el, size, {z, onClick});
    this.map.addAnnotation(a);
    return {remove: () => { try { this.map.removeAnnotation(a); } catch {} }};
  }
  resize() {}
  destroy() { try { this.map.destroy(); } catch {} }
}

/* ═══ Google Maps adapter ════════════════════════════════════════════════ */
let GHtmlMarker = null;
function googleHtmlMarkerClass() {
  if (GHtmlMarker) return GHtmlMarker;
  GHtmlMarker = class extends google.maps.OverlayView {
    constructor(pos, el, z) { super(); this.pos = pos; this.box = document.createElement('div'); this.box.className = 'ei-gbox'; this.box.style.zIndex = z; this.box.appendChild(el); }
    onAdd() {
      this.getPanes().overlayMouseTarget.appendChild(this.box);
      google.maps.OverlayView.preventMapHitsAndGesturesFrom && google.maps.OverlayView.preventMapHitsAndGesturesFrom(this.box);
    }
    draw() { const p = this.getProjection().fromLatLngToDivPixel(this.pos); if (p) { this.box.style.left = p.x + 'px'; this.box.style.top = p.y + 'px'; } }
    onRemove() { this.box.remove(); }
    setPosition(pos) { this.pos = pos; this.draw && this.getProjection && this.getProjection() && this.draw(); }
  };
  return GHtmlMarker;
}
class GoogleAdapter {
  async create(el, o, hooks) {
    this.map = new google.maps.Map(el, {
      center: {lat: o.lat, lng: o.lng}, zoom: Math.round(o.zoom), disableDefaultUI: true, zoomControl: !!o.controls, clickableIcons: false,
      zoomControlOptions: {position: google.maps.ControlPosition.RIGHT_TOP},
      gestureHandling: o.gestures, fullscreenControl: false, keyboardShortcuts: true,
      styles: o.dark ? null : [
        {elementType: 'geometry', stylers: [{color: '#EFEADF'}]},
        {featureType: 'water', elementType: 'geometry', stylers: [{color: '#D5CCEC'}]},
        {featureType: 'poi.park', elementType: 'geometry', stylers: [{color: '#DCE7CF'}]},
        {featureType: 'road', elementType: 'geometry', stylers: [{color: '#FFFFFF'}]},
        {featureType: 'poi', elementType: 'labels.icon', stylers: [{visibility: 'off'}]},
      ],
    });
    this.map.addListener('idle', () => hooks.moved());
    this.map.addListener('click', () => hooks.click());
  }
  center() { const c = this.map.getCenter(); return c && {lat: c.lat(), lng: c.lng()}; }
  zoom() { return this.map.getZoom(); }
  bounds() {
    const b = this.map.getBounds(); if (!b) return null;
    const ne = b.getNorthEast(), sw = b.getSouthWest();
    return {s: sw.lat(), w: sw.lng(), n: ne.lat(), e: ne.lng()};
  }
  setView(lat, lng, z) { this.map.setCenter({lat, lng}); if (z != null) this.map.setZoom(Math.round(z)); }
  fitBounds(b, {pad = 24, maxZoom = 17} = {}) {
    this.map.fitBounds(new google.maps.LatLngBounds({lat: b.s, lng: b.w}, {lat: b.n, lng: b.e}), pad);
    google.maps.event.addListenerOnce(this.map, 'idle', () => { if (this.map.getZoom() > maxZoom) this.map.setZoom(maxZoom); });
  }
  setRing(lat, lng, km) {
    if (!this.ring) this.ring = new google.maps.Circle({map: this.map, strokeColor: DARK_MQ.matches ? '#F2EEE4' : '#141414', strokeOpacity: .9, strokeWeight: 2, fillOpacity: .05, fillColor: '#FF5A1F', clickable: false});
    this.ring.setCenter({lat, lng}); this.ring.setRadius(km * 1000);
  }
  setUser(lat, lng, accM) {
    const M = googleHtmlMarkerClass();
    if (!this.user) { this.user = new M(new google.maps.LatLng(lat, lng), userElement(), 900); this.user.setMap(this.map); }
    else this.user.setPosition(new google.maps.LatLng(lat, lng));
    if (!this.acc) this.acc = new google.maps.Circle({map: this.map, fillColor: '#2F80ED', fillOpacity: .12, strokeOpacity: 0, clickable: false});
    this.acc.setCenter({lat, lng}); this.acc.setRadius(Math.max(accM || 0, 1)); this.acc.setVisible(!!accM && accM < 20000);
  }
  addMarker(lat, lng, el, {z = 0, onClick} = {}) {
    const M = googleHtmlMarkerClass();
    if (onClick) el.addEventListener('click', e => { e.stopPropagation(); onClick(); });
    const m = new M(new google.maps.LatLng(lat, lng), el, z); m.setMap(this.map);
    return {remove: () => m.setMap(null)};
  }
  resize() {}
  destroy() {}
}

/* ═══ provider loading + fallback ═══════════════════════════════════════ */
function wantedProvider() {
  const want = (CONFIG && CONFIG.map_provider) || 'maplibre';
  if (!MAPS.failed[want]) return want;
  return MAPS.failed.maplibre ? 'none' : 'maplibre';
}
async function loadProvider(p) {
  if (p === 'maplibre') {
    if (!window.maplibregl) await withTimeout(loadScript(MAPLIBRE_JS), 12000, 'MapLibre');
    return !!window.maplibregl;
  }
  if (p === 'apple') {
    if (!window.mapkit) await withTimeout(loadScript(MAPKIT_JS, {crossorigin: 'anonymous'}), 12000, 'MapKit');
    if (!MAPS.mapkitInit) {
      MAPS.mapkitInit = true;
      mapkit.init({
        authorizationCallback: done => fetch((API_BASE || '') + '/mapkit/token').then(r => { if (!r.ok) throw new Error(r.status); return r.text(); })
          .then(done).catch(() => mapsProviderFailed('apple')),
        language: navigator.language,
      });
      mapkit.addEventListener('error', e => { console.warn('MapKit error', e && e.status); mapsProviderFailed('apple'); });
    }
    return true;
  }
  if (p === 'google') {
    const key = CONFIG && CONFIG.google_maps_browser_key;
    if (!key) return false;
    if (!(window.google && google.maps && google.maps.Map)) {
      await withTimeout(new Promise((resolve, reject) => {
        window.__eiGmapsReady = resolve;
        loadScript(`https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}&v=weekly&loading=async&callback=__eiGmapsReady`).catch(reject);
      }), 12000, 'Google Maps');
    }
    return !!(window.google && google.maps && google.maps.Map);
  }
  return false;
}
function ensureMapProvider() {
  if (MAPS.loading) return MAPS.loading;
  MAPS.loading = (async () => {
    for (let guard = 0; guard < 3; guard++) {
      const p = wantedProvider();
      if (p === 'none') break;
      try { if (await loadProvider(p)) return (MAPS.provider = p); } catch (e) { console.warn(`${p} map unavailable`, e); }
      MAPS.failed[p] = true;
    }
    return (MAPS.provider = 'none');
  })();
  return MAPS.loading;
}
/** A provider broke after load (key rejected, token refused): rebuild every map on the next one. */
async function mapsProviderFailed(p) {
  if (MAPS.failed[p]) return;
  console.warn(`${p} maps rejected — switching to OpenStreetMap`);
  MAPS.failed[p] = true; MAPS.loading = null;
  for (const m of MAPS.instances) await m.rebuild();
}
window.gm_authFailure = () => mapsProviderFailed('google');

/* ═══ EIMap — what the app talks to ══════════════════════════════════════ */
class EIMap {
  constructor(el, {gestures = 'greedy', zoom = 12, onMoveEnd = null, controls = true} = {}) {
    this.el = el; this.opts = {gestures, zoom, onMoveEnd, controls};
    this.pins = []; this.events = []; this.onPin = null; this._quietUntil = 0; this.a = null;
    MAPS.instances.push(this);
  }
  get map() { return this.a; }
  async init(lat, lng, zoom) {
    const provider = await ensureMapProvider();
    this.el.innerHTML = '';
    this.provider = provider;
    const A = {maplibre: MapLibreAdapter, apple: AppleAdapter, google: GoogleAdapter}[provider];
    if (!A) { this._fallback(); return this; }
    try {
      this.a = new A();
      await this.a.create(this.el, {lat, lng, zoom: zoom || this.opts.zoom, gestures: this.opts.gestures, controls: this.opts.controls, dark: DARK_MQ.matches},
        {moved: () => this._moved(), click: () => window.hideMapPeek && hideMapPeek()});
    } catch (e) {
      console.warn(`${provider} map failed to start`, e);
      this.a = null; MAPS.failed[provider] = true; MAPS.loading = null;
      return provider === 'none' ? this._fallback() : this.init(lat, lng, zoom);
    }
    this.el.dataset.provider = provider;
    return this;
  }
  _fallback() {
    this.a = null; this.provider = 'none';
    this.el.innerHTML = '<div class="map-fallback"><b>map unavailable here</b><span>every event is still in the list.</span></div>';
    return this;
  }
  async rebuild() {
    const c = this.center() || {lat: 0, lng: 0}, z = this.zoom() || this.opts.zoom;
    try { this.a && this.a.destroy(); } catch {}
    this.a = null; this.pins = []; this._user = null; this._ring = null;
    await this.init(c.lat, c.lng, z);
    if (this._lastRing) this.setRadius(...this._lastRing);
    if (this._lastUser) this.setUser(...this._lastUser);
    this._renderPins();
  }
  quiet(ms = 1200) { this._quietUntil = Date.now() + ms; }
  _moved() {
    if (this.events.length && this._pinZoom != null && Math.abs(this._pinZoom - this.zoom()) > 0.25) this._renderPins();
    if (Date.now() < this._quietUntil) return;
    this.opts.onMoveEnd && this.opts.onMoveEnd(this);
  }
  center() { return this.a ? this.a.center() : null; }
  zoom() { return this.a ? this.a.zoom() : 0; }
  visibleRadiusKm() {
    const b = this.a && this.a.bounds(); if (!b) return null;
    const c = this.center();
    return Math.min(haversine(c.lat, c.lng, b.n, c.lng), haversine(c.lat, c.lng, c.lat, b.e));
  }
  invalidate() { this.a && this.a.resize(); }
  fitCircle(lat, lng, km) { if (!this.a) return; this.quiet(); this.a.fitBounds(circleBounds(lat, lng, km), {pad: 20, maxZoom: 16}); }
  fitPoints(pts, maxZoom = 16) {
    if (!this.a || !pts.length) return;
    const lats = pts.map(p => p.lat), lngs = pts.map(p => p.lng);
    this.quiet();
    this.a.fitBounds({s: Math.min(...lats), w: Math.min(...lngs), n: Math.max(...lats), e: Math.max(...lngs)}, {pad: 56, maxZoom});
  }
  setView(lat, lng, zoom) { if (!this.a) return; this.quiet(); this.a.setView(lat, lng, zoom); }
  setRadius(lat, lng, km) { this._lastRing = [lat, lng, km]; this.a && this.a.setRing(lat, lng, km); }
  setUser(lat, lng, accM) { this._lastUser = [lat, lng, accM]; this.a && this.a.setUser(lat, lng, accM); }

  setPins(events, onPin, selectedId) {
    this.events = events || []; this.onPin = onPin; this.selectedId = selectedId;
    this._renderPins();
  }
  /* Pins cluster in screen space, so a busy neighbourhood reads as "12" instead of a
     pile of overlapping dots; from street level up, pins at one venue fan out in a ring. */
  _renderPins() {
    if (!this.a) return;
    this.pins.forEach(m => m.remove()); this.pins = [];
    const z = this.zoom(); this._pinZoom = z;
    const zi = Math.floor(z);
    const pts = [];
    for (const ev of this.events) {
      if (!Number.isFinite(ev.lat) || !Number.isFinite(ev.lng)) continue;
      if (typeof mapFilterActive !== 'undefined' && mapFilterActive !== 'all' && this === MAPS.main && ev.category !== mapFilterActive) continue;
      pts.push({ev, ...mercProject(ev.lat, ev.lng, z)});
    }
    const sel = pts.find(p => p.ev.id === this.selectedId);
    const groups = clusterPoints(pts.filter(p => p !== sel), zi >= PIN_FAN_ZOOM ? 6 : 40);
    for (const g of groups) {
      if (g.length === 1) { this._addPin(g[0].ev, g[0].ev.lat, g[0].ev.lng, false); continue; }
      if (zi >= PIN_FAN_ZOOM) {
        const cx = g.reduce((a, p) => a + p.x, 0) / g.length, cy = g.reduce((a, p) => a + p.y, 0) / g.length;
        const r = 16 + g.length * 2;
        g.forEach((p, i) => {
          const ang = (2 * Math.PI * i) / g.length - Math.PI / 2;
          const ll = mercUnproject(cx + r * Math.cos(ang), cy + r * Math.sin(ang), z);
          this._addPin(p.ev, ll.lat, ll.lng, false);
        });
      } else this._addCluster(g);
    }
    if (sel) this._addPin(sel.ev, sel.ev.lat, sel.ev.lng, true);
  }
  _addPin(ev, lat, lng, isSel) {
    const onPin = this.onPin;
    this.pins.push(this.a.addMarker(lat, lng, pinElement(ev, {selected: isSel}),
      {z: isSel ? 800 : 100, size: isSel ? 28 : 22, onClick: onPin ? () => onPin(ev) : null}));
  }
  _addCluster(g) {
    const n = g.length, lat = g.reduce((a, p) => a + p.ev.lat, 0) / n, lng = g.reduce((a, p) => a + p.ev.lng, 0) / n;
    const zoomIn = () => {
      const lats = g.map(p => p.ev.lat), lngs = g.map(p => p.ev.lng);
      const b = {s: Math.min(...lats), w: Math.min(...lngs), n: Math.max(...lats), e: Math.max(...lngs)};
      this.quiet();
      if (haversine(b.s, b.w, b.n, b.e) < 0.05) this.a.setView(lat, lng, PIN_FAN_ZOOM + 1);   // one venue: jump to street level so they fan out
      else this.a.fitBounds(b, {pad: 56, maxZoom: PIN_FAN_ZOOM + 1});
    };
    this.pins.push(this.a.addMarker(lat, lng, clusterElement(n), {z: 200 + Math.min(n, 500), size: 42, onClick: zoomIn}));
  }
}
