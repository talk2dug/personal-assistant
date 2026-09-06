import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { ComposeModal } from '../components/ComposeModal';
import * as api from '../api';

jest.mock('../api');

describe('ComposeModal', () => {
  const mockedSend = api.sendEmailConfirmed as jest.Mock;

  beforeEach(() => {
    mockedSend.mockReset();
    mockedSend.mockResolvedValue({ ok: true });
  });

  it('does not send until the user confirms the exact recipient/subject/body', async () => {
    const onSent = jest.fn();
    const onCancel = jest.fn();
    render(<ComposeModal onCancel={onCancel} onSent={onSent} />);

    fireEvent.change(screen.getByLabelText(/to/i), { target: { value: 'friend@example.com' } });
    fireEvent.change(screen.getByLabelText(/subject/i), { target: { value: 'Hello there' } });
    fireEvent.change(screen.getByLabelText(/body/i), { target: { value: 'Just checking in.' } });

    fireEvent.click(screen.getByText(/review & send/i));

    // Confirmation screen must show the exact values before anything is sent.
    expect(await screen.findByText('friend@example.com')).toBeInTheDocument();
    expect(screen.getByText('Hello there')).toBeInTheDocument();
    expect(screen.getByText('Just checking in.')).toBeInTheDocument();
    expect(mockedSend).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /^send$/i }));

    await waitFor(() =>
      expect(mockedSend).toHaveBeenCalledWith({
        to: ['friend@example.com'],
        cc: [],
        subject: 'Hello there',
        body: 'Just checking in.',
      })
    );
    await waitFor(() => expect(onSent).toHaveBeenCalled());
  });

  it('blocks review when recipient or subject is missing', () => {
    render(<ComposeModal onCancel={jest.fn()} onSent={jest.fn()} />);
    fireEvent.click(screen.getByText(/review & send/i));
    expect(screen.getByText(/add at least one recipient/i)).toBeInTheDocument();
    expect(api.sendEmailConfirmed).not.toHaveBeenCalled();
  });

  it('lets the user go back to edit instead of sending', async () => {
    render(<ComposeModal onCancel={jest.fn()} onSent={jest.fn()} />);
    fireEvent.change(screen.getByLabelText(/to/i), { target: { value: 'a@b.com' } });
    fireEvent.change(screen.getByLabelText(/subject/i), { target: { value: 'Subj' } });
    fireEvent.click(screen.getByText(/review & send/i));

    expect(await screen.findByText('a@b.com')).toBeInTheDocument();
    fireEvent.click(screen.getByText(/back to edit/i));

    expect(screen.getByLabelText(/to/i)).toHaveValue('a@b.com');
    expect(api.sendEmailConfirmed).not.toHaveBeenCalled();
  });

  it('cancel from the confirm screen does not send', async () => {
    const onCancel = jest.fn();
    render(<ComposeModal onCancel={onCancel} onSent={jest.fn()} />);
    fireEvent.change(screen.getByLabelText(/to/i), { target: { value: 'a@b.com' } });
    fireEvent.change(screen.getByLabelText(/subject/i), { target: { value: 'Subj' } });
    fireEvent.click(screen.getByText(/review & send/i));

    fireEvent.click(await screen.findByText(/cancel/i));

    expect(onCancel).toHaveBeenCalled();
    expect(api.sendEmailConfirmed).not.toHaveBeenCalled();
  });
});
