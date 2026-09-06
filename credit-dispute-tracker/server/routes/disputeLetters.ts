import { Router } from 'express';
import {
  draftLetter, quoteLetter, confirmAndMailLetter, getLetterForDispute, getLetter, refreshMailStatus,
  ConfirmationMismatchError,
} from '../services/disputeLetterService';

const router = Router();

router.get('/by-dispute/:disputeItemId', (req, res) => {
  const letter = getLetterForDispute(req.params.disputeItemId);
  if (!letter) return res.status(404).json({ error: 'no letter drafted yet' });
  res.json({ letter });
});

/** Step 1: draft -- pure local write, no external call, nothing to confirm. */
router.post('/draft', (req, res) => {
  const { disputeItemId, letterText, recipient } = req.body || {};
  if (!disputeItemId || !letterText || !recipient) {
    return res.status(400).json({ error: 'disputeItemId, letterText, recipient are required' });
  }
  try {
    const letter = draftLetter({ disputeItemId, letterText, recipient });
    res.status(201).json({ letter });
  } catch (err: any) {
    res.status(400).json({ error: err.message });
  }
});

/** Step 2: quote -- calls letterstream_send_mail. Still no money spent. */
router.post('/:id/quote', async (req, res) => {
  try {
    const letter = await quoteLetter(req.params.id);
    res.json({ letter });
  } catch (err: any) {
    res.status(502).json({ error: err.message });
  }
});

/**
 * Step 3: the hard confirm gate. Requires the exact recipient name and
 * quoted cost in the body, plus userApproved: true. This endpoint is
 * the only caller of confirmAndMailLetter, which is itself the only
 * caller of letterstream_authorize_mail -- real postage never happens
 * without a request that looks like this reaching the server.
 */
router.post('/:id/confirm-and-mail', async (req, res) => {
  const { confirmedRecipientName, confirmedQuotedCostCents, userApproved } = req.body || {};
  if (userApproved !== true) {
    return res.status(400).json({ error: 'userApproved must be true -- this step cannot be automated' });
  }
  try {
    const letter = await confirmAndMailLetter(req.params.id, {
      confirmedRecipientName,
      confirmedQuotedCostCents,
      userApproved: true,
    });
    res.json({ letter });
  } catch (err: any) {
    if (err instanceof ConfirmationMismatchError) return res.status(409).json({ error: err.message });
    res.status(502).json({ error: err.message });
  }
});

/** Step 4: status refresh via letterstream_track_mail -- read-only. */
router.post('/:id/refresh-status', async (req, res) => {
  try {
    const letter = await refreshMailStatus(req.params.id);
    res.json({ letter });
  } catch (err: any) {
    res.status(502).json({ error: err.message });
  }
});

router.get('/:id', (req, res) => {
  const letter = getLetter(req.params.id);
  if (!letter) return res.status(404).json({ error: 'not found' });
  res.json({ letter });
});

export default router;
