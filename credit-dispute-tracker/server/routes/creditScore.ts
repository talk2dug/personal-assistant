import { Router } from 'express';
import { createCreditScoreEntry, listCreditScoreHistory, deleteCreditScoreEntry } from '../models/creditScore';

const router = Router();

/**
 * GET /api/credit-score?bureau=experian
 * Returns history sorted oldest -> newest for charting.
 */
router.get('/', (req, res) => {
  const bureau = req.query.bureau as any;
  try {
    const history = listCreditScoreHistory(bureau);
    res.json({ entries: history });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

/**
 * POST /api/credit-score
 * Manual entry -- this dashboard has no live bureau feed, so every
 * score is typed in by the user (e.g. after checking Credit Karma, a
 * card issuer's free score, or an annual bureau pull).
 */
router.post('/', (req, res) => {
  const { recordedAt, score, bureau, source, notes } = req.body || {};
  if (!recordedAt || typeof score !== 'number' || !bureau) {
    return res.status(400).json({ error: 'recordedAt, score, and bureau are required' });
  }
  try {
    const entry = createCreditScoreEntry({ recordedAt, score, bureau, source, notes });
    res.status(201).json({ entry });
  } catch (err: any) {
    res.status(400).json({ error: err.message });
  }
});

router.delete('/:id', (req, res) => {
  const ok = deleteCreditScoreEntry(req.params.id);
  if (!ok) return res.status(404).json({ error: 'not found' });
  res.status(204).end();
});

export default router;
