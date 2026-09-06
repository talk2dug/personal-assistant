import { Router } from 'express';
import { createDisputeItem, listDisputeItems, getDisputeItem, transitionDisputeState, InvalidTransitionError } from '../models/dispute';

const router = Router();

router.get('/', (req, res) => {
  const { bureau, state } = req.query as any;
  res.json({ items: listDisputeItems({ bureau, state }) });
});

router.get('/:id', (req, res) => {
  const item = getDisputeItem(req.params.id);
  if (!item) return res.status(404).json({ error: 'not found' });
  res.json({ item });
});

router.post('/', (req, res) => {
  const { bureau, creditorName, accountNumberLast4, itemDescription, disputeReason } = req.body || {};
  if (!bureau || !creditorName || !itemDescription || !disputeReason) {
    return res.status(400).json({ error: 'bureau, creditorName, itemDescription, disputeReason are required' });
  }
  const item = createDisputeItem({ bureau, creditorName, accountNumberLast4, itemDescription, disputeReason });
  res.status(201).json({ item });
});

/**
 * Manual/direct state moves -- used for edge cases that don't go
 * through LetterStream (e.g. marking a dispute resolved after a phone
 * call, or correcting a mistaken state). The drafted -> mailed step in
 * the normal flow is set by disputeLetterService after LetterStream
 * confirms authorization, not through this endpoint -- so mailing
 * status here always reflects a real letter, never a manual guess.
 */
router.post('/:id/transition', (req, res) => {
  const { toState, resolutionOutcome, resolutionNotes } = req.body || {};
  try {
    const item = transitionDisputeState(req.params.id, toState, { resolutionOutcome, resolutionNotes });
    res.json({ item });
  } catch (err: any) {
    if (err instanceof InvalidTransitionError) return res.status(409).json({ error: err.message });
    res.status(400).json({ error: err.message });
  }
});

export default router;
