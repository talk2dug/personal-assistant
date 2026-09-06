import { Router } from 'express';
import { asyncHandler } from '../../utils/asyncHandler';
import { requireAuth } from '../../middleware/auth'; // ASSUMPTION: see docs/phase5-credit-dispute-tracker.md
import { creditScoreInputSchema } from './validation';
import {
  listCreditScores,
  createCreditScore,
  updateCreditScore,
  deleteCreditScore,
} from './service';

export const creditScoreRouter = Router();
creditScoreRouter.use(requireAuth);

creditScoreRouter.get(
  '/',
  asyncHandler(async (req, res) => {
    const bureau = typeof req.query.bureau === 'string' ? req.query.bureau : undefined;
    const entries = await listCreditScores(req.userId, { bureau });
    res.json({ entries });
  })
);

creditScoreRouter.post(
  '/',
  asyncHandler(async (req, res) => {
    const parsed = creditScoreInputSchema.safeParse(req.body);
    if (!parsed.success) {
      res.status(400).json({ error: 'invalid_input', details: parsed.error.flatten() });
      return;
    }
    const entry = await createCreditScore(req.userId, parsed.data);
    res.status(201).json({ entry });
  })
);

creditScoreRouter.put(
  '/:id',
  asyncHandler(async (req, res) => {
    const parsed = creditScoreInputSchema.partial().safeParse(req.body);
    if (!parsed.success) {
      res.status(400).json({ error: 'invalid_input', details: parsed.error.flatten() });
      return;
    }
    const entry = await updateCreditScore(req.userId, req.params.id, parsed.data);
    if (!entry) {
      res.status(404).json({ error: 'not_found' });
      return;
    }
    res.json({ entry });
  })
);

creditScoreRouter.delete(
  '/:id',
  asyncHandler(async (req, res) => {
    const deleted = await deleteCreditScore(req.userId, req.params.id);
    if (!deleted) {
      res.status(404).json({ error: 'not_found' });
      return;
    }
    res.status(204).send();
  })
);
