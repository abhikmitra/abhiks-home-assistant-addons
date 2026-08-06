/* Portal ChatGPT lane — in-page kiosk layer.
 *
 * Injected into chatgpt.com by portal_chatgpt_bridge.py, both via
 * Page.addScriptToEvaluateOnNewDocument (so it runs before ChatGPT's own
 * scripts and can wrap browser APIs) and via Runtime.evaluate for pages that
 * are already open.
 *
 * It owns four things the Portal needs and chatgpt.com does not provide:
 *
 *  1. ORB-ONLY VIEW. The Portal is a wall display, not a laptop: the sidebar,
 *     chat list, top bar and composer are noise. Rather than hiding a list of
 *     class names that OpenAI will rename next month, we walk up from the orb
 *     <canvas> to <body> and hide every SIBLING along that path. Whatever the
 *     markup around the orb becomes, the orb survives and the rest goes.
 *
 *  2. VOICE ACTIVITY. Nothing in the DOM reliably says "someone is talking",
 *     so we tap the audio itself: getUserMedia gives us the microphone track,
 *     RTCPeerConnection.ontrack gives us ChatGPT's reply audio. Both feed
 *     AnalyserNodes, and any level above the floor stamps lastActivityTs.
 *     That is what the idle timeout measures — real speech, not wall-clock.
 *
 *  3. LOADING STATE. Waking the browser, signing the page in and entering
 *     voice mode takes ~15 s. A blank white page for 15 s reads as broken, so
 *     an overlay covers the page from first paint and narrates each phase.
 *
 *  4. THE EXIT AFFORDANCE. A large "End" button, and a console PORTAL_EXIT
 *     signal the bridge listens for.
 */
(function () {
  if (window.__portalKiosk) return 'already-installed';

  var K = {
    phase: 'booting',
    lastActivityTs: Date.now(),
    micLevel: 0,
    spkLevel: 0,
    everHeardSpeaker: false,
    everHeardMic: false,
    kioskApplied: false,
  };
  window.__portalKiosk = K;

  // Two things make this hard. The room throws loud short bursts (measured with
  // nobody speaking: median 0.0027, p90 0.056, peaks 0.13), so a single loud
  // sample means nothing. And ChatGPT applies echo cancellation, noise
  // suppression and AGC to the track we tap, so the SAME voice measured 0.067
  // one hour and 0.005 the next — an absolute threshold cannot survive that.
  // Speech is therefore: sustained for ~1s, AND clearly above this room's own
  // rolling quiet baseline.
  var ABS_FLOOR = 0.004;   // never call anything below this speech
  var REL_MULT = 3.0;      // ...or anything under 3x the room's quiet baseline
  var RUN = 4;             // sustained for 4 x 250ms = 1s
  var HIDDEN_ATTR = 'data-portal-hidden';
  var ORB_PX = 320;           // orb diameter on the Portal's 1280x800 panel

  /* ---------------------------------------------------------------- audio */
  var AC = null;
  function ctx() {
    if (!AC) { try { AC = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { return null; } }
    if (AC.state === 'suspended') { try { AC.resume(); } catch (e) {} }
    return AC;
  }

  function watch(stream, which) {
    var c = ctx(); if (!c || !stream || !stream.getAudioTracks || !stream.getAudioTracks().length) return;
    var src, an;
    try {
      src = c.createMediaStreamSource(stream);
      an = c.createAnalyser(); an.fftSize = 1024;
      src.connect(an);
    } catch (e) { return; }
    var buf = new Float32Array(an.fftSize);
    var run = 0;
    var baseline = 0.002;   // rolling estimate of "this room, right now, quiet"
    setInterval(function () {
      try {
        an.getFloatTimeDomainData(buf);
        var s = 0;
        for (var i = 0; i < buf.length; i++) s += buf[i] * buf[i];
        var rms = Math.sqrt(s / buf.length);
        if (which === 'mic') K.micLevel = rms; else K.spkLevel = rms;

        // Absolute thresholds do not survive here. ChatGPT asks for echo
        // cancellation, noise suppression and AGC, so the level of the SAME
        // voice through the SAME mic measured 0.067 one hour and 0.005 the
        // next. Speech is therefore detected RELATIVE to a rolling baseline:
        // clearly above the room's own quiet level, held for ~1s.
        var speaking = rms > Math.max(ABS_FLOOR, baseline * REL_MULT);
        if (speaking) {
          run++;
          if (run >= RUN) {
            K.lastActivityTs = Date.now();
            if (which === 'mic') K.everHeardMic = true; else K.everHeardSpeaker = true;
          }
        } else {
          run = 0;
          // Only quiet samples update the baseline, so a long answer cannot
          // drag the floor up and deafen the detector.
          baseline = baseline * 0.95 + rms * 0.05;
        }
        if (which === 'mic') K.micBaseline = baseline;
      } catch (e) {}
    }, 250);
  }

  // Microphone: what the household says.
  if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
    var origGUM = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = function (c) {
      return origGUM(c).then(function (s) {
        try { watch(s, 'mic'); K._micStream = s; } catch (e) {}
        return s;
      });
    };
  }

  // Remote track: what ChatGPT says back. Advanced voice runs over WebRTC, so
  // the reply audio never touches an <audio src>; ontrack is the only hook.
  if (window.RTCPeerConnection) {
    var OrigPC = window.RTCPeerConnection;
    function PatchedPC() {
      var pc = new OrigPC(arguments[0], arguments[1]);
      pc.addEventListener('track', function (ev) {
        try { if (ev.streams && ev.streams[0]) watch(ev.streams[0], 'spk'); } catch (e) {}
      });
      return pc;
    }
    PatchedPC.prototype = OrigPC.prototype;
    ['generateCertificate'].forEach(function (k) { if (OrigPC[k]) PatchedPC[k] = OrigPC[k].bind(OrigPC); });
    window.RTCPeerConnection = PatchedPC;
  }

  // New UI (2026-08-03): reply audio can be routed through an <audio>/<video>
  // element whose srcObject comes from a peer connection this patch never saw
  // (e.g. created in a worker) — everHeardSpeaker stayed false for the whole
  // call while the OS-level output players were demonstrably running. Tap the
  // srcObject setter so any MediaStream attached to a media element is watched
  // as speaker output too.
  try {
    var mdesc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'srcObject');
    if (mdesc && mdesc.set) {
      Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', {
        configurable: true,
        get: mdesc.get,
        set: function (s) {
          try {
            if (s && s.getAudioTracks && s.getAudioTracks().length) watch(s, 'spk');
          } catch (e) {}
          return mdesc.set.call(this, s);
        },
      });
    }
  } catch (e) {}

  /* -------------------------------------------------------------- overlay */
  var OV = 'portal-kiosk-overlay';
  function overlay(text, sub) {
    var el = document.getElementById(OV);
    if (!el) {
      el = document.createElement('div');
      el.id = OV;
      el.innerHTML =
        '<div id="portal-kiosk-spinner"></div>' +
        '<div id="portal-kiosk-text"></div>' +
        '<div id="portal-kiosk-sub"></div>';
      var s = document.createElement('style');
      s.textContent =
        '#' + OV + '{position:fixed;inset:0;z-index:2147483646;background:#0d0d0f;' +
        'display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;' +
        'font-family:-apple-system,system-ui,sans-serif;color:#e8e9ec;transition:opacity .35s}' +
        '#portal-kiosk-spinner{width:54px;height:54px;border-radius:50%;' +
        'border:3px solid rgba(255,255,255,.14);border-top-color:#8fa2ff;animation:pk-spin 1s linear infinite}' +
        '#portal-kiosk-text{font-size:23px;font-weight:600;letter-spacing:.2px}' +
        '#portal-kiosk-sub{font-size:15px;color:#8b8f99}' +
        '@keyframes pk-spin{to{transform:rotate(360deg)}}';
      (document.head || document.documentElement).appendChild(s);
      (document.body || document.documentElement).appendChild(el);
    }
    var t = document.getElementById('portal-kiosk-text');
    var u = document.getElementById('portal-kiosk-sub');
    if (t) t.textContent = text || '';
    if (u) u.textContent = sub || '';
    el.style.opacity = '1';
    el.style.display = 'flex';
    return 'overlay:' + text;
  }
  function overlayDone() {
    var el = document.getElementById(OV);
    if (!el) return 'no-overlay';
    el.style.opacity = '0';
    setTimeout(function () { if (el) el.style.display = 'none'; }, 400);
    return 'overlay-hidden';
  }

  /* ----------------------------------------------------------- exit chip */
  var CHIP = 'portal-exit-chip';
  function chip() {
    if (document.getElementById(CHIP)) return true;
    var b = document.createElement('button');
    b.id = CHIP;
    b.setAttribute('aria-label', 'End and return to Portal');
    b.textContent = '✕  End';
    var st = {
      position: 'fixed', left: '50%', bottom: '34px', transform: 'translateX(-50%)',
      zIndex: '2147483647', display: 'flex', alignItems: 'center', justifyContent: 'center',
      minHeight: '62px', minWidth: '190px', padding: '14px 34px',
      fontSize: '20px', fontWeight: '700', letterSpacing: '.3px',
      fontFamily: '-apple-system,system-ui,sans-serif',
      color: '#fff', background: 'rgba(20,20,24,.92)', border: '2px solid rgba(255,255,255,.85)',
      borderRadius: '34px', boxShadow: '0 6px 26px rgba(0,0,0,.65)', cursor: 'pointer',
      pointerEvents: 'auto', userSelect: 'none', WebkitUserSelect: 'none', touchAction: 'manipulation'
    };
    for (var k in st) b.style[k] = st[k];
    function fire(e) { e.preventDefault(); e.stopPropagation(); try{var ts=String(Date.now()); window.__portalExitRequested=ts; try{window.sessionStorage.setItem('__portalExitRequested', ts);}catch(_){}}catch(_){} console.log('PORTAL_EXIT'); }
    b.addEventListener('click', fire, { capture: true });
    b.addEventListener('touchend', fire, { capture: true, passive: false });
    (document.body || document.documentElement).appendChild(b);
    return true;
  }

  /* ------------------------------------------------------------ orb-only */
  function orbEl() {
    // The orb canvas animates in after focus mode, starting near zero size, so
    // a generous floor here is the difference between "no-orb" and a working
    // kiosk. 40px is still far above any icon-sized canvas on the page.
    var cs = document.querySelectorAll('canvas');
    for (var i = 0; i < cs.length; i++) {
      var r = cs[i].getBoundingClientRect();
      if (r.width > 40 && r.height > 40) return cs[i];
    }
    return null;
  }

  function applyKiosk() {
    var orb = orbEl();
    if (!orb) return 'no-orb';
    // Hide every sibling on the orb's ancestor path. Survives class renames.
    var node = orb, guard = 0;
    while (node && node !== document.body && guard++ < 40) {
      var p = node.parentElement; if (!p) break;
      for (var i = 0; i < p.children.length; i++) {
        var sib = p.children[i];
        if (sib === node) continue;
        if (sib.id === CHIP || sib.id === OV) continue;
        if (sib.tagName === 'SCRIPT' || sib.tagName === 'STYLE') continue;
        if (!sib.hasAttribute(HIDDEN_ATTR)) {
          sib.setAttribute(HIDDEN_ATTR, sib.style.display || '');
          sib.style.display = 'none';
        }
      }
      node = p;
    }
    // Body-level leftovers (top bar, toasts, sidebar rail live outside the path).
    ['#stage-slideover-sidebar', '#stage-sidebar-tiny-bar', '.composer-parent'].forEach(function (sel) {
      document.querySelectorAll(sel).forEach(function (e) {
        if (!e.hasAttribute(HIDDEN_ATTR)) { e.setAttribute(HIDDEN_ATTR, e.style.display || ''); e.style.display = 'none'; }
      });
    });
    // Hiding the siblings also removes whatever was centring the orb — the
    // layout it relied on (grid columns, flex rows) is now half gone and it
    // collapses into the top-left corner. Re-establish centring explicitly on
    // its own ancestor chain, which is the only part of the tree left.
    node = orb.parentElement; guard = 0;
    while (node && node !== document.body && guard++ < 40) {
      node.style.setProperty('display', 'flex', 'important');
      node.style.setProperty('align-items', 'center', 'important');
      node.style.setProperty('justify-content', 'center', 'important');
      node.style.setProperty('width', '100%', 'important');
      node.style.setProperty('height', '100%', 'important');
      node.style.setProperty('max-width', 'none', 'important');
      node.style.setProperty('max-height', 'none', 'important');
      node.style.setProperty('padding', '0', 'important');
      node.style.setProperty('margin', '0', 'important');
      node.style.setProperty('inset', 'auto', 'important');
      node.style.setProperty('transform', 'none', 'important');
      node = node.parentElement;
    }
    // Forcing 100% on the chain lets the canvas stretch to the viewport, which
    // reads as a wall of blue rather than an orb. Pin it to a fixed size.
    orb.style.setProperty('width', ORB_PX + 'px', 'important');
    orb.style.setProperty('height', ORB_PX + 'px', 'important');
    orb.style.setProperty('max-width', ORB_PX + 'px', 'important');
    orb.style.setProperty('max-height', ORB_PX + 'px', 'important');
    orb.style.setProperty('flex', '0 0 auto', 'important');
    document.body.style.setProperty('display', 'flex', 'important');
    document.body.style.setProperty('align-items', 'center', 'important');
    document.body.style.setProperty('justify-content', 'center', 'important');
    document.body.style.setProperty('height', '100vh', 'important');
    document.body.style.setProperty('width', '100vw', 'important');
    document.body.style.setProperty('overflow', 'hidden', 'important');
    document.documentElement.style.background = '#0d0d0f';
    document.body.style.background = '#0d0d0f';
    K.kioskApplied = true;
    chip();
    return 'kiosk-applied';
  }

  function releaseKiosk() {
    document.querySelectorAll('[' + HIDDEN_ATTR + ']').forEach(function (e) {
      e.style.display = e.getAttribute(HIDDEN_ATTR) || '';
      e.removeAttribute(HIDDEN_ATTR);
    });
    K.kioskApplied = false;
    return 'kiosk-released';
  }

  /* ------------------------------------------------- ChatGPT UI controls */
  function byLabel(re) {
    var bs = document.querySelectorAll('button');
    for (var i = 0; i < bs.length; i++) {
      var a = bs[i].getAttribute('aria-label') || '';
      if (re.test(a)) return bs[i];
    }
    return null;
  }

  // The silence clock must start when the conversation is actually usable, not
  // when the page loaded. Opening the lane takes ~35s, so without this the user
  // gets barely 25s of the 60s budget and the lane closes mid-sentence.
  K.markLive = function () {
    K.phase = 'live';
    K.lastActivityTs = Date.now();
    return 'live';
  };

  K.overlay = overlay;
  K.overlayDone = overlayDone;
  K.chip = chip;
  K.apply = applyKiosk;
  K.release = releaseKiosk;
  K.byLabel = byLabel;
  K.idleMs = function () { return Date.now() - K.lastActivityTs; };
  K.snapshot = function () {
    return {
      phase: K.phase,
      idleMs: K.idleMs(),
      micLevel: +K.micLevel.toFixed(4),
      spkLevel: +K.spkLevel.toFixed(4),
      everHeardMic: K.everHeardMic,
      everHeardSpeaker: K.everHeardSpeaker,
      kioskApplied: K.kioskApplied,
      // Two ChatGPT web UIs coexist (2026-08-03 update): the old one signals a
      // live call with an "End voice" button and has an explicit mic toggle;
      // the new one (entered via chatgpt.com/voice) signals it with the
      // "Voice mode selector" control and starts the mic hot with no toggle —
      // there, a live captured mic track IS the mic-on signal.
      voiceLive: !!(byLabel(/end voice/i) || byLabel(/voice mode selector/i)),
      micOn: !!byLabel(/turn off microphone/i) ||
        !!(K._micStream && K._micStream.getAudioTracks().some(function (t) {
          return t.readyState === 'live' && t.enabled;
        })),
      focusMode: !!byLabel(/exit focus mode/i),
      orbVisible: !!orbEl(),
      hasChip: !!document.getElementById(CHIP),
    };
  };

  // The exit affordance must exist from the moment the layer installs, not
  // only once the orb-only view succeeds — otherwise a lane that fails halfway
  // leaves a fullscreen browser with no way out.
  try { chip(); } catch (e) {}

  // Keep the kiosk and chip nailed down against SPA re-renders.
  setInterval(function () {
    try {
      chip();
      if (K.kioskApplied) applyKiosk();
    } catch (e) {}
  }, 2000);

  return 'installed';
})();
