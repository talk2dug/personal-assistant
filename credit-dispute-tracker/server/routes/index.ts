import { Router } from 'express';
import creditScoreRouter from './creditScore';
import disputesRouter from './disputes';

const router = Router();
router.use('/credit-score', creditScoreRouter);
router.use('/disputes', disputesRouter);

/**
 * Mount this router under /api in the main dashboard server, e.g.:
 *   app.use('/api', creditDisputeTrackerRouter);
 */
export default router;
