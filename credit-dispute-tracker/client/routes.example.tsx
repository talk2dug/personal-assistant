/**
 * Example wiring for react-router v6 -- adapt to however the
 * dashboard shell already sets up routing/nav. Not imported by
 * anything else in this module; copy the relevant bits into the real
 * app router.
 */
import React from 'react';
import { Routes, Route } from 'react-router-dom';
import CreditScorePage from './pages/CreditScorePage';
import DisputeTrackerPage from './pages/DisputeTrackerPage';
import DisputeDetailPage from './pages/DisputeDetailPage';

export function CreditDisputeRoutes() {
  return (
    <Routes>
      <Route path="/credit/score" element={<CreditScorePage />} />
      <Route path="/credit/disputes" element={<DisputeTrackerPage />} />
      <Route path="/credit/disputes/:id" element={<DisputeDetailPage />} />
    </Routes>
  );
}
