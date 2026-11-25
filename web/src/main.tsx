import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { LoginGate } from "./components/LoginGate";
import { TermsGate } from "./components/TermsGate";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <LoginGate>
      <TermsGate>
        <App />
      </TermsGate>
    </LoginGate>
  </StrictMode>,
);
