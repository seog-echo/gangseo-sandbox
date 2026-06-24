// The patient's phone app. A slide-in phone-shaped panel with the controls a
// DBS patient actually has: turn stimulation on/off, pick continuous vs.
// adaptive mode, dial the amplitude (continuous only), and see IPG battery.
// Also alarms on adverse events (FoG / fall) and asks to report them, keeping
// an event-report log. The demo opens with stim OFF.

const STYLE = `
#phone { position: absolute; left: 22px; bottom: 22px; width: 244px; height: 472px;
  background: #0b0a09; border: 9px solid #2a2420; border-radius: 36px; z-index: 8;
  box-shadow: 0 18px 50px #000b; transform: translateY(125%); opacity: 0;
  transition: transform .35s cubic-bezier(.2,.8,.2,1), opacity .35s; }
#phone.open { transform: translateY(0); opacity: 1; }
#phone .notch { position: absolute; top: 8px; left: 50%; transform: translateX(-50%);
  width: 70px; height: 6px; background: #2a2420; border-radius: 3px; z-index: 2; }
#phone .screen { position: absolute; inset: 16px 12px 12px; display: flex; flex-direction: column; overflow: hidden; }
#phone .head { display: flex; justify-content: space-between; align-items: center; margin: 4px 2px 10px; }
#phone .head .app { color: #f4b860; font-weight: 700; letter-spacing: 1px; font-size: 14px; }
#phone .batt { font-size: 11px; color: #c9bca5; display: flex; align-items: center; gap: 5px; }
#phone .batt .cell { width: 26px; height: 12px; border: 1.5px solid #c9bca5; border-radius: 3px; position: relative; }
#phone .batt .cell::after { content: ""; position: absolute; right: -4px; top: 3px; width: 2px; height: 6px; background: #c9bca5; }
#phone .batt .cell i { position: absolute; inset: 1.5px; width: 0%; background: #7bd88f; border-radius: 1px; display: block; }
#phone .power { margin: 4px 0 12px; padding: 12px; border-radius: 16px; text-align: center;
  background: #17130f; border: 1px solid #ffffff10; cursor: pointer; user-select: none; }
#phone .power .dot { width: 50px; height: 50px; border-radius: 50%; margin: 0 auto 6px;
  background: #2a231d; border: 3px solid #5a4d3f; display: flex; align-items: center; justify-content: center;
  font-size: 22px; color: #7a6c58; transition: all .2s; }
#phone.on .power .dot { background: #1d3a26; border-color: #7bd88f; color: #7bd88f; box-shadow: 0 0 18px #7bd88f55; }
#phone .power .lbl { font-size: 13px; color: #c9bca5; }
#phone.on .power .lbl { color: #7bd88f; }
#phone .sect { font-size: 10px; text-transform: uppercase; letter-spacing: 1px; color: #8a7e6c; margin: 6px 2px 6px; }
#phone .seg { display: flex; gap: 6px; }
#phone .seg button { flex: 1; padding: 8px 0; border-radius: 10px; border: 1px solid #ffffff10;
  background: #17130f; color: #c9bca5; font-size: 12px; cursor: pointer; }
#phone .seg button.active { background: #f4b860; color: #1b1612; font-weight: 600; }
#phone[disabled-modes] .seg button { opacity: .4; pointer-events: none; }
#phone .amp { margin-top: 10px; padding: 10px 12px; border-radius: 12px; background: #17130f; border: 1px solid #ffffff10; }
#phone .amp .v { float: right; color: #f4b860; font-weight: 600; }
#phone .amp input { width: 100%; margin-top: 6px; accent-color: #f4b860; }
#phone .amp.disabled { opacity: .4; }
#phone .amp.disabled input { pointer-events: none; }
#phone .reports { flex: 1; overflow-y: auto; margin-top: 4px; font-size: 11px; color: #b8a98f; }
#phone .reports .none { color: #6f6555; }
#phone .reports .ev { display: flex; justify-content: space-between; padding: 4px 6px; margin: 3px 0;
  background: #17130f; border-radius: 7px; border-left: 3px solid #e0795b; }
#phone .reports .ev .when { color: #8a7e6c; }
#phone .close { height: 5px; width: 110px; margin: 8px auto 0; background: #3a3027; border-radius: 3px; cursor: pointer; }

/* adverse-event alert overlay */
#phone .alert { position: absolute; inset: 0; background: #1b0f0bf5; border-radius: 28px;
  display: none; flex-direction: column; align-items: center; justify-content: center; gap: 10px;
  padding: 24px; text-align: center; z-index: 3; }
#phone .alert.show { display: flex; animation: buzz .5s; }
@keyframes buzz { 0%,100%{transform:translateX(0)} 20%{transform:translateX(-4px)} 40%{transform:translateX(4px)} 60%{transform:translateX(-3px)} 80%{transform:translateX(3px)} }
#phone .alert .warn { width: 56px; height: 56px; border-radius: 50%; background: #e0552f22;
  border: 2px solid #e0795b; display: flex; align-items: center; justify-content: center; font-size: 30px; }
#phone .alert .atitle { color: #f3b0a0; font-weight: 700; font-size: 15px; }
#phone .alert .amsg { color: #c9bca5; font-size: 12px; line-height: 1.5; }
#phone .alert .abtns { display: flex; gap: 8px; margin-top: 6px; }
#phone .alert .abtns button { padding: 9px 16px; border-radius: 10px; border: none; cursor: pointer; font-size: 13px; }
#phone .alert .abtns .yes { background: #e0795b; color: #1b1612; font-weight: 600; }
#phone .alert .abtns .no { background: #2a231d; color: #c9bca5; }
`;

export class Phone {
  constructor({ onChange, onVisibility, onNeedsAttention } = {}) {
    this.onChange = onChange;
    this.onVisibility = onVisibility;
    this.onNeedsAttention = onNeedsAttention; // (bool) -> show/hide the head alarm
    this._pending = null;   // queued adverse-event type awaiting the patient
    this._batt = 0;
    this._charging = false;
    this.isOpen = false;
    this.on = false;
    this.mode = "continuous";
    this.adaptiveKind = "state"; // "state" (schedule) | "closed_loop" (reads brain)
    this.amp = 2.0;
    this.reports = [];
    this._audio = null;

    const style = document.createElement("style"); style.textContent = STYLE;
    document.head.appendChild(style);

    const el = document.createElement("div"); el.id = "phone";
    el.innerHTML = `
      <div class="notch"></div>
      <div class="screen">
        <div class="head">
          <span class="app">Ambit</span>
          <span class="batt"><span class="cell"><i id="phBatt"></i></span><span id="phBattv">—</span></span>
        </div>
        <div class="power" id="phPower">
          <div class="dot">⏻</div>
          <div class="lbl" id="phPowerLbl">Stimulation OFF</div>
        </div>
        <div class="sect">Mode</div>
        <div class="seg" id="phModes">
          <button data-mode="continuous">Continuous</button>
          <button data-mode="adaptive">Adaptive</button>
        </div>
        <div id="phAdaptiveBox" style="display:none">
          <div class="sect">Adaptive source</div>
          <div class="seg" id="phAdaptive">
            <button data-kind="state">Schedule</button>
            <button data-kind="closed_loop">Closed-loop</button>
          </div>
        </div>
        <div class="amp" id="phAmpBox">
          <span>Amplitude <span class="v" id="phAmpv">2.0 mA</span></span>
          <input type="range" id="phAmp" min="1" max="3" step="0.1" value="2">
        </div>
        <div class="sect" id="phReportsSect">Event reports</div>
        <div class="reports" id="phReports"><div class="none">no events reported</div></div>
        <div class="close" id="phClose" title="put away"></div>
      </div>
      <div class="alert" id="phAlert">
        <div class="warn">⚠</div>
        <div class="atitle"></div>
        <div class="amsg"></div>
        <div class="abtns"><button class="yes" id="phYes">Report</button><button class="no" id="phNo">Dismiss</button></div>
      </div>`;
    document.getElementById("app").appendChild(el);
    this.el = el;

    el.querySelector("#phPower").onclick = () => { this.on = !this.on; this._emit(); this._render(); };
    el.querySelector("#phModes").onclick = (e) => {
      const b = e.target.closest("button"); if (!b || !this.on) return;
      this.mode = b.dataset.mode; this._emit(); this._render();
    };
    el.querySelector("#phAdaptive").onclick = (e) => {
      const b = e.target.closest("button"); if (!b || !this.on) return;
      this.adaptiveKind = b.dataset.kind; this._emit(); this._render();
    };
    const amp = el.querySelector("#phAmp");
    amp.oninput = () => { this.amp = +amp.value; this._emit(); this._render(); };
    el.querySelector("#phClose").onclick = () => this.close();
    el.querySelector("#phYes").onclick = () => { this._pushReport(this._pending); this._pending = null; this._hideAlert(); };
    el.querySelector("#phNo").onclick = () => { this._pending = null; this._hideAlert(); };

    this._render();
  }

  // programmatic control (demo scripting / verification)
  set({ on, mode, kind, amp } = {}) {
    if (on !== undefined) this.on = on;
    if (mode) this.mode = mode;
    if (kind) this.adaptiveKind = kind;
    if (amp !== undefined) this.amp = amp;
    this._emit(); this._render();
  }

  open() {
    this.isOpen = true; this.el.classList.add("open");
    if (this._pending) this._showAlert();
    this.onNeedsAttention?.(false);
    this.onVisibility?.(true);
  }
  close() { this.isOpen = false; this.el.classList.remove("open"); this.onVisibility?.(false); }
  toggle() { this.isOpen ? this.close() : this.open(); return this.isOpen; }

  setBattery(frac) { this._batt = frac; this._renderBattery(); }
  setCharging(on) { this._charging = on; this._renderBattery(); }
  _renderBattery() {
    const f = Math.max(0, Math.min(1, this._batt || 0));
    const cell = this.el.querySelector("#phBatt");
    cell.style.width = f * 100 + "%";
    cell.style.background = this._charging ? "#7bd88f" : (f < 0.2 ? "#e0795b" : "#7bd88f");
    this.el.querySelector("#phBattv").textContent = (this._charging ? "⚡" : "") + Math.round(f * 100) + "%";
  }

  // --- adverse-event alarm ---
  // Don't yank the phone open; queue the alert and flag for attention. If the
  // phone is already open, show it now; otherwise the avatar's head alarm tells
  // the patient to take out the phone, which then surfaces this dialog.
  raiseAlert(type) {
    this._pending = type;
    this._beep();
    if (this.isOpen) this._showAlert();
    else this.onNeedsAttention?.(true);
  }
  _showAlert() {
    const a = this.el.querySelector("#phAlert");
    a.querySelector(".atitle").textContent = this._pending === "FoG" ? "Freezing detected" : "Fall detected";
    a.querySelector(".amsg").textContent = this._pending === "FoG"
      ? "Mr. Echo froze while walking. Report this episode to your care team?"
      : "A fall was detected. Report this episode to your care team?";
    a.classList.add("show");
  }
  _hideAlert() { this.el.querySelector("#phAlert").classList.remove("show"); }

  _pushReport(type) {
    const now = new Date();
    const t = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    this.reports.unshift({ type, t });
    this._renderReports();
  }
  _renderReports() {
    const host = this.el.querySelector("#phReports");
    this.el.querySelector("#phReportsSect").textContent =
      this.reports.length ? `Event reports (${this.reports.length})` : "Event reports";
    if (!this.reports.length) { host.innerHTML = `<div class="none">no events reported</div>`; return; }
    host.innerHTML = this.reports.slice(0, 12).map(
      (r) => `<div class="ev"><span>${r.type}</span><span class="when">${r.t}</span></div>`
    ).join("");
  }

  _beep() {
    try {
      this._audio = this._audio || new (window.AudioContext || window.webkitAudioContext)();
      const ctx = this._audio;
      for (const [i, f] of [[0, 880], [0.18, 880]]) {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.frequency.value = f; o.type = "sine";
        g.gain.setValueAtTime(0.0001, ctx.currentTime + i);
        g.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + i + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + i + 0.14);
        o.connect(g).connect(ctx.destination);
        o.start(ctx.currentTime + i); o.stop(ctx.currentTime + i + 0.15);
      }
    } catch { /* audio may be blocked; ignore */ }
  }

  _emit() {
    const mode = this.on ? this.mode : "off";
    this.onChange?.({ mode, adaptive_kind: this.adaptiveKind, amplitude_ma: this.amp });
  }

  _render() {
    this.el.classList.toggle("on", this.on);
    this.el.querySelector("#phPowerLbl").textContent = this.on ? "Stimulation ON" : "Stimulation OFF";
    for (const b of this.el.querySelectorAll("#phModes button"))
      b.classList.toggle("active", this.on && b.dataset.mode === this.mode);
    if (this.on) this.el.removeAttribute("disabled-modes"); else this.el.setAttribute("disabled-modes", "");
    // adaptive source toggle (only when adaptive + on)
    const adaptiveOn = this.on && this.mode === "adaptive";
    this.el.querySelector("#phAdaptiveBox").style.display = adaptiveOn ? "block" : "none";
    for (const b of this.el.querySelectorAll("#phAdaptive button"))
      b.classList.toggle("active", adaptiveOn && b.dataset.kind === this.adaptiveKind);
    // amplitude (continuous only)
    const ampBox = this.el.querySelector("#phAmpBox");
    ampBox.classList.toggle("disabled", !(this.on && this.mode === "continuous"));
    this.el.querySelector("#phAmpv").textContent = this.amp.toFixed(1) + " mA";
    this.el.querySelector("#phAmp").value = this.amp;
  }
}
