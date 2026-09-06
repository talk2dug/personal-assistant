/**
 * Sprite loading for the pixel office.
 *
 * Art and sheet layout come from Pixel Agents (MIT, © Pablo De Lucca); the character
 * sheets are based on JIK-A-4's Metro City pack. We draw the PNGs straight to canvas
 * with source rects rather than decoding them to per-pixel arrays the way the original
 * does — that decode exists there to support runtime recolouring, which we don't need,
 * and skipping it removes a server round trip and a dependency for an identical result.
 *
 * Sheet layout (verified against the real files): each character PNG is 112x96 — seven
 * 16x32 frames across, three direction rows down, ordered down / up / right. Frames
 * 0-2 are the walk cycle, 3-4 typing, 5-6 reading. LEFT is RIGHT mirrored at draw time.
 */

export const TILE = 16;
export const CHAR_W = 16;
export const CHAR_H = 32;

export const DIR = { DOWN: 0, UP: 1, RIGHT: 2, LEFT: 3 };
const SHEET_ROW = { [DIR.DOWN]: 0, [DIR.UP]: 1, [DIR.RIGHT]: 2, [DIR.LEFT]: 2 };

// Frame column within a direction row, per animation.
const WALK_FRAMES = [0, 1, 2, 1];
const TYPE_FRAMES = [3, 4];
const READ_FRAMES = [5, 6];

function loadImage(src) {
  return new Promise((resolve) => {
    const img = new Image();
    // Resolve with null rather than rejecting: one missing plant must not stop the
    // office from rendering.
    img.onload = () => resolve(img);
    img.onerror = () => resolve(null);
    img.src = src;
  });
}

export async function loadAssets({ characters = 6, floors = ['floor_1'], furniture = [] } = {}) {
  const [charImgs, floorImgs, wallImg, furnitureImgs] = await Promise.all([
    Promise.all(Array.from({ length: characters }, (_, i) => loadImage(`/pixel/characters/char_${i}.png`))),
    Promise.all(floors.map((name) => loadImage(`/pixel/floors/${name}.png`))),
    loadImage('/pixel/walls/wall_0.png'),
    Promise.all(furniture.map(({ key, file }) => loadImage(`/pixel/furniture/${key}/${file}.png`))),
  ]);

  const furnitureMap = {};
  furniture.forEach(({ key, file }, i) => {
    if (furnitureImgs[i]) furnitureMap[file] = furnitureImgs[i];
  });

  return {
    characters: charImgs.filter(Boolean),
    floors: floorImgs.filter(Boolean),
    wall: wallImg,
    furniture: furnitureMap,
  };
}

/** Source rect for a character frame, plus whether it needs mirroring. */
export function characterFrame(state, dir, frame) {
  let col;
  if (state === 'type') col = TYPE_FRAMES[frame % TYPE_FRAMES.length];
  else if (state === 'read') col = READ_FRAMES[frame % READ_FRAMES.length];
  else if (state === 'walk') col = WALK_FRAMES[frame % WALK_FRAMES.length];
  else col = WALK_FRAMES[1]; // idle: the mid-stride pose, which reads as standing still
  return {
    sx: col * CHAR_W,
    sy: SHEET_ROW[dir] * CHAR_H,
    flip: dir === DIR.LEFT,
  };
}

/**
 * Draws a character frame. Mirroring for LEFT is done with a canvas transform rather
 * than a pre-flipped cached sheet — one draw call either way, and no second copy of
 * every sprite in memory.
 */
export function drawCharacter(ctx, sheet, state, dir, frame, x, y, scale) {
  if (!sheet) return;
  const { sx, sy, flip } = characterFrame(state, dir, frame);
  const w = CHAR_W * scale;
  const h = CHAR_H * scale;
  // Feet sit on the tile centre, so the sprite is anchored bottom-centre.
  const dx = Math.round(x * scale - w / 2);
  const dy = Math.round(y * scale - h + (TILE * scale) / 2);

  if (flip) {
    ctx.save();
    ctx.translate(dx + w, dy);
    ctx.scale(-1, 1);
    ctx.drawImage(sheet, sx, sy, CHAR_W, CHAR_H, 0, 0, w, h);
    ctx.restore();
  } else {
    ctx.drawImage(sheet, sx, sy, CHAR_W, CHAR_H, dx, dy, w, h);
  }
}
