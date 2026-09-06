import { z } from 'zod';

export const disputeInputSchema = z.object({
  bureau: z.enum(['equifax', 'experian', 'transunion']),
  creditorName: z.string().min(1).max(200),
  accountReference: z.string().max(60).optional(),
  itemDescription: z.string().min(1).max(4000),
  disputeReason: z.enum(['not_mine', 'incorrect_balance', 'paid_in_full', 'duplicate', 'other']),
});

export const disputeResolutionSchema = z.object({
  resolution: z.enum(['removed', 'updated', 'verified_accurate', 'no_response']),
  resolutionNotes: z.string().max(4000).optional(),
});
