// Builds Mr. Echo's cozy two-room home from low-poly primitives:
// bedroom (left) + living room (right), joined by a single central doorway
// (the natural Freezing-of-Gait chokepoint). Returns walkable regions and the
// set of interactable objects (bed / chairs / sofa) for the game layer.

import * as THREE from "three";

// --- cozy palette ---
const C = {
  floor: 0xb98a5a, wall: 0xe9dcc6, rug1: 0xc2603f, rug2: 0x6f8f74,
  wood: 0x8a5a3c, woodDark: 0x6e4630, sheet: 0xf2ead9, pillow: 0xe7d2b6,
  sofa: 0x7a9a8b, armchair: 0xc08552, lampShade: 0xffd9a0, lampGlow: 0xffca73,
  leaf: 0x4e8d57, pot: 0xb5654a, tv: 0x20262b, book: [0xb5654a, 0x6f8f74, 0xd8b15a, 0x8c6f9e],
  highlight: 0xf4b860,
};

function mat(color, opts = {}) {
  return new THREE.MeshStandardMaterial({
    color, roughness: opts.rough ?? 0.85, metalness: opts.metal ?? 0.0,
    emissive: opts.emissive ?? 0x000000, emissiveIntensity: opts.emi ?? 1,
  });
}

function box(w, h, d, color, p = {}) {
  const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat(color, p));
  m.position.set(p.x ?? 0, p.y ?? h / 2, p.z ?? 0);
  if (p.ry) m.rotation.y = p.ry;
  m.castShadow = p.cast ?? true; m.receiveShadow = true;
  return m;
}

function cyl(r1, r2, h, color, p = {}) {
  const m = new THREE.Mesh(new THREE.CylinderGeometry(r1, r2, h, p.seg ?? 16), mat(color, p));
  m.position.set(p.x ?? 0, p.y ?? h / 2, p.z ?? 0);
  m.castShadow = true; m.receiveShadow = true;
  return m;
}

// House dimensions (interior).
export const HOUSE = { halfW: 9, halfD: 5, wallH: 1.2, wallT: 0.3, doorHalf: 1.3 };

export function buildWorld(scene) {
  const root = new THREE.Group();
  scene.add(root);
  const interactables = [];
  const lamps = [];
  const addLamp = (x, y, z, intensity) => {
    const l = new THREE.PointLight(C.lampGlow, intensity, 8, 1.6);
    l.position.set(x, y, z); l.userData.base = intensity;
    root.add(l); lamps.push(l); return l;
  };

  // --- floor ---
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(HOUSE.halfW * 2 + 0.6, HOUSE.halfD * 2 + 0.6),
    mat(C.floor, { rough: 0.95 })
  );
  floor.rotation.x = -Math.PI / 2;
  floor.receiveShadow = true;
  root.add(floor);

  // --- walls (low, dollhouse style so we see inside) ---
  const { halfW, halfD, wallH: H, wallT: T, doorHalf } = HOUSE;
  root.add(box(halfW * 2 + 0.6, H, T, C.wall, { z: -halfD })); // back
  root.add(box(halfW * 2 + 0.6, H, T, C.wall, { z: halfD }));  // front
  root.add(box(T, H, halfD * 2 + 0.6, C.wall, { x: -halfW }));  // left
  root.add(box(T, H, halfD * 2 + 0.6, C.wall, { x: halfW }));   // right
  // divider wall at x=0 with a central doorway gap
  const segLen = halfD - doorHalf;
  root.add(box(T, H, segLen, C.wall, { x: 0, z: -(doorHalf + segLen / 2) }));
  root.add(box(T, H, segLen, C.wall, { x: 0, z: (doorHalf + segLen / 2) }));
  // doorway posts (subtle frame, taller)
  root.add(box(0.35, H + 0.4, 0.35, C.woodDark, { x: 0, z: -doorHalf }));
  root.add(box(0.35, H + 0.4, 0.35, C.woodDark, { x: 0, z: doorHalf }));

  // --- rugs ---
  const rug = (color, x, z, w, d) => {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, d), mat(color, { rough: 1 }));
    m.rotation.x = -Math.PI / 2; m.position.set(x, 0.01, z); m.receiveShadow = true;
    root.add(m);
  };
  rug(C.rug2, -5, 1.2, 5, 4);
  rug(C.rug1, 5.2, 1.0, 5.5, 4.2);

  // ===================== BEDROOM (left, x < 0) =====================
  // Bed (interactable: lie / sleep)
  const bed = new THREE.Group();
  bed.add(box(3.0, 0.5, 2.0, C.wood, { y: 0.25 }));               // frame
  bed.add(box(2.8, 0.25, 1.85, C.sheet, { y: 0.62 }));            // mattress/sheet
  bed.add(box(2.6, 0.3, 0.5, C.pillow, { y: 0.8, z: -0.6 }));     // pillow
  bed.add(box(3.0, 0.9, 0.18, C.woodDark, { y: 0.45, z: -1.0 })); // headboard
  bed.position.set(-6.2, 0, -3.0);
  root.add(bed);
  interactables.push({
    name: "Bed", group: bed,
    approach: { x: -6.2, z: -1.3 }, seat: { x: -6.2, y: 0.95, z: -3.0 },
    yaw: 0, range: 2.0,
    options: [
      { label: "Lie down", pose: "lie", state: "Rest" },
      { label: "Sleep", pose: "sleep", state: "Sleep" },
    ],
  });

  // Nightstand + little lamp
  root.add(box(0.7, 0.6, 0.7, C.wood, { x: -4.3, z: -3.8 }));
  root.add(cyl(0.18, 0.22, 0.35, C.lampShade, { x: -4.3, y: 0.95, z: -3.8, emissive: C.lampGlow, emi: 0.6 }));
  addLamp(-4.3, 1.0, -3.8, 0.5);

  // Wardrobe (back wall)
  root.add(box(1.6, 2.0, 0.7, C.woodDark, { x: -1.6, y: 1.0, z: -4.3 }));

  // Chair (interactable: sit)
  const chair = makeChair(C.wood);
  chair.position.set(-3.2, 0, 2.6); chair.rotation.y = Math.PI;
  root.add(chair);
  interactables.push({
    name: "Chair", group: chair,
    approach: { x: -3.2, z: 1.5 }, seat: { x: -3.2, y: 0.55, z: 2.6 },
    yaw: 0, range: 1.6, options: [{ label: "Sit", pose: "sit", state: "Rest" }],
  });

  // IPG charger — a wearable headband resting on a small dock (interactable)
  const chargerDock = new THREE.Group();
  chargerDock.add(box(0.62, 0.5, 0.42, C.woodDark, { y: 0.25 }));
  const dockBand = makeHeadband();
  dockBand.position.set(0, 0.78, 0);
  chargerDock.add(dockBand);
  chargerDock.position.set(-3.0, 0, -4.25);
  root.add(chargerDock);
  interactables.push({
    name: "IPG charger", group: chargerDock,
    approach: { x: -3.0, z: -3.2 }, seat: { x: -3.0, y: 0.95, z: -4.25 },
    yaw: 0, range: 1.8, band: dockBand, worn: false,
    getOptions() {
      return this.worn
        ? [{ label: "Take off charger", action: "charger_off" }]
        : [{ label: "Put on charger", action: "charger_on" }];
    },
  });

  // Decor: plant, floor lamp, wall pictures
  root.add(makePlant(-8.2, 4.0));
  root.add(makeFloorLamp(-8.3, -4.0)); addLamp(-8.3, 1.6, -4.0, 0.6);
  addPicture(root, -7.5, -4.84, 0xb5654a);
  addPicture(root, -5.6, -4.84, 0x6f8f74);

  // ===================== LIVING ROOM (right, x > 0) =====================
  // Sofa (interactable: sit)
  const sofa = makeSofa(C.sofa);
  sofa.position.set(5.2, 0, -3.2);
  root.add(sofa);
  interactables.push({
    name: "Sofa", group: sofa,
    approach: { x: 5.2, z: -1.7 }, seat: { x: 5.2, y: 0.6, z: -3.0 },
    yaw: 0, range: 1.8, options: [{ label: "Sit", pose: "sit", state: "Rest" }],
  });

  // Coffee table (decor)
  root.add(box(1.8, 0.45, 1.0, C.woodDark, { x: 5.2, y: 0.22, z: -1.2 }));
  root.add(cyl(0.18, 0.18, 0.18, 0xd8c4a0, { x: 4.8, y: 0.55, z: -1.2 })); // mug

  // Armchair (interactable: sit)
  const arm = makeArmchair(C.armchair);
  arm.position.set(7.4, 0, 2.2); arm.rotation.y = -Math.PI / 2;
  root.add(arm);
  interactables.push({
    name: "Armchair", group: arm,
    approach: { x: 6.1, z: 2.2 }, seat: { x: 7.2, y: 0.6, z: 2.2 },
    yaw: -Math.PI / 2, range: 1.7, options: [{ label: "Sit", pose: "sit", state: "Rest" }],
  });

  // TV on stand (right wall)
  root.add(box(2.4, 0.5, 0.6, C.woodDark, { x: 8.2, y: 0.25, z: 0, ry: Math.PI / 2 }));
  root.add(box(0.12, 1.0, 1.8, C.tv, { x: 8.0, y: 1.0, z: 0, emissive: 0x2b4a66, emi: 0.4 }));

  // Bookshelf (back wall) with colorful books
  const shelf = makeBookshelf();
  shelf.position.set(2.4, 0, -4.4);
  root.add(shelf);

  // Decor: plant, floor lamp, side table + vase, picture, window
  root.add(makePlant(3.8, 4.2));
  root.add(makeFloorLamp(8.3, 4.0)); addLamp(8.3, 1.6, 4.0, 0.6);
  root.add(box(0.6, 0.6, 0.6, C.wood, { x: 8.3, z: 3.0 }));
  root.add(cyl(0.1, 0.14, 0.3, 0x6f8f74, { x: 8.3, y: 0.9, z: 3.0 })); // vase
  addPicture(root, 6.2, -4.84, 0xd8b15a);
  addWindow(root, 8.94, 1.0);

  // --- walkable regions (avatar radius already inset) ---
  const walkRects = [
    { x0: -8.5, x1: -0.45, z0: -4.5, z1: 4.5 }, // bedroom
    { x0: 0.45, x1: 8.5, z0: -4.5, z1: 4.5 },   // living room
    { x0: -0.45, x1: 0.45, z0: -1.1, z1: 1.1 }, // doorway
  ];

  const inUnion = (x, z) =>
    walkRects.some((r) => x >= r.x0 && x <= r.x1 && z >= r.z0 && z <= r.z1);

  const clamp = (x, z) => {
    if (inUnion(x, z)) return { x, z };
    let best = null, bestD = Infinity;
    for (const r of walkRects) {
      const cx = Math.min(Math.max(x, r.x0), r.x1);
      const cz = Math.min(Math.max(z, r.z0), r.z1);
      const d = (cx - x) ** 2 + (cz - z) ** 2;
      if (d < bestD) { bestD = d; best = { x: cx, z: cz }; }
    }
    return best;
  };

  // Doorway center, for the FoG trigger in later phases.
  const doorway = { x: 0, z: 0, halfZ: doorHalf };

  return { root, interactables, walkRects, inUnion, clamp, doorway, lamps };
}

// --- furniture builders ---
function makeChair(color) {
  const g = new THREE.Group();
  g.add(box(0.8, 0.1, 0.8, color, { y: 0.5 }));        // seat
  g.add(box(0.8, 0.8, 0.12, color, { y: 0.9, z: -0.34 })); // back
  for (const [dx, dz] of [[-0.32, -0.32], [0.32, -0.32], [-0.32, 0.32], [0.32, 0.32]])
    g.add(box(0.1, 0.5, 0.1, color, { x: dx, y: 0.25, z: dz }));
  return g;
}

function makeSofa(color) {
  const g = new THREE.Group();
  g.add(box(3.4, 0.5, 1.3, color, { y: 0.3 }));          // base
  g.add(box(3.4, 0.7, 0.3, color, { y: 0.75, z: -0.55 })); // back
  g.add(box(0.3, 0.6, 1.3, color, { x: -1.7, y: 0.6 }));  // arm L
  g.add(box(0.3, 0.6, 1.3, color, { x: 1.7, y: 0.6 }));   // arm R
  g.add(box(1.4, 0.18, 1.0, 0xe7d2b6, { x: -0.7, y: 0.6 })); // cushion
  g.add(box(1.4, 0.18, 1.0, 0xe7d2b6, { x: 0.7, y: 0.6 }));
  return g;
}

function makeArmchair(color) {
  const g = new THREE.Group();
  g.add(box(1.2, 0.5, 1.2, color, { y: 0.3 }));
  g.add(box(1.2, 0.7, 0.3, color, { y: 0.75, z: -0.5 }));
  g.add(box(0.25, 0.6, 1.2, color, { x: -0.55, y: 0.6 }));
  g.add(box(0.25, 0.6, 1.2, color, { x: 0.55, y: 0.6 }));
  return g;
}

function makeBookshelf() {
  const g = new THREE.Group();
  g.add(box(2.6, 2.0, 0.5, 0x6e4630, { y: 1.0 }));
  for (let row = 0; row < 3; row++) {
    for (let i = 0; i < 7; i++) {
      const h = 0.4 + Math.sin(i * 1.7 + row) * 0.08;
      g.add(box(0.18, h, 0.3, C.book[(i + row) % C.book.length],
        { x: -1.05 + i * 0.32, y: 0.45 + row * 0.62 + h / 2 - 0.2, z: 0.05 }));
    }
  }
  return g;
}

function makePlant(x, z) {
  const g = new THREE.Group();
  g.add(cyl(0.3, 0.25, 0.5, C.pot, { y: 0.25 }));
  for (let i = 0; i < 5; i++) {
    const a = (i / 5) * Math.PI * 2;
    const leaf = new THREE.Mesh(new THREE.SphereGeometry(0.35, 8, 8), mat(C.leaf, { rough: 0.9 }));
    leaf.scale.set(0.5, 1.2, 0.5);
    leaf.position.set(Math.cos(a) * 0.22, 0.9 + Math.sin(i) * 0.1, Math.sin(a) * 0.22);
    leaf.rotation.z = Math.cos(a) * 0.4; leaf.castShadow = true;
    g.add(leaf);
  }
  g.position.set(x, 0, z);
  return g;
}

function makeFloorLamp(x, z) {
  const g = new THREE.Group();
  g.add(cyl(0.18, 0.22, 0.06, C.woodDark, { y: 0.03 }));
  g.add(cyl(0.04, 0.04, 1.6, C.woodDark, { y: 0.8 }));
  g.add(cyl(0.28, 0.18, 0.4, C.lampShade, { y: 1.7, emissive: C.lampGlow, emi: 0.7 }));
  g.position.set(x, 0, z);
  return g;
}

// A wearable IPG charger headband (headphone-style band, no earcups) with a
// charging LED. Shared by the dock (resting) and the avatar (worn).
export function makeHeadband() {
  const g = new THREE.Group();
  const bandMat = new THREE.MeshStandardMaterial({ color: 0x2b2f36, roughness: 0.5, metalness: 0.3 });
  const band = new THREE.Mesh(new THREE.TorusGeometry(0.36, 0.05, 8, 24, Math.PI), bandMat);
  band.castShadow = true; g.add(band);
  for (const sx of [-1, 1]) {
    const pad = new THREE.Mesh(new THREE.SphereGeometry(0.075, 10, 10), bandMat);
    pad.position.set(sx * 0.36, 0, 0); g.add(pad);
  }
  const led = new THREE.Mesh(
    new THREE.SphereGeometry(0.045, 8, 8),
    new THREE.MeshStandardMaterial({ color: 0x7bd88f, emissive: 0x3bd86a, emissiveIntensity: 1.2 })
  );
  led.position.set(0, 0.33, 0.08); led.name = "led"; g.add(led);
  return g;
}

function addPicture(root, x, z, color) {
  const frame = box(1.0, 0.7, 0.06, 0x3a2c20, { x, y: 0.95, z, cast: false });
  const art = box(0.8, 0.5, 0.02, color, { x, y: 0.95, z: z + 0.04, cast: false });
  root.add(frame, art);
}

function addWindow(root, x, y) {
  const frame = box(0.06, 1.0, 2.0, 0xf2ead9, { x, y, cast: false });
  const glass = new THREE.Mesh(new THREE.BoxGeometry(0.02, 0.8, 1.7),
    new THREE.MeshStandardMaterial({ color: 0x9fd2e6, emissive: 0x6fa8c0, emissiveIntensity: 0.5,
      transparent: true, opacity: 0.6 }));
  glass.position.set(x - 0.05, y, 0);
  root.add(frame, glass);
}
