import React, { useCallback, useEffect, useState } from 'react';
import { InboxList } from './components/InboxList';
import { ThreadView } from './components/ThreadView';
import { fetchInbox, searchInbox, fetchMessage } from './api';
import { EmailSummary, EmailMessage } from './types';
import './EmailPage.css';

type ViewState = 'idle' | 'loading' | 'error';

// NOTE: compose lands in the next commit on this branch.
export function EmailPage() {
  const [messages, setMessages] = useState<EmailSummary[]>([]);
  const [listState, setListState] = useState<ViewState>('idle');
  const [listError, setListError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [isSearchMode, setIsSearchMode] = useState(false);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedMessage, setSelectedMessage] = useState<EmailMessage | null>(null);
  const [threadState, setThreadState] = useState<ViewState>('idle');
  const [threadError, setThreadError] = useState<string | null>(null);

  const loadInbox = useCallback(async () => {
    setListState('loading');
    setListError(null);
    try {
      const page = await fetchInbox({ folder: 'inbox' });
      setMessages(page.messages);
      setListState('idle');
    } catch (err) {
      setListState('error');
      setListError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    loadInbox();
  }, [loadInbox]);

  const runSearch = useCallback(
    async (query: string) => {
      if (!query.trim()) {
        setIsSearchMode(false);
        loadInbox();
        return;
      }
      setIsSearchMode(true);
      setListState('loading');
      setListError(null);
      try {
        const page = await searchInbox(query);
        setMessages(page.messages);
        setListState('idle');
      } catch (err) {
        setListState('error');
        setListError((err as Error).message);
      }
    },
    [loadInbox]
  );

  const openMessage = useCallback(async (id: string) => {
    setSelectedId(id);
    setThreadState('loading');
    setThreadError(null);
    setSelectedMessage(null);
    try {
      const msg = await fetchMessage(id);
      setSelectedMessage(msg);
      setThreadState('idle');
    } catch (err) {
      setThreadState('error');
      setThreadError((err as Error).message);
    }
  }, []);

  return (
    <div className="email-page">
      <div className="email-page__toolbar">
        <input
          className="email-page__search"
          type="search"
          placeholder="Search emails..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') runSearch(searchQuery);
          }}
        />
        <button onClick={() => runSearch(searchQuery)} disabled={listState === 'loading'}>
          Search
        </button>
        {isSearchMode && (
          <button
            onClick={() => {
              setSearchQuery('');
              setIsSearchMode(false);
              loadInbox();
            }}
          >
            Clear
          </button>
        )}
      </div>
      <div className="email-page__body">
        <InboxList
          messages={messages}
          selectedId={selectedId}
          loading={listState === 'loading'}
          error={listError}
          onSelect={openMessage}
        />
        <ThreadView message={selectedMessage} loading={threadState === 'loading'} error={threadError} />
      </div>
    </div>
  );
}

export default EmailPage;
