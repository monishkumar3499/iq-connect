/**
 * IQ Connect by JMAN Group. The addresses below are the ones the server's sign-in
 * redirects use, so a person always lands back on the screen they left.
 */
import "./index.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes, useParams, useSearchParams } from "react-router-dom";

import { AppShell, SessionProvider } from "@/components/layout";
import { MicrosoftSetup } from "@/pages/Admin";
import { MyConnections } from "@/pages/Connections";
import { Connect, DevSignIn, ErrorPage, Landing } from "@/pages/Entry";
import { DoneStep, InstructionsStep, StatusStep } from "@/pages/Finish";
import { SettingsPage } from "@/pages/Settings";
import { SignInStep } from "@/pages/SignIn";
import { ModelsQuestion, ModelsStep, Platforms, WarehousesStep, WorkspacePicker } from "@/pages/Setup";

/** /app/setup/{platform}/ws/{id} was the old workspace address: its warehouses. */
function WorkspaceHome() {
  const { platform, workspace } = useParams();
  const [search] = useSearchParams();
  return <Navigate replace to={`/app/setup/${platform}/ws/${workspace}/warehouses?${search}`} />;
}

function App() {
  return (
    <SessionProvider>
      <Routes>
        <Route path="/setup" element={<Landing />} />
        <Route path="/app/setup/welcome" element={<Landing />} />
        <Route path="/error" element={<ErrorPage />} />
        <Route path="/auth/dev-login" element={<DevSignIn />} />
        {/* Signed in: the sidebar (My connections, Add a new connection, Settings) and a page. */}
        <Route element={<AppShell />}>
          <Route path="/connect" element={<Connect />} />
          <Route path="/app" element={<MyConnections />} />
          <Route path="/app/settings" element={<SettingsPage />} />
          <Route path="/app/setup" element={<Platforms />} />
          <Route path="/app/setup/microsoft" element={<MicrosoftSetup />} />
          <Route path="/app/setup/semantic-models" element={<ModelsQuestion />} />
          <Route path="/app/setup/review" element={<StatusStep />} />
          <Route path="/app/setup/instructions" element={<InstructionsStep />} />
          <Route path="/app/setup/done" element={<DoneStep />} />
          <Route path="/app/setup/:platform" element={<WorkspacePicker />} />
          <Route path="/app/setup/:platform/sign-in" element={<SignInStep />} />
          <Route path="/app/setup/:platform/ws/:workspace" element={<WorkspaceHome />} />
          <Route path="/app/setup/:platform/ws/:workspace/warehouses" element={<WarehousesStep />} />
          <Route path="/app/setup/:platform/ws/:workspace/models" element={<ModelsStep />} />
        </Route>
        <Route path="*" element={<Navigate replace to="/setup" />} />
      </Routes>
    </SessionProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
