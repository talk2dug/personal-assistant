import React, { useCallback, useEffect, useState } from 'react';
import { InboxList } from './components/InboxList';
import { fetchInbox, searchInbox } from './api';
import { EmailSummary } from './types';
import './EmailPage.css';

type ViewState = 'idle' | 'loading' | 'error';

// NOTE: this is an intermediate version (list + search only). Thread view
// and compose land in follow-up commits on this branch.
export function EmailPage() {
  const [messages, setMessages] = useState<EmailSummary[]>([]);
  const [listState, setListState] = useState<ViewState>('idle');
  const [listError, setListError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [isSearchMode, setIsSearchMode] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);

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
          onSelect={setSelectedId}
        />
      </div>
    </div>
  );
}

export default EmailPage;
