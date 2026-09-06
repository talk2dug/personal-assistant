import { Bureau } from './types';

/**
 * Standard dispute mailing addresses. Bureaus change these periodically --
 * verify before relying on them for anything real. This is exactly why the
 * confirm UI shows the full recipient address for explicit review rather
 * than hiding it behind a bureau name.
 */
export const BUREAU_ADDRESSES: Record<Bureau, { name: string; addressLines: string[] }> = {
  equifax: {
    name: 'Equifax Information Services LLC',
    addressLines: ['P.O. Box 740256', 'Atlanta, GA 30374-0256'],
  },
  experian: {
    name: 'Experian',
    addressLines: ['P.O. Box 4500', 'Allen, TX 75013'],
  },
  transunion: {
    name: 'TransUnion Consumer Solutions',
    addressLines: ['P.O. Box 2000', 'Chester, PA 19016-2000'],
  },
};
