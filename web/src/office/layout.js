/**
 * The office floorplan.
 *
 * Hand-authored rather than loaded from Pixel Agents' layout editor: that editor exists
 * so a user can design an arbitrary room, whereas this office has a fixed cast and one
 * specific story to tell — seven agents at desks and a shared GPU everyone queues for.
 * A literal map is easier to read and reason about than a serialised layout blob.
 *
 * The GPU bridge is drawn as a real machine on the floor with a queue lane in front of
 * it, because that is exactly what it is: one piece of equipment the whole office shares
 * and has to wait its turn for.
 */

export const COLS = 26;
export const ROWS = 17;

export const FURNITURE_FILES = [
  { key: 'DESK', file: 'DESK_FRONT' },
  { key: 'PC', file: 'PC_FRONT_OFF' },
  { key: 'PC', file: 'PC_FRONT_ON_1' },
  { key: 'PC', file: 'PC_FRONT_ON_2' },
  { key: 'PC', file: 'PC_FRONT_ON_3' },
  { key: 'WOODEN_CHAIR', file: 'WOODEN_CHAIR_BACK' },
  { key: 'WHITEBOARD', file: 'WHITEBOARD' },
  { key: 'DOUBLE_BOOKSHELF', file: 'DOUBLE_BOOKSHELF' },
  { key: 'PLANT', file: 'PLANT' },
  { key: 'LARGE_PLANT', file: 'LARGE_PLANT' },
  { key: 'CACTUS', file: 'CACTUS' },
  { key: 'COFFEE_TABLE', file: 'COFFEE_TABLE' },
  { key: 'SOFA', file: 'SOFA_FRONT' },
  { key: 'BIN', file: 'BIN' },
  { key: 'CLOCK', file: 'CLOCK' },
];

/** One desk per agent, in roster order. The seat is the tile the character occupies;
 *  they face UP into the desk, which is the tile above. */
// Eleven desks for seven built-in agents, because the owner can now hire more staff and
// createCharacter seats people with DESKS[index % DESKS.length] — too few desks and new
// hires stack invisibly on top of existing ones instead of getting their own seat.
// Cols 2/6/10 on row 13 are clear of the sofa, coffee table and bin further right.
export const DESKS = [
  { col: 2, row: 4 },
  { col: 6, row: 4 },
  { col: 10, row: 4 },
  { col: 14, row: 4 },
  { col: 2, row: 10 },
  { col: 6, row: 10 },
  { col: 10, row: 10 },
  { col: 14, row: 10 },
  { col: 2, row: 13 },
  { col: 6, row: 13 },
  { col: 10, row: 13 },
];

export const seatOf = (desk) => ({ col: desk.col, row: desk.row + 1 });

/** The shared GPU: a bank of machines on the right, with a marked lane in front. */
export const GPU_STATION = {
  machines: [
    { col: 21, row: 4 },
    { col: 22, row: 4 },
    { col: 23, row: 4 },
  ],
  label: { col: 21, row: 2 },
  /** Where the agent currently using the GPU stands. */
  operator: { col: 22, row: 5 },
  /** Where everyone else lines up, nearest-first. Starts directly behind the operator
   *  bay so the lane reads as one continuous queue rather than two separate marks. */
  queue: [
    { col: 22, row: 6 },
    { col: 22, row: 7 },
    { col: 22, row: 8 },
    { col: 22, row: 9 },
    { col: 22, row: 10 },
    { col: 22, row: 11 },
    { col: 21, row: 11 },
    { col: 20, row: 11 },
  ],
};

/** Decoration — purely cosmetic, drawn behind characters. */
export const DECOR = [
  { file: 'DOUBLE_BOOKSHELF', col: 17, row: 2 },
  { file: 'WHITEBOARD', col: 15, row: 2 },
  { file: 'LARGE_PLANT', col: 0, row: 2 },
  { file: 'PLANT', col: 24, row: 12 },
  { file: 'CACTUS', col: 19, row: 2 },
  { file: 'SOFA_FRONT', col: 16, row: 13 },
  { file: 'COFFEE_TABLE', col: 18, row: 13 },
  { file: 'BIN', col: 13, row: 13 },
  { file: 'CLOCK', col: 12, row: 2 },
  { file: 'PLANT', col: 5, row: 15 },
];

/** Tiles a character may not stand on: desks, machines, and decoration. */
export function blockedTiles() {
  const blocked = new Set();
  const add = (col, row) => blocked.add(`${col},${row}`);
  DESKS.forEach((d) => add(d.col, d.row));
  GPU_STATION.machines.forEach((m) => add(m.col, m.row));
  DECOR.forEach((d) => add(d.col, d.row));
  // The top two rows are wall.
  for (let c = 0; c < COLS; c++) {
    add(c, 0);
    add(c, 1);
  }
  return blocked;
}

export function walkableTiles(blocked) {
  const tiles = [];
  for (let row = 2; row < ROWS; row++) {
    for (let col = 0; col < COLS; col++) {
      if (!blocked.has(`${col},${row}`)) tiles.push({ col, row });
    }
  }
  return tiles;
}

/** Breadth-first path between two tiles.
 *
 *  BFS rather than A*: the grid is 26x17 and every step costs the same, so BFS is
 *  already optimal here and there is no heuristic to get wrong. */
export function findPath(fromCol, fromRow, toCol, toRow, blocked) {
  if (fromCol === toCol && fromRow === toRow) return [];
  const key = (c, r) => `${c},${r}`;
  const start = key(fromCol, fromRow);
  const goal = key(toCol, toRow);
  const cameFrom = new Map([[start, null]]);
  const queue = [{ col: fromCol, row: fromRow }];

  while (queue.length) {
    const cur = queue.shift();
    if (key(cur.col, cur.row) === goal) break;
    const neighbours = [
      { col: cur.col + 1, row: cur.row },
      { col: cur.col - 1, row: cur.row },
      { col: cur.col, row: cur.row + 1 },
      { col: cur.col, row: cur.row - 1 },
    ];
    for (const n of neighbours) {
      if (n.col < 0 || n.col >= COLS || n.row < 2 || n.row >= ROWS) continue;
      const k = key(n.col, n.row);
      // The destination may itself be blocked (walking up to a desk) — allow it as a
      // final step so an agent can reach a seat that sits against furniture.
      if (cameFrom.has(k)) continue;
      if (blocked.has(k) && k !== goal) continue;
      cameFrom.set(k, cur);
      queue.push(n);
    }
  }

  if (!cameFrom.has(goal)) return [];
  const path = [];
  let cursor = goal;
  while (cursor && cursor !== start) {
    const [c, r] = cursor.split(',').map(Number);
    path.unshift({ col: c, row: r });
    const prev = cameFrom.get(cursor);
    cursor = prev ? key(prev.col, prev.row) : null;
  }
  return path;
}
