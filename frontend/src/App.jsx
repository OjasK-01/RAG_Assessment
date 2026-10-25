import { useEffect, useRef, useState } from "react";
import * as api from "./api.js";

const SAMPLES = [
  "My account got locked. How long until it unlocks by itself?",
  "What VPN server address should I use when working from home?",
  "What is the resolution target for a P2 ticket?",
  "How do I get a parking pass for the Gurugram office?",
  "Hi, I'm Anand Sharma (anand.sharma@vectralogistics.example, +91 9876543210). Company mail doesn't show up in my iPhone's built-in Mail app. How do I fix it?",
];
const MAX_CHARS = 500;

export default function App() {
  const [session, setSession] = useState(() => {
    const s = api.loadSession();
    return s && s.expiresAt > Date.now() ? s : null;
  });
  const [tab, setTab] = useState("chat");

  const signOut = () => {
    api.logout();
    setSession(null);
    setTab("chat");
  };

  if (!session) return <Login onLogin={setSession} />;

  const canSeeKb = session.role === "agent" || session.role === "admin";
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo" aria-hidden>V</span>
          <div>
            <strong>Vectra IT Assistant</strong>
            <small>Answers from the IT knowledge base only</small>
          </div>
        </div>
        <nav className="tabs">
          <button className={tab === "chat" ? "active" : ""} onClick={() => setTab("chat")}>Ask</button>
          {canSeeKb && (
            <button className={tab === "kb" ? "active" : ""} onClick={() => setTab("kb")}>Knowledge base</button>
          )}
        </nav>
        <div className="who">
          <span className={`role role-${session.role}`}>{session.role}</span>
          <span className="name">{session.username}</span>
          <button className="ghost" onClick={signOut}>Sign out</button>
        </div>
      </header>
      {tab === "chat" ? <Chat session={session} onAuthError={signOut} /> : <KbPanel session={session} onAuthError={signOut} />}
    </div>
  );
}

function Login({ onLogin }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      onLogin(await api.login(username.trim(), password));
    } catch (err) {
      setError(
        err.status === 0 ? err.message
          : err.status === 429 ? "Too many attempts. Wait a minute and try again."
          : "Invalid username or password."
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-wrap">
      <form className="login card" onSubmit={submit}>
        <span className="logo big" aria-hidden>V</span>
        <h1>Vectra IT Assistant</h1>
        <p className="muted">Sign in with your service desk account.</p>
        <label>
          Username
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required maxLength={64} autoFocus />
        </label>
        <label>
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required maxLength={128} />
        </label>
        {error && <p className="error" role="alert">{error}</p>}
        <button className="primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </div>
  );
}

function Chat({ session, onAuthError }) {
  const saved = api.loadChat(session.username);
  const [messages, setMessages] = useState(saved?.messages || []);
  const [conversationId, setConversationId] = useState(saved?.conversationId || null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  useEffect(() => {
    api.saveChat(messages.length ? { username: session.username, conversationId, messages } : null);
  }, [messages, conversationId, session.username]);

  const newChat = () => {
    if (conversationId) api.deleteConversation(session.token, conversationId).catch(() => {});
    setConversationId(null);
    setMessages([]);
  };

  const send = async (text) => {
    const question = text.trim();
    if (!question || busy) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", text: question }]);
    setBusy(true);
    try {
      const res = await api.ask(session.token, question, conversationId);
      setConversationId(res.conversation_id);
      setMessages((m) => [...m, { role: "bot", ...res }]);
    } catch (err) {
      if (err.status === 401) return onAuthError();
      const msg = err.status === 429 ? "You're sending questions too quickly. Please wait a moment." : err.message;
      setMessages((m) => [...m, { role: "error", text: msg }]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="chat">
      {messages.length > 0 && (
        <div className="chat-bar">
          <small className="muted">Follow-up questions use this conversation.</small>
          <button className="ghost" onClick={newChat} disabled={busy}>New chat</button>
        </div>
      )}
      <div className="messages" aria-live="polite">
        {messages.length === 0 && (
          <div className="empty">
            <h2>How can IT help?</h2>
            <p className="muted">Answers come only from Vectra's IT knowledge base, with the article cited. If the KB doesn't cover it, you'll be told to raise a ticket.</p>
            <div className="samples">
              {SAMPLES.map((q) => (
                <button key={q} className="sample" onClick={() => send(q)}>{q}</button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m, i) => <Message key={i} m={m} />)}
        {busy && <div className="bubble bot typing"><span /><span /><span /></div>}
        <div ref={endRef} />
      </div>
      <form className="composer" onSubmit={(e) => { e.preventDefault(); send(input); }}>
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value.slice(0, MAX_CHARS))}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); } }}
          placeholder="Ask an IT question…"
          rows={2}
          aria-label="Your question"
        />
        <div className="composer-side">
          <small className="muted">{input.length}/{MAX_CHARS}</small>
          <button className="primary" disabled={busy || input.trim().length < 2}>Send</button>
        </div>
      </form>
    </main>
  );
}

function Message({ m }) {
  if (m.role === "user") return <div className="bubble user">{m.text}</div>;
  if (m.role === "error") return <div className="bubble error-bubble">{m.text}</div>;
  return (
    <div className={`bubble bot ${m.abstained ? "abstained" : ""}`}>
      {m.abstained && <span className="badge warn">Not covered by the KB</span>}
      <p className="answer">{m.answer}</p>
      {m.sources?.length > 0 && (
        <div className="sources">
          {m.sources.map((s) => (
            <span key={s.doc_id} className="chip" title={s.title}><b>{s.doc_id}</b> {s.title}</span>
          ))}
        </div>
      )}
      {m.trace && <Trace t={m.trace} />}
    </div>
  );
}

function Trace({ t }) {
  return (
    <details className="trace">
      <summary>
        Why this answer · <code>{t.outcome}</code> · {t.latency_ms} ms{t.model ? ` · ${t.model}` : ""}
      </summary>
      <dl>
        <dt>Sent to model (masked)</dt>
        <dd><code>{t.question_masked}</code>{t.pii_types?.length > 0 && <span className="badge">PII masked: {t.pii_types.join(", ")}</span>}</dd>
        {t.followup && (<><dt>Follow-up</dt><dd>searched with the previous question: <code>{t.search_query}</code></dd></>)}
        {t.flags?.length > 0 && (<><dt>Flags</dt><dd>{t.flags.join(", ")}</dd></>)}
        {t.violations?.length > 0 && (<><dt>Guard</dt><dd className="bad">{t.violations.join(", ")}</dd></>)}
        <dt>Retrieval</dt>
        <dd>
          <table>
            <thead><tr><th>Doc</th><th>Cosine</th><th>Keyword</th><th>Gate</th><th>Used</th></tr></thead>
            <tbody>
              {t.retrieved?.map((r) => (
                <tr key={r.doc_id} className={r.used ? "used" : ""}>
                  <td>{r.doc_id}</td><td>{r.sim.toFixed(3)}</td><td>{r.keyword.toFixed(2)}{r.code_match ? " ★" : ""}</td><td>{r.gate || "–"}</td><td>{r.used ? "✓" : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </dd>
        {t.evidence?.length > 0 && (
          <>
            <dt>Evidence</dt>
            <dd>
              <ul className="evidence">
                {t.evidence.map((e, i) => (
                  <li key={i} className={e.verified ? "ok" : "bad"}>{e.verified ? "✓" : "✗"} <b>{e.doc_id}</b> “{e.quote}”</li>
                ))}
              </ul>
            </dd>
          </>
        )}
      </dl>
    </details>
  );
}

function KbPanel({ session, onAuthError }) {
  const [data, setData] = useState(null);
  const [report, setReport] = useState(null);
  const [audit, setAudit] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const isAdmin = session.role === "admin";

  const guard = async (fn) => {
    setError("");
    try {
      await fn();
    } catch (err) {
      if (err.status === 401) onAuthError();
      else setError(err.message);
    }
  };

  useEffect(() => {
    guard(async () => setData(await api.listArticles(session.token)));
    if (isAdmin) guard(async () => setAudit(await api.auditLog(session.token, 15)));
  }, []);

  const runIngest = () => guard(async () => {
    setBusy(true);
    try {
      setReport(await api.reingest(session.token));
      setData(await api.listArticles(session.token));
    } finally {
      setBusy(false);
    }
  });

  return (
    <main className="panel">
      {error && <p className="error">{error}</p>}
      <section className="card">
        <div className="card-head">
          <h2>Indexed articles</h2>
          {isAdmin && <button className="primary" onClick={runIngest} disabled={busy}>{busy ? "Re-indexing…" : "Re-index KB"}</button>}
        </div>
        {data && (
          <>
            <p className="muted small">Embeddings: {data.index.provider}/{data.index.embed_model} ({data.index.embed_dim}d) · last ingested {data.index.ingested_at}</p>
            <table className="grid">
              <thead><tr><th>Doc</th><th>Title</th><th>Effective</th><th>Owner</th></tr></thead>
              <tbody>{data.articles.map((a) => (
                <tr key={a.doc_id}><td><b>{a.doc_id}</b></td><td>{a.title}</td><td>{a.effective_date}</td><td>{a.owner}</td></tr>
              ))}</tbody>
            </table>
          </>
        )}
      </section>
      {report && (
        <section className="card">
          <h2>Last ingestion</h2>
          <p className="muted small">Embedded this run: {report.embedded_this_run.join(", ") || "none (unchanged)"} · removed: {report.removed_from_index.join(", ") || "none"}</p>
          <h3>Rejected by admission policy</h3>
          <ul>{report.rejected.map((r) => <li key={r.file}><b>{r.doc_id || r.file}</b>: {r.reasons.join("; ")}</li>)}</ul>
        </section>
      )}
      {isAdmin && audit && (
        <section className="card">
          <h2>Recent activity (masked)</h2>
          <table className="grid">
            <thead><tr><th>Time</th><th>User</th><th>Question</th><th>Outcome</th><th>Cited</th><th>ms</th></tr></thead>
            <tbody>{audit.records.map((r, i) => (
              <tr key={i}><td>{r.ts?.slice(11, 19)}</td><td>{r.account}</td><td className="q">{r.question_masked}</td><td><code>{r.outcome}</code></td><td>{(r.citations || []).join(", ")}</td><td>{r.latency_ms}</td></tr>
            ))}</tbody>
          </table>
        </section>
      )}
    </main>
  );
}
