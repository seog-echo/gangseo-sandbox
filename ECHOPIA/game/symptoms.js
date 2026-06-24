// Parkinsonian symptom model: stim efficacy x behavioral state x location.
// Drives the avatar's tremor / bradykinesia / freezing / falls, and raises
// adverse events (FoG, fall) for the phone to alarm on.
//
// Protection (0..1) is how well the current therapy controls symptoms:
//   off -> 0 (frequent symptoms), continuous -> ~0.45-0.65,
//   adaptive(state) -> ~0.82, adaptive(closed_loop) -> ~0.9 (best).
// Everything scales with (1 - protection), so the relief ladder is visible.

export class Symptoms {
  constructor(avatar, { onAdverse } = {}) {
    this.a = avatar;
    this.onAdverse = onAdverse;
    this.doorArmed = true;   // roll for FoG once per doorway crossing
    this.freezeT = 0;        // remaining freeze time
    this.pendingFall = 0;    // fall likelihood after the current freeze
    this.fallCd = 0;         // cooldown so falls don't chain
  }

  update(dt, ctx) {
    const unprot = 1 - ctx.protection;

    // --- tremor: rest tremor while standing/sitting (continuous) ---
    const tremorTarget = ctx.state === "Rest" && (ctx.mode === "idle" || ctx.mode === "sit") ? unprot : 0;
    this.a.tremor += (tremorTarget - this.a.tremor) * Math.min(1, dt * 3);

    // --- bradykinesia: slowed gait while moving (continuous). More therapy ->
    // faster, more fluid gait; so 3 mA walks noticeably faster than 2 mA. ---
    const speedTarget = ctx.state === "Movement" ? 0.45 + 0.55 * ctx.protection : 1;
    this.a.speedScale += (speedTarget - this.a.speedScale) * Math.min(1, dt * 3);

    // --- resolve an active freeze ---
    if (this.freezeT > 0) {
      this.freezeT -= dt;
      if (this.freezeT <= 0) {
        this.a.setFrozen(false);
        if (Math.random() < 0.32 * this.pendingFall) this._fall();
      }
    }

    // --- FoG: triggered crossing the doorway while walking ---
    // ctx.pos is a THREE.Vector2 where .x is world-x and .y is world-z.
    const inDoor = Math.abs(ctx.pos.x) < 0.7 && Math.abs(ctx.pos.y) < 1.3;
    if (!inDoor) this.doorArmed = true;
    if (inDoor && this.doorArmed && ctx.mode === "walk" && !this.a.frozen && !this.a.fallen) {
      this.doorArmed = false;
      if (Math.random() < 0.85 * unprot) this._fog(unprot);
    }

    // --- rare fall while walking unprotected ---
    this.fallCd -= dt;
    if (ctx.mode === "walk" && !this.a.frozen && !this.a.fallen && this.fallCd <= 0) {
      if (Math.random() < 0.02 * unprot * dt) this._fall();
    }
  }

  _fog(unprot) {
    this.a.setFrozen(true);
    this.freezeT = 4.5; // longer, more obvious freeze
    this.pendingFall = unprot;
    this.onAdverse?.("FoG");
  }

  _fall() {
    this.a.fall();
    this.fallCd = 4;
    this.onAdverse?.("Fall");
  }
}
