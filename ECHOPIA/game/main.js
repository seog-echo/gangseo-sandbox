// Echopia game entry. Sets up the scene, the cozy world, Mr. Echo, input
// (right-click to walk, click to interact), and the live link to the backend.

import * as THREE from "three";
import { buildWorld, HOUSE } from "./world.js";
import { Avatar } from "./avatar.js";
import { connectBackend } from "./net.js";
import { SignalPanel } from "./plots.js";
import { Phone } from "./phone.js";
import { Symptoms } from "./symptoms.js";
import { AmbientMusic } from "./audio.js";

const app = document.getElementById("app");

// --- renderer (fills the #app viewport, not the whole window) ---
const renderer = new THREE.WebGLRenderer({ antialias: true, canvas: document.getElementById("scene") });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(app.clientWidth, app.clientHeight);
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.outputColorSpace = THREE.SRGBColorSpace;
app.appendChild(renderer.domElement);

// --- scene + camera ---
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0e0c0b);
scene.fog = new THREE.Fog(0x0e0c0b, 28, 46);

const camera = new THREE.PerspectiveCamera(50, app.clientWidth / app.clientHeight, 0.1, 100);
const CAM_BASE = new THREE.Vector3(0, 17, 12.5);
let camZoom = 1.0;
let introT = 0; // camera ease-in progress
function placeCamera() {
  camera.position.copy(CAM_BASE).multiplyScalar(camZoom);
  camera.lookAt(0, 0, 0.5);
}
placeCamera();

// --- lighting (warm, cozy) ---
scene.add(new THREE.HemisphereLight(0xfff0dd, 0x3a2a1e, 0.55));
const key = new THREE.DirectionalLight(0xffe7c2, 1.0);
key.position.set(6, 16, 8);
key.castShadow = true;
key.shadow.mapSize.set(2048, 2048);
key.shadow.camera.left = -14; key.shadow.camera.right = 14;
key.shadow.camera.top = 12; key.shadow.camera.bottom = -12;
key.shadow.camera.near = 1; key.shadow.camera.far = 50;
key.shadow.bias = -0.0004;
scene.add(key);

// --- world + avatar ---
const world = buildWorld(scene);
const avatar = new Avatar(scene);

// highlight rings on the floor under each interactable
const ringGeo = new THREE.RingGeometry(0.7, 0.92, 28);
for (const it of world.interactables) {
  const ring = new THREE.Mesh(ringGeo,
    new THREE.MeshBasicMaterial({ color: 0xf4b860, transparent: true, opacity: 0.0, side: THREE.DoubleSide }));
  ring.rotation.x = -Math.PI / 2;
  ring.position.set(it.seat.x, 0.03, it.seat.z);
  scene.add(ring);
  it.ring = ring;
  it.group.userData.it = it;
}

// --- backend link + signal panel ---
const control = { state: "Rest", charging: false, stim: { mode: "off", adaptive_kind: "state", amplitude_ma: 2.0 } };
let lastStim = null; // latest applied-stim info from the backend, for protection calc
const panel = new SignalPanel();
const phone = new Phone({
  onChange: (stim) => { Object.assign(control.stim, stim); net.send(control); },
  onVisibility: (open) => avatar.setPhoneOut(open),
  onNeedsAttention: (needs) => avatar.setAlarm(needs),
});
const symptoms = new Symptoms(avatar, {
  onAdverse: (type) => phone.raiseAlert(type),
});

// How well current therapy controls symptoms (0 = none, 1 = full). In continuous
// mode, higher amplitude controls symptoms (and speeds gait) noticeably more.
function protection() {
  const sa = lastStim;
  if (!sa || sa.mode === "off") return 0;
  if (sa.mode === "continuous") {
    const amp = (sa.left + sa.right) / 2;          // 1 mA -> 0.20, 2 mA -> 0.50, 3 mA -> 0.80
    return Math.max(0, Math.min(0.85, 0.2 + 0.6 * ((amp - 1) / 2)));
  }
  return sa.adaptive_kind === "closed_loop" ? 0.92 : 0.85; // adaptive (best)
}
const els = {
  state: document.getElementById("hState"), stim: document.getElementById("hStim"),
  batt: document.getElementById("hBatt"), conn: document.getElementById("hConn"),
};
const net = connectBackend({
  onStatus: (s) => { els.conn.textContent = s; if (s === "connected") net.send(control); },
  onTick: (m) => {
    els.state.textContent = m.state;
    const sa = m.stim_applied;
    els.stim.textContent = sa.mode === "off" ? "OFF"
      : sa.mode === "continuous" ? `continuous ${sa.left.toFixed(1)}mA`
      : `adaptive ${sa.left.toFixed(1)}mA`;
    els.batt.textContent = (m.charging ? "⚡" : "") + (m.battery * 100).toFixed(0) + "%";
    lastStim = m.stim_applied;
    panel.onTick(m);
    phone.setBattery(m.battery);
  },
});
avatar.onState = (s) => { control.state = s; net.send(control); };

// peaceful background music (opt-in via the top-bar toggle)
const music = new AmbientMusic();
const musicEl = document.getElementById("music");
musicEl.addEventListener("click", () => {
  const on = music.toggle();
  musicEl.textContent = on ? "♪ music: on" : "♪ music: off";
});

// optional demo hooks via URL (e.g. ?phone=1&stim=continuous&amp=3)
const q = new URLSearchParams(location.search);
if (q.get("phone") === "1") { phone.open(); avatar.setPhoneOut(true); }
if (q.get("stim")) phone.set({ on: true, mode: q.get("stim"), kind: q.get("kind") || undefined, amp: q.get("amp") ? +q.get("amp") : undefined });
if (q.get("event") === "fog") { avatar.pos.set(0, 0); setTimeout(() => symptoms._fog(1.0), 1500); }
if (q.get("event") === "fall") { setTimeout(() => symptoms._fall(), 1500); }
if (q.get("zoom")) { camZoom = Math.max(0.4, Math.min(1.7, parseFloat(q.get("zoom")))); placeCamera(); }
if (q.get("charger") === "1") {
  const ch = world.interactables.find((i) => i.name === "IPG charger");
  if (ch) { avatar.setCharger(true); ch.band.visible = false; ch.worn = true; control.charging = true; phone.setCharging(true); }
}

// --- input ---
const raycaster = new THREE.Raycaster();
const ndc = new THREE.Vector2();
const floorPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
const hitPoint = new THREE.Vector3();
const popup = document.getElementById("popup");

function setNDC(e) {
  const r = renderer.domElement.getBoundingClientRect();
  ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  raycaster.setFromCamera(ndc, camera);
}

function findInteractable(obj) {
  while (obj) { if (obj.userData?.it) return obj.userData.it; obj = obj.parent; }
  return null;
}

function inRange(it) {
  return Math.hypot(avatar.pos.x - it.approach.x, avatar.pos.y - it.approach.z) < it.range;
}

// right-click: walk
renderer.domElement.addEventListener("contextmenu", (e) => {
  e.preventDefault();
  hidePopup();
  setNDC(e);
  if (raycaster.ray.intersectPlane(floorPlane, hitPoint)) {
    const p = world.clamp(hitPoint.x, hitPoint.z);
    if (p) avatar.walkTo(p.x, p.z);
  }
});

// left-click: phone icon, then furniture interaction (or walk closer first)
renderer.domElement.addEventListener("click", (e) => {
  setNDC(e);
  // phone icon takes priority
  if (avatar.phoneIcon.visible && raycaster.intersectObject(avatar.phoneIcon, false).length) {
    phone.toggle(); avatar.setPhoneOut(phone.isOpen); return;
  }
  const hits = raycaster.intersectObjects(world.interactables.map((i) => i.group), true);
  const it = hits.length ? findInteractable(hits[0].object) : null;
  if (!it) { hidePopup(); return; }
  if (inRange(it)) openPopup(it);
  else { hidePopup(); avatar.walkTo(it.approach.x, it.approach.z); }
});

// hover cursor
renderer.domElement.addEventListener("pointermove", (e) => {
  setNDC(e);
  if (avatar.phoneIcon.visible && raycaster.intersectObject(avatar.phoneIcon, false).length) {
    renderer.domElement.style.cursor = "pointer"; return;
  }
  const hits = raycaster.intersectObjects(world.interactables.map((i) => i.group), true);
  const it = hits.length ? findInteractable(hits[0].object) : null;
  renderer.domElement.style.cursor = it && inRange(it) ? "pointer" : "default";
});

// wheel zoom
addEventListener("wheel", (e) => {
  camZoom = Math.min(1.7, Math.max(0.55, camZoom + Math.sign(e.deltaY) * 0.08));
  placeCamera();
}, { passive: true });

function chooseOption(it, opt) {
  if (opt.action === "charger_on") {
    avatar.goAction(it.approach, it.yaw, () => {
      avatar.setCharger(true); it.band.visible = false; it.worn = true;
      control.charging = true; net.send(control); phone.setCharging(true);
    });
  } else if (opt.action === "charger_off") {
    avatar.goAction(it.approach, it.yaw, () => {
      avatar.setCharger(false); it.band.visible = true; it.worn = false;
      control.charging = false; net.send(control); phone.setCharging(false);
    });
  } else {
    avatar.goInteract(it, opt);
  }
}

function openPopup(it) {
  popup.querySelector(".title").textContent = it.name;
  popup.querySelectorAll("button").forEach((b) => b.remove());
  const options = it.getOptions ? it.getOptions() : it.options;
  for (const opt of options) {
    const b = document.createElement("button");
    b.textContent = opt.label;
    b.onclick = () => { hidePopup(); chooseOption(it, opt); };
    popup.appendChild(b);
  }
  const r = renderer.domElement.getBoundingClientRect();
  const v = new THREE.Vector3(it.seat.x, 1.2, it.seat.z).project(camera);
  popup.style.left = (r.left + (v.x * 0.5 + 0.5) * r.width) + "px";
  popup.style.top = (r.top + (-v.y * 0.5 + 0.5) * r.height) + "px";
  popup.style.display = "block";
}
function hidePopup() { popup.style.display = "none"; }

// --- resize ---
addEventListener("resize", () => {
  camera.aspect = app.clientWidth / app.clientHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(app.clientWidth, app.clientHeight);
});

// --- loop ---
const clock = new THREE.Clock();
let firstFrame = true;
function animate() {
  requestAnimationFrame(animate);
  const dt = Math.min(clock.getDelta(), 0.05);
  symptoms.update(dt, { state: avatar.state, mode: avatar.mode, pos: avatar.pos, protection: protection() });
  avatar.update(dt, world);

  // highlight rings pulse when in range
  const tt = clock.elapsedTime;
  for (const it of world.interactables) {
    const target = inRange(it) ? 0.45 + Math.sin(tt * 4) * 0.18 : 0.0;
    it.ring.material.opacity += (target - it.ring.material.opacity) * 0.2;
  }

  // cozy lamp flicker
  for (let i = 0; i < world.lamps.length; i++) {
    const l = world.lamps[i];
    l.intensity = l.userData.base * (0.9 + 0.1 * Math.sin(tt * 2.4 + i * 1.7));
  }

  // gentle camera ease-in on load
  if (introT < 1) {
    introT = Math.min(1, introT + dt / 1.6);
    const e = 1 - (1 - introT) ** 3;
    camera.position.copy(CAM_BASE).multiplyScalar(camZoom * (1 + 0.5 * (1 - e)));
    camera.lookAt(0, 0, 0.5);
  }

  panel.draw();
  renderer.render(scene, camera);
  if (firstFrame) {
    firstFrame = false;
    const ld = document.getElementById("loading");
    ld.style.opacity = "0"; setTimeout(() => ld.remove(), 500);
  }
}
animate();
