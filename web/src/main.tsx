import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { TermsGate } from "./components/TermsGate";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <TermsGate>
      <App />
    </TermsGate>
  </StrictMode>,
);
