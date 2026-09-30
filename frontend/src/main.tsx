import { StrictMode, useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { LoginPage } from "./auth/LoginPage";
import { BrandMark } from "./components/Brand";
import { authApi, UNAUTHORIZED_EVENT, type SessionUser } from "./services/api";
import "./styles.css";

/** Session gate: the workspace only mounts for a signed-in or demo user.
 * The server enforces this too - every data API answers 401 without a
 * session - so this is the UX layer, not the security boundary. */
function Root() {
  const [user, setUser] = useState<SessionUser | null | undefined>(undefined);
  const [loginMode, setLoginMode] = useState<"signin" | "signup">("signin");

  useEffect(() => { authApi.me().then(setUser).catch(() => setUser(null)); }, []);
  useEffect(() => {
    const expired = () => setUser(null);
    window.addEventListener(UNAUTHORIZED_EVENT, expired);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, expired);
  }, []);

  const signOut = useCallback(async (next: "signin" | "signup" = "signin") => {
    await authApi.logout().catch(() => undefined);
    setLoginMode(next);
    setUser(null);
  }, []);

  if (user === undefined) {
    return <div className="boot" aria-busy="true" aria-label="Loading VeriFi"><BrandMark size={44} /></div>;
  }
  if (user === null) return <LoginPage key={loginMode} initialMode={loginMode} onAuthenticated={setUser} />;
  return <App user={user} onSignOut={signOut} />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
