import express from 'express';
import request from 'supertest';

jest.mock('../../tools/mailTools', () => ({
  listEmails: jest.fn(),
  searchEmails: jest.fn(),
  readEmail: jest.fn(),
  sendEmail: jest.fn(),
}));

// eslint-disable-next-line import/first
import emailRouter from '../email';
// eslint-disable-next-line import/first
import * as mailTools from '../../tools/mailTools';

function buildApp() {
  const app = express();
  app.use(express.json());
  app.use('/api/email', emailRouter);
  return app;
}

describe('email routes', () => {
  beforeEach(() => {
    jest.resetAllMocks();
  });

  it('GET /inbox calls list_emails and returns its result', async () => {
    (mailTools.listEmails as jest.Mock).mockResolvedValue({ messages: [], nextPageToken: null });
    const res = await request(buildApp()).get('/api/email/inbox');
    expect(res.status).toBe(200);
    expect(mailTools.listEmails).toHaveBeenCalledWith(
      expect.objectContaining({ folder: 'inbox', maxResults: 25 })
    );
  });

  it('GET /search requires a query', async () => {
    const res = await request(buildApp()).get('/api/email/search');
    expect(res.status).toBe(400);
    expect(mailTools.searchEmails).not.toHaveBeenCalled();
  });

  it('GET /search calls search_emails with the query', async () => {
    (mailTools.searchEmails as jest.Mock).mockResolvedValue({ messages: [], nextPageToken: null });
    const res = await request(buildApp()).get('/api/email/search').query({ q: 'invoice' });
    expect(res.status).toBe(200);
    expect(mailTools.searchEmails).toHaveBeenCalledWith(expect.objectContaining({ query: 'invoice' }));
  });

  it('GET /message/:id calls read_email', async () => {
    (mailTools.readEmail as jest.Mock).mockResolvedValue({ id: '1', body: 'hi' });
    const res = await request(buildApp()).get('/api/email/message/1');
    expect(res.status).toBe(200);
    expect(mailTools.readEmail).toHaveBeenCalledWith({ id: '1' });
  });

  it('returns 502 if a mail tool throws', async () => {
    (mailTools.readEmail as jest.Mock).mockRejectedValue(new Error('upstream down'));
    const res = await request(buildApp()).get('/api/email/message/1');
    expect(res.status).toBe(502);
  });

  it('POST /send rejects when not confirmed', async () => {
    const res = await request(buildApp())
      .post('/api/email/send')
      .send({ to: ['a@b.com'], subject: 'hi', body: 'hi' });
    expect(res.status).toBe(400);
    expect(mailTools.sendEmail).not.toHaveBeenCalled();
  });

  it('POST /send rejects missing recipient even if confirmed', async () => {
    const res = await request(buildApp())
      .post('/api/email/send')
      .send({ to: [], subject: 'hi', body: 'hi', confirmed: true });
    expect(res.status).toBe(400);
    expect(mailTools.sendEmail).not.toHaveBeenCalled();
  });

  it('POST /send calls send_email only when confirmed with full fields', async () => {
    (mailTools.sendEmail as jest.Mock).mockResolvedValue({ id: 'sent-1' });
    const res = await request(buildApp())
      .post('/api/email/send')
      .send({ to: ['a@b.com'], subject: 'hi', body: 'hi', confirmed: true });
    expect(res.status).toBe(200);
    expect(mailTools.sendEmail).toHaveBeenCalledWith({
      to: ['a@b.com'],
      cc: undefined,
      bcc: undefined,
      subject: 'hi',
      body: 'hi',
    });
  });
});
