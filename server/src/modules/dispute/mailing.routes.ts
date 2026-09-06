import { Router } from 'express';
import { z } from 'zod';
import { asyncHandler } from '../../utils/asyncHandler';
import { requireAuth } from '../../middleware/auth';
import { MailingService } from './mailing.service';
import { LetterStreamHttpClient } from '../../integrations/letterstream/client';

// Swap LetterStreamHttpClient for MockLetterStreamClient in local/dev/test
// environments until the real binding (see client.ts TODOs) is wired up.
const mailingService = new MailingService(new LetterStreamHttpClient());

export const mailingRouter = Router();
mailingRouter.use(requireAuth);

// GET /api/disputes/:disputeId/letters -- all letter versions for a dispute.
mailingRouter.get(
  '/:disputeId/letters',
  asyncHandler(async (req, res) => {
    const letters = await mailingService.listLettersForDispute(req.userId, req.params.disputeId);
    res.json({ letters });
  })
);

const draftSchema = z.object({
  consumer: z.object({
    fullName: z.string().min(1),
    addressLines: z.array(z.string().min(1)).min(1),
  }),
});

// POST /api/disputes/:disputeId/letters -- draft + quote only, no mailing.
mailingRouter.post(
  '/:disputeId/letters',
  asyncHandler(async (req, res) => {
    const parsed = draftSchema.safeParse(req.body);
    if (!parsed.success) {
      return res.status(400).json({ error: 'invalid_input', details: parsed.error.flatten() });
    }
    const letter = await mailingService.draftLetter(req.userId, req.params.disputeId, parsed.data.consumer);
    res.status(201).json({ letter });
  })
);

// POST /api/disputes/letters/:letterId/confirm -- the hard-confirm step.
// The UI must have already shown the user the exact recipient, letter
// text, and quoted cost before calling this.
mailingRouter.post(
  '/letters/:letterId/confirm',
  asyncHandler(async (req, res) => {
    const letter = await mailingService.confirmLetter(req.params.letterId);
    res.json({ letter });
  })
);

const authorizeSchema = z.object({
  explicitApproval: z.literal(true),
});

// POST /api/disputes/letters/:letterId/authorize -- REAL POSTAGE.
// Will reject unless the letter was already confirmed and its content
// hasn't changed since.
mailingRouter.post(
  '/letters/:letterId/authorize',
  asyncHandler(async (req, res) => {
    const parsed = authorizeSchema.safeParse(req.body);
    if (!parsed.success) {
      return res.status(400).json({
        error: 'explicit_approval_required',
        message: 'Request must include explicitApproval: true to authorize real postage.',
      });
    }
    const letter = await mailingService.authorizeLetter(req.params.letterId, true);
    res.json({ letter });
  })
);

mailingRouter.post(
  '/letters/:letterId/track',
  asyncHandler(async (req, res) => {
    const letter = await mailingService.trackLetter(req.params.letterId);
    res.json({ letter });
  })
);

mailingRouter.post(
  '/letters/:letterId/cancel',
  asyncHandler(async (req, res) => {
    const letter = await mailingService.cancelLetter(req.params.letterId);
    res.json({ letter });
  })
);
