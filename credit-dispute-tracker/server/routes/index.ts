import { Router } from 'express';
import creditScoreRouter from './creditScore';

const router = Router();
router.use('/credit-score', creditScoreRouter);

/**
 * Mount this router under /api in the main dashboard server, e.g.:
 *   app.use('/api', creditDisputeTrackerRouter);
 */
export default router;
