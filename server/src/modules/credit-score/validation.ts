import { z } from 'zod';

const isoDate = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'Expected YYYY-MM-DD');

export const creditScoreInputSchema = z.object({
  bureau: z.enum(['equifax', 'experian', 'transunion', 'other']),
  score: z.number().int().min(300).max(850),
  scoreModel: z.string().max(60).optional(),
  recordedOn: isoDate.refine((d) => new Date(d) <= new Date(), {
    message: 'recordedOn cannot be in the future',
  }),
  source: z.string().max(120).optional(),
  notes: z.string().max(2000).optional(),
});

export type CreditScoreInputParsed = z.infer<typeof creditScoreInputSchema>;
