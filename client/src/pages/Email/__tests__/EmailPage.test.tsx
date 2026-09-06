import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { EmailPage } from '../EmailPage';
import * as api from '../api';

jest.mock('../api');

describe('EmailPage', () => {
  beforeEach(() => {
    (api.fetchInbox as jest.Mock).mockReset();
  });

  it('loads and displays inbox messages on mount', async () => {
    (api.fetchInbox as jest.Mock).mockResolvedValue({
      messages: [
        {
          id: '1',
          threadId: 't1',
          from: 'a@x.com',
          to: ['me@x.com'],
          subject: 'Hi',
          snippet: 'hello',
          date: new Date().toISOString(),
          isRead: false,
        },
      ],
      nextPageToken: null,
    });

    render(<EmailPage />);

    expect(await screen.findByText('Hi')).toBeInTheDocument();
    expect(api.fetchInbox).toHaveBeenCalledWith({ folder: 'inbox' });
  });

  it('shows an error state if the inbox fails to load', async () => {
    (api.fetchInbox as jest.Mock).mockRejectedValue(new Error('boom'));
    render(<EmailPage />);
    expect(await screen.findByText(/failed to load: boom/i)).toBeInTheDocument();
  });
});
