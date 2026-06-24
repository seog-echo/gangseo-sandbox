// Light, gently upbeat generative background music (WebAudio, no asset files).
// An eighth-note arpeggio over a I–vi–IV–V progression with a soft bass and pad
// — warm and cozy, but with more movement than a slow ambient wash. Started by a
// user gesture (the music toggle), per browser autoplay rules.

export class AmbientMusic {
  constructor() {
    this.ctx = null; this.master = null; this.on = false; this.timer = null; this.step = 0;
  }

  _init() {
    this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    this.master = this.ctx.createGain();
    this.master.gain.value = 0.0001;
    const lp = this.ctx.createBiquadFilter();
    lp.type = "lowpass"; lp.frequency.value = 2400; lp.Q.value = 0.5;
    this.master.connect(lp).connect(this.ctx.destination);
  }

  start() {
    if (!this.ctx) this._init();
    if (this.ctx.state === "suspended") this.ctx.resume();
    this.on = true; this.step = 0;
    this.master.gain.cancelScheduledValues(this.ctx.currentTime);
    this.master.gain.setTargetAtTime(0.14, this.ctx.currentTime, 0.8);
    this._tick();
  }

  stop() {
    this.on = false;
    if (this.master) this.master.gain.setTargetAtTime(0.0001, this.ctx.currentTime, 0.5);
    if (this.timer) { clearTimeout(this.timer); this.timer = null; }
  }

  toggle() { this.on ? this.stop() : this.start(); return this.on; }

  _tick() {
    if (!this.on) return;
    this._playStep(this.step++);
    this.timer = setTimeout(() => this._tick(), 300); // eighth notes, ~100 BPM feel
  }

  _playStep(step) {
    // I – vi – IV – V, each held 8 steps (~2.4 s). Chord tones (semitones from C).
    const prog = [[0, 4, 7, 12], [-3, 0, 4, 9], [-7, -3, 0, 5], [-5, -1, 2, 7]];
    const chord = prog[Math.floor(step / 8) % prog.length];
    const base = 261.63; // C4

    // up–down arpeggio across the chord tones
    const pattern = [0, 1, 2, 3, 2, 1, 2, 3];
    const semi = chord[pattern[step % 8] % chord.length];
    this._voice(base * 2 ** (semi / 12), 0.5, 0.16, "triangle", 0.012);

    if (step % 8 === 0) this._voice(base * 2 ** ((chord[0] - 12) / 12), 1.9, 0.14, "sine", 0.04);   // bass
    if (step % 4 === 0) this._voice(base * 2 ** (chord[0] / 12), 1.6, 0.05, "sine", 0.25);          // soft pad
  }

  _voice(freq, dur, vel, type, attack) {
    const t = this.ctx.currentTime;
    const o = this.ctx.createOscillator(), g = this.ctx.createGain();
    o.type = type; o.frequency.value = freq; o.detune.value = (Math.random() - 0.5) * 6;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(vel, t + attack);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(g).connect(this.master);
    o.start(t); o.stop(t + dur + 0.05);
  }
}
