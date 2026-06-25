// Live signal panel: 4 hotspot channels (one per lead) with Raw / PSD /
// Spectrogram views, plus stim-amplitude and STN-beta readouts. Fed by the
// backend per-tick payload. Self-contained (no plotting library): a small
// radix-2 FFT powers the PSD and spectrogram.

const CH = [
  ["paddleL", "Paddle L · cortex", "#8fb8d8"],
  ["paddleR", "Paddle R · cortex", "#a7d0e6"],
  ["depthL", "Depth L · STN", "#f4b860"],
  ["depthR", "Depth R · STN", "#f0a060"],
];

const RAW_WIN = 2560;   // ~2.5 s shown in Raw
const PSD_N = 1024;     // FFT size for PSD (power of 2)
const SPEC_N = 256;     // FFT size for spectrogram columns (time resolution)
const FMAX = 100;       // Hz shown in PSD / spectrogram
const SPEC_COLS = 320;  // spectrogram history width (px)

export class SignalPanel {
  constructor() {
    const h = (location.hash || "").replace("#", "");
    this.mode = ["raw", "psd", "spec"].includes(h) ? h : "raw";
    this.fs = 1024;
    this.buf = {};
    this.psdCache = {};
    this.canvases = {};
    this.specCanvas = {};   // offscreen scrolling images per channel
    this.specReady = false;

    const plots = document.getElementById("plots");
    for (const [key, label, color] of CH) {
      const d = document.createElement("div"); d.className = "plot";
      d.innerHTML = `<div class="lbl"><span>${label}</span><span class="unit" data-u="${key}"></span></div>`;
      const c = document.createElement("canvas"); d.appendChild(c); plots.appendChild(d);
      this.canvases[key] = c;
      this.buf[key] = new Float32Array(RAW_WIN);
      this.psdCache[key] = null;
      const oc = document.createElement("canvas"); oc.width = SPEC_COLS; oc.height = SPEC_N / 2;
      this.specCanvas[key] = oc;
    }
    this._colors = Object.fromEntries(CH.map(([k, , c]) => [k, c]));

    // mode toggle
    for (const x of document.querySelectorAll("#modes button")) x.classList.toggle("active", x.dataset.mode === this.mode);
    document.getElementById("modes").addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      this.mode = b.dataset.mode;
      for (const x of document.querySelectorAll("#modes button")) x.classList.toggle("active", x === b);
    });

    this.els = {
      ampL: g("ampL"), ampLv: g("ampLv"), ampR: g("ampR"), ampRv: g("ampRv"),
      stimMode: g("stimMode"), betaL: g("betaL"), betaR: g("betaR"),
    };
  }

  onTick(m) {
    this.fs = m.fs || this.fs;
    for (const [key] of CH) {
      const chunk = m.channels[key]; if (!chunk) continue;
      const buf = this.buf[key]; const n = chunk.length;
      buf.copyWithin(0, n); buf.set(chunk, RAW_WIN - n);
      // cache PSD for this channel (cheap; computed at tick rate, ~20 Hz)
      this.psdCache[key] = welchPSD(buf, this.fs);
      // advance the spectrogram by one column
      this._pushSpecColumn(key);
    }
    this.specReady = true;

    // readouts
    const sa = m.stim_applied;
    setBar(this.els.ampL, sa.left / 3); this.els.ampLv.textContent = sa.left.toFixed(1);
    setBar(this.els.ampR, sa.right / 3); this.els.ampRv.textContent = sa.right.toFixed(1);
    this.els.stimMode.textContent = sa.mode === "adaptive" ? `adaptive (${sa.adaptive_kind})` : sa.mode;
    setBar(this.els.betaL, m.beta.depthL); setBar(this.els.betaR, m.beta.depthR);
  }

  _pushSpecColumn(key) {
    const oc = this.specCanvas[key]; const ctx = oc.getContext("2d");
    ctx.drawImage(oc, -1, 0); // scroll left 1px
    const seg = this.buf[key].subarray(RAW_WIN - SPEC_N);
    const ps = periodogram(seg, this.fs);
    const bins = oc.height;
    const fbinMax = Math.min(ps.power.length - 1, Math.floor(FMAX / (this.fs / SPEC_N)));
    for (let y = 0; y < bins; y++) {
      const fb = Math.floor((y / bins) * fbinMax);
      const v = clamp01(Math.log10(ps.power[fb] + 1e-9) * 0.28 + 0.9);
      const [r, gC, b] = spectroColor(v);
      ctx.fillStyle = `rgb(${r},${gC},${b})`;
      ctx.fillRect(oc.width - 1, bins - 1 - y, 1, 1); // low freq at bottom
    }
  }

  draw() {
    for (const [key] of CH) {
      const c = this.canvases[key], dpr = devicePixelRatio || 1;
      const w = c.clientWidth, h = c.clientHeight;
      if (c.width !== w * dpr || c.height !== h * dpr) { c.width = w * dpr; c.height = h * dpr; }
      const ctx = c.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      if (this.mode === "raw") this._drawRaw(ctx, w, h, key);
      else if (this.mode === "psd") this._drawPSD(ctx, w, h, key);
      else this._drawSpec(ctx, w, h, key);
    }
  }

  _drawRaw(ctx, w, h, key) {
    const buf = this.buf[key];
    let mn = Infinity, mx = -Infinity;
    for (let i = 0; i < buf.length; i++) { const v = buf[i]; if (v < mn) mn = v; if (v > mx) mx = v; }
    if (!isFinite(mn) || mx - mn < 1e-6) { mn = -1; mx = 1; }
    const pad = (mx - mn) * 0.12; mn -= pad; mx += pad;
    ctx.strokeStyle = this._colors[key]; ctx.lineWidth = 1.1; ctx.beginPath();
    for (let x = 0; x < w; x++) {
      const i = Math.floor((x / w) * buf.length);
      const y = h - ((buf[i] - mn) / (mx - mn)) * h;
      x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();
    setUnit(key, "µV");
  }

  _drawPSD(ctx, w, h, key) {
    const ps = this.psdCache[key]; if (!ps) return;
    const { freqs, power } = ps;
    const nMax = power.length - 1;
    const fbinMax = Math.min(nMax, freqs.findIndex((f) => f > FMAX));
    const top = fbinMax > 0 ? fbinMax : nMax;
    // log-power scaling
    let lmin = Infinity, lmax = -Infinity;
    const lp = new Float32Array(top);
    for (let i = 1; i < top; i++) { lp[i] = Math.log10(power[i] + 1e-9); if (lp[i] < lmin) lmin = lp[i]; if (lp[i] > lmax) lmax = lp[i]; }
    if (!isFinite(lmin) || lmax - lmin < 1e-6) { lmin = -3; lmax = 1; }
    // beta band shading (13-30 Hz)
    const xOf = (f) => (f / FMAX) * w;
    ctx.fillStyle = "#f4b86018";
    ctx.fillRect(xOf(13), 0, xOf(30) - xOf(13), h);
    // curve
    ctx.strokeStyle = this._colors[key]; ctx.lineWidth = 1.3; ctx.beginPath();
    for (let i = 1; i < top; i++) {
      const x = (freqs[i] / FMAX) * w;
      const y = h - ((lp[i] - lmin) / (lmax - lmin)) * h;
      i === 1 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();
    // freq ticks
    ctx.fillStyle = "#6f6555"; ctx.font = "9px system-ui";
    for (const f of [20, 50, 80]) ctx.fillText(f, xOf(f) - 6, h - 2);
    setUnit(key, "dB/Hz");
  }

  _drawSpec(ctx, w, h, key) {
    if (!this.specReady) return;
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(this.specCanvas[key], 0, 0, w, h);
    ctx.fillStyle = "#bbb"; ctx.font = "9px system-ui";
    ctx.fillText(FMAX + "Hz", 2, 10); ctx.fillText("0", 2, h - 2);
    setUnit(key, "0–" + FMAX + "Hz");
  }
}

// --- helpers ---
function g(id) { return document.getElementById(id); }
function setBar(el, frac) { el.style.width = Math.max(0, Math.min(1, frac)) * 100 + "%"; }
function setUnit(key, txt) { const e = document.querySelector(`[data-u="${key}"]`); if (e) e.textContent = txt; }
function clamp01(x) { return x < 0 ? 0 : x > 1 ? 1 : x; }

// Classic audio-spectrogram palette: dark navy background -> blue -> orange ->
// yellow for high power (matches the attached reference).
const SPECTRO_STOPS = [
  [0.00, [12, 26, 58]],   // dark navy (background / low power)
  [0.30, [24, 56, 106]],  // blue
  [0.48, [46, 56, 92]],   // dim blue (transition)
  [0.58, [150, 78, 42]],  // orange onset
  [0.74, [232, 126, 38]], // orange
  [0.90, [248, 182, 78]], // amber
  [1.00, [255, 238, 196]],// pale yellow (peaks)
];
function spectroColor(t) {
  t = clamp01(t);
  let i = 0;
  while (i < SPECTRO_STOPS.length - 1 && t > SPECTRO_STOPS[i + 1][0]) i++;
  const [a0, c0] = SPECTRO_STOPS[i];
  const [a1, c1] = SPECTRO_STOPS[Math.min(i + 1, SPECTRO_STOPS.length - 1)];
  const f = a1 === a0 ? 0 : (t - a0) / (a1 - a0);
  return [
    Math.round(c0[0] + (c1[0] - c0[0]) * f),
    Math.round(c0[1] + (c1[1] - c0[1]) * f),
    Math.round(c0[2] + (c1[2] - c0[2]) * f),
  ];
}

// Hann-windowed periodogram of the last N samples (N power of 2).
function periodogram(samples, fs) {
  const N = samples.length;
  const re = new Float64Array(N), im = new Float64Array(N);
  for (let i = 0; i < N; i++) {
    const wnd = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (N - 1));
    re[i] = samples[i] * wnd;
  }
  fft(re, im);
  const half = N / 2;
  const power = new Float64Array(half), freqs = new Float64Array(half);
  const norm = 1 / (fs * N);
  for (let i = 0; i < half; i++) {
    power[i] = (re[i] * re[i] + im[i] * im[i]) * norm;
    freqs[i] = (i * fs) / N;
  }
  return { freqs, power };
}

// Welch PSD: average periodograms over 50%-overlapping PSD_N segments.
function welchPSD(buf, fs) {
  const N = PSD_N, hop = N / 2;
  const segs = Math.max(1, Math.floor((buf.length - N) / hop) + 1);
  let acc = null, freqs = null;
  let count = 0;
  for (let s = 0; s < segs; s++) {
    const start = buf.length - N - s * hop;
    if (start < 0) break;
    const p = periodogram(buf.subarray(start, start + N), fs);
    if (!acc) { acc = new Float64Array(p.power.length); freqs = p.freqs; }
    for (let i = 0; i < acc.length; i++) acc[i] += p.power[i];
    count++;
  }
  for (let i = 0; i < acc.length; i++) acc[i] /= count;
  return { freqs, power: acc };
}

// In-place iterative radix-2 Cooley-Tukey FFT.
function fft(re, im) {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) { [re[i], re[j]] = [re[j], re[i]]; [im[i], im[j]] = [im[j], im[i]]; }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = (-2 * Math.PI) / len;
    const wr = Math.cos(ang), wi = Math.sin(ang);
    for (let i = 0; i < n; i += len) {
      let cwr = 1, cwi = 0;
      for (let k = 0; k < len / 2; k++) {
        const a = i + k, b = i + k + len / 2;
        const tr = re[b] * cwr - im[b] * cwi;
        const ti = re[b] * cwi + im[b] * cwr;
        re[b] = re[a] - tr; im[b] = im[a] - ti;
        re[a] += tr; im[a] += ti;
        const ncwr = cwr * wr - cwi * wi;
        cwi = cwr * wi + cwi * wr; cwr = ncwr;
      }
    }
  }
}
