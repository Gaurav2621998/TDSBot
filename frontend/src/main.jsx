import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { AlertCircle, FileUp, Link, Paperclip, Plus, Send, Sparkles, Trash2, Upload, X } from 'lucide-react';
import './styles.css';

const API_BASE = import.meta.env.VITE_API_BASE || '/api';

function parseAnswer(answer) {
  return answer.split('\n').filter(Boolean);
}

function SourceList({ sources }) {
  if (!sources?.length) return null;
  return (
    <div className="sources">
      {sources.map((source, index) => (
        <a
          className="source-pill"
          key={`${source.title}-${index}`}
          href={source.url || '#'}
          target={source.url ? '_blank' : undefined}
          rel="noreferrer"
        >
          {index + 1}. {source.title}
        </a>
      ))}
    </div>
  );
}

function Message({ message }) {
  const isUser = message.role === 'user';
  return (
    <article className={`message ${isUser ? 'user' : 'assistant'}`}>
      <div className="avatar">{isUser ? 'You' : 'AI'}</div>
      <div className="bubble">
        {parseAnswer(message.content).map((line, index) => (
          <p key={index}>{line}</p>
        ))}
        <SourceList sources={message.sources} />
      </div>
    </article>
  );
}

function App() {
  const [question, setQuestion] = useState('');
  const [messages, setMessages] = useState([
    {
      role: 'assistant',
      content:
        'Ask a TDS/TCS question, upload an invoice PDF, or add a reference URL. I will answer with section, return code, rate, threshold, reasoning, and sources.',
      sources: [],
    },
  ]);
  const [chatId, setChatId] = useState(null);
  const [chats, setChats] = useState([]);
  const [referenceDocuments, setReferenceDocuments] = useState([]);
  const [questionAttachments, setQuestionAttachments] = useState([]);
  const [url, setUrl] = useState('');
  const [loading, setLoading] = useState(false);
  const [notice, setNotice] = useState('');
  const [config, setConfig] = useState(null);
  const scrollRef = useRef(null);

  const documentIds = useMemo(
    () => [...referenceDocuments, ...questionAttachments].map((doc) => doc.id),
    [referenceDocuments, questionAttachments],
  );

  useEffect(() => {
    fetch(`${API_BASE}/chats`)
      .then((res) => (res.ok ? res.json() : []))
      .then(setChats)
      .catch(() => {});
    fetch(`${API_BASE}/config`)
      .then((res) => (res.ok ? res.json() : null))
      .then(setConfig)
      .catch(() => {});
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  async function sendMessage(event) {
    event?.preventDefault();
    const trimmed = question.trim();
    if (!trimmed || loading) return;
    const attachedNames = questionAttachments.map((doc) => doc.title);
    setQuestion('');
    setLoading(true);
    setNotice('');
    setMessages((current) => [
      ...current,
      {
        role: 'user',
        content: attachedNames.length ? `${trimmed}\nAttached PDF: ${attachedNames.join(', ')}` : trimmed,
        sources: [],
      },
    ]);
    try {
      const response = await fetch(`${API_BASE}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: trimmed, chat_id: chatId, document_ids: documentIds }),
      });
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json();
      setChatId(data.chat_id);
      setMessages((current) => [
        ...current,
        { role: 'assistant', content: data.answer, sources: data.sources || [] },
      ]);
      setQuestionAttachments([]);
      fetch(`${API_BASE}/chats`)
        .then((res) => res.json())
        .then(setChats)
        .catch(() => {});
    } catch (error) {
      setMessages((current) => [
        ...current,
        {
          role: 'assistant',
          content: `Short answer: I could not complete the request.\nReasoning: ${error.message}`,
          sources: [],
        },
      ]);
    } finally {
      setLoading(false);
    }
  }

  async function uploadPdf(event, scope = 'reference') {
    const file = event.target.files?.[0];
    if (!file) return;
    setLoading(true);
    setNotice('');
    const form = new FormData();
    form.append('file', file);
    try {
      const response = await fetch(`${API_BASE}/upload-pdf`, { method: 'POST', body: form });
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json();
      const doc = { id: data.document_id, title: file.name, type: 'pdf', invoice: data.invoice };
      if (scope === 'question') {
        setQuestionAttachments((current) => [...current, doc]);
        setNotice(`PDF attached to next question: ${file.name}. Extracted vendor: ${data.invoice.vendor_name || 'not found'}.`);
      } else {
        setReferenceDocuments((current) => [...current, doc]);
        setNotice(`Reference PDF uploaded: ${file.name}. Extracted vendor: ${data.invoice.vendor_name || 'not found'}.`);
      }
    } catch (error) {
      setNotice(`PDF upload failed: ${error.message}`);
    } finally {
      setLoading(false);
      event.target.value = '';
    }
  }

  async function addUrl(event) {
    event.preventDefault();
    const trimmed = url.trim();
    if (!trimmed) return;
    setLoading(true);
    setNotice('');
    try {
      const response = await fetch(`${API_BASE}/add-url`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: trimmed }),
      });
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json();
      setReferenceDocuments((current) => [...current, { id: data.document_id, title: data.title, type: 'url' }]);
      setUrl('');
      setNotice(`Reference added: ${data.title}`);
    } catch (error) {
      setNotice(`URL ingestion failed: ${error.message}`);
    } finally {
      setLoading(false);
    }
  }

  async function openChat(id) {
    const response = await fetch(`${API_BASE}/chats/${id}`);
    if (!response.ok) return;
    const data = await response.json();
    setChatId(id);
    setMessages(data.messages);
  }

  function newChat() {
    setChatId(null);
    setQuestionAttachments([]);
    setMessages([
      {
        role: 'assistant',
        content:
          'New chat ready. Ask about a section, a transaction, or upload an invoice and ask me to classify it.',
        sources: [],
      },
    ]);
  }

  async function clearChats() {
    if (loading) return;
    try {
      const response = await fetch(`${API_BASE}/chats`, { method: 'DELETE' });
      if (!response.ok) throw new Error(await response.text());
      setChats([]);
      setChatId(null);
      setQuestionAttachments([]);
      setMessages([
        {
          role: 'assistant',
          content: 'Chat history cleared. Start a new TDS/TCS question when ready.',
          sources: [],
        },
      ]);
    } catch (error) {
      setNotice(`Could not clear chats: ${error.message}`);
    }
  }

  function removeQuestionAttachment(id) {
    setQuestionAttachments((current) => current.filter((doc) => doc.id !== id));
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><Sparkles size={18} /></div>
          <div>
            <strong>TDSBot</strong>
            <span>FY 2026-27 assistant</span>
          </div>
        </div>
        <button className="new-chat" onClick={newChat}>
          <Plus size={16} /> New chat
        </button>
        <h2 className="history-title">Chats</h2>
        <div className="history">
          {chats.map((chat) => (
            <button key={chat.id} className={chat.id === chatId ? 'active' : ''} onClick={() => openChat(chat.id)}>
              {chat.title}
            </button>
          ))}
        </div>
        <button className="clear-chats" onClick={clearChats} disabled={loading || chats.length === 0}>
          <Trash2 size={15} /> Clear chats
        </button>
        <div className="references">
          <h2>Current Chat References</h2>
          <label className="upload-control">
            <FileUp size={16} />
            Add reference PDF
            <input type="file" accept="application/pdf" onChange={(event) => uploadPdf(event, 'reference')} />
          </label>
          <form className="url-form" onSubmit={addUrl}>
            <Link size={16} />
            <input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="Add reference URL" />
            <button type="submit" aria-label="Add URL"><Upload size={16} /></button>
          </form>
          <div className="document-list">
            {referenceDocuments.map((doc) => (
              <div className="document" key={doc.id}>
                <span>{doc.type}</span>
                <strong>{doc.title}</strong>
              </div>
            ))}
          </div>
        </div>
      </aside>

      <section className="chat-panel">
        <header className="topbar">
          <div>
            <h1>Financial/TDS Answering Bot</h1>
            <p>Grounded on uploaded invoices, reference URLs, and the TDSMAN FY 2026-27 chart.</p>
          </div>
          <div className="status">
            {config?.google_custom_search_enabled || config?.serpapi_enabled ? 'Web search on' : 'Web search off'}
          </div>
        </header>

        {notice && (
          <div className="notice">
            <AlertCircle size={16} />
            {notice}
          </div>
        )}

        <div className="messages">
          {messages.map((message, index) => (
            <Message message={message} key={index} />
          ))}
          {loading && (
            <article className="message assistant">
              <div className="avatar">AI</div>
              <div className="bubble loading">Checking documents, rules, and sources...</div>
            </article>
          )}
          <div ref={scrollRef} />
        </div>

        <form className="composer" onSubmit={sendMessage}>
          <div className="composer-input">
            {questionAttachments.length > 0 && (
              <div className="attachment-row">
                {questionAttachments.map((doc) => (
                  <div className="attachment-chip" key={doc.id}>
                    <FileUp size={14} />
                    <span>{doc.title}</span>
                    <button type="button" onClick={() => removeQuestionAttachment(doc.id)} aria-label={`Remove ${doc.title}`}>
                      <X size={14} />
                    </button>
                  </div>
                ))}
              </div>
            )}
            <textarea
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) sendMessage(event);
              }}
              placeholder="Ask: How much TDS is applicable on this invoice?"
            />
          </div>
          <label className={`attach-button ${loading ? 'disabled' : ''}`} aria-label="Attach PDF to question">
            <Paperclip size={18} />
            <input
              type="file"
              accept="application/pdf"
              disabled={loading}
              onChange={(event) => uploadPdf(event, 'question')}
            />
          </label>
          <button type="submit" disabled={loading || !question.trim()} aria-label="Send">
            <Send size={18} />
          </button>
        </form>
      </section>
    </main>
  );
}

createRoot(document.getElementById('root')).render(<App />);
