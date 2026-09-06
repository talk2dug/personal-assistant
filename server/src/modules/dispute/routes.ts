import { Router } from 'express';
import { asyncHandler } from '../../utils/asyncHandler';
import { requireAuth } from '../../middleware/auth';
import { disputeInputSchema, disputeResolutionSchema } from './validation';
import {
  listDisputes,
  getDispute,
  createDispute,
  transitionDisputeState,
  deleteDispute,
} from './service';
import { InvalidDisputeTransitionError } from './stateMachine';

export const disputeRouter = Router();
disputeRouter.use(requireAuth);

disputeRouter.get(
  '/',
  asyncHandler(async (req, res) => {
    const { bureau, state } = req.query;
    const disputes = await listDisputes(req.userId, {
      bureau: typeof bureau === 'string' ? bureau : undefined,
      state: typeof state === 'string' ? state : undefined,
    });
    res.json({ disputes });
  })
);

disputeRouter.get(
  '/:id',
  asyncHandler(async (req, res) => {
    const dispute = await getDispute(req.userId, req.params.id);
    if (!dispute) return res.status(404).json({ error: 'not_found' });
    res.json({ dispute });
  })
);

disputeRouter.post(
  '/',
  asyncHandler(async (req, res) => {
    const parsed = disputeInputSchema.safeParse(req.body);
    if (!parsed.success) {
      return res.status(400).json({ error: 'invalid_input', details: parsed.error.flatten() });
    }
    const dispute = await createDispute(req.userId, parsed.data);
    res.status(201).json({ dispute });
  })
);

// Manual resolution â€” user records what the bureau ultimately did.
disputeRouter.post(
  '/:id/resolve',
  asyncHandler(async (req, res) => {
    const parsed = disputeResolutionSchema.safeParse(req.body);
    if (!parsed.success) {
      return res.status(400).json({ error: 'invalid_input', details: parsed.error.flatten() });
    }
    try {
      const dispute = await transitionDisputeState(req.userId, req.params.id, 'resolved', parsed.data);
      if (!dispute) return res.status(404).json({ error: 'not_found' });
      res.json({ dispute });
    } catch (err) {
      if (err instanceof InvalidDisputeTransitionError) {
        return res.status(409).json({ error: 'invalid_transition', message: err.message });
      }
      throw err;
    }
  })
);

// Reopen a resolved dispute for a follow-up round.
disputeRouter.post(
  '/:id/reopen',
  asyncHandler(async (req, res) => {
    try {
      const dispute = await transitionDisputeState(req.userId, req.params.id, 'drafted');
      if (!dispute) return res.status(404).json({ error: 'not_found' });
      res.json({ dispute });
    } catch (err) {
      if (err instanceof InvalidDisputeTransitionError) {
        return res.status(409).json({ error: 'invalid_transition', message: err.message });
      }
      throw err;
    }
  })
);

disputeRouter.delete(
  '/:id',
  asyncHandler(async (req, res) => {
    const deleted = await deleteDispute(req.userId, req.params.id);
    if (!deleted) return res.status(404).json({ error: 'not_found' });
    res.status(204).send();
  })
);
