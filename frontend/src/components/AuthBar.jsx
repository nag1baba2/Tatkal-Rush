import { useState } from "react";
import { login, register } from "../api";
import { messageFor } from "../format";

export default function AuthBar({ session, onLogin, onLogout }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(mode) {
    setBusy(true);
    setError("");
    try {
      if (mode === "register") await register(email, password);
      const token = await login(email, password);
      onLogin({ email: email.trim().toLowerCase(), token });
      setPassword("");
    } catch (e) {
      setError(messageFor(e));
    } finally {
      setBusy(false);
    }
  }

  if (session) {
    return (
      <div className="auth">
        <span>Signed in as <strong>{session.email}</strong></span>
        <button type="button" onClick={onLogout}>Log out</button>
      </div>
    );
  }

  return (
    <form
      className="auth"
      onSubmit={(e) => {
        e.preventDefault();
        submit("login");
      }}
    >
      <input
        type="email"
        placeholder="Email"
        aria-label="Email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        required
      />
      <input
        type="password"
        placeholder="Password (8+ characters)"
        aria-label="Password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        required
      />
      <button type="submit" className="primary" disabled={busy}>Log in</button>
      <button type="button" disabled={busy} onClick={() => submit("register")}>Register</button>
      {error && <span role="alert" className="warn">{error}</span>}
    </form>
  );
}
