/**
 * Character state machine and office renderer.
 *
 * Ported in spirit from Pixel Agents (MIT) — the walk/type/idle model, the wander
 * behaviour and the frame timings are theirs, and they're what make it read as an office
 * rather than a chart. What's replaced is the data source: theirs is driven by Claude
 * Code hooks and transcript parsing, ours by Jarvis's own agent runs and GPU queue, so
 * the sub-agent, team, permission and pet machinery has no equivalent here and is gone.
 *
 * Each character's *destination* is decided by its server status; the walking, seating
 * and idling in between is local so the office keeps moving between polls.
 */

import { COLS, DESKS, findPath, GPU_STATION, ROWS, seatOf } from './layout.js';
import { CHAR_H, DIR, drawCharacter, TILE } from './sprites.js';

const WALK_SPEED = 34; // world px/sec
const WALK_FRAME_SEC = 0.14;
const TYPE_FRAME_SEC = 0.28;
const WANDER_PAUSE_MIN = 2.5;
const WANDER_PAUSE_MAX = 7;

const rand = (min, max) => min + Math.random() * (max - min);
const tileCenter = (col, row) => ({ x: col * TILE + TILE / 2, y: row * TILE + TILE / 2 });

export function createCharacter(index, agent) {
  const seat = seatOf(DESKS[index % DESKS.length]);
  const c = tileCenter(seat.col, seat.row);
  return {
    key: agent.key,
    title: agent.title,
    sheetIndex: agent.character ?? index,
    homeSeat: seat,
    col: seat.col,
    row: seat.row,
    x: c.x,
    y: c.y,
    dir: DIR.UP,
    anim: 'idle',
    frame: 0,
    frameTimer: 0,
    path: [],
    moveProgress: 0,
    wanderTimer: rand(WANDER_PAUSE_MIN, WANDER_PAUSE_MAX),
    status: agent.status,
    detail: agent.detail,
    target: null,
    queueSlot: null,
  };
}

/**
 * Where this character should be, given its server status. Returning a tile (rather
 * than mutating) keeps the "what the server says" decision separate from the "how it
 * gets there" animation below.
 */
function destinationFor(ch, gpuOrder) {
  if (ch.status === 'on_gpu') return GPU_STATION.operator;
  if (ch.status === 'waiting_gpu') {
    const slot = gpuOrder.indexOf(ch.key);
    const idx = slot === -1 ? 0 : Math.min(slot, GPU_STATION.queue.length - 1);
    return GPU_STATION.queue[idx];
  }
  if (ch.status === 'working' || ch.status === 'just_finished' || ch.status === 'failed') {
    return ch.homeSeat;
  }
  return null; // idle — free to wander
}

export function applyServerState(characters, agents, gpuJobs) {
  const byKey = new Map(agents.map((a) => [a.key, a]));
  // Queue order is submission order, so the line in the office matches the real queue.
  const gpuOrder = gpuJobs.filter((j) => j.status === 'queued').map((j) => j.agent);
  for (const ch of characters) {
    const agent = byKey.get(ch.key);
    if (!agent) continue;
    ch.status = agent.status;
    ch.detail = agent.detail;
    ch.target = destinationFor(ch, gpuOrder);
  }
}

function startWalk(ch, dest, blocked) {
  const path = findPath(ch.col, ch.row, dest.col, dest.row, blocked);
  if (path.length) {
    ch.path = path;
    ch.moveProgress = 0;
    ch.anim = 'walk';
    ch.frame = 0;
    ch.frameTimer = 0;
    return true;
  }
  return false;
}

/** Animation the character plays once it has arrived where it's going. */
function restingAnim(ch) {
  if (ch.status === 'working') return 'type';
  if (ch.status === 'on_gpu') return 'read';
  return 'idle';
}

export function updateCharacter(ch, dt, blocked, walkable) {
  ch.frameTimer += dt;

  if (ch.anim === 'walk') {
    if (ch.frameTimer >= WALK_FRAME_SEC) {
      ch.frameTimer -= WALK_FRAME_SEC;
      ch.frame = (ch.frame + 1) % 4;
    }
    if (!ch.path.length) {
      ch.anim = restingAnim(ch);
      ch.frame = 0;
      // Face the desk when seated, the machines when at the GPU.
      if (ch.status !== 'idle') ch.dir = DIR.UP;
      ch.wanderTimer = rand(WANDER_PAUSE_MIN, WANDER_PAUSE_MAX);
      return;
    }
    const next = ch.path[0];
    if (next.col > ch.col) ch.dir = DIR.RIGHT;
    else if (next.col < ch.col) ch.dir = DIR.LEFT;
    else if (next.row > ch.row) ch.dir = DIR.DOWN;
    else if (next.row < ch.row) ch.dir = DIR.UP;

    ch.moveProgress += (WALK_SPEED / TILE) * dt;
    const from = tileCenter(ch.col, ch.row);
    const to = tileCenter(next.col, next.row);
    const t = Math.min(ch.moveProgress, 1);
    ch.x = from.x + (to.x - from.x) * t;
    ch.y = from.y + (to.y - from.y) * t;

    if (ch.moveProgress >= 1) {
      ch.col = next.col;
      ch.row = next.row;
      ch.x = to.x;
      ch.y = to.y;
      ch.path.shift();
      ch.moveProgress = 0;
    }
    return;
  }

  // Not walking: either head somewhere the server wants us, or mill about.
  const dest = ch.target;
  if (dest && (ch.col !== dest.col || ch.row !== dest.row)) {
    if (startWalk(ch, dest, blocked)) return;
  }

  const resting = restingAnim(ch);
  if (resting === 'type' || resting === 'read') {
    ch.anim = resting;
    if (ch.frameTimer >= TYPE_FRAME_SEC) {
      ch.frameTimer -= TYPE_FRAME_SEC;
      ch.frame = (ch.frame + 1) % 2;
    }
    return;
  }

  ch.anim = 'idle';
  ch.frame = 0;
  if (dest) return; // parked at the GPU queue — wait, don't wander off
  ch.wanderTimer -= dt;
  if (ch.wanderTimer <= 0) {
    const spot = walkable[Math.floor(Math.random() * walkable.length)];
    if (spot) startWalk(ch, spot, blocked);
    ch.wanderTimer = rand(WANDER_PAUSE_MIN, WANDER_PAUSE_MAX);
  }
}

// ── rendering ────────────────────────────────────────────────────────────────

const STATUS_COLOR = {
  working: '#22e8ff',
  on_gpu: '#a970ff',
  waiting_gpu: '#ffb020',
  just_finished: '#38e08b',
  failed: '#ff5470',
  idle: '#4a5c66',
};

/**
 * The back wall, drawn rather than tiled from wall_0.png.
 *
 * That sheet is a 4x4 mask-indexed autotile — each piece assumes particular neighbours
 * and leaves transparent edges for them, so tiling any single piece across a straight
 * run leaves gaps. Reproducing the mask logic to draw one flat wall would be a lot of
 * machinery for a shape that is four rectangles. Colours are sampled from the sheet so
 * it still sits in the same palette as the furniture.
 */
function drawWall(ctx, scale) {
  const s = TILE * scale;
  const width = COLS * s;
  const upper = Math.round(s * 1.25);

  ctx.fillStyle = '#cdc8b8'; // upper wall
  ctx.fillRect(0, 0, width, upper);
  ctx.fillStyle = '#f2f0ea'; // lower wall / wainscot
  ctx.fillRect(0, upper, width, s * 2 - upper);
  ctx.fillStyle = '#a49f92'; // rail between the two
  ctx.fillRect(0, upper - Math.max(1, scale / 2), width, Math.max(1, scale / 2));
  ctx.fillStyle = '#8d8a7e'; // baseboard shadow at the floor line
  ctx.fillRect(0, s * 2 - Math.max(1, scale / 2), width, Math.max(1, scale / 2));
}

/**
 * The shared-GPU floor markings: a bay in front of the machines and a numbered queue
 * lane leading to it. Drawn as real floor paint rather than a UI overlay because the
 * whole point of the metaphor is that this is a machine on the office floor that people
 * physically queue for.
 */
function drawGpuLane(ctx, gpu, scale, time) {
  const reserved = gpu?.mode === 'reserved';
  const s = TILE * scale;
  const accent = reserved ? '#ff5470' : '#22e8ff';

  ctx.save();
  // Operator bay — hatched, and pulsing while a job is actually running.
  const op = GPU_STATION.operator;
  ctx.globalAlpha = reserved ? 0.28 : 0.18 + (gpu?.running ? 0.12 * Math.sin(time * 4) : 0);
  ctx.fillStyle = accent;
  ctx.fillRect(op.col * s, op.row * s, s, s);
  ctx.globalAlpha = reserved ? 0.7 : 0.5;
  ctx.strokeStyle = accent;
  ctx.lineWidth = Math.max(1, scale / 2);
  ctx.strokeRect(op.col * s + 1, op.row * s + 1, s - 2, s - 2);

  // Queue lane: fainter the further back you are, so the line has direction.
  GPU_STATION.queue.forEach((q, i) => {
    ctx.globalAlpha = Math.max(0.05, 0.2 - i * 0.022);
    ctx.fillStyle = accent;
    ctx.fillRect(q.col * s, q.row * s, s, s);
  });

  // A locked-out cross when the owner has the machine.
  if (reserved) {
    ctx.globalAlpha = 0.85;
    ctx.strokeStyle = '#ff5470';
    ctx.lineWidth = Math.max(2, scale);
    const x = op.col * s;
    const y = op.row * s;
    ctx.beginPath();
    ctx.moveTo(x + 3, y + 3);
    ctx.lineTo(x + s - 3, y + s - 3);
    ctx.moveTo(x + s - 3, y + 3);
    ctx.lineTo(x + 3, y + s - 3);
    ctx.stroke();
  }
  ctx.restore();
}

function drawFurniture(ctx, assets, file, col, row, scale) {
  const img = assets.furniture[file];
  if (!img) return;
  const w = img.width * scale;
  const h = img.height * scale;
  // Anchored so tall pieces stand on their tile rather than floating above it.
  ctx.drawImage(img, Math.round(col * TILE * scale), Math.round((row + 1) * TILE * scale - h), w, h);
}

export function renderOffice(ctx, assets, characters, gpu, scale, time) {
  const W = COLS * TILE * scale;
  const H = ROWS * TILE * scale;
  ctx.imageSmoothingEnabled = false;
  ctx.clearRect(0, 0, W, H);

  // Floor
  const floor = assets.floors[0];
  if (floor) {
    for (let row = 2; row < ROWS; row++) {
      for (let col = 0; col < COLS; col++) {
        ctx.drawImage(floor, col * TILE * scale, row * TILE * scale, TILE * scale, TILE * scale);
      }
    }
  }
  drawWall(ctx, scale);

  drawGpuLane(ctx, gpu, scale, time);

  DECOR_CACHE.forEach((d) => drawFurniture(ctx, assets, d.file, d.col, d.row, scale));

  // Desks
  DESKS.forEach((d) => drawFurniture(ctx, assets, 'DESK_FRONT', d.col, d.row, scale));

  // GPU machines: animated when busy, dark when reserved or idle.
  const busy = (gpu?.running || 0) > 0;
  const reserved = gpu?.mode === 'reserved';
  const pcFrame = reserved
    ? 'PC_FRONT_OFF'
    : busy
      ? `PC_FRONT_ON_${(Math.floor(time * 6) % 3) + 1}`
      : 'PC_FRONT_ON_1';
  GPU_STATION.machines.forEach((m) => drawFurniture(ctx, assets, pcFrame, m.col, m.row, scale));

  // Characters, painted back to front so nearer ones overlap correctly.
  [...characters]
    .sort((a, b) => a.y - b.y)
    .forEach((ch) => {
      drawCharacter(ctx, assets.characters[ch.sheetIndex % assets.characters.length],
        ch.anim, ch.dir, ch.frame, ch.x, ch.y, scale);

      // Status pip above the head — the one thing a glance should always answer.
      const color = STATUS_COLOR[ch.status] || STATUS_COLOR.idle;
      const px = Math.round(ch.x * scale);
      const py = Math.round(ch.y * scale - CHAR_H * scale + TILE * scale * 0.5 - 5 * scale);
      ctx.fillStyle = color;
      ctx.fillRect(px - 2 * scale, py, 4 * scale, 4 * scale);
      if (ch.status === 'working' || ch.status === 'on_gpu') {
        ctx.globalAlpha = 0.35 + 0.35 * Math.sin(time * 5);
        ctx.fillRect(px - 3 * scale, py - scale, 6 * scale, 6 * scale);
        ctx.globalAlpha = 1;
      }
    });
}

// Kept module-level so renderOffice doesn't re-import per frame.
let DECOR_CACHE = [];
export function setDecor(decor) {
  DECOR_CACHE = decor;
}
