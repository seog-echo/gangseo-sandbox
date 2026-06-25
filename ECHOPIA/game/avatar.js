// Mr. Echo: a cute capsule avatar with a tiny bit of personality (sweater,
// grey hair, eyes). Handles click-to-move navigation with wall sliding, and
// sit / lie / sleep poses. Reports its behavioral state (Rest / Movement /
// Sleep) so the backend can drive NODES accordingly.

import * as THREE from "three";
import { makeHeadband } from "./world.js";

const WALK_SPEED = 2.2;       // units/s (calmer, realistic walking pace)
const ARRIVE_EPS = 0.06;
const STAND_Y = 0.0;          // base offset; body built around feet at y=0

export class Avatar {
  constructor(scene) {
    this.group = new THREE.Group();
    this.body = new THREE.Group();
    this.group.add(this.body);

    const skin = 0xf0c9a0, sweater = 0xd9774e, pants = 0x5a6b7a, hair = 0xcfc8bf;

    // torso
    const torso = new THREE.Mesh(
      new THREE.CapsuleGeometry(0.36, 0.5, 6, 14),
      new THREE.MeshStandardMaterial({ color: sweater, roughness: 0.85 })
    );
    torso.position.y = 1.0; torso.castShadow = true;
    this.body.add(torso);

    // head
    const head = new THREE.Mesh(
      new THREE.SphereGeometry(0.32, 18, 18),
      new THREE.MeshStandardMaterial({ color: skin, roughness: 0.8 })
    );
    head.position.y = 1.66; head.castShadow = true;
    this.body.add(head);

    // grey hair on the BACK & sides only — the bare face at the front makes the
    // facing direction read clearly from the top-down camera (older gentleman).
    // The phi range excludes a wedge around +z (the face), so the forehead/face
    // stays skin-colored while the back of the head is grey.
    const hairMat = new THREE.MeshStandardMaterial({ color: hair, roughness: 0.9 });
    const hairMesh = new THREE.Mesh(
      new THREE.SphereGeometry(0.35, 22, 16, Math.PI * 0.72, Math.PI * 1.56, 0, Math.PI * 0.66),
      hairMat
    );
    hairMesh.position.y = 1.66; hairMesh.castShadow = true;
    this.body.add(hairMesh);

    // grey eyebrows — extra front cue + a bit of character
    for (const dx of [-0.13, 0.13]) {
      const brow = new THREE.Mesh(new THREE.BoxGeometry(0.12, 0.03, 0.05), hairMat);
      brow.position.set(dx, 1.7, 0.29);
      this.body.add(brow);
    }

    // eyes (face +z) — warm brown, small and friendly, with a tiny glint
    const eyeMat = new THREE.MeshStandardMaterial({ color: 0x6b4f3a, roughness: 0.4 });
    const glintMat = new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0x555555 });
    for (const dx of [-0.11, 0.11]) {
      const eye = new THREE.Mesh(new THREE.SphereGeometry(0.04, 12, 12), eyeMat);
      eye.position.set(dx, 1.61, 0.305);
      this.body.add(eye);
      const glint = new THREE.Mesh(new THREE.SphereGeometry(0.013, 8, 8), glintMat);
      glint.position.set(dx + 0.013, 1.625, 0.335);
      this.body.add(glint);
    }
    // little nose pointing forward
    const nose = new THREE.Mesh(
      new THREE.ConeGeometry(0.045, 0.12, 10),
      new THREE.MeshStandardMaterial({ color: 0xe6b48c, roughness: 0.8 })
    );
    nose.position.set(0, 1.54, 0.33); nose.rotation.x = Math.PI / 2;
    this.body.add(nose);
    // rosy cheeks (cute)
    const cheekMat = new THREE.MeshStandardMaterial({ color: 0xe79e8a, roughness: 0.9 });
    for (const dx of [-0.18, 0.18]) {
      const cheek = new THREE.Mesh(new THREE.SphereGeometry(0.06, 10, 10), cheekMat);
      cheek.position.set(dx, 1.55, 0.25); cheek.scale.set(1, 0.7, 0.5);
      this.body.add(cheek);
    }

    // arms + legs as pivot groups (so they can swing from the shoulder/hip),
    // each capped with a hand (skin) or a shoe so the limbs read clearly
    const limb = (color, r, len, opts = {}) => {
      const pivot = new THREE.Group();
      const m = new THREE.Mesh(new THREE.CapsuleGeometry(r, len, 6, 12),
        new THREE.MeshStandardMaterial({ color, roughness: 0.85 }));
      m.position.y = -(len / 2 + r); m.castShadow = true;
      pivot.add(m);
      const bottom = -(len + 2 * r);
      if (opts.hand) {
        const hand = new THREE.Mesh(new THREE.SphereGeometry(r * 1.05, 10, 10),
          new THREE.MeshStandardMaterial({ color: opts.hand, roughness: 0.8 }));
        hand.position.y = bottom + r * 0.5; hand.castShadow = true; pivot.add(hand);
      }
      if (opts.foot) {
        const foot = new THREE.Mesh(new THREE.CapsuleGeometry(r * 0.7, r * 1.1, 4, 8),
          new THREE.MeshStandardMaterial({ color: opts.foot, roughness: 0.7 }));
        foot.rotation.x = Math.PI / 2;
        foot.position.set(0, bottom + r * 0.55, r * 0.95); foot.castShadow = true; pivot.add(foot);
      }
      return pivot;
    };
    this.armL = limb(sweater, 0.1, 0.34, { hand: skin }); this.armL.position.set(-0.42, 1.28, 0);
    this.armR = limb(sweater, 0.1, 0.34, { hand: skin }); this.armR.position.set(0.42, 1.28, 0);
    this.legL = limb(pants, 0.14, 0.32, { foot: 0x6e4630 }); this.legL.position.set(-0.17, 0.62, 0);
    this.legR = limb(pants, 0.14, 0.32, { foot: 0x6e4630 }); this.legR.position.set(0.17, 0.62, 0);
    this.body.add(this.armL, this.armR, this.legL, this.legR);

    // soft shadow blob fallback under feet (reads well in top-down)
    const blob = new THREE.Mesh(
      new THREE.CircleGeometry(0.45, 16),
      new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.18 })
    );
    blob.rotation.x = -Math.PI / 2; blob.position.y = 0.02;
    this.group.add(blob);

    // red alert ring shown under the feet during adverse events (FoG / fall)
    this.alertRing = new THREE.Mesh(
      new THREE.RingGeometry(0.5, 0.72, 22),
      new THREE.MeshBasicMaterial({ color: 0xe0552f, transparent: true, opacity: 0, side: THREE.DoubleSide })
    );
    this.alertRing.rotation.x = -Math.PI / 2; this.alertRing.position.y = 0.03;
    this.group.add(this.alertRing);

    // sleep "z z z" sprite
    this.zzz = makeZzzSprite();
    this.zzz.position.set(0.3, 1.9, 0); this.zzz.visible = false;
    this.group.add(this.zzz);

    // floating phone icon (tap to open the patient app; turns green with a
    // lightning bolt while the IPG is charging)
    this._phoneTexNormal = makePhoneIconTexture(false);
    this._phoneTexCharging = makePhoneIconTexture(true);
    this.phoneIcon = new THREE.Sprite(new THREE.SpriteMaterial({ map: this._phoneTexNormal, transparent: true }));
    this.phoneIcon.name = "phoneIcon";
    this.phoneIcon.position.set(0, 2.6, 0);
    this.phoneIcon.scale.set(0.55, 0.55, 1);
    this.group.add(this.phoneIcon);

    // IPG charger headband (worn while charging; can walk around with it)
    this.headband = makeHeadband();
    this.headband.position.set(0, 1.68, 0);
    this.headband.visible = false;
    this.body.add(this.headband);
    this._headbandLed = this.headband.getObjectByName("led");

    // attention alarm above the head (an adverse event needs the phone)
    this.alarmIcon = makeBadgeSprite("!", "#e0552f");
    this.alarmIcon.position.set(0, 3.25, 0); this.alarmIcon.scale.set(0.5, 0.5, 1);
    this.alarmIcon.visible = false;
    this.group.add(this.alarmIcon);

    scene.add(this.group);

    this.pos = new THREE.Vector2(-5, 1.5);
    this.facing = 0;
    this.target = null;          // {x,z} or null
    this.pending = null;         // {pose, seat, yaw, state} to apply on arrival
    this.mode = "idle";          // idle | walk | sit | lie | sleep
    this.t = 0;
    this.onState = null;
    this._lastState = null;

    // symptom states (driven by the Symptoms system)
    this.tremor = 0;             // 0..1 rest-tremor intensity
    this.speedScale = 1;         // <1 = bradykinesia
    this.frozen = false;         // freezing of gait
    this.fallen = false;         // mid-fall
    this.fallT = 0;              // fall animation timer

    // pose bookkeeping (sit/lie/sleep)
    this._poseExit = null;       // approach point to pop back to when standing up
    this._poseY = 0; this._poseZ = 0; this._poseRotX = 0;

    this._sync();
  }

  get state() {
    if (this.mode === "sleep") return "Sleep";
    if (this.mode === "walk") return "Movement";
    return "Rest"; // idle, sit, lie
  }

  _emitState() {
    if (this.state !== this._lastState) {
      this._lastState = this.state;
      this.onState?.(this.state);
    }
  }

  // Right-click: walk to a free point, abandoning any pose.
  walkTo(x, z) {
    this.target = { x, z };
    this.pending = null;
    this._exitPose();
    this.mode = "walk";
  }

  // Interact: walk to the approach point, then apply the chosen pose.
  goInteract(it, option) {
    this.target = { x: it.approach.x, z: it.approach.z };
    this.pending = {
      pose: option.pose, seat: it.seat, yaw: it.yaw, state: option.state,
      exit: { x: it.approach.x, z: it.approach.z },
    };
    this._exitPose();
    this.mode = "walk";
  }

  _exitPose() {
    // Standing up from a seat: pop back to the (collision-free) approach point
    // so the avatar never has to walk out from inside the furniture.
    if (this._poseExit) { this.pos.set(this._poseExit.x, this._poseExit.z); this._poseExit = null; }
    this.body.rotation.set(0, 0, 0);
    this.body.position.set(0, 0, 0);
    this.zzz.visible = false;
  }

  _applyPose(p) {
    this.mode = p.pose === "sleep" ? "sleep" : p.pose; // sit | lie | sleep
    this.facing = p.yaw;
    this.pos.set(p.seat.x, p.seat.z);
    this._poseExit = p.exit || null;
    if (p.pose === "sit") {
      // hips rest on the seat surface; torso upright, legs fold forward
      this._poseRotX = 0;
      this._poseY = p.seat.y - 0.62;
      this._poseZ = 0;
    } else {
      // lie / sleep: on the back along the bed, resting on the mattress
      this._poseRotX = -Math.PI / 2;
      this._poseY = p.seat.y + 0.35;
      this._poseZ = 0.8;
      this.zzz.visible = p.pose === "sleep";
    }
    this.body.rotation.set(this._poseRotX, 0, 0);
    this.body.position.set(0, this._poseY, this._poseZ);
    this.target = null;
    this.pending = null;
    this._sync();
  }

  update(dt, world) {
    this.t += dt;
    const blocked = this.frozen || this.fallen;

    if (this.target && !blocked) {
      const dx = this.target.x - this.pos.x;
      const dz = this.target.z - this.pos.y;
      const dist = Math.hypot(dx, dz);
      if (dist < ARRIVE_EPS) {
        if (this.pending && this.pending.action) {
          this.facing = this.pending.yaw ?? this.facing;
          this.mode = "idle"; const act = this.pending.action;
          this.pending = null; this.target = null; act();
        } else if (this.pending) this._applyPose(this.pending);
        else { this.mode = "idle"; this.target = null; }
      } else {
        const step = Math.min(WALK_SPEED * this.speedScale * dt, dist);
        const ux = dx / dist, uz = dz / dist;
        let nx = this.pos.x + ux * step, nz = this.pos.y + uz * step;
        // walls + furniture collision, with wall-sliding on each axis
        if (world.free(nx, nz)) this.pos.set(nx, nz);
        else if (world.free(nx, this.pos.y)) this.pos.x = nx;
        else if (world.free(this.pos.x, nz)) this.pos.y = nz;
        else { this.mode = "idle"; this.target = null; this.pending = null; }
        this.facing = Math.atan2(ux, uz);
      }
    }

    // base posture
    if (this.fallT > 0) {
      this._animateFall(dt);
    } else if (this.frozen) {
      // freezing of gait: pronounced trembling, feet stuck to the floor
      this.body.position.set((Math.random() - 0.5) * 0.05, Math.abs(Math.sin(this.t * 22)) * 0.04, 0);
      this.body.rotation.z = Math.sin(this.t * 22) * 0.12;
    } else if (this.mode === "walk") {
      this.body.position.set(0, Math.abs(Math.sin(this.t * 12) * 0.05), 0);
      this.body.rotation.z = Math.sin(this.t * 12) * 0.06;
    } else if (this.mode === "idle") {
      this.body.position.set(0, Math.sin(this.t * 2) * 0.01, 0);
      this.body.rotation.z = 0;
    } else {
      // sit / lie / sleep — hold the pose each frame (tremor overlay adds on top)
      this.body.position.set(0, this._poseY, this._poseZ);
      this.body.rotation.set(this._poseRotX, 0, 0);
    }

    // rest-tremor overlay (standing or sitting)
    if (this.tremor > 0.01 && (this.mode === "idle" || this.mode === "sit")) {
      this.body.position.x += (Math.random() - 0.5) * 0.05 * this.tremor;
      this.body.rotation.z += Math.sin(this.t * 45) * 0.05 * this.tremor;
    }

    this._animateLimbs(dt);

    // alert ring during adverse events
    const ringTarget = (this.frozen || this.fallen) ? 0.45 + Math.sin(this.t * 8) * 0.2 : 0;
    this.alertRing.material.opacity += (ringTarget - this.alertRing.material.opacity) * 0.25;

    if (this.zzz.visible) this.zzz.position.y = 1.9 + Math.sin(this.t * 2) * 0.08;
    if (this.phoneIcon.visible) this.phoneIcon.position.y = 2.6 + Math.sin(this.t * 2.5) * 0.06;
    if (this.headband.visible && this._headbandLed) {
      this._headbandLed.material.emissiveIntensity = 0.8 + 0.6 * Math.sin(this.t * 5);
    }
    if (this.alarmIcon.visible) {
      this.alarmIcon.position.y = 3.25 + Math.abs(Math.sin(this.t * 6)) * 0.12;
      const s = 0.5 + Math.sin(this.t * 8) * 0.06; this.alarmIcon.scale.set(s, s, 1);
    }

    this._sync();
    this._emitState();
  }

  _animateLimbs(dt) {
    const lerp = (o, target) => { o.rotation.x += (target - o.rotation.x) * Math.min(1, dt * 10); };
    if (this.fallT > 0) {
      // arms reach out in front to brace, legs forward — limbs ahead of the body
      lerp(this.armL, -1.05); lerp(this.armR, -0.9); lerp(this.legL, -0.5); lerp(this.legR, -0.35); return;
    }
    if (this.frozen) {
      // arms braced forward; legs shuffle in front (rapid, can't step through)
      const j = Math.sin(this.t * 26) * 0.2;
      this.legL.rotation.x = -0.25 + j; this.legR.rotation.x = -0.25 - j;
      lerp(this.armL, -0.4); lerp(this.armR, -0.4); return;
    }
    if (this.mode === "walk") {
      const sw = Math.sin(this.t * 12) * 0.5;
      this.armL.rotation.x = sw; this.armR.rotation.x = -sw;
      this.legL.rotation.x = -sw; this.legR.rotation.x = sw; return;
    }
    if (this.mode === "sit") {
      // legs nearly horizontal so they rest on the seat and hang off the front
      // edge instead of dropping into the solid seat base; arms forward on lap
      lerp(this.legL, -1.45); lerp(this.legR, -1.45); lerp(this.armL, -0.25); lerp(this.armR, -0.25); return;
    }
    // idle / lie / sleep
    for (const L of [this.armL, this.armR, this.legL, this.legR]) lerp(L, 0);
  }

  _animateFall(dt) {
    const FALL = 2.6;
    this.fallT -= dt;
    const elapsed = FALL - this.fallT;
    let frac; // 0 = upright, 1 = fully down
    if (elapsed < 0.4) frac = elapsed / 0.4;          // tipping over
    else if (this.fallT > 0.7) frac = 1;              // lying on the floor
    else frac = Math.max(0, this.fallT / 0.7);        // getting back up
    // Rotate to horizontal and RAISE the body so the (now sideways) torso rests
    // on the floor rather than sinking through it.
    this.body.rotation.set(0, 0, (-Math.PI / 2) * frac);
    this.body.position.set(0, 0.36 * frac, 0);
    if (this.fallT <= 0) {
      this.fallen = false; this.fallT = 0;
      this.body.rotation.set(0, 0, 0); this.body.position.set(0, 0, 0);
    }
  }

  setPhoneOut() {
    // No held-phone model; the floating phone icon is the open/close toggle.
  }

  setCharger(on) {
    this.headband.visible = on;
    // show charging on the phone icon itself (green + bolt) instead of a badge
    this.phoneIcon.material.map = on ? this._phoneTexCharging : this._phoneTexNormal;
    this.phoneIcon.material.needsUpdate = true;
  }

  setAlarm(on) {
    this.alarmIcon.visible = on;
  }

  // Walk to a point, then run a callback on arrival (no pose). Used for the charger.
  goAction(approach, yaw, action) {
    this.target = { x: approach.x, z: approach.z };
    this.pending = { action, yaw };
    this._exitPose();
    this.mode = "walk";
  }

  setFrozen(b) {
    this.frozen = b;
    if (!b) { this.body.rotation.z = 0; this.body.position.set(0, 0, 0); }
  }

  fall() {
    if (this.fallen) return;
    this.fallen = true; this.fallT = 2.6;
  }

  _sync() {
    this.group.position.set(this.pos.x, STAND_Y, this.pos.y);
    this.group.rotation.y = this.facing;
  }
}

function makeBadgeSprite(text, color) {
  const c = document.createElement("canvas"); c.width = 64; c.height = 64;
  const ctx = c.getContext("2d");
  ctx.fillStyle = color; ctx.beginPath(); ctx.arc(32, 32, 26, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = "#1b1612"; ctx.font = "bold 36px system-ui";
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(text, 32, 35);
  const tex = new THREE.CanvasTexture(c);
  return new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true }));
}

// Phone-icon texture. Normal = gold phone; charging = green phone with a
// lightning bolt on the screen (so charging is shown on the icon itself rather
// than a separate floating badge).
function makePhoneIconTexture(charging) {
  const c = document.createElement("canvas"); c.width = 64; c.height = 64;
  const ctx = c.getContext("2d");
  const accent = charging ? "#2fe06a" : "#f4b860";   // vivid green vs gold
  const bg = charging ? "#103a22" : "#1b1612";        // green-tinted bg while charging
  ctx.fillStyle = bg; ctx.beginPath(); ctx.arc(32, 32, 30, 0, Math.PI * 2); ctx.fill();
  ctx.lineWidth = charging ? 5 : 4; ctx.strokeStyle = accent; ctx.stroke();
  ctx.fillStyle = accent; ctx.fillRect(24, 15, 16, 34);            // phone body
  ctx.fillStyle = charging ? "#0c2616" : "#1b1612"; ctx.fillRect(26.5, 20, 11, 24); // screen
  if (charging) {
    ctx.fillStyle = "#fff14d";                                     // bright lightning bolt
    ctx.beginPath();
    ctx.moveTo(34, 20.5); ctx.lineTo(27.5, 33.5); ctx.lineTo(31.6, 33.5);
    ctx.lineTo(30, 44); ctx.lineTo(37, 29.5); ctx.lineTo(32.6, 29.5);
    ctx.closePath(); ctx.fill();
  } else {
    ctx.fillStyle = accent; ctx.beginPath(); ctx.arc(32, 46, 1.6, 0, Math.PI * 2); ctx.fill();
  }
  return new THREE.CanvasTexture(c);
}

function makeZzzSprite() {
  const c = document.createElement("canvas"); c.width = 128; c.height = 64;
  const ctx = c.getContext("2d");
  ctx.fillStyle = "#f4d9a8"; ctx.font = "bold 38px system-ui";
  ctx.fillText("z", 8, 44); ctx.font = "bold 28px system-ui"; ctx.fillText("z", 48, 34);
  ctx.font = "bold 20px system-ui"; ctx.fillText("z", 82, 26);
  const tex = new THREE.CanvasTexture(c);
  const spr = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true }));
  spr.scale.set(0.9, 0.45, 1);
  return spr;
}
